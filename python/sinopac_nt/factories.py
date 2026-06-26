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

import asyncio
from collections.abc import Callable
from functools import lru_cache

from sinopac_nt.config import SinopacDataClientConfig
from sinopac_nt.config import SinopacExecClientConfig
from sinopac_nt.data import SinopacDataClient
from sinopac_nt.execution import SinopacExecutionClient
from sinopac_nt.providers import SinopacInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.config import InstrumentProviderConfig
from sinopac_nt import _sinopac as pyo3_sinopac
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.factories import LiveExecClientFactory


@lru_cache(1)
def get_sinopac_http_client(
    gateway_host: str = "localhost",
    gateway_port: int = 8000,
) -> pyo3_sinopac.SinopacHttpClient:
    """
    Cache and return a Sinopac gateway HTTP client.

    Parameters
    ----------
    gateway_host : str, default "localhost"
        The Sinopac gateway host address.
    gateway_port : int, default 8000
        The Sinopac gateway HTTP/WS port.

    Returns
    -------
    pyo3_sinopac.SinopacHttpClient

    """
    base_url = f"http://{gateway_host}:{gateway_port}"
    return pyo3_sinopac.SinopacHttpClient(base_url=base_url)


@lru_cache(1)
def get_sinopac_ws_client(
    gateway_host: str = "localhost",
    gateway_port: int = 8000,
    gateway_ws_path: str = "/ws",
) -> pyo3_sinopac.SinopacWebSocketClient:
    """
    Cache and return a Sinopac gateway WebSocket client.

    Parameters
    ----------
    gateway_host : str, default "localhost"
        The Sinopac gateway host address.
    gateway_port : int, default 8000
        The Sinopac gateway HTTP/WS port.
    gateway_ws_path : str, default "/ws"
        The WebSocket endpoint path.

    Returns
    -------
    pyo3_sinopac.SinopacWebSocketClient

    """
    ws_url = f"ws://{gateway_host}:{gateway_port}{gateway_ws_path}"
    return pyo3_sinopac.SinopacWebSocketClient(url=ws_url)


@lru_cache(1)
def get_sinopac_instrument_provider(
    client: pyo3_sinopac.SinopacHttpClient,
    config: InstrumentProviderConfig,
) -> SinopacInstrumentProvider:
    """
    Cache and return a Sinopac instrument provider.

    Parameters
    ----------
    client : pyo3_sinopac.SinopacHttpClient
        The Sinopac gateway HTTP client.
    config : InstrumentProviderConfig
        The configuration for the instrument provider.

    Returns
    -------
    SinopacInstrumentProvider

    """
    return SinopacInstrumentProvider(
        client=client,
        config=config,
    )


# ---------------------------------------------------------------------------
# WS callback dispatch -- single WS connection shared by Data + Exec clients
# ---------------------------------------------------------------------------


class _WsDispatcher:
    """
    Fan out shared-WS messages to registered client handlers.

    Own the handler registry and a connection refcount so the singleton socket
    is closed only when the last registered client disconnects. The data and
    exec clients share one WS; either may establish it, and order/fill events
    are broadcast to every connection, so a single fan-out keeps both clients in
    sync without a second socket.

    Parameters
    ----------
    ws_client : pyo3_sinopac.SinopacWebSocketClient
        The shared Sinopac gateway WebSocket client.

    """

    def __init__(self, ws_client: pyo3_sinopac.SinopacWebSocketClient) -> None:
        self._ws_client = ws_client
        self._handlers: list[Callable[[object], None]] = []
        self._refcount = 0

    def dispatch(self, msg: object) -> None:
        # Iterate a copy so a handler can (un)register during dispatch
        for handler in list(self._handlers):
            handler(msg)

    def register(self, handler: Callable[[object], None]) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)
        self._refcount += 1

    def unregister(self, handler: Callable[[object], None]) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    async def ensure_connected(self, instruments: list) -> None:
        # Idempotent: SinopacWebSocketClient.connect() early-returns when active,
        # so the first client to connect wins and later callers are no-ops.
        if not self._ws_client.is_connected():
            await self._ws_client.connect(
                instruments=instruments,
                callback=self.dispatch,
            )

    async def release(self) -> None:
        # Decrement the refcount; disconnect the shared WS only at zero so the
        # data client disconnecting does not kill the exec client's event stream.
        if self._refcount > 0:
            self._refcount -= 1
        if self._refcount == 0 and self._ws_client.is_connected():
            await self._ws_client.disconnect()


@lru_cache(1)
def get_sinopac_ws_dispatcher(
    ws_client: pyo3_sinopac.SinopacWebSocketClient,
) -> _WsDispatcher:
    """
    Cache and return the shared Sinopac WS dispatcher.

    Parameters
    ----------
    ws_client : pyo3_sinopac.SinopacWebSocketClient
        The shared Sinopac gateway WebSocket client.

    Returns
    -------
    _WsDispatcher

    """
    return _WsDispatcher(ws_client)


# ---------------------------------------------------------------------------
# Factory classes
# ---------------------------------------------------------------------------


class SinopacLiveDataClientFactory(LiveDataClientFactory):
    """
    Provides a Sinopac live data client factory.
    """

    @staticmethod
    def create(  # type: ignore
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: SinopacDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> SinopacDataClient:
        """
        Create a new Sinopac data client.

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop
            The event loop for the client.
        name : str
            The custom client ID.
        config : SinopacDataClientConfig
            The client configuration.
        msgbus : MessageBus
            The message bus for the client.
        cache : Cache
            The cache for the client.
        clock : LiveClock
            The clock for the client.

        Returns
        -------
        SinopacDataClient

        """
        http_client = get_sinopac_http_client(
            gateway_host=config.gateway_host,
            gateway_port=config.gateway_port,
        )
        ws_client = get_sinopac_ws_client(
            gateway_host=config.gateway_host,
            gateway_port=config.gateway_port,
            gateway_ws_path=config.gateway_ws_path,
        )
        provider = get_sinopac_instrument_provider(
            client=http_client,
            config=config.instrument_provider,
        )
        ws_dispatcher = get_sinopac_ws_dispatcher(ws_client=ws_client)

        client = SinopacDataClient(
            loop=loop,
            client=http_client,
            ws_client=ws_client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            name=name,
            ws_dispatcher=ws_dispatcher,
        )

        return client


class SinopacLiveExecClientFactory(LiveExecClientFactory):
    """
    Provides a Sinopac live execution client factory.
    """

    @staticmethod
    def create(  # type: ignore
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: SinopacExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> SinopacExecutionClient:
        """
        Create a new Sinopac execution client.

        Parameters
        ----------
        loop : asyncio.AbstractEventLoop
            The event loop for the client.
        name : str
            The custom client ID.
        config : SinopacExecClientConfig
            The client configuration.
        msgbus : MessageBus
            The message bus for the client.
        cache : Cache
            The cache for the client.
        clock : LiveClock
            The clock for the client.

        Returns
        -------
        SinopacExecutionClient

        """
        http_client = get_sinopac_http_client(
            gateway_host=config.gateway_host,
            gateway_port=config.gateway_port,
        )
        ws_client = get_sinopac_ws_client(
            gateway_host=config.gateway_host,
            gateway_port=config.gateway_port,
            gateway_ws_path=config.gateway_ws_path,
        )
        provider = get_sinopac_instrument_provider(
            client=http_client,
            config=config.instrument_provider,
        )
        ws_dispatcher = get_sinopac_ws_dispatcher(ws_client=ws_client)

        client = SinopacExecutionClient(
            loop=loop,
            client=http_client,
            ws_client=ws_client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            name=name,
            ws_dispatcher=ws_dispatcher,
        )

        return client
