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

from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.config import LiveExecClientConfig


class SinopacDataClientConfig(LiveDataClientConfig, frozen=True):
    """
    Configuration for ``SinopacDataClient`` instances.

    Parameters
    ----------
    venue : str, default "SINOPAC"
        The venue for the client.
    gateway_host : str, default "localhost"
        The Sinopac gateway host address.
    gateway_port : int, default 8123
        The Sinopac gateway HTTP/WS port.
    gateway_ws_path : str, default "/ws"
        The WebSocket endpoint path on the gateway.

    """

    venue: str = "SINOPAC"
    gateway_host: str = "localhost"
    gateway_port: int = 8123
    gateway_ws_path: str = "/ws"

    @property
    def gateway_base_url(self) -> str:
        return f"http://{self.gateway_host}:{self.gateway_port}"

    @property
    def gateway_ws_url(self) -> str:
        return f"ws://{self.gateway_host}:{self.gateway_port}{self.gateway_ws_path}"


class SinopacExecClientConfig(LiveExecClientConfig, frozen=True):
    """
    Configuration for ``SinopacExecutionClient`` instances.

    Parameters
    ----------
    venue : str, default "SINOPAC"
        The venue for the client.
    account_id : str, optional
        The Sinopac account identifier. If None, sourced from SINOPAC_ACCOUNT_ID env var.
    gateway_host : str, default "localhost"
        The Sinopac gateway host address.
    gateway_port : int, default 8123
        The Sinopac gateway HTTP/WS port.
    gateway_ws_path : str, default "/ws"
        The WebSocket endpoint path on the gateway.

    """

    venue: str = "SINOPAC"
    account_id: str | None = None
    gateway_host: str = "localhost"
    gateway_port: int = 8123
    gateway_ws_path: str = "/ws"

    @property
    def gateway_base_url(self) -> str:
        return f"http://{self.gateway_host}:{self.gateway_port}"

    @property
    def gateway_ws_url(self) -> str:
        return f"ws://{self.gateway_host}:{self.gateway_port}{self.gateway_ws_path}"
