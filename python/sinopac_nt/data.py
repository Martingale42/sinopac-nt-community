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
from typing import Protocol

from nautilus_trader.adapters.sinopac.config import SinopacDataClientConfig
from nautilus_trader.adapters.sinopac.constants import SINOPAC
from nautilus_trader.adapters.sinopac.constants import SINOPAC_VENUE
from nautilus_trader.adapters.sinopac.providers import SinopacInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.core import nautilus_pyo3
from nautilus_trader.core.nautilus_pyo3 import sinopac as pyo3_sinopac
from nautilus_trader.core.nautilus_pyo3.sinopac import SinopacQuoteType
from nautilus_trader.data.messages import RequestBars
from nautilus_trader.data.messages import RequestQuoteTicks
from nautilus_trader.data.messages import RequestTradeTicks
from nautilus_trader.data.messages import SubscribeBars
from nautilus_trader.data.messages import SubscribeInstrumentClose
from nautilus_trader.data.messages import SubscribeInstrumentStatus
from nautilus_trader.data.messages import SubscribeOrderBook
from nautilus_trader.data.messages import SubscribeQuoteTicks
from nautilus_trader.data.messages import SubscribeTradeTicks
from nautilus_trader.data.messages import UnsubscribeBars
from nautilus_trader.data.messages import UnsubscribeInstrumentClose
from nautilus_trader.data.messages import UnsubscribeInstrumentStatus
from nautilus_trader.data.messages import UnsubscribeOrderBook
from nautilus_trader.data.messages import UnsubscribeQuoteTicks
from nautilus_trader.data.messages import UnsubscribeTradeTicks
from nautilus_trader.live.cancellation import DEFAULT_FUTURE_CANCELLATION_TIMEOUT
from nautilus_trader.live.cancellation import cancel_tasks_with_timeout
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.data import capsule_to_data
from nautilus_trader.model.enums import PriceType
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId


class WsDispatcherProtocol(Protocol):
    """
    Structural type for the shared-WS dispatcher.

    Matches the public API of ``factories._WsDispatcher``, which fans out shared
    WebSocket messages to the data and exec clients and refcounts the singleton
    socket. Declared as a Protocol so this module avoids importing the private
    ``_WsDispatcher`` from ``factories`` (which imports this module, a cycle).

    """

    def register(self, handler: Callable[[object], None]) -> None: ...
    def unregister(self, handler: Callable[[object], None]) -> None: ...
    def dispatch(self, msg: object) -> None: ...
    async def ensure_connected(self, instruments: list) -> None: ...
    async def release(self) -> None: ...


class SinopacDataClient(LiveMarketDataClient):
    """
    Provides a data client for the Sinopac (SinoPac) adapter.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    client : pyo3_sinopac.SinopacHttpClient
        The Sinopac gateway HTTP client.
    ws_client : pyo3_sinopac.SinopacWebSocketClient
        The Sinopac gateway WebSocket client.
    msgbus : MessageBus
        The message bus for the client.
    cache : Cache
        The cache for the client.
    clock : LiveClock
        The clock for the client.
    instrument_provider : SinopacInstrumentProvider
        The instrument provider.
    config : SinopacDataClientConfig
        The configuration for the client.
    name : str, optional
        The custom client ID.
    ws_dispatcher : WsDispatcherProtocol, optional
        The shared-WS dispatcher that fans out messages to the data and exec
        clients and refcounts the singleton socket. Always supplied by
        ``SinopacLiveDataClientFactory`` in production; the client registers its
        handler on connect and releases its refcount on disconnect. The optional
        default exists only for direct construction in tests that never connect;
        ``_connect`` raises if it was not supplied.

    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client: pyo3_sinopac.SinopacHttpClient,
        ws_client: pyo3_sinopac.SinopacWebSocketClient,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: SinopacInstrumentProvider,
        config: SinopacDataClientConfig,
        name: str | None = None,
        ws_dispatcher: WsDispatcherProtocol | None = None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(name or SINOPAC),
            venue=SINOPAC_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        self._http_client = client
        self._ws_client = ws_client
        self._config = config
        self._ws_dispatcher = ws_dispatcher

        # Subscription tracking
        self._subscribed_trades: set[InstrumentId] = set()
        self._subscribed_quotes: set[InstrumentId] = set()
        self._subscribed_book_depth: set[InstrumentId] = set()
        self._subscribed_book_deltas: set[InstrumentId] = set()

        # Background task tracking
        self._client_futures: set[asyncio.Future] = set()

    @property
    def sinopac_instrument_provider(self) -> SinopacInstrumentProvider:
        return self._instrument_provider  # type: ignore

    def _require_dispatcher(self) -> WsDispatcherProtocol:
        if self._ws_dispatcher is None:
            raise RuntimeError(
                "ws_dispatcher was not supplied; SinopacLiveDataClientFactory "
                "always provides one for live use",
            )
        return self._ws_dispatcher

    # -- Connection lifecycle -------------------------------------------------

    async def _connect(self) -> None:
        # 1. Load instruments
        await self._instrument_provider.initialize()
        self._send_all_instruments_to_data_engine()

        # 2. Register this client's handler, then ensure the shared WS is up.
        # The dispatcher owns the singleton socket; connect() is idempotent so
        # either the data or exec client may establish it (events broadcast to
        # all connections).
        dispatcher = self._require_dispatcher()
        instruments_pyo3 = self.sinopac_instrument_provider.instruments_pyo3()
        dispatcher.register(self._handle_msg)
        await dispatcher.ensure_connected(instruments_pyo3)
        await self._ws_client.wait_until_active(timeout_secs=10.0)

        self._log.info(
            f"Connected to Sinopac gateway at {self._config.gateway_base_url}",
            LogColor.GREEN,
        )

    async def _disconnect(self) -> None:
        await asyncio.sleep(1.0)  # Grace period for pending WS messages

        # Unregister our handler and release our WS refcount. The shared socket
        # is torn down only when the last client (data or exec) releases, so the
        # data client disconnecting never severs the exec client's event stream.
        dispatcher = self._require_dispatcher()
        dispatcher.unregister(self._handle_msg)
        await dispatcher.release()

        await cancel_tasks_with_timeout(
            self._client_futures,
            self._log,
            timeout_secs=DEFAULT_FUTURE_CANCELLATION_TIMEOUT,
        )
        self._client_futures.clear()

    def _send_all_instruments_to_data_engine(self) -> None:
        for currency in self._instrument_provider.currencies().values():
            self._cache.add_currency(currency)
        for instrument in self._instrument_provider.get_all().values():
            self._handle_data(instrument)

    def _has_bidask_subscription(self, instrument_id: InstrumentId) -> bool:
        return (
            instrument_id in self._subscribed_quotes
            or instrument_id in self._subscribed_book_depth
            or instrument_id in self._subscribed_book_deltas
        )

    def _update_bidask_outputs_for(self, instrument_id: InstrumentId) -> None:
        code = instrument_id.symbol.value
        self._ws_client.set_bidask_outputs(
            code,
            quote=instrument_id in self._subscribed_quotes,
            depth=instrument_id in self._subscribed_book_depth,
            deltas=instrument_id in self._subscribed_book_deltas,
        )

    def _handle_msg(self, msg: object) -> None:
        try:
            if nautilus_pyo3.is_pycapsule(msg):
                data = capsule_to_data(msg)
                self._handle_data(data)
                return
            if isinstance(msg, dict):
                # The Rust WS layer already resubscribed market data on reconnect;
                # log it and move on. All other dicts (order/fill events) are meant
                # for the exec client on the shared WS -- drop them silently at DEBUG
                # so the data client does not spam a WARNING per order event (A7).
                if msg.get("event") == "reconnected":
                    self._log.info("Sinopac WS reconnected; market data resubscribed")
                else:
                    self._log.debug(f"Ignoring non-data WS dict: {msg.get('event_type')}")
                return
            self._log.warning(f"Unhandled WS message type: {type(msg)}")
        except Exception as e:
            self._log.exception("Error handling Sinopac WS message", e)

    # -- Subscriptions --------------------------------------------------------

    async def _subscribe_trade_ticks(self, command: SubscribeTradeTicks) -> None:
        instrument_id = command.instrument_id
        if instrument_id in self._subscribed_trades:
            self._log.warning(f"Already subscribed to {instrument_id} trades")
            return

        self._subscribed_trades.add(instrument_id)
        code = instrument_id.symbol.value
        await self._ws_client.subscribe(code, SinopacQuoteType.TICK)
        self._log.info(f"Subscribed to trade ticks: {instrument_id}", LogColor.BLUE)

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        instrument_id = command.instrument_id
        if instrument_id in self._subscribed_quotes:
            self._log.warning(f"Already subscribed to {instrument_id} quotes")
            return

        needs_ws = not self._has_bidask_subscription(instrument_id)
        self._subscribed_quotes.add(instrument_id)
        if needs_ws:
            code = instrument_id.symbol.value
            await self._ws_client.subscribe(code, SinopacQuoteType.BID_ASK)
        self._update_bidask_outputs_for(instrument_id)
        self._log.info(f"Subscribed to quote ticks: {instrument_id}", LogColor.BLUE)

    async def _subscribe_order_book_deltas(self, command: SubscribeOrderBook) -> None:
        instrument_id = command.instrument_id
        if instrument_id in self._subscribed_book_deltas:
            self._log.warning(f"Already subscribed to {instrument_id} book deltas")
            return

        needs_ws = not self._has_bidask_subscription(instrument_id)
        self._subscribed_book_deltas.add(instrument_id)
        if needs_ws:
            code = instrument_id.symbol.value
            await self._ws_client.subscribe(code, SinopacQuoteType.BID_ASK)
        self._update_bidask_outputs_for(instrument_id)
        self._log.info(f"Subscribed to order book deltas: {instrument_id}", LogColor.BLUE)

    async def _subscribe_order_book_depth(self, command: SubscribeOrderBook) -> None:
        instrument_id = command.instrument_id
        if instrument_id in self._subscribed_book_depth:
            self._log.warning(f"Already subscribed to {instrument_id} book depth")
            return

        needs_ws = not self._has_bidask_subscription(instrument_id)
        self._subscribed_book_depth.add(instrument_id)
        if needs_ws:
            code = instrument_id.symbol.value
            await self._ws_client.subscribe(code, SinopacQuoteType.BID_ASK)
        self._update_bidask_outputs_for(instrument_id)
        self._log.info(f"Subscribed to order book depth: {instrument_id}", LogColor.BLUE)

    async def _subscribe_bars(self, command: SubscribeBars) -> None:
        self._log.error(
            f"Cannot subscribe to {command.bar_type} bars: "
            "Sinopac does not support streaming bars (use request_bars for historical)",
        )

    async def _subscribe_instrument_status(self, command: SubscribeInstrumentStatus) -> None:
        pass  # Not supported by Sinopac

    async def _subscribe_instrument_close(self, command: SubscribeInstrumentClose) -> None:
        pass  # Not supported by Sinopac

    async def _unsubscribe_trade_ticks(self, command: UnsubscribeTradeTicks) -> None:
        instrument_id = command.instrument_id
        if instrument_id not in self._subscribed_trades:
            self._log.warning(f"Not subscribed to {instrument_id} trades")
            return

        self._subscribed_trades.discard(instrument_id)
        code = instrument_id.symbol.value
        await self._ws_client.unsubscribe(code, SinopacQuoteType.TICK)
        self._log.info(f"Unsubscribed from trade ticks: {instrument_id}", LogColor.BLUE)

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        instrument_id = command.instrument_id
        if instrument_id not in self._subscribed_quotes:
            self._log.warning(f"Not subscribed to {instrument_id} quotes")
            return

        self._subscribed_quotes.discard(instrument_id)
        self._update_bidask_outputs_for(instrument_id)
        if not self._has_bidask_subscription(instrument_id):
            code = instrument_id.symbol.value
            await self._ws_client.unsubscribe(code, SinopacQuoteType.BID_ASK)
        self._log.info(f"Unsubscribed from quote ticks: {instrument_id}", LogColor.BLUE)

    async def _unsubscribe_order_book_deltas(self, command: UnsubscribeOrderBook) -> None:
        instrument_id = command.instrument_id
        if instrument_id not in self._subscribed_book_deltas:
            self._log.warning(f"Not subscribed to {instrument_id} book deltas")
            return

        self._subscribed_book_deltas.discard(instrument_id)
        self._update_bidask_outputs_for(instrument_id)
        if not self._has_bidask_subscription(instrument_id):
            code = instrument_id.symbol.value
            await self._ws_client.unsubscribe(code, SinopacQuoteType.BID_ASK)
        self._log.info(f"Unsubscribed from order book deltas: {instrument_id}", LogColor.BLUE)

    async def _unsubscribe_order_book_depth(self, command: UnsubscribeOrderBook) -> None:
        instrument_id = command.instrument_id
        if instrument_id not in self._subscribed_book_depth:
            self._log.warning(f"Not subscribed to {instrument_id} book depth")
            return

        self._subscribed_book_depth.discard(instrument_id)
        self._update_bidask_outputs_for(instrument_id)
        if not self._has_bidask_subscription(instrument_id):
            code = instrument_id.symbol.value
            await self._ws_client.unsubscribe(code, SinopacQuoteType.BID_ASK)
        self._log.info(f"Unsubscribed from order book depth: {instrument_id}", LogColor.BLUE)

    async def _unsubscribe_bars(self, command: UnsubscribeBars) -> None:
        pass  # No-op

    async def _unsubscribe_instrument_status(self, command: UnsubscribeInstrumentStatus) -> None:
        pass  # No-op

    async def _unsubscribe_instrument_close(self, command: UnsubscribeInstrumentClose) -> None:
        pass  # No-op

    # -- Historical data requests ---------------------------------------------

    async def _request_trade_ticks(self, request: RequestTradeTicks) -> None:
        instrument_id = request.instrument_id
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            self._log.error(f"Cannot find instrument for {instrument_id}")
            return

        code = instrument_id.symbol.value
        date_str = request.start.strftime("%Y-%m-%d") if request.start else None
        if date_str is None:
            self._log.error("request_trade_ticks requires a start date")
            return

        try:
            pyo3_trades = await self._http_client.request_trade_ticks(
                code=code,
                date=date_str,
                price_precision=instrument.price_precision,
                size_precision=instrument.size_precision,
            )
            trades = TradeTick.from_pyo3_list(pyo3_trades)

            self._handle_trade_ticks(
                instrument_id,
                trades,
                request.id,
                request.start,
                request.end,
                request.params,
            )
        except Exception as e:
            self._log.exception("Failed to request trade ticks from Sinopac", e)

    async def _request_bars(self, request: RequestBars) -> None:
        if request.bar_type.is_internally_aggregated():
            self._log.error(
                f"Cannot request {request.bar_type} bars: only EXTERNAL aggregation supported",
            )
            return

        if not request.bar_type.spec.is_time_aggregated():
            self._log.error(
                f"Cannot request {request.bar_type} bars: only time bars supported",
            )
            return

        if request.bar_type.spec.price_type != PriceType.LAST:
            self._log.error(
                f"Cannot request {request.bar_type} bars: only LAST price type supported",
            )
            return

        instrument_id = request.bar_type.instrument_id
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            self._log.error(f"Cannot find instrument for {instrument_id}")
            return

        code = instrument_id.symbol.value
        start_str = request.start.strftime("%Y-%m-%d") if request.start else None
        end_str = request.end.strftime("%Y-%m-%d") if request.end else None

        if not start_str or not end_str:
            self._log.error("request_bars requires start and end dates")
            return

        try:
            pyo3_bars = await self._http_client.request_bars(
                code=code,
                start=start_str,
                end=end_str,
                bar_type=str(request.bar_type),
                price_precision=instrument.price_precision,
                size_precision=instrument.size_precision,
            )
            bars = Bar.from_pyo3_list(pyo3_bars)

            self._handle_bars(
                request.bar_type,
                bars,
                request.id,
                request.start,
                request.end,
                request.params,
            )
        except Exception as e:
            self._log.exception("Failed to request bars from Sinopac", e)

    async def _request_quote_ticks(self, request: RequestQuoteTicks) -> None:
        self._log.error("Sinopac does not support historical quote tick requests")
