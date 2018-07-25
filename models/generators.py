
import torch
import torch.nn as nn
import torch.nn.functional as F

import torch_utils
import loaders
from lstm import embedding_layer


def trim_hidden(hidden, new_lengths):
    if hidden is not None:
        batch_size = hidden[0].size(1)

        if batch_size < len(new_lengths):
            # starting new buffer
            return None

        # remove finished batch song from hidden
        return (hidden[0][:,:len(new_lengths)].contiguous(),
                hidden[1][:,:len(new_lengths)].contiguous())

    return hidden


def pad_sequence(batch, lengths, padding_idx=0, batch_first=False):
    if isinstance(lengths, torch.Tensor):
        lengths = lengths.tolist()
    batch_size, maxlen = len(batch), max(lengths)
    t = torch.zeros(batch_size, maxlen, dtype=torch.int64)
    for idx, (example, length) in enumerate(zip(batch, lengths)):
        t = t.to(example.device)
        t[idx, :length].copy_(example)

    if not batch_first:
        t = t.t()

    return t


class LM(nn.Module):
    """
    conds: list of condition encoders
    """
    def __init__(self, vocab, emb_dim, hid_dim, num_layers=1, dropout=0.0,
                 padding_idx=None, conds=None, cond_emb_dim=None):
        self.dropout = dropout
        super().__init__()

        if padding_idx is not None:
            nll_weight = torch.ones(vocab)
            nll_weight[padding_idx] = 0.
            self.register_buffer('nll_weight', nll_weight)
        else:
            self.nll_weight = None

        # input emb
        self.embs = nn.Embedding(vocab, emb_dim, padding_idx=padding_idx)
        # conds emb
        rnn_inp = emb_dim
        self.conds = None
        if conds is not None:
            self.conds = {}
            for cond in conds:
                emb = nn.Embedding(len(cond), cond_emb_dim or emb_dim)
                self.conds[cond.name] = emb
                self.add_module(cond.name, emb)
                rnn_inp += cond_emb_dim or emb_dim
        # rnn
        self.rnn = nn.LSTM(rnn_inp, hid_dim, num_layers,
                           dropout=dropout, batch_first=True)

        # output
        self.proj = nn.Linear(hid_dim, vocab)

        if emb_dim == hid_dim:
            print("Tying embedding and projection weights")
            self.proj.weight = self.embs.weight

        self.init()

    def device(self):
        return next(self.parameters()).device

    def init(self):
        initrange = 0.1
        nn.init.uniform_(self.embs.weight, -initrange, initrange)
        nn.init.constant_(self.proj.bias, 0.)
        nn.init.uniform_(self.proj.weight, -initrange, initrange)

    def forward(self, inp, lengths, hidden=None, conds=None):
        if conds is not None and self.conds is not None:
            raise ValueError("conds needs conds")

        embs = self.embs(inp)
        embs = F.dropout(embs, p=self.dropout, training=self.training)
        if conds is not None:
            batch, seq_len = inp.size()
            conds = [self.conds[cond](conds[cond]).expand(batch, seq_len, -1)
                     for cond in conds]
            embs = torch.cat([embs, *conds], dim=2)
            
        embs, unsort = torch_utils.pack_sort(embs, lengths, batch_first=True)
        output, hidden = self.rnn(embs, hidden)
        output, _ = nn.utils.rnn.pad_packed_sequence(output, batch_first=True)
        output = output[unsort]
        hidden = hidden[0][:, unsort], hidden[1][:, unsort]

        logits = self.proj(output)  # (batch x seq_len x vocab)

        return logits, hidden

    def loss(self, logits, targets, lengths):
        batch, seq_len, vocab = logits.size()

        loss = F.cross_entropy(
            logits.contiguous().view(batch * seq_len, vocab),
            targets.contiguous().view(-1),
            weight=self.nll_weight, size_average=False)

        loss = loss / lengths.sum().item()

        return loss

    def prepare_batch(self, batch):
        lengths = torch.LongTensor(batch['length'])
        syllables = batch['syllables']
        syllables = pad_sequence(syllables, lengths, batch_first=True).to(device)
        lengths = lengths.to(device)

        # TODO: add conditions, should be a list of 1D tensors (only batch dim)
        conds = None

        return syllables, lengths, conds

    def evaluate(self, dataset, device):
        hidden = None
        total_loss, total_batches = 0, 0

        for batch in dataset.batches():
            # prepare data
            syllables, lengths, conds = self.prepare_batch(batch)

            # get loss
            hidden = trim_hidden(hidden, lengths)
            logits, hidden = self(syllables, lengths, hidden, conds)
            loss = self.loss(logits[:, :-1], syllables[:, 1:], lengths-1)
            total_loss += loss.exp().item()
            total_batches += 1

        return total_loss / total_batches

    def train_model(self, epochs, dataset, optim, device, devset=None, max_norm=None,
                    report_freq=1000, check_freq=4000, encoder=None):

        for e in range(epochs):
            print("Training on epoch [{}]".format(e+1))
            hidden = None
            epoch_loss, epoch_batches = 0, 0            
            total_loss, total_batches = 0, 0

            for batch_num, batch in enumerate(dataset.batches()):
                syllables, lengths, conds = self.prepare_batch(batch)

                # get loss
                hidden = trim_hidden(hidden, lengths)
                logits, hidden = self(syllables, lengths, hidden, conds)
                loss = self.loss(logits[:, :-1], syllables[:, 1:], lengths-1)

                # optimize
                loss.backward(retain_graph=True)

                # bptt (4 lines)
                if batch_num % 4 == 0:
                    if max_norm is not None:
                        nn.utils.clip_grad_norm_(self.parameters(), max_norm)
                    optim.step()
                    optim.zero_grad()
                    # detach hidden from previous
                    hidden = hidden[0].detach(), hidden[1].detach()

                total_loss += loss.exp().item()
                epoch_loss += loss.exp().item()
                total_batches += 1
                epoch_batches += 1

                if batch_num > 0 and (batch_num + 1) % report_freq == 0:
                    print("Epoch/batch: {}/{}: {:.3f} ppl".format(
                        e+1, batch_num+1, total_loss / total_batches))
                    total_loss, total_batches = 0, 0

                if batch_num > 0 and (batch_num + 1) % check_freq == 0:
                    if devset is not None:
                        print("Evaluating...")
                        with torch.no_grad():
                            self.eval()
                            dev_loss = self.evaluate(devset, device)
                            self.train()
                        print("Dev loss: {:.3f}".format(dev_loss))

                    if encoder is not None:
                        print("Generating samples:")
                        for hyp, score in zip(*self.generate(encoder)):
                            print('[{:.2f}] => {}'.format(score, ' '.join(hyp)))

            print("Epoch loss: {:.3f}".format(epoch_loss / epoch_batches))

            if devset is not None:
                print("Evaluating...")
                with torch.no_grad():
                    self.eval()
                    dev_loss = self.evaluate(devset, device)
                    self.train()
                print("Dev loss: {:.3f}".format(dev_loss))

    def generate(self, encoder, conds=None, nlines=5, maxlen=25, sample=True, temp=1.0):
        # input variables
        inp = torch.tensor([encoder.bos_index] * nlines).to(self.device())
        lengths = torch.ones(nlines, dtype=torch.int64).to(self.device())
        hidden = None
        # accumulators
        mask = torch.ones(nlines, dtype=torch.int64)
        scores, hyps = 0, []

        for _ in range(maxlen):
            # check if finished
            if sum(mask).item() == 0:
                break

            # run forward
            inp = inp.unsqueeze(1)  # add seq_len dim
            preds, hidden = self(inp, lengths, hidden, conds=conds)
            preds = preds.squeeze(1)  # remove seq_len dim

            # sample
            preds = F.log_softmax(preds, dim=-1)
            if sample:
                inp = (preds/temp).exp().multinomial(1)
                score = preds.gather(1, inp)
                score, inp = score.squeeze(1), inp.squeeze(1)
            else:
                score, inp = torch.max(preds, dim=-1)

            # accumulate
            mask = mask * (inp.cpu() != encoder.eos_index).long()
            scores += score.cpu() * mask.float()
            hyp = inp.tolist()
            for idx, active in enumerate(mask.tolist()):
                if not active:
                    hyp[idx] = None
            hyps.append(hyp)

        # get results
        hyps = [list(filter(None, hyp)) for hyp in zip(*hyps)]
        hyps = [encoder.decode(hyp) for hyp in hyps]
        scores = scores.tolist()
        scores = [score/(len(hyp) + 1e-25) for score, hyp in zip(scores, hyps)]

        return hyps, scores


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--num_layers', default=2, type=int)
    parser.add_argument('--emb_dim', default=200, type=int)
    parser.add_argument('--hid_dim', default=200, type=int)
    parser.add_argument('--dropout', default=0.5, type=float)
    parser.add_argument('--epochs', default=100, type=int)
    parser.add_argument('--batch_size', default=50, type=int)
    parser.add_argument('--train_file', required=True, type=str)
    parser.add_argument('--dev_file', required=True, type=str)
    parser.add_argument('--learning_rate', default=0.001, type=float)
    parser.add_argument('--max_norm', default=5, type=float)
    parser.add_argument('--lr_patience', type=int, default=5)
    args = parser.parse_args()

    # data loading
    syllable_encoder = loaders.Encoder('syllables', preprocessor=loaders.format_syllables)

    # fit label encoder on quicker dataset type
    print("Fitting dataset")
    import time
    start = time.time()
    for _ in loaders.DataSet(
            args.train_file, batch_size=args.batch_size, syllables=syllable_encoder):
        pass
    print("Took {:g} secs".format(time.time()-start))
    syllable_encoder.save_vocab('syllable_encoder.txt')
    syllable_encoder.fixed_vocab = True

    trainset = loaders.BlockDataSet(
        args.train_file, batch_size=args.batch_size, syllables=syllable_encoder)

    devset = loaders.BlockDataSet(
        args.dev_file, batch_size=args.batch_size, syllables=syllable_encoder)

    # model
    model = LM(len(syllable_encoder), args.emb_dim, args.hid_dim, args.num_layers,
               dropout=args.dropout, padding_idx=syllable_encoder.pad_index)
    print(model)

    # training
    optim = torch.optim.Adam(model.parameters(), lr=args.learning_rate)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    model.train_model(args.epochs, trainset, optim, device,
                      devset=devset, max_norm=args.max_norm,
                      encoder=syllable_encoder)
