# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------

import pytest

from sinopac_nt.config import SinopacDataClientConfig
from sinopac_nt.config import SinopacExecClientConfig
from sinopac_nt.constants import SINOPAC_VENUE
from nautilus_trader.model.identifiers import Venue


@pytest.fixture
def sinopac_data_config() -> SinopacDataClientConfig:
    return SinopacDataClientConfig()


@pytest.fixture
def sinopac_exec_config() -> SinopacExecClientConfig:
    return SinopacExecClientConfig()


@pytest.fixture
def venue() -> Venue:
    return SINOPAC_VENUE


@pytest.fixture
def instrument():
    return None


@pytest.fixture
def data_client():
    return None


@pytest.fixture
def exec_client():
    return None


@pytest.fixture
def instrument_provider():
    return None


@pytest.fixture
def account_state():
    return None
