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
""" This folder contains single-objective optimization functions. """
from .active_learning import (
    ExpectedFeasibility,
    PredictiveVariance,
    bichon_ranjan_criterion,
    predictive_variance,
)
from .entropy import (
    GIBBON,
    MinValueEntropySearch,
    gibbon_quality_term,
    gibbon_repulsion_term,
    min_value_entropy_search,
)
from .function import (
    AugmentedExpectedImprovement,
    BatchMonteCarloExpectedImprovement,
    ExpectedConstrainedImprovement,
    ExpectedImprovement,
    NegativeLowerConfidenceBound,
    NegativePredictiveMean,
    ProbabilityOfFeasibility,
    augmented_expected_improvement,
    expected_improvement,
    lower_confidence_bound,
    probability_of_feasibility,
)
from .local_penalization import (
    LocalPenalizationAcquisitionFunction,
    hard_local_penalizer,
    soft_local_penalizer,
)
from .multi_objective import (
    BatchMonteCarloExpectedHypervolumeImprovement,
    ExpectedConstrainedHypervolumeImprovement,
    ExpectedHypervolumeImprovement,
    batch_ehvi,
    expected_hv_improvement,
)
