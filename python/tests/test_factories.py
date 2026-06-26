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

from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from sinopac_nt.factories import _WsDispatcher
from sinopac_nt.factories import get_sinopac_http_client
from sinopac_nt.factories import get_sinopac_instrument_provider
from sinopac_nt.factories import get_sinopac_ws_client
from sinopac_nt.factories import get_sinopac_ws_dispatcher
from sinopac_nt.providers import SinopacInstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from sinopac_nt import _sinopac as pyo3_sinopac


def test_get_sinopac_http_client():
    # Clear the lru_cache to avoid cross-test pollution
    get_sinopac_http_client.cache_clear()

    client = get_sinopac_http_client()
    assert isinstance(client, pyo3_sinopac.SinopacHttpClient)

    # Second call should return the cached instance
    client2 = get_sinopac_http_client()
    assert client is client2

    get_sinopac_http_client.cache_clear()


def test_get_sinopac_ws_client():
    get_sinopac_ws_client.cache_clear()

    client = get_sinopac_ws_client()
    assert isinstance(client, pyo3_sinopac.SinopacWebSocketClient)

    # Second call should return the cached instance
    client2 = get_sinopac_ws_client()
    assert client is client2

    get_sinopac_ws_client.cache_clear()


def test_get_sinopac_instrument_provider():
    get_sinopac_http_client.cache_clear()
    get_sinopac_instrument_provider.cache_clear()

    http_client = get_sinopac_http_client()
    config = InstrumentProviderConfig()
    provider = get_sinopac_instrument_provider(http_client, config)
    assert isinstance(provider, SinopacInstrumentProvider)

    # Second call should return the cached instance
    provider2 = get_sinopac_instrument_provider(http_client, config)
    assert provider is provider2

    get_sinopac_instrument_provider.cache_clear()
    get_sinopac_http_client.cache_clear()


# -- Task 3.1: lifecycle-bound WsDispatcher with refcounted teardown -------------------------------


@pytest.fixture
def ws_client_stub():
    """
    Stub a pyo3 WS client whose connection state tracks connect/disconnect.
    """
    stub = MagicMock(spec=pyo3_sinopac.SinopacWebSocketClient)
    connected = {"value": False}

    async def _connect(*args, **kwargs):
        connected["value"] = True

    async def _disconnect(*args, **kwargs):
        connected["value"] = False

    stub.connect = AsyncMock(side_effect=_connect)
    stub.disconnect = AsyncMock(side_effect=_disconnect)
    stub.is_connected = MagicMock(side_effect=lambda: connected["value"])
    return stub


def test_dispatcher_fans_out_to_all_registered_handlers(ws_client_stub):
    """
    A dispatched message must reach every registered handler exactly once.
    """
    dispatcher = _WsDispatcher(ws_client_stub)
    calls_a: list[object] = []
    calls_b: list[object] = []
    dispatcher.register(calls_a.append)
    dispatcher.register(calls_b.append)

    msg = {"event": "reconnected"}
    dispatcher.dispatch(msg)

    assert calls_a == [msg]
    assert calls_b == [msg]


def test_dispatcher_unregister_removes_exact_handler(ws_client_stub):
    """
    Unregistering a handler removes only that handler from the fan-out.
    """
    dispatcher = _WsDispatcher(ws_client_stub)
    received_a: list[object] = []
    received_b: list[object] = []

    def handler_a(msg: object) -> None:
        received_a.append(msg)

    def handler_b(msg: object) -> None:
        received_b.append(msg)

    dispatcher.register(handler_a)
    dispatcher.register(handler_b)

    dispatcher.unregister(handler_a)
    dispatcher.dispatch("after-unregister")

    assert received_a == []
    assert received_b == ["after-unregister"]


def test_dispatcher_register_is_idempotent_handler_set(ws_client_stub):
    """
    Registering the same handler twice must not duplicate it in the fan-out.
    """
    dispatcher = _WsDispatcher(ws_client_stub)
    received: list[object] = []

    def handler(msg: object) -> None:
        received.append(msg)

    dispatcher.register(handler)
    dispatcher.register(handler)  # second registration -- must not duplicate

    dispatcher.dispatch("once")

    assert received == ["once"], "handler must fire exactly once despite double-register"


@pytest.mark.asyncio
async def test_dispatcher_releases_only_at_refcount_zero(ws_client_stub):
    """
    The shared WS must disconnect only when the last registered client releases.
    """
    dispatcher = _WsDispatcher(ws_client_stub)

    def exec_handler(msg: object) -> None:
        pass

    def data_handler(msg: object) -> None:
        pass

    dispatcher.register(exec_handler)
    dispatcher.register(data_handler)

    await dispatcher.ensure_connected(instruments=[])
    assert ws_client_stub.is_connected()

    # First client releases -- WS must stay up for the second client.
    dispatcher.unregister(data_handler)
    await dispatcher.release()
    assert ws_client_stub.is_connected(), "WS closed too early (other client still active)"
    ws_client_stub.disconnect.assert_not_called()

    # Last client releases -- now the WS is torn down.
    dispatcher.unregister(exec_handler)
    await dispatcher.release()
    assert not ws_client_stub.is_connected()
    ws_client_stub.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatcher_ensure_connected_is_idempotent(ws_client_stub):
    """
    ensure_connected must connect once and no-op while the WS is already active.
    """
    dispatcher = _WsDispatcher(ws_client_stub)

    await dispatcher.ensure_connected(instruments=[])
    await dispatcher.ensure_connected(instruments=[])

    ws_client_stub.connect.assert_awaited_once()


def test_dispatcher_handler_count_never_grows_across_dispose_cycles(ws_client_stub):
    """
    Repeated register/unregister cycles must not leak handlers (no unbounded growth).
    """
    dispatcher = _WsDispatcher(ws_client_stub)

    def handler(msg: object) -> None:
        pass

    for _ in range(3):
        dispatcher.register(handler)
        dispatcher.unregister(handler)

    received: list[object] = []
    dispatcher.register(received.append)
    dispatcher.dispatch("final")

    # Only the surviving handler fires; the cycled handler left no residue.
    assert received == ["final"]
    assert len(dispatcher._handlers) == 1


def test_get_sinopac_ws_dispatcher_is_cached(ws_client_stub):
    """
    The dispatcher factory must return one shared instance per process.
    """
    get_sinopac_ws_dispatcher.cache_clear()

    d1 = get_sinopac_ws_dispatcher(ws_client=ws_client_stub)
    d2 = get_sinopac_ws_dispatcher(ws_client=ws_client_stub)
    assert d1 is d2

    get_sinopac_ws_dispatcher.cache_clear()
