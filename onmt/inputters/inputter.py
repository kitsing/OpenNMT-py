# -*- coding: utf-8 -*-
"""
    Defining general functions for inputters
"""
import glob
import os
import codecs

from collections import Counter, defaultdict, OrderedDict
from itertools import count
from functools import partial

import torch
import torchtext.data
from torchtext.data import Field
import torchtext.vocab

from onmt.inputters.dataset_base import UNK_WORD, PAD_WORD, BOS_WORD, EOS_WORD
from onmt.inputters.text_dataset import TextDataset
from onmt.inputters.image_dataset import ImageDataset
from onmt.inputters.audio_dataset import AudioDataset
from onmt.utils.logging import logger

import gc


def _getstate(self):
    return dict(self.__dict__, stoi=dict(self.stoi))


def _setstate(self, state):
    self.__dict__.update(state)
    self.stoi = defaultdict(lambda: 0, self.stoi)


torchtext.vocab.Vocab.__getstate__ = _getstate
torchtext.vocab.Vocab.__setstate__ = _setstate


def make_src(data, vocab):
    src_size = max([t.size(0) for t in data])
    src_vocab_size = max([t.max() for t in data]) + 1
    alignment = torch.zeros(src_size, len(data), src_vocab_size)
    for i, sent in enumerate(data):
        for j, t in enumerate(sent):
            alignment[j, i, t] = 1
    return alignment


def make_tgt(data, vocab):
    tgt_size = max([t.size(0) for t in data])
    alignment = torch.zeros(tgt_size, len(data)).long()
    for i, sent in enumerate(data):
        alignment[:sent.size(0), i] = sent
    return alignment


def make_img(data, vocab):
    c = data[0].size(0)
    h = max([t.size(1) for t in data])
    w = max([t.size(2) for t in data])
    imgs = torch.zeros(len(data), c, h, w).fill_(1)
    for i, img in enumerate(data):
        imgs[i, :, 0:img.size(1), 0:img.size(2)] = img
    return imgs


def make_audio(data, vocab):
    """ batch audio data """
    nfft = data[0].size(0)
    t = max([t.size(1) for t in data])
    sounds = torch.zeros(len(data), 1, nfft, t)
    for i, spect in enumerate(data):
        sounds[i, :, :, 0:spect.size(1)] = spect
    return sounds


def get_fields(src_data_type, n_src_features, n_tgt_features):
    """
    Args:
        src_data_type: type of the source input. Options are [text|img|audio].
        n_src_features: the number of source features to
            create `torchtext.data.Field` for.
        n_tgt_features: the number of target features to
            create `torchtext.data.Field` for.

    Returns:
        A dictionary whose keys are strings and whose values are the
        corresponding Field objects.
    """
    assert src_data_type in ['text', 'img', 'audio'], \
        "Data type not implemented"
    fields = dict()

    if src_data_type == 'text':
        fields["src"] = Field(pad_token=PAD_WORD, include_lengths=True)
        for i in range(n_src_features):
            fields["src_feat_" + str(i)] = Field(pad_token=PAD_WORD)
    elif src_data_type == 'img':
        fields["src"] = Field(
            use_vocab=False, dtype=torch.float,
            postprocessing=make_img, sequential=False)
    else:
        fields["src"] = Field(
            use_vocab=False, dtype=torch.float,
            postprocessing=make_audio, sequential=False)

    if src_data_type == 'audio':
        # only audio has src_lengths
        fields["src_lengths"] = Field(
            use_vocab=False, dtype=torch.long, sequential=False)
    else:
        # everything except audio has src_map and alignment
        fields["src_map"] = Field(
            use_vocab=False, dtype=torch.float,
            postprocessing=make_src, sequential=False)

        fields["alignment"] = Field(
            use_vocab=False, dtype=torch.long,
            postprocessing=make_tgt, sequential=False)

    # below this: things defined for all source data types
    fields["tgt"] = Field(
        init_token=BOS_WORD, eos_token=EOS_WORD, pad_token=PAD_WORD)

    for i in range(n_tgt_features):
        fields["tgt_feat_" + str(i)] = Field(
            init_token=BOS_WORD, eos_token=EOS_WORD, pad_token=PAD_WORD)

    fields["indices"] = Field(
        use_vocab=False, dtype=torch.long, sequential=False)

    return fields


def load_fields_from_vocab(vocab, data_type="text"):
    """
    vocab: a list of (field name, torchtext.vocab.Vocab) pairs
    data_type: text, img, or audio
    returns: a dictionary whose keys are the field names and whose values
             are field objects with the vocab set to the corresponding vocab
             object from the input.
    """
    vocab = dict(vocab)
    n_src_features = len(collect_features(vocab, 'src'))
    n_tgt_features = len(collect_features(vocab, 'tgt'))
    fields = get_fields(data_type, n_src_features, n_tgt_features)
    for k, v in vocab.items():
        fields[k].vocab = v
    return fields


def save_fields_to_vocab(fields):
    """
    fields: a dictionary whose keys are field names and whose values are
            Field objects
    returns: a list of (field name, vocab) pairs for the fields that have a
             vocabulary
    """
    vocab = []
    for k, f in fields.items():
        if f is not None and 'vocab' in f.__dict__:
            f.vocab.stoi = f.vocab.stoi
            vocab.append((k, f.vocab))
    return vocab


def merge_vocabs(vocabs, vocab_size=None, min_frequency=1):
    """
    Merge individual vocabularies (assumed to be generated from disjoint
    documents) into a larger vocabulary.

    Args:
        vocabs: `torchtext.vocab.Vocab` vocabularies to be merged
        vocab_size: `int` the final vocabulary size. `None` for no limit.
        min_frequency: `int` minimum frequency for word to be retained.
    Return:
        `torchtext.vocab.Vocab`
    """
    merged = sum([vocab.freqs for vocab in vocabs], Counter())
    return torchtext.vocab.Vocab(merged,
                                 specials=[UNK_WORD, PAD_WORD,
                                           BOS_WORD, EOS_WORD],
                                 max_size=vocab_size,
                                 min_freq=min_frequency)


def get_num_features(src_data_type, corpus_file, side):
    """
    Args:
        src_data_type (str): ['text'|'img'|'audio']
        corpus_file (str): file path to get the features.
        side (str): src or tgt

    Returns:
        number of features on `side`.
    """
    assert side in ["src", "tgt"]
    assert src_data_type in ['text', 'img', 'audio'], \
        "Data type not implemented"
    if side == 'src' and src_data_type != 'text':
        return 0  # no features for non-text
    else:
        with codecs.open(corpus_file, "r", "utf-8") as f:
            line = f.readline().strip().split()
            _, _, n_feats = TextDataset.extract_text_features(line)
            return n_feats


def make_features(batch, side, data_type='text'):
    """
    Args:
        batch (Tensor): a batch of source or target data.
        side (str): for source or for target.
        data_type (str): type of the source input.
            Options are [text|img|audio].
    Returns:
        A sequence of src/tgt tensors with optional feature tensors
        of size (len x batch).
    """
    assert side in ['src', 'tgt']
    if isinstance(batch.__dict__[side], tuple):
        data = batch.__dict__[side][0]
    else:
        data = batch.__dict__[side]

    feat_start = side + "_feat_"
    keys = sorted([k for k in batch.__dict__ if feat_start in k])
    features = [batch.__dict__[k] for k in keys]
    levels = [data] + features

    if data_type == 'text':
        return torch.cat([level.unsqueeze(2) for level in levels], 2)
    else:
        return levels[0]


def collect_features(fields, side="src"):
    """
    Collect features from Field object.
    """
    assert side in ["src", "tgt"]
    feats = []
    for j in count():
        key = side + "_feat_" + str(j)
        if key not in fields:
            break
        feats.append(key)
    return feats


def collect_feature_vocabs(fields, side):
    """
    Collect feature Vocab objects from Field object.
    """
    assert side in ['src', 'tgt']
    feature_vocabs = []
    for j in count():
        key = side + "_feat_" + str(j)
        if key not in fields:
            break
        feature_vocabs.append(fields[key].vocab)
    return feature_vocabs


# min_len is misnamed
def filter_example(ex, use_src_len=True, use_tgt_len=True,
                   min_src_len=0, max_src_len=float('inf'),
                   min_tgt_len=0, max_tgt_len=float('inf')):
    """
    A generalized function for filtering examples based on the length of their
    src or tgt values. Rather than being used by itself as the filter_pred
    argument to a dataset, it should be partially evaluated with everything
    specified except the value of the example.
    """
    return (not use_src_len or min_src_len < len(ex.src) <= max_src_len) and \
        (not use_tgt_len or min_tgt_len < len(ex.tgt) <= max_tgt_len)


def build_dataset(fields, data_type, src_data_iter=None, src_path=None,
                  src_dir=None, tgt_data_iter=None, tgt_path=None,
                  src_seq_len=0, tgt_seq_len=0,
                  src_seq_length_trunc=0, tgt_seq_length_trunc=0,
                  dynamic_dict=True, sample_rate=0,
                  window_size=0, window_stride=0, window=None,
                  normalize_audio=True, use_filter_pred=True,
                  image_channel_size=3):
    """
    Build src/tgt examples iterator from corpus files, also extract
    number of features.
    """

    if data_type == 'text':
        src_examples_iter = TextDataset.make_text_examples(
            src_data_iter, src_path, src_seq_length_trunc, "src"
        )
    elif data_type == 'img':
        src_examples_iter = ImageDataset.make_image_examples(
            src_data_iter, src_path, src_dir, image_channel_size)

    elif data_type == 'audio':
        if src_data_iter:
            raise ValueError("""Data iterator for AudioDataset isn't
                                implemented""")
        if src_path is None:
            raise ValueError("AudioDataset requires a non None path")
        src_examples_iter = AudioDataset.make_audio_examples(
            src_path, src_dir, sample_rate, window_size, window_stride,
            window, normalize_audio
        )

    # For all data types, the tgt side corpus is in form of text.
    tgt_examples_iter = TextDataset.make_text_examples(
        tgt_data_iter, tgt_path, tgt_seq_length_trunc, "tgt"
    )

    # I'm not certain about the practical utility of the second part
    if use_filter_pred and tgt_examples_iter is not None:
        filter_pred = partial(
            filter_example, use_src_len=data_type == 'text',
            max_src_len=src_seq_len, max_tgt_len=tgt_seq_len
        )
    else:
        filter_pred = None

    if data_type == 'text':
        dataset = TextDataset(
            fields, src_examples_iter, tgt_examples_iter,
            dynamic_dict=dynamic_dict, filter_pred=filter_pred)
    else:
        dataset_cls = ImageDataset if data_type == 'img' else AudioDataset
        dataset = dataset_cls(
            fields, src_examples_iter, tgt_examples_iter,
            filter_pred=filter_pred
        )
    return dataset


def _build_field_vocab(field, counter, **kwargs):
    specials = list(OrderedDict.fromkeys(
        tok for tok in [field.unk_token, field.pad_token, field.init_token,
                        field.eos_token]
        if tok is not None))
    field.vocab = field.vocab_cls(counter, specials=specials, **kwargs)


def build_vocab(train_dataset_files, fields, data_type, share_vocab,
                src_vocab_path, src_vocab_size, src_words_min_frequency,
                tgt_vocab_path, tgt_vocab_size, tgt_words_min_frequency):
    """
    Args:
        train_dataset_files: a list of train dataset pt file.
        fields (dict): fields to build vocab for.
        data_type: "text", "img" or "audio"?
        share_vocab(bool): share source and target vocabulary?
        src_vocab_path(string): Path to src vocabulary file.
        src_vocab_size(int): size of the source vocabulary.
        src_words_min_frequency(int): the minimum frequency needed to
                include a source word in the vocabulary.
        tgt_vocab_path(string): Path to tgt vocabulary file.
        tgt_vocab_size(int): size of the target vocabulary.
        tgt_words_min_frequency(int): the minimum frequency needed to
                include a target word in the vocabulary.

    Returns:
        Dict of Fields
    """
    # Prop src from field to get lower memory using when training with image
    if data_type == 'img' or data_type == 'audio':
        fields.pop("src")
    counters = {k: Counter() for k in fields}

    # Load vocabulary
    src_vocab = load_vocabulary(src_vocab_path, tag="source")
    if src_vocab is not None:
        src_vocab_size = len(src_vocab)
        logger.info('Loaded source vocab has %d tokens.' % src_vocab_size)
        for i, token in enumerate(src_vocab):
            # keep the order of tokens specified in the vocab file by
            # adding them to the counter with decreasing counting values
            counters['src'][token] = src_vocab_size - i

    tgt_vocab = load_vocabulary(tgt_vocab_path, tag="target")
    if tgt_vocab is not None:
        tgt_vocab_size = len(tgt_vocab)
        logger.info('Loaded source vocab has %d tokens.' % tgt_vocab_size)
        for i, token in enumerate(tgt_vocab):
            counters['tgt'][token] = tgt_vocab_size - i

    for i, path in enumerate(train_dataset_files):
        dataset = torch.load(path)
        logger.info(" * reloading %s." % path)
        for ex in dataset.examples:
            for k in fields:
                has_vocab = (k == 'src' and src_vocab) or \
                    (k == 'tgt' and tgt_vocab)
                if fields[k].sequential and not has_vocab:
                    val = getattr(ex, k, None)
                    counters[k].update(val)

        # Drop the none-using from memory but keep the last
        if i < len(train_dataset_files) - 1:
            dataset.examples = None
            gc.collect()
            del dataset.examples
            gc.collect()
            del dataset
            gc.collect()

    _build_field_vocab(
        fields["tgt"], counters["tgt"],
        max_size=tgt_vocab_size, min_freq=tgt_words_min_frequency)
    logger.info(" * tgt vocab size: %d." % len(fields["tgt"].vocab))

    # All datasets have same num of n_tgt_features,
    # getting the last one is OK.
    n_tgt_feats = sum('tgt_feat_' in k for k in fields)
    for j in range(n_tgt_feats):
        key = "tgt_feat_" + str(j)
        _build_field_vocab(fields[key], counters[key])
        logger.info(" * %s vocab size: %d." % (key, len(fields[key].vocab)))

    if data_type == 'text':
        _build_field_vocab(
            fields["src"], counters["src"],
            max_size=src_vocab_size, min_freq=src_words_min_frequency)
        logger.info(" * src vocab size: %d." % len(fields["src"].vocab))

        # All datasets have same num of n_src_features,
        # getting the last one is OK.
        n_src_feats = sum('src_feat_' in k for k in fields)
        for j in range(n_src_feats):
            key = "src_feat_" + str(j)
            _build_field_vocab(fields[key], counters[key])
            logger.info(" * %s vocab size: %d." %
                        (key, len(fields[key].vocab)))

        # Merge the input and output vocabularies.
        if share_vocab:
            # `tgt_vocab_size` is ignored when sharing vocabularies
            logger.info(" * merging src and tgt vocab...")
            merged_vocab = merge_vocabs(
                [fields["src"].vocab, fields["tgt"].vocab],
                vocab_size=src_vocab_size,
                min_frequency=src_words_min_frequency)
            fields["src"].vocab = merged_vocab
            fields["tgt"].vocab = merged_vocab

    return fields


def load_vocabulary(vocabulary_path, tag=""):
    """
    Loads a vocabulary from the given path.
    :param vocabulary_path: path to load vocabulary from
    :param tag: tag for vocabulary (only used for logging)
    :return: vocabulary or None if path is null
    """
    vocabulary = None
    if vocabulary_path:
        vocabulary = []
        logger.info("Loading {} vocabulary from {}".format(tag,
                                                           vocabulary_path))

        if not os.path.exists(vocabulary_path):
            raise RuntimeError(
                "{} vocabulary not found at {}!".format(tag, vocabulary_path))
        else:
            with codecs.open(vocabulary_path, 'r', 'utf-8') as f:
                for line in f:
                    if len(line.strip()) == 0:
                        continue
                    word = line.strip().split()[0]
                    vocabulary.append(word)
    return vocabulary


class OrderedIterator(torchtext.data.Iterator):

    def create_batches(self):
        """ Create batches """
        if self.train:
            def _pool(data, random_shuffler):
                for p in torchtext.data.batch(data, self.batch_size * 100):
                    p_batch = torchtext.data.batch(
                        sorted(p, key=self.sort_key),
                        self.batch_size, self.batch_size_fn)
                    for b in random_shuffler(list(p_batch)):
                        yield b

            self.batches = _pool(self.data(), self.random_shuffler)
        else:
            self.batches = []
            for b in torchtext.data.batch(self.data(), self.batch_size,
                                          self.batch_size_fn):
                self.batches.append(sorted(b, key=self.sort_key))


class DatasetLazyIter(object):
    """ An Ordered Dataset Iterator, supporting multiple datasets,
        and lazy loading.

    Args:
        datasets (list): a list of datasets, which are lazily loaded.
        fields (dict): fields dict for the datasets.
        batch_size (int): batch size.
        batch_size_fn: custom batch process function.
        device: the GPU device.
        is_train (bool): train or valid?
    """

    def __init__(self, datasets, fields, batch_size, batch_size_fn,
                 device, is_train):
        self.datasets = datasets
        self.fields = fields
        self.batch_size = batch_size
        self.batch_size_fn = batch_size_fn
        self.device = device
        self.is_train = is_train

        self.cur_iter = self._next_dataset_iterator(datasets)
        # We have at least one dataset.
        assert self.cur_iter is not None

    def __iter__(self):
        dataset_iter = (d for d in self.datasets)
        while self.cur_iter is not None:
            for batch in self.cur_iter:
                yield batch
            self.cur_iter = self._next_dataset_iterator(dataset_iter)

    def __len__(self):
        # We return the len of cur_dataset, otherwise we need to load
        # all datasets to determine the real len, which loses the benefit
        # of lazy loading.
        assert self.cur_iter is not None
        return len(self.cur_iter)

    def _next_dataset_iterator(self, dataset_iter):
        try:
            # Drop the current dataset for decreasing memory
            if hasattr(self, "cur_dataset"):
                self.cur_dataset.examples = None
                gc.collect()
                del self.cur_dataset
                gc.collect()

            self.cur_dataset = next(dataset_iter)
        except StopIteration:
            return None

        # We clear `fields` when saving, restore when loading.
        self.cur_dataset.fields = self.fields

        # Sort batch by decreasing lengths of sentence required by pytorch.
        # sort=False means "Use dataset's sortkey instead of iterator's".
        return OrderedIterator(
            dataset=self.cur_dataset, batch_size=self.batch_size,
            batch_size_fn=self.batch_size_fn,
            device=self.device, train=self.is_train,
            sort=False, sort_within_batch=True,
            repeat=False)


def build_dataset_iter(datasets, fields, opt, is_train=True):
    """
    This returns user-defined train/validate data iterator for the trainer
    to iterate over. We implement simple ordered iterator strategy here,
    but more sophisticated strategy like curriculum learning is ok too.
    """
    batch_size = opt.batch_size if is_train else opt.valid_batch_size
    if is_train and opt.batch_type == "tokens":
        def batch_size_fn(new, count, sofar):
            """
            In token batching scheme, the number of sequences is limited
            such that the total number of src/tgt tokens (including padding)
            in a batch <= batch_size
            """
            # Maintains the longest src and tgt length in the current batch
            global max_src_in_batch, max_tgt_in_batch
            # Reset current longest length at a new batch (count=1)
            if count == 1:
                max_src_in_batch = 0
                max_tgt_in_batch = 0
            # Src: <bos> w1 ... wN <eos>
            max_src_in_batch = max(max_src_in_batch, len(new.src) + 2)
            # Tgt: w1 ... wN <eos>
            max_tgt_in_batch = max(max_tgt_in_batch, len(new.tgt) + 1)
            src_elements = count * max_src_in_batch
            tgt_elements = count * max_tgt_in_batch
            return max(src_elements, tgt_elements)
    else:
        batch_size_fn = None

    device = "cuda" if opt.gpu_ranks else "cpu"

    return DatasetLazyIter(datasets, fields, batch_size, batch_size_fn,
                           device, is_train)


def lazily_load_dataset(corpus_type, opt):
    """
    Dataset generator. Don't do extra stuff here, like printing,
    because they will be postponed to the first loading time.

    Args:
        corpus_type: 'train' or 'valid'
    Returns:
        A list of dataset, the dataset(s) are lazily loaded.
    """
    assert corpus_type in ["train", "valid"]

    def _lazy_dataset_loader(pt_file, corpus_type):
        dataset = torch.load(pt_file)
        logger.info('Loading %s dataset from %s, number of examples: %d' %
                    (corpus_type, pt_file, len(dataset)))
        return dataset

    # Sort the glob output by file name (by increasing indexes).
    pts = sorted(glob.glob(opt.data + '.' + corpus_type + '.[0-9]*.pt'))
    if pts:
        for pt in pts:
            yield _lazy_dataset_loader(pt, corpus_type)
    else:
        # Only one inputters.*Dataset, simple!
        pt = opt.data + '.' + corpus_type + '.pt'
        yield _lazy_dataset_loader(pt, corpus_type)


def load_fields(dataset, opt, checkpoint):
    if isinstance(dataset, TextDataset):
        data_type = 'text'
    elif isinstance(dataset, AudioDataset):
        data_type = 'audio'
    else:
        data_type = 'img'
    if checkpoint is not None:
        logger.info('Loading vocab from checkpoint at %s.' % opt.train_from)
        vocab = checkpoint['vocab']
    else:
        vocab = torch.load(opt.data + '.vocab.pt')

    fields = load_fields_from_vocab(vocab, data_type)

    ex_fields = dataset.examples[0].__dict__
    fields = {k: f for k, f in fields.items() if k in ex_fields}

    if data_type == 'text':
        logger.info(' * vocabulary size. source = %d; target = %d' %
                    (len(fields['src'].vocab), len(fields['tgt'].vocab)))
    else:
        logger.info(' * vocabulary size. target = %d' %
                    (len(fields['tgt'].vocab)))

    return fields


def _collect_report_features(fields):
    src_features = collect_features(fields, side='src')
    tgt_features = collect_features(fields, side='tgt')

    return src_features, tgt_features
