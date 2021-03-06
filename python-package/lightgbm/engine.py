# coding: utf-8
# pylint: disable = invalid-name, W0105
"""Training Library containing training routines of LightGBM."""
from __future__ import absolute_import

import collections
from operator import attrgetter
import numpy as np
from .basic import LightGBMError, _InnerPredictor, Dataset, Booster, is_str
from . import callback

def train(params, train_set, num_boost_round=100,
          valid_sets=None, valid_names=None,
          fobj=None, feval=None, init_model=None,
          feature_name=None, categorical_feature=None,
          early_stopping_rounds=None, evals_result=None,
          verbose_eval=True, learning_rates=None, callbacks=None):
    """
    Train with given parameters.

    Parameters
    ----------
    params : dict
        Parameters for training.
    train_set : Dataset
        Data to be trained.
    num_boost_round: int
        Number of boosting iterations.
    valid_sets: list of Datasets
        List of data to be evaluated during training
    valid_names: list of string
        Names of valid_sets
    fobj : function
        Customized objective function.
    feval : function
        Customized evaluation function.
        Note: should return (eval_name, eval_result, is_higher_better) of list of this
    init_model : file name of lightgbm model or 'Booster' instance
        model used for continued train
    feature_name : list of str
        Feature names
    categorical_feature : list of str or int
        Categorical features,
        type int represents index,
        type str represents feature names (need to specify feature_name as well)
    early_stopping_rounds: int
        Activates early stopping.
        Requires at least one validation data and one metric
        If there's more than one, will check all of them
        Returns the model with (best_iter + early_stopping_rounds)
        If early stopping occurs, the model will add 'best_iteration' field
    evals_result: dict or None
        This dictionary used to store all evaluation results of all the items in valid_sets.
        Example: with a valid_sets containing [valid_set, train_set]
                 and valid_names containing ['eval', 'train']
                 and a paramater containing ('metric':'logloss')
        Returns: {'train': {'logloss': ['0.48253', '0.35953', ...]},
                  'eval': {'logloss': ['0.480385', '0.357756', ...]}}
        passed with None means no using this function
    verbose_eval : bool or int
        Requires at least one item in evals.
        If `verbose_eval` is True,
            the eval metric on the valid set is printed at each boosting stage.
        If `verbose_eval` is int,
            the eval metric on the valid set is printed at every `verbose_eval` boosting stage.
        The last boosting stage
            or the boosting stage found by using `early_stopping_rounds` is also printed.
        Example: with verbose_eval=4 and at least one item in evals,
            an evaluation metric is printed every 4 (instead of 1) boosting stages.
    learning_rates: list or function
        List of learning rate for each boosting round
        or a customized function that calculates learning_rate
        in terms of current number of round (e.g. yields learning rate decay)
        - list l: learning_rate = l[current_round]
        - function f: learning_rate = f(current_round)
    callbacks : list of callback functions
        List of callback functions that are applied at end of each iteration.

    Returns
    -------
    booster : a trained booster model
    """
    """create predictor first"""
    if is_str(init_model):
        predictor = _InnerPredictor(model_file=init_model)
    elif isinstance(init_model, Booster):
        predictor = init_model._to_predictor()
    else:
        predictor = None
    init_iteration = predictor.num_total_iteration if predictor is not None else 0
    """check dataset"""
    if not isinstance(train_set, Dataset):
        raise TypeError("Traninig only accepts Dataset object")

    train_set._set_predictor(predictor)
    train_set.set_feature_name(feature_name)
    train_set.set_categorical_feature(categorical_feature)

    is_valid_contain_train = False
    train_data_name = "training"
    reduced_valid_sets = []
    name_valid_sets = []
    if valid_sets is not None:
        if isinstance(valid_sets, Dataset):
            valid_sets = [valid_sets]
        if isinstance(valid_names, str):
            valid_names = [valid_names]
        for i, valid_data in enumerate(valid_sets):
            """reduce cost for prediction training data"""
            if valid_data is train_set:
                is_valid_contain_train = True
                if valid_names is not None:
                    train_data_name = valid_names[i]
                continue
            if not isinstance(valid_data, Dataset):
                raise TypeError("Traninig only accepts Dataset object")
            valid_data.set_reference(train_set)
            reduced_valid_sets.append(valid_data)
            if valid_names is not None and len(valid_names) > i:
                name_valid_sets.append(valid_names[i])
            else:
                name_valid_sets.append('valid_'+str(i))

    """process callbacks"""
    if callbacks is None:
        callbacks = set()
    else:
        for i, cb in enumerate(callbacks):
            cb.__dict__.setdefault('order', i - len(callbacks))
        callbacks = set(callbacks)

    # Most of legacy advanced options becomes callbacks
    if verbose_eval is True:
        callbacks.add(callback.print_evaluation())
    elif isinstance(verbose_eval, int):
        callbacks.add(callback.print_evaluation(verbose_eval))

    if early_stopping_rounds is not None:
        callbacks.add(callback.early_stopping(early_stopping_rounds, verbose=bool(verbose_eval)))

    if learning_rates is not None:
        callbacks.add(callback.reset_parameter(learning_rate=learning_rates))

    if evals_result is not None:
        callbacks.add(callback.record_evaluation(evals_result))

    callbacks_before_iter = {cb for cb in callbacks if getattr(cb, 'before_iteration', False)}
    callbacks_after_iter = callbacks - callbacks_before_iter
    callbacks_before_iter = sorted(callbacks_before_iter, key=attrgetter('order'))
    callbacks_after_iter = sorted(callbacks_after_iter, key=attrgetter('order'))

    """construct booster"""
    booster = Booster(params=params, train_set=train_set)
    if is_valid_contain_train:
        booster.set_train_data_name(train_data_name)
    for valid_set, name_valid_set in zip(reduced_valid_sets, name_valid_sets):
        booster.add_valid(valid_set, name_valid_set)

    """start training"""
    for i in range(init_iteration, init_iteration + num_boost_round):
        for cb in callbacks_before_iter:
            cb(callback.CallbackEnv(model=booster,
                                    cvfolds=None,
                                    iteration=i,
                                    begin_iteration=init_iteration,
                                    end_iteration=init_iteration + num_boost_round,
                                    evaluation_result_list=None))

        booster.update(fobj=fobj)

        evaluation_result_list = []
        # check evaluation result.
        if valid_sets is not None:
            if is_valid_contain_train:
                evaluation_result_list.extend(booster.eval_train(feval))
            evaluation_result_list.extend(booster.eval_valid(feval))
        try:
            for cb in callbacks_after_iter:
                cb(callback.CallbackEnv(model=booster,
                                        cvfolds=None,
                                        iteration=i,
                                        begin_iteration=init_iteration,
                                        end_iteration=init_iteration + num_boost_round,
                                        evaluation_result_list=evaluation_result_list))
        except callback.EarlyStopException:
            break
    if booster.attr('best_iteration') is not None:
        booster.best_iteration = int(booster.attr('best_iteration')) + 1
    else:
        booster.best_iteration = num_boost_round
    return booster


class CVBooster(object):
    """"Auxiliary datastruct to hold one fold of CV."""
    def __init__(self, train_set, valid_test, params):
        """"Initialize the CVBooster"""
        self.train_set = train_set
        self.valid_test = valid_test
        self.booster = Booster(params=params, train_set=train_set)
        self.booster.add_valid(valid_test, 'valid')

    def update(self, fobj):
        """"Update the boosters for one iteration"""
        self.booster.update(fobj=fobj)

    def eval(self, feval):
        """"Evaluate the CVBooster for one iteration."""
        return self.booster.eval_valid(feval)

try:
    from sklearn.model_selection import StratifiedKFold
    SKLEARN_StratifiedKFold = True
except ImportError:
    try:
        from sklearn.cross_validation import StratifiedKFold
        SKLEARN_StratifiedKFold = True
    except ImportError:
        SKLEARN_StratifiedKFold = False

def _make_n_folds(full_data, nfold, params, seed, fpreproc=None, stratified=False, shuffle=True):
    """
    Make an n-fold list of CVBooster from random indices.
    """
    np.random.seed(seed)
    if stratified:
        if SKLEARN_StratifiedKFold:
            sfk = StratifiedKFold(n_splits=nfold, shuffle=shuffle, random_state=seed)
            idset = [x[1] for x in sfk.split(X=full_data.get_label(), y=full_data.get_label())]
        else:
            raise LightGBMError('Scikit-learn is required for stratified cv')
    else:
        full_data.construct()
        if shuffle:
            randidx = np.random.permutation(full_data.num_data())
        kstep = int(len(randidx) / nfold)
        idset = [randidx[(i * kstep): min(len(randidx), (i + 1) * kstep)] for i in range(nfold)]

    ret = []
    for k in range(nfold):
        train_set = full_data.subset(np.concatenate([idset[i] for i in range(nfold) if k != i]))
        valid_set = full_data.subset(idset[k])
        # run preprocessing on the data set if needed
        if fpreproc is not None:
            train_set, valid_set, tparam = fpreproc(train_set, valid_set, params.copy())
        else:
            tparam = params
        ret.append(CVBooster(train_set, valid_set, tparam))
    return ret

def _agg_cv_result(raw_results):
    """
    Aggregate cross-validation results.
    """
    cvmap = collections.defaultdict(list)
    metric_type = {}
    for one_result in raw_results:
        for one_line in one_result:
            metric_type[one_line[1]] = one_line[3]
            cvmap[one_line[1]].append(one_line[2])
    return [('cv_agg', k, np.mean(v), metric_type[k], np.std(v)) for k, v in cvmap.items()]

def cv(params, train_set, num_boost_round=10, nfold=5, stratified=False,
       shuffle=True, metrics=None, fobj=None, feval=None, init_model=None,
       feature_name=None, categorical_feature=None,
       early_stopping_rounds=None, fpreproc=None,
       verbose_eval=None, show_stdv=True, seed=0,
       callbacks=None):
    """
    Cross-validation with given paramaters.

    Parameters
    ----------
    params : dict
        Booster params.
    train_set : Dataset
        Data to be trained.
    num_boost_round : int
        Number of boosting iterations.
    nfold : int
        Number of folds in CV.
    stratified : bool
        Perform stratified sampling.
    shuffle: bool
        Whether shuffle before split data
    folds : a KFold or StratifiedKFold instance
        Sklearn KFolds or StratifiedKFolds.
    metrics : string or list of strings
        Evaluation metrics to be watched in CV.
    fobj : function
        Custom objective function.
    feval : function
        Custom evaluation function.
    init_model : file name of lightgbm model or 'Booster' instance
        model used for continued train
    feature_name : list of str
        Feature names
    categorical_feature : list of str or int
        Categorical features, type int represents index,
        type str represents feature names (need to specify feature_name as well)
    early_stopping_rounds: int
        Activates early stopping. CV error needs to decrease at least
        every <early_stopping_rounds> round(s) to continue.
        Last entry in evaluation history is the one from best iteration.
    fpreproc : function
        Preprocessing function that takes (dtrain, dtest, param)
        and returns transformed versions of those.
    verbose_eval : bool, int, or None, default None
        Whether to display the progress.
        If None, progress will be displayed when np.ndarray is returned.
        If True, progress will be displayed at boosting stage.
        If an integer is given,
            progress will be displayed at every given `verbose_eval` boosting stage.
    show_stdv : bool, default True
        Whether to display the standard deviation in progress.
        Results are not affected, and always contains std.
    seed : int
        Seed used to generate the folds (passed to numpy.random.seed).
    callbacks : list of callback functions
        List of callback functions that are applied at end of each iteration.

    Returns
    -------
    evaluation history : list(string)
    """
    if not isinstance(train_set, Dataset):
        raise TypeError("Traninig only accepts Dataset object")

    if is_str(init_model):
        predictor = _InnerPredictor(model_file=init_model)
    elif isinstance(init_model, Booster):
        predictor = init_model._to_predictor()
    else:
        predictor = None

    train_set._set_predictor(predictor)
    train_set.set_feature_name(feature_name)
    train_set.set_categorical_feature(categorical_feature)

    if metrics:
        params.setdefault('metric', [])
        if is_str(metrics):
            params['metric'].append(metrics)
        else:
            params['metric'].extend(metrics)

    results = collections.defaultdict(list)
    cvfolds = _make_n_folds(train_set, nfold, params, seed, fpreproc, stratified, shuffle)

    # setup callbacks
    if callbacks is None:
        callbacks = set()
    else:
        for i, cb in enumerate(callbacks):
            cb.__dict__.setdefault('order', i - len(callbacks))
        callbacks = set(callbacks)
    if early_stopping_rounds is not None:
        callbacks.add(callback.early_stopping(early_stopping_rounds, verbose=False))
    if verbose_eval is True:
        callbacks.add(callback.print_evaluation(show_stdv=show_stdv))
    elif isinstance(verbose_eval, int):
        callbacks.add(callback.print_evaluation(verbose_eval, show_stdv=show_stdv))

    callbacks_before_iter = {cb for cb in callbacks if getattr(cb, 'before_iteration', False)}
    callbacks_after_iter = callbacks - callbacks_before_iter
    callbacks_before_iter = sorted(callbacks_before_iter, key=attrgetter('order'))
    callbacks_after_iter = sorted(callbacks_after_iter, key=attrgetter('order'))

    for i in range(num_boost_round):
        for cb in callbacks_before_iter:
            cb(callback.CallbackEnv(model=None,
                                    cvfolds=cvfolds,
                                    iteration=i,
                                    begin_iteration=0,
                                    end_iteration=num_boost_round,
                                    evaluation_result_list=None))
        for fold in cvfolds:
            fold.update(fobj)
        res = _agg_cv_result([f.eval(feval) for f in cvfolds])
        for _, key, mean, _, std in res:
            results[key + '-mean'].append(mean)
            results[key + '-stdv'].append(std)
        try:
            for cb in callbacks_after_iter:
                cb(callback.CallbackEnv(model=None,
                                        cvfolds=cvfolds,
                                        iteration=i,
                                        begin_iteration=0,
                                        end_iteration=num_boost_round,
                                        evaluation_result_list=res))
        except callback.EarlyStopException as e:
            for k in results:
                results[k] = results[k][:e.best_iteration + 1]
            break
    return dict(results)
