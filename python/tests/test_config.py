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

from sinopac_nt.config import SinopacDataClientConfig
from sinopac_nt.config import SinopacExecClientConfig


def test_data_client_config_defaults(sinopac_data_config: SinopacDataClientConfig):
    assert sinopac_data_config.venue == "SINOPAC"
    assert sinopac_data_config.gateway_host == "localhost"
    assert sinopac_data_config.gateway_port == 8123
    assert sinopac_data_config.gateway_ws_path == "/ws"


def test_exec_client_config_defaults(sinopac_exec_config: SinopacExecClientConfig):
    assert sinopac_exec_config.venue == "SINOPAC"
    assert sinopac_exec_config.account_id is None
    assert sinopac_exec_config.gateway_host == "localhost"
    assert sinopac_exec_config.gateway_port == 8123
    assert sinopac_exec_config.gateway_ws_path == "/ws"


def test_config_gateway_base_url(sinopac_data_config: SinopacDataClientConfig):
    assert sinopac_data_config.gateway_base_url == "http://localhost:8123"


def test_config_gateway_ws_url(sinopac_data_config: SinopacDataClientConfig):
    assert sinopac_data_config.gateway_ws_url == "ws://localhost:8123/ws"


def test_exec_config_gateway_base_url(sinopac_exec_config: SinopacExecClientConfig):
    assert sinopac_exec_config.gateway_base_url == "http://localhost:8123"


def test_exec_config_gateway_ws_url(sinopac_exec_config: SinopacExecClientConfig):
    assert sinopac_exec_config.gateway_ws_url == "ws://localhost:8123/ws"


def test_config_custom_host():
    config = SinopacDataClientConfig(
        gateway_host="192.168.1.100",
        gateway_port=9000,
    )
    assert config.gateway_base_url == "http://192.168.1.100:9000"
    assert config.gateway_ws_url == "ws://192.168.1.100:9000/ws"
