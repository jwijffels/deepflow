import os
import uuid
import random

import torch
from allennlp.predictors import Predictor

from .generation import RNNLanguageModel, CharLanguageModel  # needed by torch.load
from .generation import Cache
from .generation import utils


def load_models(config):
    """
    Load the models into a dictionary. Only called at loading time.
    """
    models = {}
    for model in os.path.isdir(config.MODEL_DIR):
        if not model.endswith('.pt'):  # avoid weird files if present
            continue

        mconfig = {"path": model}
        if config.MODELS:
            if model not in config.MODELS:
                mconfig = config.MODELS[model]
            else:
                continue

        print("Loading model: {}".format(model))
        # load model
        stuff = torch.load(os.path.join(config.MODEL_DIR, model))
        model, encoder = stuff["model"], stuff["encoder"]
        del stuff

        # create cache if necessary
        cache = None
        if mconfig.get("options", {}).get("cache"):
            cache = Cache(
                model.hidden_dim,               # hidden_dim
                config.DEFAULTS["cache_size"],  # cache_size
                len(encoder.word.w2i))          # vocabulary

        # add mconfig
        models[model] = {
            "model": stuff["model"],
            "encoder": stuff["encoder"],
            "options": mconfig.get("options", {}),
            "cache": cache,
            "hidden": None}

    return models


def resample_conds(encoder, conds, counter):
    """
    Takes care of sampling conditions and planning the song over consecutive generations
    """
    def sample_conds(encoder):
        conds = {}
        for cond, values in encoder.conds.items():
            # TODO: add weights to conditions based on rhyme evaluation
            #       e.g. shorter phonological endings are more productive in general
            conds[cond] = random.choice(list(values.keys()))
        return conds

    # TODO: better planning
    if not conds or counter % 4 == 0:
        return sample_conds(encoder)

    return conds


def process_seed(mconfig, seed, seed_conds):
    """
    Process the picked option (seed) and return the new hidden state / cache
    """
    (word, nwords), (char, nchars), conds = \
        mconfig["encoder"].transform_batch([seed], [seed_conds])

    _, hidden = mconfig["model"](
        # TODO: add cache
        word, nwords, char, nchars, conds, mconfig.get("hidden"))

    return hidden


def syllabify(syllabifier, words):
    sent = []
    for word in words:
        syllables = []
        syllabifier.predict(' '.join(word))
        for char, tag in zip(pred['words'], pred['tags']):
            if int(tag) > 0:
                syllables.append('')
            syllables[-1] += char

        sent.extend(utils.format_syllables(syllables))

    return sent


def get_model_generation(mconfig, config, conds,
                         # seed params
                         seed=None, seed_conds=None):
    """
    Run a model over a given seed to generate a continuation. This function
    shouldn't have any side-effects. Instead all updates are done by the
    Generator class itself.

    Arguments:
    ----------
    - mconfig : dict, model-specific dictionary with keys:
        "model", "encoder", "hidden", "cache", "options"
    - config : AppConfig, global AppConfig object
    - conds : dict, dictionary of currently active conditions
    - seed : str, picked sentence to use as seed (already syllabified)
    - seed_conds : dict, dictionary of conditions used to generate previous sentence

    Returns:
    --------
    hyp : str, generated sentence
    score : float, score for the generated sentence
    hidden : tuple, torch.Tensor or None, hidden state after reading currently
        picked candidate
    """
    model = mconfig["model"]

    # preprocess seed
    hidden = mconfig.get("hidden")
    if seed is not None:
        hidden = process_seed(mconfig, seed, seed_conds)

    # transform conditions to actual input
    conds = {key: mconfig['encoder'].conds[key].w2i[val] for key, val in conds.items()}

    (hyps, _), scores, _ = model.sample(
        mconfig['encoder'],
        # general config
        batch=config.TRIES,
        # model config
        conds=conds,
        hidden=hidden.repeat(1, config.TRIES, 1),  # expand to batch
        tau=mconfig.get("options", {}).get("tau", config.DEFAULTS["tau"]),
        # cache: TODO: don't force-update cache until a pick has been done
        cache=mconfig.get("cache"))

    # filter out invalid hyps
    c, seed = 0, seed.split()
    while c < len(hyps):
        hyp = hyps[c].split()
        if not utils.is_valid(hyp) or (seed and not utils.is_valid_pair(hyp, seed)):
            hyps.pop(c)
            scores.pop(c)
        else:
            c += 1

    # sample from the filtered hyps
    idx = random.choices(list(range(len(hyps))), scores)[0]
    # select sampled
    hyp, score = hyps[idx], scores[idx]

    return hyp, score, hidden


class Generator:
    """
    Attributes:
    - config : AppConfig
    - counter : number of accepted candidates so far
    - syllabifier : allennlp.services.Predictor
    - models : dictionary
    - candidates : dictionary,

        { "conds": dict,
          "hyps": {
            modelname: {
                id: {
                    "hyp": str,
                    "score": score
                }
            }
          }
        }
    """
    def __init__(self, config):
        self.config = config
        # counter
        self.counter = 0
        # load syllabifier
        self.syllabifier = Predictor.from_path(
            os.path.join(config['MODEL_DIR'], config['SYLLABIFIER']))
        # load models
        self.models = load_models(config)
        # sample initial candidates
        self.candidates = {"conds": {}, "hyps": {}}

    def sample(self, seed_id=None):
        candidates = {}

        # add new conditions
        encoder = self.models.values()[0]["encoder"]
        conds = resample_conds(encoder, self.candidates["conds"], self.counter)
        candidates["conds"] = conds

        # prepare seed
        seed = None
        if seed_id is not None:
            modelname, uuid = seed_id.split('/')
            seed = self.candidates["hyps"][modelname][uuid]["hyp"].split()
            if isinstance(self.models[modelname], CharLanguageModel):
                # needs syllabification
                seed = syllabify(self.syllabifier, seed.split())

        # add generations
        candidates["hyps"] = {}
        payload = []

        for model, mconfig in self.models.items():
            # TODO: this should run multiprocessing
            hyp, score, hidden = get_model_generation(
                mconfig, self.config, conds,
                seed=seed, seed_conds=self.candidates["conds"])

            # update new candidates
            id = str(uuid.uuid1())
            candidates["hyps"][modelname][id] = {"hyp": hyp, "score": score}

            # update hidden
            self.models[model]["hidden"] = hidden

            # TODO: update cache

            # payload
            if isinstance(self.models[model], RNNLanguageModel):
                hyp = utils.join_syllables(hyp.split())
            payload.append({"id": "{}/{}".format(modelname, id), "text": hyp})

        # reset candidates
        self.candidates = candidates

        return payload

    def reset(self):
        self.counter = 0
        # needs to stop any other model-related process first
        for model, mconfig in self.models.items():
            # reset hidden
            mconfig["hidden"] = None
            # reset cache if necessary
            if mconfig.get("cache"):
                mconfig["cache"].reset()

        self.candidates = {"conds": {}, "hyps": {}}
