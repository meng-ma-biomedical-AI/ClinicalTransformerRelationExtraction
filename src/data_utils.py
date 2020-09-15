import pickle as pkl
import traceback
from sklearn.metrics import f1_score, precision_recall_fscore_support
from config import MODEL_REQUIRE_SEGMENT_ID, SPEC_TAGS
import csv
from pathlib import Path
import torch
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler, TensorDataset
import re


def load_text(ifn):
    with open(ifn, "r") as f:
        txt = f.read()
    return txt


def save_text(text, ofn):
    with open(ofn, "w") as f:
        f.write(text)


def pkl_save(data, file):
    with open(file, "wb") as f:
        pkl.dump(data, f)


def pkl_load(file):
    with open(file, "rb") as f:
        data = pkl.load(f)
    return data


def try_catch_annotator(func):
    def try_catch(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            traceback.print_exc()
            return None
    return try_catch


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, guid, text_a, text_b=None, label=None):
        """Constructs a InputExample.

        Args:
            guid: Unique id for the example.
            text_a: string. The not tokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
            text_b: (Optional) string. The not tokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
            label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.label = label


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids, attention_mask=None, token_type_ids=None, label=None):
        self.input_ids = input_ids
        self.attention_mask = attention_mask
        self.token_type_ids = token_type_ids
        self.label = label


def convert_examples_to_relation_extraction_features(
        examples, label2idx, tokenizer, max_length=128):
    """This function is the same as transformers.glue_convert_examples_to_features"""
    features = []
    for idx, example in enumerate(examples):
        text_a, text_b = example.text_a, example.text_b
        tokens_a = tokenizer.convert_tokens_to_ids(tokenizer.tokenize(text_a))
        if text_b:
            tokens_b = tokenizer.convert_tokens_to_ids(tokenizer.tokenize(text_b))
        else:
            tokens_b = None
        inputs = tokenizer.encode_plus(
            tokens_a, tokens_b, pad_to_max_length=True, max_length=max_length, truncation=False)
        # Truncate tokens
        label = label2idx[example.label]
        feature = InputFeatures(**inputs, label=label)
        features.append(feature)

        if idx < 3:
            print("###exampel###\nguide: {}\ntext: {}\ntoken ids: {}\nmasks: {}\nlabel: {}\n########".format(
                example.guid,
                example.text_a + " " + example.text_b,
                feature.input_ids,
                feature.attention_mask,
                feature.label))

    return features


def features2tensors(features, logger=None):
    tensor_input_ids = []
    tensor_attention_masks = []
    tensor_token_type_ids = []
    tensor_label_ids = []
    for idx, feature in enumerate(features):
        if logger and idx < 3:
            logger.info("Feature{}:\n{}\n".format(idx+1, feature))
        tensor_input_ids.append(feature.input_ids)
        tensor_attention_masks.append(feature.attention_mask)
        tensor_label_ids.append(feature.label)
        if feature.token_type_ids:
            tensor_token_type_ids.append(feature.token_type_ids)
    tensor_input_ids = torch.tensor(tensor_input_ids, dtype=torch.long)
    tensor_attention_masks = torch.tensor(tensor_attention_masks, dtype=torch.long)
    tensor_label_ids = torch.tensor(tensor_label_ids, dtype=torch.long)
    tensor_token_type_ids = torch.tensor(tensor_token_type_ids, dtype=torch.long) if tensor_token_type_ids \
        else torch.zeros(tensor_attention_masks.shape)

    return TensorDataset(tensor_input_ids, tensor_attention_masks, tensor_token_type_ids, tensor_label_ids)


def relation_extraction_data_loader(dataset, batch_size=2, task='train', logger=None):
    """
    task has two levels:
    train for training using RandomSampler
    test for evaluation and prediction using SequentialSampler

    if set auto to True we will default call convert_features_to_tensors,
    so features can be directly passed into the function
    """
    dataset = features2tensors(dataset, logger=logger)

    if task == 'train':
        sampler = RandomSampler(dataset)
    elif task == 'test':
        sampler = SequentialSampler(dataset)
    else:
        raise ValueError('task argument only support train or test but get {}'.format(task))

    data_loader = DataLoader(dataset, sampler=sampler, batch_size=batch_size, pin_memory=True)
    return data_loader


def batch_to_model_input(batch, model_type="bert", device=torch.device("cpu")):
    return {"input_ids": batch[0].to(device),
            "attention_mask": batch[1].to(device),
            "labels": batch[3].to(device),
            "token_type_ids": batch[2].to(device) if model_type in MODEL_REQUIRE_SEGMENT_ID else None}


class DataProcessor(object):
    """Base class for data converters for sequence classification data sets."""
    def __init__(self, data_dir=None, max_seq_len=128):
        if data_dir:
            self.data_dir = Path(data_dir)
        else:
            self.data_dir = data_dir
        self.tokenizer = None
        self.max_seq_len = max_seq_len

    def set_data_dir(self, data_dir):
        self.data_dir = Path(data_dir)

    def set_tokenizer(self, tokenizer):
        self.tokenizer = tokenizer

    def get_train_examples(self, filename=None):
        """See base class."""
        input_file_name = self.data_dir / filename if filename else self.data_dir / "train.tsv"
        return self._create_examples(
            self._read_tsv(input_file_name), "train")

    def get_dev_examples(self, filename=None):
        """See base class."""
        input_file_name = self.data_dir / filename if filename else self.data_dir / "dev.tsv"
        return self._create_examples(
            self._read_tsv(input_file_name), "dev")

    def get_test_examples(self, filename=None):
        """See base class."""
        input_file_name = self.data_dir / filename if filename else self.data_dir / "test.tsv"
        return self._create_examples(
            self._read_tsv(input_file_name), "test")

    def get_labels(self, filename=None):
        """
            Gets the list of labels for this data set.
            In all different formats, the first column always should be label
        """
        lines = self._read_tsv(self.data_dir / filename if filename else self.data_dir / "train.tsv")
        unique_labels = set()
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            unique_labels.add(line[0])
        label2idx = {k: v for v, k in enumerate(unique_labels)}
        idx2label = {v: k for k, v in label2idx.items()}
        # pkl_save((label2idx, idx2label), "")
        print(unique_labels)
        return unique_labels, label2idx, idx2label

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        raise NotImplementedError(
            "You must use FamilyHistoryRelationDataFormatSep or FamilyHistoryRelationDataFormatOne.")

    @staticmethod
    def _read_tsv(input_file, quotechar=None):
        """Reads a tab separated value file."""
        with open(input_file, "r", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter="\t", quotechar=quotechar)
            lines = []
            for line in reader:
                lines.append(line)
            return lines


class RelationDataFormatSepProcessor(DataProcessor):
    """
        data format:
            [CLS] sent1 [SEP] sent2 [SEP]
    """

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            guid = "%s-%s" % (set_type, i)
            text_a = line[1]
            text_b = line[2]
            label = line[0]
            # text after tokenization has a len > max_seq_len:
            # 1. skip all these cases
            # 2. use truncate strategy
            # we adopt truncate way (2) in this implementation as _process_seq_len
            text_a, text_b = self._process_seq_len(text_a, text_b)
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples

    def _process_seq_len(self, text_a, text_b):
        """
            This function is used to truncate sequences with len > max_seq_len
            Truncate strategy:
            1. find all the index for special tags
            3. count distances between leading word to first tag and second tag to last.
            first -1- tag1 entity tag2 -2- last
            4. pick the longest distance from (1, 2), if 1 remove first token, if 2 remove last token
            5. repeat until len is equal to max_seq_len
        """
        # ### the procedure here should be done in preprocessing ####
        text_a = re.sub("\[ \* \*|\* \* \]", "", text_a)
        text_b = re.sub("\[ \* \*|\* \* \]", "", text_b)
        # ###########################################################
        while len(self.tokenizer.tokenize(text_a) + self.tokenizer.tokenize(text_b)) > self.max_seq_len:
            w1 = text_a.split(" ")
            w2 = text_b.split(" ")
            t1, t2 = [idx for (idx, w) in enumerate(w1) if w.lower() in SPEC_TAGS]
            t3, t4 = [idx for (idx, w) in enumerate(w2) if w.lower() in SPEC_TAGS]
            ss1, se1 = 0, len(w1)
            ss2, se2 = 0, len(w2)
            a1 = t1 - ss1
            b1 = se1 - t2
            if a1 > b1:
                w1.pop(0)
            else:
                w1.pop(-1)
            a2 = t3 - ss2
            b2 = se2 - t4
            if a2 > b2:
                w2.pop(0)
            else:
                w2.pop(-1)
            text_a = " ".join(w1)
            text_b = " ".join(w2)

        return text_a, text_b


class RelationDataFormatUniProcessor(DataProcessor):
    """
        data format:
            [CLS] sent1 sent2 [SEP]
    """

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            guid = "%s-%s" % (set_type, i)
            text_a = line[1]
            text_a_1 = line[2]
            text_a = " ".join([text_a, text_a_1])
            label = line[0]
            # text after tokenization has a len > max_seq_len:
            # 1. skip all these cases
            # 2. use truncate strategy (truncate from both side) (adopted)
            text_a = self._process_seq_len(text_a)
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=None, label=label))
        return examples

    def _process_seq_len(self, text_a):
        """
            see RelationDataFormatSepProcessor._process_seq_len for details
        """
        # ### the procedure here should be done in preprocessing ####
        text_a = re.sub("\[ \* \*|\* \* \]", "", text_a)
        # ###########################################################
        while len(self.tokenizer.tokenize(text_a)) > self.max_seq_len:
            w1 = text_a.split(" ")
            t1, t2 = [idx for (idx, w) in enumerate(w1) if w.lower() in SPEC_TAGS]
            ss1, se1 = 0, len(w1)
            a1 = t1 - ss1
            b1 = se1 - t2
            if a1 > b1:
                w1.pop(0)
            else:
                w1.pop(-1)
            text_a = " ".join(w1)

        return text_a


def simple_accuracy(labels, preds):
    return (preds == labels).mean()


def acc_and_f1(labels, preds):
    acc = simple_accuracy(labels, preds)
    pm, rm, fm, _ = precision_recall_fscore_support(y_true=labels, y_pred=preds, average='micro')
    pw, rw, fw, _ = precision_recall_fscore_support(y_true=labels, y_pred=preds, average='weighted')
    return {
        "acc": acc,
        "F1-micro": fm, "Pre-micro": pm, "Rec-micro": rm,
        "F1-weight": fw, "Pre-weight": pw, "Rec-weight": rw}