# Copyright 2020 The Trieste Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import copy
from abc import ABC, abstractmethod

from collections.abc import Callable
from typing import Any, TypeVar, Tuple

import gpflow
import tensorflow as tf
from gpflow.models import GPR, SGPR, SVGP, VGP, GPModel
from gpflux.models import DeepGP
from gpflow.conditionals.util import sample_mvn
from gpflux.models.deep_gp import sample_dgp
from gpflux.layers.basis_functions.random_fourier_features import RandomFourierFeatures
from trieste.utils.robustgp import ConditionalVariance


from ..data import Dataset
from ..type import TensorType
from ..utils import DEFAULTS
from .optimizer import Optimizer
from ..utils import InducingPointSelector, KMeans

class ProbabilisticModel(ABC):
    """ A probabilistic model. """

    @abstractmethod
    def predict(self, query_points: TensorType) -> tuple[TensorType, TensorType]:
        """
        Return the mean and variance of the independent marginal distributions at each point in
        ``query_points``.

        This is essentially a convenience method for :meth:`predict_joint`, where non-event
        dimensions of ``query_points`` are all interpreted as broadcasting dimensions instead of
        batch dimensions, and the covariance is squeezed to remove redundant nesting.

        :param query_points: The points at which to make predictions, of shape [..., D].
        :return: The mean and variance of the independent marginal distributions at each point in
            ``query_points``. For a predictive distribution with event shape E, the mean and
            variance will both have shape [...] + E.
        """
        raise NotImplementedError

    @abstractmethod
    def predict_joint(self, query_points: TensorType) -> tuple[TensorType, TensorType]:
        """
        :param query_points: The points at which to make predictions, of shape [..., B, D].
        :return: The mean and covariance of the joint marginal distribution at each batch of points
            in ``query_points``. For a predictive distribution with event shape E, the mean will
            have shape [..., B] + E, and the covariance shape [...] + E + [B, B].
        """
        raise NotImplementedError

    @abstractmethod
    def sample(self, query_points: TensorType, num_samples: int) -> TensorType:
        """
        Return ``num_samples`` samples from the independent marginal distributions at
        ``query_points``.

        :param query_points: The points at which to sample, with shape [..., D].
        :param num_samples: The number of samples at each point.
        :return: The samples. For a predictive distribution with event shape E, this has shape
            [..., S] + E, where S is the number of samples.
        """
        raise NotImplementedError


class TrainableProbabilisticModel(ProbabilisticModel):
    """ A trainable probabilistic model. """

    @abstractmethod
    def update(self, dataset: Dataset) -> None:
        """
        Update the model given the specified ``dataset``. Does not train the model.

        :param dataset: The data with which to update the model.
        """
        raise NotImplementedError

    @abstractmethod
    def optimize(self, dataset: Dataset) -> None:
        """
        Optimize the model objective with respect to (hyper)parameters given the specified
        ``dataset``.

        :param dataset: The data with which to train the model.
        """
        raise NotImplementedError


class ModelStack(TrainableProbabilisticModel):
    r"""
    A :class:`ModelStack` is a wrapper around a number of :class:`TrainableProbabilisticModel`\ s.
    It combines the outputs of each model for predictions and sampling, and delegates training data
    to each model for updates and optimization.

    **Note:** Only supports vector outputs (i.e. with event shape [E]). Outputs for any two models
    are assumed independent. Each model may itself be single- or multi-output, and any one
    multi-output model may have dependence between its outputs. When we speak of *event size* in
    this class, we mean the output dimension for a given :class:`TrainableProbabilisticModel`,
    whether that is the :class:`ModelStack` itself, or one of the subsidiary
    :class:`TrainableProbabilisticModel`\ s within the :class:`ModelStack`. Of course, the event
    size for a :class:`ModelStack` will be the sum of the event sizes of each subsidiary model.
    """

    def __init__(
        self,
        model_with_event_size: tuple[TrainableProbabilisticModel, int],
        *models_with_event_sizes: tuple[TrainableProbabilisticModel, int],
    ):
        r"""
        The order of individual models specified at :meth:`__init__` determines the order of the
        :class:`ModelStack` output dimensions.

        :param model_with_event_size: The first model, and the size of its output events.
            **Note:** This is a separate parameter to ``models_with_event_sizes`` simply so that the
            method signature requires at least one model. It is not treated specially.
        :param \*models_with_event_sizes: The other models, and sizes of their output events.
        """
        super().__init__()
        self._models, self._event_sizes = zip(*(model_with_event_size,) + models_with_event_sizes)

    def predict(self, query_points: TensorType) -> tuple[TensorType, TensorType]:
        r"""
        :param query_points: The points at which to make predictions, of shape [..., D].
        :return: The predictions from all the wrapped models, concatenated along the event axis in
            the same order as they appear in :meth:`__init__`. If the wrapped models have predictive
            distributions with event shapes [:math:`E_i`], the mean and variance will both have
            shape [..., :math:`\sum_i E_i`].
        """
        means, vars_ = zip(*[model.predict(query_points) for model in self._models])
        return tf.concat(means, axis=-1), tf.concat(vars_, axis=-1)

    def predict_joint(self, query_points: TensorType) -> tuple[TensorType, TensorType]:
        r"""
        :param query_points: The points at which to make predictions, of shape [..., B, D].
        :return: The predictions from all the wrapped models, concatenated along the event axis in
            the same order as they appear in :meth:`__init__`. If the wrapped models have predictive
            distributions with event shapes [:math:`E_i`], the mean will have shape
            [..., B, :math:`\sum_i E_i`], and the covariance shape
            [..., :math:`\sum_i E_i`, B, B].
        """
        means, covs = zip(*[model.predict_joint(query_points) for model in self._models])
        return tf.concat(means, axis=-1), tf.concat(covs, axis=-3)

    def sample(self, query_points: TensorType, num_samples: int) -> TensorType:
        r"""
        :param query_points: The points at which to sample, with shape [..., D].
        :param num_samples: The number of samples at each point.
        :return: The samples from all the wrapped models, concatenated along the event axis. For
            wrapped models with predictive distributions with event shapes [:math:`E_i`], this has
            shape [..., S, :math:`\sum_i E_i`], where S is the number of samples.
        """
        samples = [model.sample(query_points, num_samples) for model in self._models]
        return tf.concat(samples, axis=-1)

    def update(self, dataset: Dataset) -> None:
        """
        Update all the wrapped models on their corresponding data. The data for each model is
        extracted by splitting the observations in ``dataset`` along the event axis according to the
        event sizes specified at :meth:`__init__`.

        :param dataset: The query points and observations for *all* the wrapped models.
        """
        observations = tf.split(dataset.observations, self._event_sizes, axis=-1)

        for model, obs in zip(self._models, observations):
            model.update(Dataset(dataset.query_points, obs))

    def optimize(self, dataset: Dataset) -> None:
        """
        Optimize all the wrapped models on their corresponding data. The data for each model is
        extracted by splitting the observations in ``dataset`` along the event axis according to the
        event sizes specified at :meth:`__init__`.

        :param dataset: The query points and observations for *all* the wrapped models.
        """
        observations = tf.split(dataset.observations, self._event_sizes, axis=-1)

        for model, obs in zip(self._models, observations):
            model.optimize(Dataset(dataset.query_points, obs))


M = TypeVar("M", bound=tf.Module)
""" A type variable bound to :class:`tf.Module`. """


def module_deepcopy(self: M, memo: dict[int, object]) -> M:
    r"""
    This function provides a workaround for `a bug`_ in TensorFlow Probability (fixed in `version
    0.12`_) where a :class:`tf.Module` cannot be deep-copied if it has
    :class:`tfp.bijectors.Bijector` instances on it. The function can be used to directly copy an
    object ``self`` as e.g. ``module_deepcopy(self, {})``, but it is perhaps more useful as an
    implemention for :meth:`__deepcopy__` on classes, where it can be used as follows:

    .. _a bug: https://github.com/tensorflow/probability/issues/547
    .. _version 0.12: https://github.com/tensorflow/probability/releases/tag/v0.12.1

    .. testsetup:: *

        >>> import tensorflow_probability as tfp

    >>> class Foo(tf.Module):
    ...     example_bijector = tfp.bijectors.Exp()
    ...
    ...     __deepcopy__ = module_deepcopy

    Classes with this method can be deep-copied even if they contain
    :class:`tfp.bijectors.Bijector`\ s.

    :param self: The object to copy.
    :param memo: References to existing deep-copied objects (by object :func:`id`).
    :return: A deep-copy of ``self``.
    """
    gpflow.utilities.reset_cache_bijectors(self)

    new = self.__new__(type(self))
    memo[id(self)] = new

    for name, value in self.__dict__.items():
        setattr(new, name, copy.deepcopy(value, memo))

    return new


class GPflowPredictor(ProbabilisticModel, tf.Module, ABC):
    """ A trainable wrapper for a GPflow Gaussian process model. """

    def __init__(self, optimizer: Optimizer | None = None):
        """
        :param optimizer: The optimizer with which to train the model. Defaults to
            :class:`~trieste.models.optimizer.Optimizer` with :class:`~gpflow.optimizers.Scipy`.
        """
        super().__init__()

        if optimizer is None:
            optimizer = Optimizer(gpflow.optimizers.Scipy())

        self._optimizer = optimizer

    @property
    def optimizer(self) -> Optimizer:
        return self._optimizer

    @property
    @abstractmethod
    def model(self) -> GPModel:
        """ The underlying GPflow model. """

    def predict(self, query_points: TensorType) -> tuple[TensorType, TensorType]:
        return self.model.predict_f(query_points)

    def predict_joint(self, query_points: TensorType) -> tuple[TensorType, TensorType]:
        return self.model.predict_f(query_points, full_cov=True)

    def sample(self, query_points: TensorType, num_samples: int) -> TensorType:
        return self.model.predict_f_samples(query_points, num_samples)

    def optimize(self, dataset: Dataset) -> None:
        self.optimizer.optimize(self.model, dataset)

    __deepcopy__ = module_deepcopy


class GaussianProcessRegression(GPflowPredictor, TrainableProbabilisticModel):
    def __init__(self, model: GPR | SGPR, optimizer: Optimizer | None = None):
        """
        :param model: The GPflow model to wrap.
        :param optimizer: The optimizer with which to train the model. Defaults to
            :class:`~trieste.models.optimizer.Optimizer` with :class:`~gpflow.optimizers.Scipy`.
        """
        super().__init__(optimizer)
        self._model = model

    def __repr__(self) -> str:
        """"""
        return f"GaussianProcessRegression({self._model!r}, {self.optimizer!r})"

    @property
    def model(self) -> GPR | SGPR:
        return self._model

    def update(self, dataset: Dataset) -> None:
        x, y = self.model.data

        _assert_data_is_compatible(dataset, Dataset(x, y))

        if dataset.query_points.shape[-1] != x.shape[-1]:
            raise ValueError

        if dataset.observations.shape[-1] != y.shape[-1]:
            raise ValueError

        self.model.data = dataset.query_points, dataset.observations


class SparseVariational(GPflowPredictor, TrainableProbabilisticModel):
    def __init__(self, model: SVGP, data: Dataset, optimizer: Optimizer | None = None):
        """
        :param model: The underlying GPflow sparse variational model.
        :param data: The initial training data.
        :param optimizer: The optimizer with which to train the model. Defaults to
            :class:`~trieste.models.optimizer.Optimizer` with :class:`~gpflow.optimizers.Scipy`.
        """
        super().__init__(optimizer)
        self._model = model
        self._data = data

    def __repr__(self) -> str:
        """"""
        return f"SparseVariational({self._model!r}, {self._data!r}, {self.optimizer!r})"

    @property
    def model(self) -> SVGP:
        return self._model

    def update(self, dataset: Dataset) -> None:
        _assert_data_is_compatible(dataset, self._data)

        self._data = dataset

        num_data = dataset.query_points.shape[0]
        self.model.num_data = num_data


class VariationalGaussianProcess(GPflowPredictor, TrainableProbabilisticModel):
    """ A :class:`TrainableProbabilisticModel` wrapper for a GPflow :class:`~gpflow.models.VGP`. """

    def __init__(self, model: VGP, optimizer: Optimizer | None = None):
        """
        :param model: The GPflow :class:`~gpflow.models.VGP`.
        :param optimizer: The optimizer with which to train the model. Defaults to
            :class:`~trieste.models.optimizer.Optimizer` with :class:`~gpflow.optimizers.Scipy`.
        :raise ValueError (or InvalidArgumentError): If ``model``'s :attr:`q_sqrt` is not rank 3.
        """
        tf.debugging.assert_rank(model.q_sqrt, 3)
        super().__init__(optimizer)
        self._model = model

    def __repr__(self) -> str:
        """"""
        return f"VariationalGaussianProcess({self._model!r}, {self.optimizer!r})"

    @property
    def model(self) -> VGP:
        return self._model

    def update(self, dataset: Dataset, *, jitter: float = DEFAULTS.JITTER) -> None:
        """
        Update the model given the specified ``dataset``. Does not train the model.

        :param dataset: The data with which to update the model.
        :param jitter: The size of the jitter to use when stabilising the Cholesky decomposition of
            the covariance matrix.
        """
        model = self.model

        _assert_data_is_compatible(dataset, Dataset(*model.data))

        f_mu, f_cov = self.model.predict_f(dataset.query_points, full_cov=True)  # [N, L], [L, N, N]

        # GPflow's VGP model is hard-coded to use the whitened representation, i.e.
        # q_mu and q_sqrt parametrise q(v), and u = f(X) = L v, where L = cholesky(K(X, X))
        # Hence we need to back-transform from f_mu and f_cov to obtain the updated
        # new_q_mu and new_q_sqrt:
        Knn = model.kernel(dataset.query_points, full_cov=True)  # [N, N]
        jitter_mat = jitter * tf.eye(len(dataset), dtype=Knn.dtype)
        Lnn = tf.linalg.cholesky(Knn + jitter_mat)  # [N, N]
        new_q_mu = tf.linalg.triangular_solve(Lnn, f_mu)  # [N, L]
        tmp = tf.linalg.triangular_solve(Lnn[None], f_cov)  # [L, N, N], L⁻¹ f_cov
        S_v = tf.linalg.triangular_solve(Lnn[None], tf.linalg.matrix_transpose(tmp))  # [L, N, N]
        new_q_sqrt = tf.linalg.cholesky(S_v + jitter_mat)  # [L, N, N]

        model.data = dataset.astuple()
        model.num_data = len(dataset)
        model.q_mu = gpflow.Parameter(new_q_mu)
        model.q_sqrt = gpflow.Parameter(new_q_sqrt, transform=gpflow.utilities.triangular())

    def predict(self, query_points: TensorType) -> tuple[TensorType, TensorType]:
        """
        :param query_points: The points at which to make predictions.
        :return: The predicted mean and variance of the observations at the specified
            ``query_points``.
        """
        return self.model.predict_y(query_points)


class GPFluxModel(TrainableProbabilisticModel):

    def __init__(self,
                 model: DeepGP,
                 data: Dataset,
                 num_epochs: int = 100,
                 batch_size: int = 128,
                 inducing_point_selector: InducingPointSelector = None,
                 max_num_inducing_points: int =None,
                 verbose:bool = True ):
        super().__init__()
        self._model = model
        print(self.model)
        if len(self.model.f_layers) > 1:
            raise NotImplementedError(
                "GPFluxModels are restricted to single-layer models."
            )

        self._data = data
        self.num_epochs = num_epochs
        self._batch_size = batch_size

        if inducing_point_selector is None:
            inducing_point_selector = KMeans
        self._inducing_point_selector = inducing_point_selector

        if max_num_inducing_points is None:
            max_num_inducing_points = 100
        self._max_num_inducing_points = max_num_inducing_points
        self._verbose=verbose



    @property
    def model(self) -> DeepGP:
        return self._model

    def predict(self, query_points: TensorType) -> Tuple[TensorType, TensorType]:
        f_mean, f_var = self.model.predict_f(query_points)
        return f_mean, f_var

    def predict_joint(self, query_points: TensorType) -> Tuple[TensorType, TensorType]:
        layer = self.model.f_layers[0]
        return layer.predict(query_points, full_cov=True)

    def predict_f(self, query_points: TensorType) -> Tuple[TensorType, TensorType]:
        return self.predict(query_points)

    def sample(self, query_points: TensorType, num_samples: int) -> TensorType:
        """ Latent function samples """
        fs_mean, fs_var = self.predict(query_points)
        samples = sample_mvn(fs_mean, fs_var, False, num_samples=num_samples)  # [..., (S), N, P]
        return samples  # [..., (S), N, P]

    def sample_trajectory(self) -> Callable:
        return sample_dgp(self.model)

    def update(self, dataset: Dataset, jitter: float = DEFAULTS.JITTER) -> None:
        _assert_data_is_compatible(dataset, self._data)

        self._data = dataset
        num_data = dataset.query_points.shape[0]
        layer = self.model.f_layers[0]
        input_shape = dataset.query_points.shape

        if hasattr(layer.kernel, 'feature_functions'): # If using RFF kernel decomp then need to resample for new kernel params
            feature_function = layer.kernel.feature_functions
            def renew_rff(feature_f, input_dim): 
                shape_bias = [1, feature_f.output_dim]
                new_b = feature_f._sample_bias(shape_bias, dtype=feature_f.dtype)
                feature_f.b = new_b
                shape_weights = [feature_f.output_dim, input_dim]
                new_W = feature_f._sample_weights(shape_weights, dtype=feature_f.dtype)
                feature_f.W = new_W
            renew_rff(feature_function,  input_shape[-1])


        num_inducing = tf.reduce_min([num_data,self._max_num_inducing_points])
        init_method = self._inducing_point_selector(dataset.query_points, dataset.observations, num_inducing, layer.kernel)
        Z = init_method.get_points()

        if layer.whiten:
            f_mu, f_cov = self.predict_joint(Z)  # [N, L], [L, N, N]
            Knn = layer.kernel(Z, full_cov=True)  # [N, N]
            jitter_mat = jitter * tf.eye(num_inducing, dtype=Knn.dtype)
            Lnn = tf.linalg.cholesky(Knn + jitter_mat)  # [N, N]
            new_q_mu = tf.linalg.triangular_solve(Lnn, f_mu)  # [N, L]
            tmp = tf.linalg.triangular_solve(Lnn[None], f_cov)  # [L, N, N], L⁻¹ f_cov
            S_v = tf.linalg.triangular_solve(Lnn[None], tf.linalg.matrix_transpose(tmp))  # [L, N, N]
            new_q_sqrt = tf.linalg.cholesky(S_v + jitter_mat)  # [L, N, N]
        else:
            new_q_mu, new_f_cov = self.predict_joint(Z)  # [N, L], [L, N, N]
            jitter_mat = jitter * tf.eye(num_inducing, dtype=new_f_cov.dtype)
            new_q_sqrt = tf.linalg.cholesky(new_f_cov + jitter_mat)

        layer.q_mu = gpflow.Parameter(new_q_mu)
        layer.q_sqrt = gpflow.Parameter(new_q_sqrt, transform=gpflow.utilities.triangular())
        # TODO: keep trainable property consistent over updates
        layer.inducing_variable.Z = gpflow.Parameter(Z, trainable=layer.inducing_variable.Z.trainable)

        self.model.num_data = num_data

    def optimize(self, dataset: Dataset) -> None:
        model = self.model.as_training_model()
        model.compile(tf.optimizers.Adam(learning_rate=0.1))
        callbacks = [
            tf.keras.callbacks.ReduceLROnPlateau(
                monitor="loss", patience=5, factor=0.95, verbose=self._verbose, min_lr=1e-6,
            ),
            tf.keras.callbacks.EarlyStopping(monitor="loss",patience=20, min_delta=0.01, verbose=self._verbose,mode="min"),

        ]
        model.fit(
            {"inputs": dataset.query_points, "targets": dataset.observations},
            batch_size=self._batch_size,
            epochs=self.num_epochs,
            verbose=self._verbose,
            callbacks=callbacks
        )


supported_models: dict[Any, Callable[[Any, Optimizer], TrainableProbabilisticModel]] = {
    GPR: GaussianProcessRegression,
    SGPR: GaussianProcessRegression,
    VGP: VariationalGaussianProcess,
    DeepGP: GPFluxModel,
}
"""
A mapping of third-party model types to :class:`CustomTrainable` classes that wrap models of those
types.
"""


def _assert_data_is_compatible(new_data: Dataset, existing_data: Dataset) -> None:
    if new_data.query_points.shape[-1] != existing_data.query_points.shape[-1]:
        raise ValueError(
            f"Shape {new_data.query_points.shape} of new query points is incompatible with"
            f" shape {existing_data.query_points.shape} of existing query points. Trailing"
            f" dimensions must match."
        )

    if new_data.observations.shape[-1] != existing_data.observations.shape[-1]:
        raise ValueError(
            f"Shape {new_data.observations.shape} of new observations is incompatible with"
            f" shape {existing_data.observations.shape} of existing observations. Trailing"
            f" dimensions must match."
        )
