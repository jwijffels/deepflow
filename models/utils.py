import numpy as np


def perm_sort(x, index):
    return [x[i] for i in index]

def inverse_perm_sort(x, index):
    return [x[i] for _, i in sorted(zip(index, range(len(index))))]

def accuracy_score(x: np.ndarray, y: np.ndarray) -> float:
    return sum((a == b).all() for a, b in zip(x, y)) / len(x)

def chop_padding(samples, lengths):
    return [samples[i, 0:lengths[i]] for i in range(samples.shape[0])]

def seq_accuracy_score(x: np.ndarray, y: np.ndarray) -> float:
    def accuracy(a, b):
        return sum(i == j for i, j in zip(a, b)) / len(a)
    return [accuracy(a, b) for a, b in zip(x, y)]

def fmap(*funcs):
    def _f(collection):
        result = collection
        for fn in reversed(funcs):
            result = fn(result)
        return result
    return _f
