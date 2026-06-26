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

from __future__ import annotations

import asyncio
import hashlib
import os
import string
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN
from decimal import Decimal
from typing import Any
from typing import Protocol

from sinopac_nt.config import SinopacExecClientConfig
from sinopac_nt.constants import SINOPAC
from sinopac_nt.constants import SINOPAC_VENUE
from sinopac_nt.providers import SinopacInstrumentProvider
from sinopac_nt.tags import SinopacOrderTags
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.core import nautilus_pyo3
from sinopac_nt import _sinopac as pyo3_sinopac
from sinopac_nt._sinopac import SinopacAction
from sinopac_nt._sinopac import SinopacMarket
from sinopac_nt._sinopac import SinopacOCType
from sinopac_nt._sinopac import SinopacOrderCond
from sinopac_nt._sinopac import SinopacOrderLot
from sinopac_nt._sinopac import SinopacOrderType
from sinopac_nt._sinopac import SinopacPriceType
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import BatchCancelOrders
from nautilus_trader.execution.messages import CancelAllOrders
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import GenerateFillReports
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import GenerateOrderStatusReports
from nautilus_trader.execution.messages import GeneratePositionStatusReports
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.messages import SubmitOrderList
from nautilus_trader.execution.reports import FillReport
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.live.cancellation import DEFAULT_FUTURE_CANCELLATION_TIMEOUT
from nautilus_trader.live.cancellation import cancel_tasks_with_timeout
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.currencies import Currency
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.enums import order_type_to_str
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.instruments import OptionContract
from nautilus_trader.model.objects import AccountBalance
from nautilus_trader.model.objects import Money
from nautilus_trader.model.orders import Order


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


_B62 = string.digits + string.ascii_letters


def _coid_token(client_order_id: str) -> str:
    """
    Deterministic 6-char base62 hash of a client_order_id.

    Definition: Computes a short, restart-safe token that fits within
        Shioaji's ``custom_field`` constraint (``ConStrAsciiMax6``, max 6 ASCII).
    Formula:    token = base62_encode(blake2s(coid, digest_size=8))[:6]
        Each character indexes ``_B62`` (62 symbols = [0-9a-zA-Z]) via
        ``(h >> (6*i)) % 62`` for i in 0..5.
    Domain:     Input must be a non-empty UTF-8 string. The 62^6 ~ 5.7e10
        address space makes collisions negligible among the O(10) active
        orders in a typical session. Deterministic: same input always yields
        the same token, so restart reconciliation can recompute the hash
        from cached orders without a persisted map.
    Returns:    A 6-character ASCII string suitable for ``custom_field``.

    """
    h = int.from_bytes(
        hashlib.blake2s(client_order_id.encode(), digest_size=8).digest(),
        "big",
    )
    return "".join(_B62[(h >> (6 * i)) % 62] for i in range(6))


def _snap_price_to_grid(price: Decimal, increment: Decimal) -> Decimal:
    """
    Snap a price onto the venue tick grid.

    Definition: Round a raw price to the nearest multiple of the instrument's
        tick increment, breaking ties to even (banker's rounding).
    Formula:    p_snapped = round_half_even(price / increment) * increment
        where ``increment`` is ``instrument.price_increment`` (e.g. 0.05).
    Domain:     ``increment`` must be > 0. ``price`` and ``increment`` are exact
        ``Decimal`` values (never binary floats) so the grid is represented
        precisely; a 0.05 tick grid cannot be expressed by price precision
        alone (precision 2 would wrongly admit 85.37). Round-half-even avoids
        the upward bias of round-half-up across many snaps.
    Returns:    A ``Decimal`` on the tick grid, exact and ready for
        ``instrument.make_price``. Units match ``price`` (venue quote currency).

    """
    steps = (price / increment).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return steps * increment


_SINOPAC_STATUS_MAP = {
    "PendingSubmit": OrderStatus.SUBMITTED,
    "PreSubmitted": OrderStatus.SUBMITTED,
    "Submitted": OrderStatus.ACCEPTED,
    "Failed": OrderStatus.REJECTED,
    "Cancelled": OrderStatus.CANCELED,
    "Filled": OrderStatus.FILLED,
    "PartFilled": OrderStatus.PARTIALLY_FILLED,
}

_NT_TO_SINOPAC_ACTION = {
    OrderSide.BUY: SinopacAction.BUY,
    OrderSide.SELL: SinopacAction.SELL,
}

_NT_TO_SINOPAC_PRICE_TYPE = {
    OrderType.LIMIT: SinopacPriceType.LMT,
    OrderType.MARKET: SinopacPriceType.MKT,
    OrderType.MARKET_TO_LIMIT: SinopacPriceType.MKP,
}

_NT_TO_SINOPAC_ORDER_TYPE = {
    TimeInForce.DAY: SinopacOrderType.ROD,
    TimeInForce.IOC: SinopacOrderType.IOC,
    TimeInForce.FOK: SinopacOrderType.FOK,
}

# Order types that send no price (price=0.0) and route as marketable orders.
_MARKETABLE_ORDER_TYPES = frozenset({OrderType.MARKET, OrderType.MARKET_TO_LIMIT})

# The 6 OrderType members the venue cannot place directly. All are conditional
# (stop/trigger) types; on Sinopac they must be emulated via NautilusTrader's
# OrderEmulator (submit with `emulation_trigger`), which releases a plain
# MARKET/LIMIT order on trigger. See docs/sinopac.md.
_CONDITIONAL_ORDER_TYPES = frozenset(
    {
        OrderType.STOP_MARKET,
        OrderType.STOP_LIMIT,
        OrderType.MARKET_IF_TOUCHED,
        OrderType.LIMIT_IF_TOUCHED,
        OrderType.TRAILING_STOP_MARKET,
        OrderType.TRAILING_STOP_LIMIT,
    },
)


def _resolve_order_type(
    order_type: OrderType,
    time_in_force: TimeInForce,
) -> tuple[SinopacOrderType | None, str | None]:
    """
    Resolve a Nautilus (order type, time-in-force) pair to a Sinopac order type.

    Taiwan venues accept only ROD/IOC/FOK and reject GTC entirely; market and
    range-market orders must be IOC or FOK. Unsupported time-in-force values are
    coerced (with a warning) or rejected so the order never reaches the gateway
    in an illegal combination.

    Parameters
    ----------
    order_type : OrderType
        The Nautilus order type (``LIMIT``, ``MARKET``, or ``MARKET_TO_LIMIT``).
    time_in_force : TimeInForce
        The Nautilus time-in-force.

    Returns
    -------
    tuple[SinopacOrderType | None, str | None]
        The mapped Sinopac order type (``None`` signals the caller to reject the
        order) and an optional human-readable warning describing any coercion.

    """
    if time_in_force in (TimeInForce.IOC, TimeInForce.FOK):
        return _NT_TO_SINOPAC_ORDER_TYPE[time_in_force], None

    if order_type in _MARKETABLE_ORDER_TYPES:
        if time_in_force in (TimeInForce.DAY, TimeInForce.GTC):
            return (
                SinopacOrderType.IOC,
                f"market orders require IOC/FOK on TWSE; coerced {time_in_force} to IOC",
            )
        return None, f"unsupported time-in-force {time_in_force} for market order"

    # LIMIT order
    if time_in_force == TimeInForce.DAY:
        return SinopacOrderType.ROD, None
    if time_in_force == TimeInForce.GTC:
        return SinopacOrderType.ROD, "GTC not supported by TWSE; coerced to ROD"

    return None, f"unsupported time-in-force {time_in_force}"


# Shioaji-verbatim string -> pyo3 enum. Only the in-scope values are accepted;
# `Odd` (post-market odd lot) and `Fixing` (fixed-price session) are rejected as
# unsupported even though the underlying enum can represent them.
_ORDER_LOT_BY_NAME = {
    "Common": SinopacOrderLot.COMMON,
    "IntradayOdd": SinopacOrderLot.INTRADAY_ODD,
}
_ORDER_COND_BY_NAME = {
    "Cash": SinopacOrderCond.CASH,
    "MarginTrading": SinopacOrderCond.MARGIN_TRADING,
    "ShortSelling": SinopacOrderCond.SHORT_SELLING,
}
_OCTYPE_BY_NAME = {
    "Auto": SinopacOCType.AUTO,
    "New": SinopacOCType.NEW,
    "Cover": SinopacOCType.COVER,
    "DayTrade": SinopacOCType.DAY_TRADE,
}


@dataclass(frozen=True)
class _ValidatedTags:
    """
    The strongly-typed, validated Sinopac order parameters resolved from tags.
    """

    order_cond: SinopacOrderCond
    order_lot: SinopacOrderLot
    octype: SinopacOCType
    daytrade_short: bool


def _resolve_tag_enums(
    tags: SinopacOrderTags,
) -> tuple[SinopacOrderCond | None, SinopacOrderLot | None, SinopacOCType | None, str | None]:
    """
    Resolve the Shioaji-verbatim tag strings to pyo3 enums.

    Only the in-scope values are accepted; an unknown (or out-of-scope ``Odd`` /
    ``Fixing``) value yields a clear rejection reason.

    Parameters
    ----------
    tags : SinopacOrderTags
        The parsed venue-specific order tags.

    Returns
    -------
    tuple[SinopacOrderCond | None, SinopacOrderLot | None, SinopacOCType | None, str | None]
        The mapped order condition, lot, and open-close type, plus a rejection
        reason (non-``None`` only when a value could not be mapped).

    """
    order_lot = _ORDER_LOT_BY_NAME.get(tags.order_lot)
    if order_lot is None:
        return (
            None,
            None,
            None,
            f"Unknown order_lot '{tags.order_lot}', expected Common|IntradayOdd",
        )

    order_cond = _ORDER_COND_BY_NAME.get(tags.order_cond)
    if order_cond is None:
        return (
            None,
            None,
            None,
            f"Unknown order_cond '{tags.order_cond}', expected Cash|MarginTrading|ShortSelling",
        )

    octype = _OCTYPE_BY_NAME.get(tags.octype)
    if octype is None:
        return None, None, None, f"Unknown octype '{tags.octype}', expected Auto|New|Cover|DayTrade"

    return order_cond, order_lot, octype, None


def _validate_stock_lot_rules(
    *,
    order_lot: SinopacOrderLot,
    order_cond: SinopacOrderCond,
    daytrade_short: bool,
    price_type: SinopacPriceType,
    order_type: SinopacOrderType,
    quantity: int,
) -> str | None:
    """
    Validate the stock-only lot/condition rules; return a reason on violation.

    Shioaji hard rules: intraday odd lot must be ``LMT + ROD``, 1-999 shares, and
    ``Cash``; common-lot quantity must be a multiple of 1000 shares;
    ``daytrade_short`` requires ``Cash``.

    Parameters
    ----------
    order_lot : SinopacOrderLot
        The resolved order lot.
    order_cond : SinopacOrderCond
        The resolved order condition.
    daytrade_short : bool
        Whether the order is a day-trade short.
    price_type : SinopacPriceType
        The resolved Sinopac price type.
    order_type : SinopacOrderType
        The resolved Sinopac order type (time-in-force).
    quantity : int
        The order quantity in shares (the wire unit, D1).

    Returns
    -------
    str | None
        A rejection reason, or ``None`` when the order satisfies the rules.

    """
    if order_lot == SinopacOrderLot.INTRADAY_ODD:
        if price_type != SinopacPriceType.LMT or order_type != SinopacOrderType.ROD:
            return "IntradayOdd orders must be LMT + ROD"
        if not 1 <= quantity <= 999:
            return f"IntradayOdd quantity must be 1-999 shares, was {quantity}"
        if order_cond != SinopacOrderCond.CASH:
            return "IntradayOdd orders must be Cash"
    elif order_lot == SinopacOrderLot.COMMON and quantity % 1000 != 0:
        return f"Common-lot quantity must be a multiple of 1000 shares, was {quantity}"

    if daytrade_short and order_cond != SinopacOrderCond.CASH:
        return "daytrade_short requires order_cond=Cash"

    return None


def _validate_and_map_tags(
    tags: SinopacOrderTags,
    *,
    market: SinopacMarket,
    price_type: SinopacPriceType,
    order_type: SinopacOrderType,
    quantity: int,
) -> tuple[_ValidatedTags | None, str | None, str | None]:
    """
    Map Sinopac order tags to pyo3 enums and validate the Taiwan order rules.

    The gateway remains the authoritative validator (422); this fail-fast layer
    blocks known-illegal combinations before they reach the wire and gives a
    clear local reason. The range-market price type (``MKP``) is
    futures/options-only on the Shioaji stock side, so a stock order routed to
    ``MKP`` is rejected locally rather than left to 500 at the gateway. A stock
    order carrying a futures ``octype`` is downgraded to ``Auto`` with a warning.

    Parameters
    ----------
    tags : SinopacOrderTags
        The parsed venue-specific order tags.
    market : SinopacMarket
        The resolved market (``STOCK``, ``FUTURES``, or ``OPTIONS``).
    price_type : SinopacPriceType
        The resolved Sinopac price type.
    order_type : SinopacOrderType
        The resolved Sinopac order type (time-in-force).
    quantity : int
        The order quantity in shares (the wire unit, D1).

    Returns
    -------
    tuple[_ValidatedTags | None, str | None, str | None]
        The validated parameters (``None`` when the order must be rejected), an
        optional rejection reason, and an optional warning describing any
        non-fatal coercion (for example a stock ``octype`` downgrade).

    """
    order_cond, order_lot, octype, reason = _resolve_tag_enums(tags)
    if reason is not None:
        return None, reason, None

    is_stock = market == SinopacMarket.STOCK

    # MKP (range market) is futures/options-only on the Shioaji stock side; a
    # stock MKP order would raise AttributeError -> HTTP 500 at the gateway.
    if is_stock and price_type == SinopacPriceType.MKP:
        return (
            None,
            "MARKET_TO_LIMIT (MKP) is not supported for stock orders on Shioaji; use LIMIT or MARKET",
            None,
        )

    # Lot-size rules are stock-only: futures/options quantities are contract
    # counts, and order_lot/order_cond/daytrade_short are stock concepts that the
    # gateway ignores for the futopt account.
    if is_stock:
        lot_reason = _validate_stock_lot_rules(
            order_lot=order_lot,
            order_cond=order_cond,
            daytrade_short=tags.daytrade_short,
            price_type=price_type,
            order_type=order_type,
            quantity=quantity,
        )

        if lot_reason is not None:
            return None, lot_reason, None

    # A futures open-close type is meaningless for stocks; downgrade to Auto and
    # warn rather than reject so an otherwise-valid stock order still goes out.
    warning: str | None = None
    if is_stock and octype != SinopacOCType.AUTO:
        warning = f"octype {tags.octype} ignored for stock order; forced to Auto"
        octype = SinopacOCType.AUTO

    validated = _ValidatedTags(
        order_cond=order_cond,
        order_lot=order_lot,
        octype=octype,
        daytrade_short=tags.daytrade_short,
    )
    return validated, None, warning


class SinopacExecutionClient(LiveExecutionClient):
    """
    Provides an execution client for the Sinopac (SinoPac) adapter.

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
    config : SinopacExecClientConfig
        The configuration for the client.
    name : str, optional
        The custom client ID.
    ws_dispatcher : WsDispatcherProtocol, optional
        The shared-WS dispatcher that fans out messages to the data and exec
        clients and refcounts the singleton socket. Always supplied by
        ``SinopacLiveExecClientFactory`` in production; the client registers its
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
        config: SinopacExecClientConfig,
        name: str | None = None,
        ws_dispatcher: WsDispatcherProtocol | None = None,
    ) -> None:
        account_id_str = config.account_id or os.environ.get(
            "SINOPAC_ACCOUNT_ID",
            "SINOPAC-001",
        )
        account_id = AccountId(f"{SINOPAC}-{account_id_str}")

        super().__init__(
            loop=loop,
            client_id=ClientId(name or SINOPAC),
            venue=SINOPAC_VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            base_currency=None,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )

        self._http_client = client
        self._ws_client = ws_client
        self._config = config
        self._ws_dispatcher = ws_dispatcher
        self._set_account_id(account_id)
        self._client_futures: set[asyncio.Future] = set()

        # Maps trade_id (VenueOrderId) → client_order_id for WS event correlation
        self._trade_id_to_client_order_id: dict[str, str] = {}

    @property
    def sinopac_instrument_provider(self) -> SinopacInstrumentProvider:
        return self._instrument_provider  # type: ignore

    def _require_dispatcher(self) -> WsDispatcherProtocol:
        if self._ws_dispatcher is None:
            raise RuntimeError(
                "ws_dispatcher was not supplied; SinopacLiveExecClientFactory "
                "always provides one for live use",
            )
        return self._ws_dispatcher

    # -- Connection lifecycle -------------------------------------------------

    async def _connect(self) -> None:
        # Standard exec-client order (adapters.md "Execution client"):
        # instruments -> shared WS -> account state. The exec client establishes
        # the shared WS itself so order/fill events arrive even with no data
        # client; connect() is idempotent (refcounted in the dispatcher).
        dispatcher = self._require_dispatcher()
        await self._instrument_provider.initialize()
        instruments = self.sinopac_instrument_provider.instruments_pyo3()
        dispatcher.register(self._handle_msg)
        await dispatcher.ensure_connected(instruments)
        await self._ws_client.wait_until_active(timeout_secs=10.0)
        await self._update_account_state()
        self._log.info(
            f"Connected to Sinopac gateway at {self._config.gateway_base_url}",
            LogColor.GREEN,
        )

    async def _disconnect(self) -> None:
        # Unregister our handler and release our WS refcount before cancelling
        # background futures; the shared socket closes only at refcount zero.
        dispatcher = self._require_dispatcher()
        dispatcher.unregister(self._handle_msg)
        await dispatcher.release()

        await cancel_tasks_with_timeout(
            self._client_futures,
            self._log,
            timeout_secs=DEFAULT_FUTURE_CANCELLATION_TIMEOUT,
        )
        self._client_futures.clear()

    async def _update_account_state(self) -> None:
        try:
            balance_data = await self._http_client.account_balance()
            twd = Currency.from_str("TWD")
            balances = [
                AccountBalance(
                    total=Money(balance_data["balance"], twd),
                    locked=Money(0, twd),
                    free=Money(balance_data["balance"], twd),
                ),
            ]
            self.generate_account_state(
                balances=balances,
                margins=[],
                reported=True,
                ts_event=self._clock.timestamp_ns(),
            )
        except Exception as e:
            self._log.error(f"Failed to update account state: {e}")

    # -- WS message handler ---------------------------------------------------

    def _handle_msg(self, msg: object) -> None:
        try:
            if nautilus_pyo3.is_pycapsule(msg):
                return  # Market data -- handled by DataClient

            if isinstance(msg, dict):
                self._handle_order_event(msg)
                return

            self._log.warning(f"Unhandled exec WS message type: {type(msg)}")
        except Exception as e:
            self._log.exception("Error handling Sinopac exec WS message", e)

    def _handle_order_event(self, event: dict[str, Any]) -> None:
        # The Rust WS layer emits a synthetic {"event": "reconnected"} dict after
        # it re-establishes the socket and resubscribes (SINOPAC-02). Order/fill
        # events that occurred during the gap are NOT replayed, so we trigger a
        # reconciliation pass to converge the ledger within one reconnect cycle.
        if event.get("event") == "reconnected":
            self._log.warning(
                "Sinopac WS reconnected; scheduling reconciliation to recover in-gap events",
                LogColor.YELLOW,
            )
            self._loop.create_task(self._reconcile_after_reconnect())
            return

        event_type = event.get("event_type")
        if event_type in ("stock_order", "futures_order"):
            self._handle_order_status_event(event)
        elif event_type in ("stock_deal", "futures_deal"):
            self._handle_deal_event(event)
        else:
            self._log.warning(f"Unknown order event type: {event_type}")

    async def _reconcile_after_reconnect(self) -> None:
        # Drive the SAME reconciliation the engine runs at startup: regenerate the
        # full ExecutionMassStatus (order/fill/position reports via list_trades and
        # list_positions) and hand it to the engine's mass-status reconciliation
        # entrypoint. This adopts any fills/cancels/rejections that landed while the
        # WS was down (the venue rejection of SINOPAC-04 also surfaces here as a
        # `Failed` order report).
        try:
            mass_status = await self.generate_mass_status(lookback_mins=None)
        except Exception as e:
            self._log.exception("Failed to reconcile after WS reconnect", e)
            return

        if mass_status is None:
            self._log.warning("Reconnect reconciliation produced no mass status")
            return

        self._send_mass_status_report(mass_status)

    def _handle_order_status_event(self, event: dict[str, Any]) -> None:
        op_code = event.get("op_code", "")
        op_type = event.get("op_type", "")
        order_id = event.get("order_id", "")
        code = event.get("code", "")
        custom_field = event.get("custom_field")

        instrument_id = InstrumentId.from_str(f"{code}.{SINOPAC}")

        # Look up the NT order: direct mapping first, then token round-trip
        client_order_id_str = self._resolve_client_order_id(order_id, custom_field)
        if client_order_id_str is None:
            self._log.info(f"External order event: {op_type} {order_id} {code}")
            return

        client_order_id = ClientOrderId(client_order_id_str)
        order = self._cache.order(client_order_id)
        if order is None:
            self._log.warning(f"Order {client_order_id} not found in cache for event")
            return

        venue_order_id = VenueOrderId(order_id)
        ts_event = self._clock.timestamp_ns()

        if op_code != "00":
            self._handle_order_op_failure(
                event,
                order,
                op_type,
                op_code,
                order_id,
                instrument_id,
                venue_order_id,
                client_order_id,
                ts_event,
            )
            return

        # Operation succeeded (op_code == "00")
        if op_type == "Cancel":
            self.generate_order_canceled(
                strategy_id=order.strategy_id,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                ts_event=ts_event,
            )
            self._trade_id_to_client_order_id.pop(order_id, None)
        elif op_type in ("UpdatePrice", "UpdateQty"):
            modified_price = event.get("modified_price", 0.0)
            order_quantity = event.get("order_quantity", 0)
            instrument = self._cache.instrument(instrument_id)
            if instrument is not None:
                self.generate_order_updated(
                    strategy_id=order.strategy_id,
                    instrument_id=instrument_id,
                    client_order_id=client_order_id,
                    venue_order_id=venue_order_id,
                    quantity=instrument.make_qty(order_quantity),
                    price=instrument.make_price(modified_price)
                    if modified_price > 0
                    else order.price,
                    trigger_price=None,
                    ts_event=ts_event,
                )
        # "New" with op_code "00" = order accepted (already handled in _submit_order)

    def _handle_order_op_failure(
        self,
        event: dict[str, Any],
        order: Any,
        op_type: str,
        op_code: str,
        order_id: str,
        instrument_id: InstrumentId,
        venue_order_id: VenueOrderId,
        client_order_id: ClientOrderId,
        ts_event: int,
    ) -> None:
        reason = event.get("op_msg", f"Operation failed: {op_type} code={op_code}")
        if op_type == "New":
            # PRIMARY closure of SINOPAC-04's dominant async path: the gateway
            # returns HTTP 200 + PendingSubmit for venue rejections (off-tick,
            # over-band) and the rejection surfaces LATER as a "New" order event
            # with op_code != "00". By then `_submit_order` has already marked the
            # order ACCEPTED, so this async failure must drive ACCEPTED -> REJECTED
            # (a legal NT transition). Only PARTIALLY_FILLED/FILLED are protected,
            # since rejecting a (partly) filled order is an illegal transition that
            # would panic the Rust state machine.
            if order.status in (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED):
                self._log.warning(
                    f"Late 'New' failure for {client_order_id} in {order.status!r} "
                    f"(reason={reason}); ignoring to avoid illegal state transition",
                )
                return
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                reason=reason,
                ts_event=ts_event,
            )
            self._trade_id_to_client_order_id.pop(order_id, None)
        elif op_type == "Cancel":
            self.generate_order_cancel_rejected(
                strategy_id=order.strategy_id,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                reason=reason,
                ts_event=ts_event,
            )
        elif op_type in ("UpdatePrice", "UpdateQty"):
            self.generate_order_modify_rejected(
                strategy_id=order.strategy_id,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                reason=reason,
                ts_event=ts_event,
            )

    def _handle_deal_event(self, event: dict[str, Any]) -> None:
        trade_id_str = event.get("trade_id", "")
        ordno = event.get("ordno", "")
        # seqno is per-ORDER (== the order's seqno) so it repeats across partial fills.
        # The per-fill-unique keys are the exchange deal sequence (exchange_seq) and the
        # deal-level ordno (last 3 chars are the deal sequence). Prefer exchange_seq;
        # fall back to ordno. NEVER key on seqno -- it corrupts the ledger via dup TradeIds.
        seq = event.get("exchange_seq") or ordno
        code = event.get("code", "")
        price = event.get("price", 0.0)
        quantity = event.get("quantity", 0)
        ts = event.get("ts", 0.0)
        custom_field = event.get("custom_field")

        instrument_id = InstrumentId.from_str(f"{code}.{SINOPAC}")
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            self._log.error(f"Cannot process deal: instrument {instrument_id} not in cache")
            return

        # Look up the NT order: direct mapping first, then token round-trip
        client_order_id_str = self._resolve_client_order_id(trade_id_str, custom_field)
        if client_order_id_str is None:
            self._log.info(
                f"External deal: {code} {event.get('action', '')} {price}x{quantity}",
            )
            return

        client_order_id = ClientOrderId(client_order_id_str)
        order = self._cache.order(client_order_id)
        if order is None:
            self._log.warning(f"Order {client_order_id} not found for deal event")
            return

        venue_order_id = order.venue_order_id or VenueOrderId(trade_id_str)
        ts_event_ns = int(ts * 1_000_000_000) if ts > 0 else self._clock.timestamp_ns()

        twd = Currency.from_str("TWD")
        self.generate_order_filled(
            strategy_id=order.strategy_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            venue_position_id=None,
            trade_id=TradeId(f"{trade_id_str}-{seq}"),
            order_side=order.side,
            order_type=order.order_type,
            last_qty=instrument.make_qty(quantity),
            last_px=instrument.make_price(price),
            quote_currency=twd,
            commission=Money(0, twd),
            liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
            ts_event=ts_event_ns,
        )

        # Clean up mapping when order is fully filled
        order = self._cache.order(client_order_id)
        if order is not None and order.is_closed:
            self._trade_id_to_client_order_id.pop(trade_id_str, None)

    def _resolve_client_order_id(
        self,
        lookup_key: str,
        custom_field: str | None,
    ) -> str | None:
        """
        Resolve the original client_order_id for a venue event/trade.

        Definition: Attempts to recover the NT client_order_id that was used
            when placing the order, using two strategies in priority order.
        Formula:    (1) Direct lookup in ``_trade_id_to_client_order_id`` by
            ``lookup_key`` (trade_id or order_id). (2) If not found, match
            the ``custom_field`` token against in-cache orders by recomputing
            ``_coid_token`` for each open/submitted order.
        Domain:     ``custom_field`` may be None/empty for orders placed before
            this feature was deployed, or for external orders. In that case
            only the direct mapping can resolve. Hash collisions among active
            orders are negligible (62^6 ~ 5.7e10 vs O(10) active orders).
        Returns:    The ``client_order_id`` string, or None if unresolvable
            (caller should fall back to synthetic id or log as external).

        """
        # Fast path: in-memory mapping populated by _submit_order on success
        coid = self._trade_id_to_client_order_id.get(lookup_key)
        if coid is not None:
            return coid

        # Slow path: recover via custom_field token round-trip
        if custom_field:
            for o in self._cache.orders():
                if o.is_closed:
                    continue
                if _coid_token(o.client_order_id.value) == custom_field:
                    # Backfill the mapping so subsequent events are fast-path
                    self._trade_id_to_client_order_id[lookup_key] = o.client_order_id.value
                    return o.client_order_id.value

        return None

    # -- Order operations -----------------------------------------------------

    def _resolve_validated_tags(
        self,
        order: Order,
        *,
        market: SinopacMarket,
        price_type: SinopacPriceType,
        order_type: SinopacOrderType,
        quantity: int,
    ) -> _ValidatedTags | None:
        instrument_id = order.instrument_id

        try:
            tags = SinopacOrderTags.from_tags(order.tags)
        except Exception as e:
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=instrument_id,
                client_order_id=order.client_order_id,
                reason=f"Malformed SinopacOrderTags: {e}",
                ts_event=self._clock.timestamp_ns(),
            )
            return None

        validated, reject_reason, tags_warning = _validate_and_map_tags(
            tags,
            market=market,
            price_type=price_type,
            order_type=order_type,
            quantity=quantity,
        )

        if validated is None:
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=instrument_id,
                client_order_id=order.client_order_id,
                reason=reject_reason or "Invalid Sinopac order tags",
                ts_event=self._clock.timestamp_ns(),
            )
            return None

        if tags_warning:
            self._log.warning(f"{tags_warning} for {order.client_order_id}")

        return validated

    def _resolve_send_price(self, order: Order, instrument: Instrument | None) -> float:
        # Snap the limit price onto the venue tick grid before sending.
        # SINOPAC-09's residual risk: price precision alone cannot express a
        # 0.05 tick grid, so an off-grid limit (e.g. 85.37) would be rejected
        # by the venue. Market orders carry no price: MarketOrder has no
        # `price` attribute at all and MarketToLimitOrder leaves `price` as
        # None pre-fill, so `getattr` keeps the access safe for both and
        # they send price=0.0 (MKT/MKP).
        order_price = getattr(order, "price", None)
        if order_price is None:
            return 0.0

        if instrument is None or instrument.price_increment <= 0:
            return float(order_price)

        increment = instrument.price_increment.as_decimal()
        snapped = _snap_price_to_grid(order_price.as_decimal(), increment)
        snapped_price = instrument.make_price(snapped)
        if snapped_price != order_price:
            self._log.warning(
                f"Snapped off-grid price {order_price} -> {snapped_price} "
                f"(tick {instrument.price_increment}) for {order.client_order_id}",
            )

        return float(snapped_price)

    async def _submit_order(self, command: SubmitOrder) -> None:
        order = command.order
        instrument_id = order.instrument_id

        if order.order_type not in _NT_TO_SINOPAC_PRICE_TYPE:
            if order.order_type in _CONDITIONAL_ORDER_TYPES:
                reason = (
                    f"Sinopac has no native conditional orders "
                    f"({order_type_to_str(order.order_type)}); "
                    "resubmit with emulation_trigger=LAST_PRICE or BID_ASK to use "
                    "NautilusTrader order emulation"
                )
            else:
                # Defensive: no current OrderType reaches here, but a future enum
                # addition would otherwise be silently dropped.
                reason = (
                    f"Unsupported order type {order_type_to_str(order.order_type)} "
                    "for Sinopac"
                )
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=instrument_id,
                client_order_id=order.client_order_id,
                reason=reason,
                ts_event=self._clock.timestamp_ns(),
            )
            return

        self.generate_order_submitted(
            strategy_id=order.strategy_id,
            instrument_id=instrument_id,
            client_order_id=order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

        try:
            code = instrument_id.symbol.value
            action = _NT_TO_SINOPAC_ACTION[order.side]
            price_type = _NT_TO_SINOPAC_PRICE_TYPE[order.order_type]

            order_type, tif_warning = _resolve_order_type(
                order.order_type,
                order.time_in_force,
            )

            if order_type is None:
                self.generate_order_rejected(
                    strategy_id=order.strategy_id,
                    instrument_id=instrument_id,
                    client_order_id=order.client_order_id,
                    reason=tif_warning or f"unsupported time-in-force {order.time_in_force}",
                    ts_event=self._clock.timestamp_ns(),
                )
                return
            if tif_warning:
                self._log.warning(f"{tif_warning} for {order.client_order_id}")

            quantity = int(order.quantity)

            instrument = self._cache.instrument(instrument_id)
            market = self._determine_market(instrument)

            validated = self._resolve_validated_tags(
                order,
                market=market,
                price_type=price_type,
                order_type=order_type,
                quantity=quantity,
            )

            if validated is None:
                return  # Rejection already emitted by the helper

            price = self._resolve_send_price(order, instrument)

            token = _coid_token(order.client_order_id.value)

            response = await self._http_client.place_order(
                code=code,
                action=action,
                price=price,
                quantity=quantity,
                price_type=price_type,
                order_type=order_type,
                order_cond=validated.order_cond,
                order_lot=validated.order_lot,
                market=market,
                custom_field=token,
                octype=validated.octype,
                daytrade_short=validated.daytrade_short,
            )

            # Defensive (second line of defense): a synchronously-rejecting or
            # stale gateway may return HTTP 200 with a terminal `Failed` status
            # rather than a 422. The PRIMARY rejection paths are (a) the gateway
            # 422 -> pyo3 raises -> the generic-Exception handler below rejects,
            # and (b) the DOMINANT async path where the venue rejects later via a
            # `New` order event with op_code != "00" (handled in
            # _handle_order_op_failure). This check only catches a gateway that
            # synchronously echoes a rejected status, and must NOT populate the
            # trade_id mapping for such a non-working order.
            raw_status = response.get("status", "")
            status_key = raw_status.split(".")[-1] if "." in raw_status else raw_status
            if _SINOPAC_STATUS_MAP.get(status_key) == OrderStatus.REJECTED:
                self.generate_order_rejected(
                    strategy_id=order.strategy_id,
                    instrument_id=instrument_id,
                    client_order_id=order.client_order_id,
                    reason=f"Gateway status {raw_status}",
                    ts_event=self._clock.timestamp_ns(),
                )
                return

            trade_id = response["trade_id"]
            venue_order_id = VenueOrderId(trade_id)

            self._trade_id_to_client_order_id[trade_id] = order.client_order_id.value

            self.generate_order_accepted(
                strategy_id=order.strategy_id,
                instrument_id=instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=venue_order_id,
                ts_event=self._clock.timestamp_ns(),
            )

        except (TimeoutError, OSError) as e:
            # Transport failure: the request may have actually reached the gateway
            # and the order may be LIVE on the exchange. Rejecting here would create
            # hidden exposure (we report REJECTED while the venue holds a working
            # order). So we leave the order in SUBMITTED rather than rejecting.
            #
            # Convergence is best-effort, NOT a guaranteed clean adopt of THIS local
            # order: on timeout we never received a `trade_id`, so the local SUBMITTED
            # order has no `venue_order_id` and the `_trade_id_to_client_order_id`
            # mapping was never populated. Reconciliation (`generate_order_status_reports`
            # -> `list_trades`) rebuilds an OrderStatusReport keyed on the venue
            # `trade_id` with a *synthesized* `client_order_id` (`SINOPAC-{trade_id}`)
            # when no mapping exists. Because that synthesized id and venue_order_id do
            # not match the local order, NT most likely treats the report as an EXTERNAL
            # order rather than adopting the local SUBMITTED one. Net effect: the venue
            # working order becomes visible/tracked (no hidden exposure), but the
            # original local SUBMITTED order's convergence depends on NT's external-order
            # handling and may require operator intervention. WS deal/order events that
            # do carry the original `trade_id` can still correlate if the mapping is
            # later established by a successful path.
            self._log.error(
                f"place_order transport failure for {order.client_order_id}: {e!r}; "
                f"leaving order SUBMITTED (venue order, if any, surfaces via "
                f"WS/reconciliation as an external order)",
            )
        except Exception as e:
            # Genuine business rejection (validation, margin, unsupported params, ...):
            # the order definitively did not reach a working state, so reject is safe.
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=instrument_id,
                client_order_id=order.client_order_id,
                reason=str(e),
                ts_event=self._clock.timestamp_ns(),
            )

    async def _modify_order(self, command: ModifyOrder) -> None:
        order = self._cache.order(command.client_order_id)
        if order is None:
            self._log.error(
                f"Cannot modify: order {command.client_order_id} not found in cache",
            )
            return
        if order.is_closed:
            self._log.warning(
                f"Cannot modify: order {command.client_order_id} already closed",
            )
            return

        venue_order_id = order.venue_order_id
        if venue_order_id is None:
            self._log.error(
                f"Cannot modify: no venue_order_id for {command.client_order_id}",
            )
            return

        # Shioaji forbids price changes on intraday odd-lot orders; only quantity
        # may be reduced. Reject a price modification locally rather than send it
        # and let the gateway 422 the round-trip.
        if (
            command.price is not None
            and SinopacOrderTags.from_tags(order.tags).order_lot == "IntradayOdd"
        ):
            self.generate_order_modify_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=venue_order_id,
                reason="IntradayOdd orders cannot change price, only reduce quantity",
                ts_event=self._clock.timestamp_ns(),
            )
            return

        try:
            trade_id = venue_order_id.value
            price = float(command.price) if command.price is not None else None
            quantity = int(command.quantity) if command.quantity is not None else None

            await self._http_client.update_order(
                trade_id=trade_id,
                price=price,
                quantity=quantity,
            )
            # Actual update confirmation comes via WS order event callback

        except Exception as e:
            self.generate_order_modify_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=venue_order_id,
                reason=str(e),
                ts_event=self._clock.timestamp_ns(),
            )

    async def _cancel_order(self, command: CancelOrder) -> None:
        order = self._cache.order(command.client_order_id)
        if order is None:
            self._log.error(
                f"Cannot cancel: order {command.client_order_id} not found in cache",
            )
            return
        if order.is_closed:
            self._log.warning(
                f"Cannot cancel: order {command.client_order_id} already closed",
            )
            return

        venue_order_id = order.venue_order_id
        if venue_order_id is None:
            self._log.error(
                f"Cannot cancel: no venue_order_id for {command.client_order_id}",
            )
            return

        try:
            await self._http_client.cancel_order(trade_id=venue_order_id.value)
            # Actual cancel confirmation comes via WS order event callback

        except Exception as e:
            self.generate_order_cancel_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                venue_order_id=venue_order_id,
                reason=str(e),
                ts_event=self._clock.timestamp_ns(),
            )

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        open_orders = self._cache.orders_open(instrument_id=command.instrument_id)
        for order in open_orders:
            if order.venue_order_id is not None:
                try:
                    await self._http_client.cancel_order(
                        trade_id=order.venue_order_id.value,
                    )
                except Exception as e:
                    self._log.error(f"Failed to cancel {order.client_order_id}: {e}")

    async def _submit_order_list(self, command: SubmitOrderList) -> None:
        for order in command.order_list.orders:
            submit = SubmitOrder(
                trader_id=command.trader_id,
                strategy_id=command.strategy_id,
                order=order,
                command_id=command.id,
                ts_init=command.ts_init,
            )
            await self._submit_order(submit)

    async def _batch_cancel_orders(self, command: BatchCancelOrders) -> None:
        for cancel in command.cancels:
            await self._cancel_order(cancel)

    def _determine_market(self, instrument: object) -> SinopacMarket:
        if isinstance(instrument, Equity):
            return SinopacMarket.STOCK
        elif isinstance(instrument, FuturesContract):
            return SinopacMarket.FUTURES
        elif isinstance(instrument, OptionContract):
            return SinopacMarket.OPTIONS
        return SinopacMarket.STOCK

    # -- Reconciliation reports -----------------------------------------------

    async def generate_order_status_reports(
        self,
        command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        reports: list[OrderStatusReport] = []
        try:
            trades = await self._http_client.list_trades()
            for trade_dict in trades:
                instrument_id = InstrumentId.from_str(
                    f"{trade_dict['code']}.{SINOPAC}",
                )

                if command.instrument_id and command.instrument_id != instrument_id:
                    continue

                instrument = self._cache.instrument(instrument_id)
                if instrument is None:
                    continue

                raw_status = trade_dict["status"]
                # Gateway may return "Status.Failed" instead of "Failed"
                status_key = raw_status.split(".")[-1] if "." in raw_status else raw_status
                order_status = _SINOPAC_STATUS_MAP.get(status_key)
                if order_status is None:
                    self._log.warning(
                        f"Unknown Sinopac order status '{raw_status}', defaulting to DENIED",
                    )
                    order_status = OrderStatus.DENIED
                order_side = OrderSide.BUY if trade_dict["action"] == "Buy" else OrderSide.SELL
                order_type = (
                    OrderType.LIMIT if trade_dict["price_type"] == "LMT" else OrderType.MARKET
                )

                # Map gateway order_type to NT TimeInForce
                raw_order_type = trade_dict.get("order_type", "ROD")
                tif_key = raw_order_type.split(".")[-1] if "." in raw_order_type else raw_order_type
                tif_map = {"ROD": TimeInForce.DAY, "IOC": TimeInForce.IOC, "FOK": TimeInForce.FOK}
                time_in_force = tif_map.get(tif_key, TimeInForce.DAY)

                trade_id = trade_dict["trade_id"]
                custom_field = trade_dict.get("custom_field")
                client_order_id_str = self._resolve_client_order_id(
                    trade_id,
                    custom_field,
                )
                # When neither the in-memory mapping nor the custom_field token
                # can recover the original client_order_id (e.g. external orders,
                # or orders placed before the token feature), we synthesize a
                # deterministic client_order_id. NOTE: this synthesized id does
                # NOT match the local SUBMITTED order's own client_order_id, so
                # NT surfaces this report as an EXTERNAL order. The report still
                # removes hidden exposure by making the venue's working order
                # visible; full convergence of the original local order depends
                # on NT's external-order handling.
                client_order_id = (
                    ClientOrderId(client_order_id_str)
                    if client_order_id_str
                    else ClientOrderId(f"SINOPAC-{trade_id}")
                )

                # Quantities are share-denominated end-to-end (D1): the gateway
                # normalizes Shioaji lots to shares at its SDK boundary, so
                # `filled_qty` is the gateway-reported filled share count. A None
                # value means an older gateway that does not report it; fall back
                # to 0 and warn once that reconciliation may be incomplete.
                filled_qty = trade_dict.get("filled_qty")
                if filled_qty is None:
                    self._log.warning(
                        "Gateway did not report filled_qty; reconciliation may be incomplete",
                    )
                    filled_qty = 0

                report = OrderStatusReport(
                    account_id=self.account_id,
                    instrument_id=instrument_id,
                    client_order_id=client_order_id,
                    venue_order_id=VenueOrderId(trade_id),
                    order_side=order_side,
                    order_type=order_type,
                    time_in_force=time_in_force,
                    quantity=instrument.make_qty(trade_dict["quantity"]),
                    filled_qty=instrument.make_qty(filled_qty),
                    price=instrument.make_price(trade_dict["price"]),
                    order_status=order_status,
                    report_id=UUID4(),
                    ts_accepted=self._clock.timestamp_ns(),
                    ts_last=self._clock.timestamp_ns(),
                    ts_init=self._clock.timestamp_ns(),
                )
                reports.append(report)
        except Exception as e:
            self._log.error(f"Failed to generate order status reports: {e}")

        return reports

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        reports = await self.generate_order_status_reports(
            GenerateOrderStatusReports(
                trader_id=command.trader_id,
                instrument_id=command.instrument_id,
                command_id=command.id,
                ts_init=command.ts_init,
            ),
        )

        for report in reports:
            if command.client_order_id and report.client_order_id == command.client_order_id:
                return report
            if command.venue_order_id and report.venue_order_id == command.venue_order_id:
                return report
        return None

    async def generate_fill_reports(
        self,
        command: GenerateFillReports,
    ) -> list[FillReport]:
        self._log.info(
            "Fill reports generated from WS events only (no historical fill endpoint)",
        )
        return []

    async def generate_position_status_reports(
        self,
        command: GeneratePositionStatusReports,
    ) -> list[PositionStatusReport]:
        reports: list[PositionStatusReport] = []
        try:
            for market in ("stock", "futures"):
                try:
                    positions = await self._http_client.list_positions(market=market)
                except Exception as e:
                    self._log.debug(f"No {market} positions available: {e}")
                    continue

                for pos_dict in positions:
                    instrument_id = InstrumentId.from_str(
                        f"{pos_dict['code']}.{SINOPAC}",
                    )

                    if command.instrument_id and command.instrument_id != instrument_id:
                        continue

                    instrument = self._cache.instrument(instrument_id)
                    if instrument is None:
                        continue

                    direction = pos_dict.get("direction", "")
                    if direction == "Buy":
                        position_side = PositionSide.LONG
                    elif direction == "Sell":
                        position_side = PositionSide.SHORT
                    else:
                        position_side = PositionSide.FLAT

                    quantity = pos_dict.get("quantity", 0)
                    if quantity == 0:
                        continue

                    report = PositionStatusReport(
                        account_id=self.account_id,
                        instrument_id=instrument_id,
                        position_side=position_side,
                        quantity=instrument.make_qty(quantity),
                        report_id=UUID4(),
                        ts_last=self._clock.timestamp_ns(),
                        ts_init=self._clock.timestamp_ns(),
                    )
                    reports.append(report)
        except Exception as e:
            self._log.error(f"Failed to generate position status reports: {e}")

        return reports
