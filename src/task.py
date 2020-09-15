"""
This script is used for training and test

We offer two types of training strategy: train-dev and 5CV

In train-dev, we directly select the best model according to the performance on dev
In train-dev, you have to decide the batch size and training epochs

In 5CV, we screen a series of batch sizes and training epochs
In 5CV, we determine the best hyper-parameters based on average 5CV performance
In 5CV, the production model will be trained on whole whole training set (train+dev) with the best hyper-parameters
"""


# from data_utils import convert_examples_to_relation_extraction_features
from data_utils import (features2tensors, relation_extraction_data_loader,
                        batch_to_model_input, RelationDataFormatSepProcessor,
                        RelationDataFormatUniProcessor, acc_and_f1, pkl_save, pkl_load)
from transformers import glue_convert_examples_to_features as convert_examples_to_relation_extraction_features
from models import (BertForRelationIdentification, RoBERTaForRelationIdentification,
                    XLNetForRelationIdentification, AlbertForRelationIdentification)
from transformers import (BertConfig, RobertaConfig, XLNetConfig, AlbertConfig,
                          BertTokenizer, RobertaTokenizer, XLNetTokenizer, AlbertTokenizer)
import torch
from tqdm import trange, tqdm
import numpy as np
from packaging import version
from pathlib import Path
from config import SPEC_TAGS


def get_linear_schedule_with_warmup(optimizer, num_warmup_steps, num_training_steps, last_epoch=-1):

    def lr_lambda(current_step: int):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        return max(
            0.0, float(num_training_steps - current_step) / float(max(1, num_training_steps - num_warmup_steps))
        )

    return torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda, last_epoch)


class TaskRunner(object):
    model_dict = {
        "bert": (BertForRelationIdentification, BertConfig, BertTokenizer),
        "roberta": (RoBERTaForRelationIdentification, RobertaConfig, RobertaTokenizer),
        "xlnet": (XLNetForRelationIdentification, XLNetConfig, XLNetTokenizer),
        "albert": (AlbertForRelationIdentification, AlbertConfig, AlbertTokenizer)
    }

    def __init__(self, args):
        super().__init__()
        self.args = args
        # set up data processor
        if self.args.data_format_mode == 0:
            self.data_processor = RelationDataFormatSepProcessor(max_seq_len=self.args.max_seq_length)
        elif self.args.data_format_mode == 1:
            self.data_processor = RelationDataFormatUniProcessor(max_seq_len=self.args.max_seq_length)
        else:
            raise NotImplementedError("Only support 0, 1 but get data_format_mode as {}"
                                      .format(self.args.data_format_mode))
        self.data_processor.set_data_dir(self.args.data_dir)
        # init or reload model
        if self.args.do_train:
            # init amp for fp16 (mix precision training)
            # _use_amp_for_fp16_from: 0 for no fp16; 1 for naive PyTorch amp; 2 for apex amp
            if self.args.fp16:
                self._use_amp_for_fp16_from = 0
                self._load_amp_for_fp16()
            self._init_new_model()
        else:
            self._init_trained_model()
        # load data
        self.train_data_loader = None
        self.dev_data_loader = None
        self.test_data_loader = None
        self.data_processor.set_tokenizer(self.tokenizer)
        self._load_data()
        if self.args.do_train:
            self._init_optimizer()
        self.args.logger.info("Model Config:\n{}".format(self.config))
        self.args.logger.info("All parameters:\n{}".format(self.args))

    def train(self):
        # create data loader
        tr_loss = .0
        epoch_iter = trange(self.args.num_train_epochs, desc="Epoch", disable=False)
        for epoch in epoch_iter:
            batch_iter = tqdm(self.train_data_loader, desc="Batch", disable=False)
            batch_total_step = len(self.train_data_loader)
            for step, batch in enumerate(batch_iter):
                self.model.train()
                self.model.zero_grad()
                batch_input = batch_to_model_input(batch, model_type=self.args.model_type, device=self.args.device)
                if self.args.fp16 and self._use_amp_for_fp16_from == 1:
                    with self.amp.autocast():
                        batch_output = self.model(**batch_input)
                        loss = batch_output[0]
                else:
                    batch_output = self.model(**batch_input)
                    loss = batch_output[0]
                loss = loss / self.args.gradient_accumulation_steps
                if self.args.fp16:
                    if self._use_amp_for_fp16_from == 1:
                        self.amp_scaler.scale(loss).backward()
                    elif self._use_amp_for_fp16_from == 2:
                        with self.amp.scale_loss(loss, self.optimizer) as scaled_loss:
                            scaled_loss.backward()
                else:
                    loss.backward()
                # update gradient
                if (step + 1) % self.args.gradient_accumulation_steps == 0 or (step + 1) == batch_total_step:
                    if self.args.fp16:
                        if self._use_amp_for_fp16_from == 1:
                            self.amp_scaler.unscale_(self.optimizer)
                            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.args.max_grad_norm)
                            self.amp_scaler.step(self.optimizer)
                            self.amp_scaler.update()
                        elif self._use_amp_for_fp16_from == 2:
                            torch.nn.utils.clip_grad_norm_(self.amp.master_params(self.optimizer),
                                                           self.args.max_grad_norm)
                            self.optimizer.step()
                    else:
                        torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.args.max_grad_norm)
                        self.optimizer.step()
                    if self.args.do_warmup:
                        self.scheduler.step()
            batch_iter.close()
        epoch_iter.close()
        self._save_model()

    def eval(self):
        # this is for dev
        true_labels = np.array([dev_fea.label for dev_fea in self.dev_features])
        preds, eval_loss = self._run_eval(self.dev_data_loader)
        eval_metric = acc_and_f1(labels=true_labels, preds=preds)
        return eval_metric

    def predict(self):
        # this is for prediction
        preds, _ = self._run_eval(self.test_data_loader)
        # convert predicted label idx to real label
        preds = [self.idx2label[pred] for pred in preds]
        return preds

    def _init_new_model(self):
        """initialize a new model for fine-tuning"""
        model, config, tokenizer = self.model_dict[self.args.model_type]
        # init tokenizer and add special tags
        self.tokenizer = tokenizer.from_pretrained(self.args.pretrained_model, do_lower_case=self.args.do_lower_case)
        last_token_idx = len(self.tokenizer)
        self.tokenizer.add_tokens(SPEC_TAGS)
        spec_token_new_ids = tuple([(last_token_idx + idx) for idx in range(len(self.tokenizer) - last_token_idx)])
        # init config
        unique_labels, label2idx, idx2label = self.data_processor.get_labels()
        num_labels = len(unique_labels)
        self.label2idx = label2idx
        self.idx2label = idx2label
        self.config = config.from_pretrained(self.args.pretrained_model, num_labels=num_labels)
        self.config.tags = spec_token_new_ids
        self.config.scheme = self.args.classification_scheme
        # init model
        self.model = model.from_pretrained(self.args.pretrained_model, config=self.config)
        total_token_num = len(self.tokenizer)
        self.model.resize_token_embeddings(total_token_num)
        self.config.vocab_size = total_token_num
        # load model to device
        self.model.to(self.args.device)

    def _init_optimizer(self):
        # set up optimizer
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {'params': [p for n, p in self.model.named_parameters() if not any(nd in n for nd in no_decay)],
             'weight_decay': self.args.weight_decay},
            {'params': [p for n, p in self.model.named_parameters() if any(nd in n for nd in no_decay)],
             'weight_decay': 0.0}
        ]
        self.optimizer = torch.optim.AdamW(optimizer_grouped_parameters,
                                           lr=self.args.learning_rate,
                                           eps=self.args.adam_epsilon)
        self.args.logger.info("The optimizer detail:\n {}".format(self.optimizer))
        # set up optimizer warm up scheduler (you can set warmup_ratio=0 to deactivated this function)
        if self.args.do_warmup:
            t_total = len(self.train_data_loader) // self.args.gradient_accumulation_steps * self.args.num_train_epochs
            warmup_steps = np.dtype('int64').type(self.args.warmup_ratio * t_total)
            self.scheduler = get_linear_schedule_with_warmup(self.optimizer,
                                                             num_warmup_steps=warmup_steps,
                                                             num_training_steps=t_total)
        # mix precision training
        if self.args.fp16 and self._use_amp_for_fp16_from == 2:
            self.model, self.optimizer = self.amp.initialize(self.model, self.optimizer,
                                                             opt_level=self.args.fp16_opt_level)

    def _init_trained_model(self):
        """initialize a fine-tuned model for prediction"""
        model, config, tokenizer = self.model_dict[self.args.model_type]
        self.config = config.from_pretrained(self.args.new_model_dir)
        self.tokenizer = tokenizer.from_pretrained(self.args.new_model_dir, do_lower_case=self.args.do_lower_case)
        self.model = model.from_pretrained(self.args.new_model_dir, config=self.config)
        # load label2idx
        self.label2idx, self.idx2label = pkl_load(Path(self.args.new_model_dir)/"label_index.pkl")
        # load model to device
        self.model.to(self.args.device)

    def _load_amp_for_fp16(self):
        # first try to load PyTorch naive amp; if fail, try apex; if fail again, throw a RuntimeError
        if version.parse(torch.__version__) >= version.parse("1.6.0"):
            self.amp = torch.cuda.amp
            self._use_amp_for_fp16_from = 1
            self.amp_scaler = torch.cuda.amp.GradScaler()
        else:
            try:
                from apex import amp
                self.amp = amp
                self._use_amp_for_fp16_from = 2
            except ImportError:
                self.args.logger.error("apex (https://www.github.com/nvidia/apex) for fp16 training is not installed.")
            finally:
                self.args.fp16 = False

    def _save_model(self):
        Path(self.args.new_model_dir).mkdir(parents=True, exist_ok=True)
        self.tokenizer.save_pretrained(self.args.new_model_dir)
        self.config.save_pretrained(self.args.new_model_dir)
        self.model.save_pretrained(self.args.new_model_dir)
        # save label2idx
        pkl_save((self.label2idx, self.idx2label), Path(self.args.new_model_dir)/"label_index.pkl")

    def _run_eval(self, data_loader):
        temp_loss = .0
        # set model to evaluate mode
        self.model.eval()
        # create dev data batch iteration
        batch_iter = tqdm(data_loader, desc="Batch", disable=False)
        total_sample_num = len(batch_iter)
        preds = None
        for batch in batch_iter:
            batch_input = batch_to_model_input(batch, model_type=self.args.model_type, device=self.args.device)
            with torch.no_grad():
                batch_output = self.model(**batch_input)
                loss, logits = batch_output[:2]
                temp_loss += loss.item()
                logits = logits.detach().cpu().numpy()
                preds = logits if preds is None else np.append(preds, logits, axis=0)
        batch_iter.close()
        temp_loss = temp_loss / total_sample_num
        preds = np.argmax(preds)

        return preds, temp_loss

    def _load_data(self):
        if self.args.do_train:
            train_examples = self.data_processor.get_train_examples()
            train_features = convert_examples_to_relation_extraction_features(
                train_examples,
                tokenizer=self.tokenizer,
                max_length=self.args.max_seq_length,
                label_list=self.label2idx,
                output_mode="classification")
            self.train_data_loader = relation_extraction_data_loader(
                train_features, batch_size=self.args.train_batch_size, task="train", logger=self.args.logger)
        if self.args.do_eval:
            dev_examples = self.data_processor.get_dev_examples()
            dev_features = convert_examples_to_relation_extraction_features(
                dev_examples,
                tokenizer=self.tokenizer,
                max_length=self.args.max_seq_length,
                label_list=self.label2idx,
                output_mode="classification")
            self.dev_features = dev_features
            self.dev_data_loader = relation_extraction_data_loader(
                dev_features, batch_size=self.args.train_batch_size, task="test", logger=self.args.logger)
        if self.args.do_predict:
            test_examples = self.data_processor.get_test_examples()
            test_features = convert_examples_to_relation_extraction_features(
                test_examples,
                tokenizer=self.tokenizer,
                max_length=self.args.max_seq_length,
                label_list=self.label2idx,
                output_mode="classification")
            self.test_data_loader = relation_extraction_data_loader(
                test_features, batch_size=self.args.eval_batch_size, task="test", logger=self.args.logger)