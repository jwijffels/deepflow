
import math
import random
import time
import itertools

import torch
import torch.nn as nn
import torch.nn.functional as F

from . import utils
from . import torch_utils
from .model import RNNLanguageModel


class HybridLanguageModel(RNNLanguageModel):
    def __init__(self, encoder, layers, wemb_dim, cemb_dim, hidden_dim, cond_dim,
                 dropout=0.0):

        self.layers = layers
        self.wemb_dim = wemb_dim
        self.cemb_dim = cemb_dim
        self.hidden_dim = hidden_dim
        self.cond_dim = cond_dim
        self.dropout = dropout
        self.modelname = self.get_modelname()
        super(RNNLanguageModel, self).__init__()

        wvocab = encoder.word.size()
        cvocab = encoder.char.size()

        nll_weight = torch.ones(cvocab)
        nll_weight[encoder.char.pad] = 0
        self.register_buffer('nll_weight', nll_weight)

        # embeddings
        self.wembs = nn.Embedding(wvocab, wemb_dim, padding_idx=encoder.word.pad)
        self.cembs = nn.Embedding(cvocab, cemb_dim, padding_idx=encoder.char.pad)
        self.cembs_rnn = nn.LSTM(cemb_dim, cemb_dim//2, bidirectional=True)
        input_dim = wemb_dim + cemb_dim

        # conds
        self.conds = {}
        for cond, cenc in encoder.conds.items():
            cemb = nn.Embedding(cenc.size(), cond_dim)
            self.add_module('cond_{}'.format(cond), cemb)
            self.conds[cond] = cemb
            input_dim += cond_dim

        # rnn
        rnn = []
        for layer in range(layers):
            rnn_inp = input_dim if layer == 0 else hidden_dim
            rnn_hid = hidden_dim
            rnn.append(nn.LSTM(rnn_inp, rnn_hid))
        self.rnn = nn.ModuleList(rnn)

        # output
        self.cout_rnn = nn.LSTM(cemb_dim + hidden_dim, hidden_dim)
        self.proj = nn.Linear(hidden_dim, cvocab)

        self.init()

    def init(self):
        pass

    def forward(self, word, nwords, char, nchars, conds, hidden=None):
        # - embeddings
        # (seq x batch x wemb_dim)
        wembs = self.wembs(word)
        # (seq x batch x cemb_dim)
        cembs = self.embed_chars(char, nchars, nwords)

        embs = torch.cat([wembs, cembs], -1)

        # - conditions
        if conds:
            conds = [self.conds[c](conds[c]) for c in sorted(conds)]
            # expand
            seq, batch = word.size()
            conds = [c.expand(seq, batch, -1) for c in conds]
            # concatenate
            embs = torch.cat([embs, *conds], -1)

        # dropout!: input dropout (dropouti)
        embs = torch_utils.sequential_dropout(
            embs, p=self.dropout, training=self.training)

        # - rnn
        embs, unsort = torch_utils.pack_sort(embs, nwords)
        outs = embs
        hidden_ = []
        hidden = hidden or [None] * len(self.rnn)
        for l, rnn in enumerate(self.rnn):
            outs, h_ = rnn(outs, hidden[l])
            if l != len(self.rnn) - 1:
                outs, lengths = nn.utils.rnn.pad_packed_sequence(outs)
                # dropout!: hidden dropout (dropouth)
                outs = torch_utils.sequential_dropout(
                    outs, self.dropout, self.training)
                outs = nn.utils.rnn.pack_padded_sequence(outs, lengths)
            hidden_.append(h_)
        outs, _ = nn.utils.rnn.pad_packed_sequence(outs)
        outs = outs[:, unsort]
        hidden = hidden_
        for l, h in enumerate(hidden):
            if isinstance(h, tuple):
                hidden[l] = h[0][:, unsort], h[1][:, unsort]
            else:
                hidden[l] = h[:, unsort]

        # dropout!: output dropout (dropouto)
        outs = torch_utils.sequential_dropout(outs, self.dropout, self.training)

        # - compute char-level logits
        breaks = list(itertools.accumulate(nwords))
        # (nwords x hidden_dim)
        outs = torch_utils.flatten_padded_batch(outs, nwords)
        # indices to remove </l> from outs
        index = [i for i in range(sum(nwords)) if i+1 not in breaks]
        # (nwords - batch x hidden_dim)
        outs = outs[torch.tensor(index).to(self.device)]
        # indices to remove <l> tokens from character targets
        index = [i for i in range(sum(nwords)) if i not in breaks][1:]
        # (nchars x nwords - batch)
        char = char[:, torch.tensor(index).to(self.device)]
        # (nchars x nwords - batch x cemb_dim + hidden_dim)
        cemb = torch.cat([self.cembs(char), outs.expand(len(char), -1, -1)], -1)
        # run rnn
        cemb, unsort = torch_utils.pack_sort(cemb, [nchars[i] for i in index])
        # (nchars x nwords - batch x hidden_dim)
        couts, _ = self.cout_rnn(cemb)
        couts, _ = nn.utils.rnn.pad_packed_sequence(couts)
        couts = couts[:, unsort]
        # logits: (nchars x nwords - batch x vocab)
        logits = self.proj(couts)

        return logits, hidden

    def loss(self, logits, word, nwords, char, nchars):
        breaks = list(itertools.accumulate(nwords))
        # indices to remove <l> tokens from targets
        index = [i for i in range(sum(nwords)) if i not in breaks][1:]
        # (nchars x nwords - batch)
        targets = char[:, torch.tensor(index).to(self.device)]

        # - remove </w> from char logits and <w> from char targets
        logits, targets = logits[:-1], targets[1:]

        loss = F.cross_entropy(
            logits.view(-1, logits.size(-1)), targets.view(-1),
            weight=self.nll_weight, size_average=False)

        # remove 1 char per word instance and 2 char per sentence (<l> tokens)
        insts = sum(nchars) - len(nchars) - (2 * len(nwords))

        return loss, insts

    def loss_formatter(self, loss):
        """
        BPC for loss monitoring
        """
        return math.log2(math.e) * loss

    def sample(self, encoder, nsyms=50, max_sym_len=10, batch=1, conds=None, hidden=None):
        """
        Generate stuff
        """
        # sample conditions if needed
        conds, bconds = conds or {}, []
        for c in sorted(self.conds):
            # sample conds
            if c not in conds:
                conds[c] = random.choice(list(encoder.conds[c].w2i.values()))
            # compute embedding
            bcond = torch.tensor([conds[c]] * batch, dtype=torch.int64).to(self.device)
            bcond = self.conds[c](bcond)
            bconds.append(bcond.expand(1, batch, -1))

        word = [encoder.word.bos] * batch  # (batch)
        word = torch.tensor(word, dtype=torch.int64).to(self.device)
        nwords = [1] * batch    # same nwords per step
        # (3 x batch)
        char = [[encoder.char.bos, encoder.char.bol, encoder.char.eos]] * batch
        char = torch.tensor(char, dtype=torch.int64).to(self.device).t()
        nchars = [3] * batch

        output = []
        scores = 0

        with torch.no_grad():
            for _ in range(nsyms):
                # embeddings
                wemb = self.wembs(word.unsqueeze(0))
                cemb = self.embed_chars(char, nchars, nwords)
                embs = torch.cat([wemb, cemb], -1)
                if conds:
                    embs = torch.cat([embs, *bconds], -1)

                outs = embs
                hidden_ = []
                hidden = hidden or [None] * len(self.rnn)
                for l, rnn in enumerate(self.rnn):
                    outs, h_ = rnn(outs, hidden[l])
                    hidden_.append(h_)
                hidden = hidden_

                # char-level
                cinp = torch.tensor([encoder.char.bos] * batch).to(self.device)  # (batch)
                coutput = []
                for _ in range(max_sym_len):
                    # (1 x batch x cemb_dim + hidden_dim)
                    cemb = torch.cat([self.cembs(cinp.unsqueeze(0)), outs], -1)
                    # (1 x batch x hidden_dim)
                    couts, _ = self.cout_rnn(cemb)
                    # (1 x batch x vocab) -> (batch x vocab)
                    logits = self.proj(couts).squeeze(0)
                    # sample
                    preds = F.log_softmax(logits, dim=-1)
                    # (1 x batch) -> (batch)
                    cinp = (preds / 1).exp().multinomial(1)
                    score = preds.gather(1, cinp)
                    cinp, score = cinp.squeeze(1), score.squeeze(1)
                    scores += score

                    # break from word generation
                    if cinp[0].item() == encoder.char.eos:
                        coutput = coutput or [[encoder.char.unk] * batch]
                        break

                    # break from sentence generation
                    if cinp[0].item() == encoder.char.eol:
                        break

                    # accumulate
                    coutput.append(cinp.tolist())

                # break from sentence generation
                if cinp[0].item() == encoder.char.eol:
                    break

                # get word-level and character-level input
                word, char, toutput = [], [], []
                for w in zip(*coutput):  # iterate over words in batch
                    w = ''.join([encoder.char.i2w[i] for i in w])  # to string
                    # get word input
                    word.append(encoder.word.transform_item(w))
                    # get character input
                    c = encoder.char.transform(w)
                    char.append(c)
                    # append to global output
                    toutput.append(w)
                # (batch)
                word = torch.tensor(word, dtype=torch.int64).to(self.device)
                # (nchars x batch)
                char, nchars = utils.get_batch(char, encoder.char.pad, self.device)

                # accumulate
                output.append(toutput)

        hyps = [' '.join(i[0] for i in (output[::-1] if encoder.reverse else output))]
        conds = {c: encoder.conds[c].i2w[cond] for c, cond in conds.items()}
        scores = scores.tolist()

        return (hyps, conds), scores, hidden


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--train')
    parser.add_argument('--dev')
    parser.add_argument('--dpath', help='path to rhyme dictionary')
    parser.add_argument('--reverse', action='store_true',
                        help='whether to reverse input')
    parser.add_argument('--wemb_dim', type=int, default=100)
    parser.add_argument('--cemb_dim', type=int, default=100)
    parser.add_argument('--cond_dim', type=int, default=50)
    parser.add_argument('--hidden_dim', type=int, default=250)
    parser.add_argument('--layers', type=int, default=1)
    parser.add_argument('--tie_weights', action='store_true')
    parser.add_argument('--maxsize', type=int, default=10000)
    parser.add_argument('--disable_chars', action='store_true')
    # train
    parser.add_argument('--lr', type=float, default=0.001)
    parser.add_argument('--lr_weight', type=float, default=1.0)
    parser.add_argument('--trainer', default='Adam')
    parser.add_argument('--clipping', type=float, default=5.0)
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--bptt', type=int, default=1)
    parser.add_argument('--dropout', type=float, default=0.2)
    parser.add_argument('--word_dropout', type=float, default=0.2)
    parser.add_argument('--minibatch', type=int, default=20)
    parser.add_argument('--repfreq', type=int, default=1000)
    parser.add_argument('--checkfreq', type=int, default=0)
    # pytorch
    parser.add_argument('--device', default='cpu')
    # extra
    parser.add_argument('--penn', action='store_true')

    args = parser.parse_args()

    from generation.utils import CorpusEncoder, CorpusReader, PennReader
    reader = PennReader if args.penn else CorpusReader

    print("Encoding corpus")
    start = time.time()
    train = reader(args.train, dpath=args.dpath)
    dev = reader(args.dev, dpath=args.dpath)

    encoder = CorpusEncoder.from_corpus(
        train, dev, most_common=args.maxsize, reverse=args.reverse)
    print("... took {} secs".format(time.time() - start))

    print("Building model")
    lm = HybridLanguageModel(encoder, args.layers, args.wemb_dim, args.cemb_dim,
                             args.hidden_dim, args.cond_dim, dropout=args.dropout)
    print(lm)
    print("Model parameters: {}".format(sum(p.nelement() for p in lm.parameters())))
    print("Storing model to path {}".format(lm.modelname))
    lm.to(args.device)

    print("Training model")
    lm.train_model(train, encoder, epochs=args.epochs, minibatch=args.minibatch,
                   dev=dev, lr=args.lr, trainer=args.trainer, clipping=args.clipping,
                   repfreq=args.repfreq, checkfreq=args.checkfreq,
                   lr_weight=args.lr_weight, bptt=args.bptt)
