# Copyright 2021 The Trieste Contributors
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

from collections.abc import Iterable
from typing import Any, Callable

import pytest
import tensorflow as tf
from gpflux.models import DeepGP

from tests.util.models.gpflux.models import simple_two_layer_dgp_model, two_layer_dgp_model
from trieste.data import Dataset
from trieste.models.optimizer import DatasetTransformer
from trieste.types import TensorType


def _batcher_bs_100(dataset: Dataset, batch_size: int) -> Iterable[tuple[TensorType, TensorType]]:
    ds = tf.data.Dataset.from_tensor_slices(dataset.astuple())
    ds = ds.shuffle(100)
    ds = ds.batch(batch_size)
    ds = ds.repeat()
    return iter(ds)


def _batcher_full_batch(dataset: Dataset, batch_size: int) -> tuple[TensorType, TensorType]:
    return dataset.astuple()


@pytest.fixture(name="batcher", params=[_batcher_bs_100, _batcher_full_batch])
def _batcher_fixture(request: Any) -> DatasetTransformer:
    return request.param


@pytest.fixture(name="compile", params=[True, False])
def _compile_fixture(request: Any) -> bool:
    return request.param


@pytest.fixture(name="two_layer_model", params=[two_layer_dgp_model, simple_two_layer_dgp_model])
def _two_layer_model_fixture(request: Any) -> Callable[[TensorType], DeepGP]:
    return request.param


# Teardown fixture to set keras floatx to float64 then return it to previous value at test finish
# pytest uses yield in a funny way, so we use type ignore
@pytest.fixture(name="keras_float")  # type: ignore
def _keras_float() -> None:
    current_float = tf.keras.backend.floatx()
    tf.keras.backend.set_floatx("float64")
    yield
    tf.keras.backend.set_floatx(current_float)
