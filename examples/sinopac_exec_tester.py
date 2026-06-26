#!/usr/bin/env python3
"""
Example: Sinopac execution client tester.

Tests order submission, modification, and cancellation for Taiwan instruments
using the Sinopac gateway adapter.

Prerequisites:
    1. Sinopac gateway running: ``uvicorn sinopac_server.main:app --port 8000``
    2. Gateway logged in with CA activated for order placement
    3. Use simulation=True in gateway for testing

CAUTION: Set dry_run=True to prevent actual order placement.

Scenarios
---------
The ``SINOPAC_EXEC_SCENARIO`` environment variable selects which strategy runs:

- ``common`` (default): the stock ``ExecTester`` behavior (unchanged). Exercises
  the generic limit/cancel/replace path on 2330 (TSMC).
- ``intraday_odd``: submits a single 37-share intraday odd-lot LIMIT @ bid, ROD,
  tagged ``order_lot=IntradayOdd``. Verifies the Taiwan intraday odd-lot path
  (LMT+ROD, 1-999 shares, share-unit factor 1).
- ``mkp``: submits a single MARKET_TO_LIMIT (MKP, range-market) order. MKP is
  futures/options-only on the Shioaji side, so this scenario targets the MXF
  front-month futures contract (a stock MKP is rejected locally by the adapter,
  which would only exercise the local-reject path already covered by unit tests).
  The adapter coerces the default GTC TIF to IOC for marketable order types.
- ``futures_octype``: submits a single MXF front-month LIMIT tagged
  ``octype=Cover``. With no open position a Cover order is rejected by the venue;
  reaching that terminal rejection is the expected observable.

The MXF front month is resolved DYNAMICALLY at start (nearest non-expired listed
contract) rather than hard-coded: the gateway lists futures by their resolved
month code (e.g. ``MXFF6``), never the ``C0``/``C1`` lookup aliases, so a literal
``MXFC0.SINOPAC`` id would never resolve in the instrument cache.

Margin/short-selling (``order_cond=MarginTrading``/``ShortSelling``) is NOT
scripted here: the simulation gateway has no credit account, so those paths
cannot reach a meaningful terminal state in sim. They are a manual pre-live
verification item (place a real margin/short order against a funded credit
account during a controlled live session) -- see the design doc test matrix.

The ``SINOPAC_EXEC_DRY_RUN`` environment variable (``true``/``false``) overrides
``dry_run`` for the scenario strategies; when true, orders are built and logged
but not submitted.

- ``stop_market``: submits a single emulated ``STOP_MARKET`` on TSMC (2330) with
  ``emulation_trigger=TriggerType.LAST_PRICE``. The NautilusTrader
  ``OrderEmulator`` holds the order until the last-trade price crosses the
  trigger, then releases a plain ``MARKET`` order to the venue. In dry-run mode
  the order is built and logged without being submitted.
- ``bracket``: submits a bracket ``OrderList`` (entry ``LIMIT`` + stop-loss
  ``STOP_MARKET`` + take-profit ``LIMIT``) on TSMC (2330), all three legs carrying
  ``emulation_trigger=TriggerType.LAST_PRICE``. All legs are emulated by the
  ``OrderEmulator``: the entry is held at submit; the OTO-child SL and TP activate
  only after the entry fills. Sinopac has no native conditional orders, so no leg
  rests natively at the venue. In dry-run mode the order list is built and logged
  without being submitted.
"""

import os
from decimal import Decimal

from sinopac_nt.config import SinopacDataClientConfig
from sinopac_nt.config import SinopacExecClientConfig
from sinopac_nt.constants import SINOPAC
from sinopac_nt.factories import SinopacLiveDataClientFactory
from sinopac_nt.factories import SinopacLiveExecClientFactory
from sinopac_nt.tags import SinopacOrderTags
from nautilus_trader.cache.config import CacheConfig
from nautilus_trader.common.enums import LogColor
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.enums import TriggerType
from nautilus_trader.model.events import OrderEvent
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.orders import Order
from nautilus_trader.test_kit.strategies.tester_exec import ExecTester
from nautilus_trader.test_kit.strategies.tester_exec import ExecTesterConfig
from nautilus_trader.trading.strategy import Strategy


# --- Shared configuration ------------------------------------------------------

# Quantities are SHARES end-to-end (gateway wire unit): 1000 shares = 1 common lot,
# which the gateway converts to 1 SDK lot at the boundary. Was Decimal(1) under the
# old lots-based wire unit; that now means 1 share (odd-lot) and a common-lot order
# of 1 share is rejected as a non-1000-multiple.
STOCK_INSTRUMENT_ID = InstrumentId.from_str("2330.SINOPAC")  # TSMC
SINOPAC_VENUE = Venue("SINOPAC")
# MXF (Mini-TAIEX) product root. The gateway lists futures by iterating
# api.Contracts.Futures and emits each contract's RESOLVED month code
# (letter+year-digit, e.g. "MXFF6"), never the "C0"/"C1" lookup aliases. The Rust
# adapter sets the instrument-id symbol to contract.code verbatim, so the cache
# holds ids like "MXFF6.SINOPAC". A hard-coded "MXFC0.SINOPAC" would never resolve;
# the front month is resolved dynamically at runtime instead (see
# _resolve_front_month).
MXF_FUTURES_ROOT = "MXF"

TRADE_SIZE = Decimal(1000)  # 1000 shares = 1 common lot
OFFSET_TICKS = 10  # Offset from market price for limit orders
SINOPAC_ACCOUNT_ID = None  # Set to your account ID, or use SINOPAC_ACCOUNT_ID env var
GATEWAY_HOST = "localhost"
GATEWAY_PORT = 8123  # gateway moved off the popular 8000 (collided with vLLM)

# Places REAL orders so the full path reaches shioaji-server (dry_run=True would
# short-circuit inside NT before the order is sent, never exercising the gateway
# end-to-end). The configured gateway (:8123) is the SIMULATION gateway -- never
# point this at a live gateway. The market-open cron wrapper additionally refuses
# to run this tester unless the gateway reports simulation=true.
DRY_RUN_DEFAULT = False

SCENARIO_COMMON = "common"
SCENARIO_INTRADAY_ODD = "intraday_odd"
SCENARIO_MKP = "mkp"
SCENARIO_FUTURES_OCTYPE = "futures_octype"
SCENARIO_STOP_MARKET = "stop_market"
SCENARIO_BRACKET = "bracket"
SCENARIOS = (
    SCENARIO_COMMON,
    SCENARIO_INTRADAY_ODD,
    SCENARIO_MKP,
    SCENARIO_FUTURES_OCTYPE,
    SCENARIO_STOP_MARKET,
    SCENARIO_BRACKET,
)


def _env_dry_run() -> bool:
    """
    Resolve the dry-run flag from ``SINOPAC_EXEC_DRY_RUN``.

    Returns
    -------
    bool
        ``True`` if the env var is set to a truthy token, otherwise the module
        default (``DRY_RUN_DEFAULT``).

    """
    raw = os.environ.get("SINOPAC_EXEC_DRY_RUN")
    if raw is None:
        return DRY_RUN_DEFAULT
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _resolve_scenario() -> str:
    """
    Resolve the active scenario from ``SINOPAC_EXEC_SCENARIO``.

    Returns
    -------
    str
        One of ``SCENARIOS``; defaults to ``common`` when unset.

    Raises
    ------
    ValueError
        If the env var holds an unknown scenario name.

    """
    scenario = os.environ.get("SINOPAC_EXEC_SCENARIO", SCENARIO_COMMON).strip().lower()
    if scenario not in SCENARIOS:
        raise ValueError(
            f"Unknown SINOPAC_EXEC_SCENARIO '{scenario}', expected one of {SCENARIOS}",
        )
    return scenario


def _resolve_front_month(
    instruments: list[Instrument],
    root: str,
    now_ns: int,
) -> FuturesContract | None:
    """
    Select the front-month futures contract for a product root.

    Definition: The front month is the nearest non-expired listed contract for the
    given product root, i.e. the live contract with the soonest delivery.
    Formula:    front = argmin_{c in C} c.expiration_ns
                where C = { c : c is FuturesContract,
                            c.symbol starts with `root`,
                            c.expiration_ns > `now_ns` }.
    Domain:     `instruments` is the set loaded into the cache for one venue;
                expirations are UNIX nanoseconds (same clock as `now_ns`). Contracts
                whose symbol does not start with `root`, or whose expiration is at or
                before `now_ns`, are excluded. Returns ``None`` when `C` is empty
                (none listed, or all expired).
    Returns:    The nearest non-expired ``FuturesContract`` for `root`, or ``None``.

    The gateway lists futures by their resolved month code (e.g. ``MXFF6``), so the
    `root` filter is a symbol prefix match on the product root (e.g. ``MXF``), not a
    lookup-alias (e.g. ``MXFC0``) match.

    """
    candidates = [
        instrument
        for instrument in instruments
        if isinstance(instrument, FuturesContract)
        and instrument.id.symbol.value.startswith(root)
        and instrument.expiration_ns > now_ns
    ]

    if not candidates:
        return None
    return min(candidates, key=lambda instrument: instrument.expiration_ns)


# --- Order-semantics scenario strategy ----------------------------------------


class OrderSemanticsScenarioConfig(StrategyConfig, frozen=True):
    """
    Configuration for ``OrderSemanticsScenarioStrategy``.

    Parameters
    ----------
    scenario : str
        The scenario name, one of ``intraday_odd``, ``mkp``, ``futures_octype``.
    instrument_id : InstrumentId, optional
        The fixed instrument to subscribe to and trade. Mutually exclusive with
        ``front_month_root``; exactly one must be set.
    front_month_root : str, optional
        The futures product root (e.g. ``MXF``) whose front month is resolved
        dynamically from the loaded instruments at start. Used by the futures
        scenarios because the gateway lists resolved month codes, not the
        ``C0``/``C1`` lookup aliases. Mutually exclusive with ``instrument_id``.
    dry_run : bool, default False
        If true, the order is built and logged but not submitted.

    """

    scenario: str
    instrument_id: InstrumentId | None = None
    front_month_root: str | None = None
    dry_run: bool = False


class OrderSemanticsScenarioStrategy(Strategy):
    """
    Submit exactly one Taiwan order-semantics order, then observe its lifecycle.

    On the first quote tick the strategy builds a single order tailored to the
    configured scenario, submits it (unless ``dry_run``), and logs every order
    event it receives. Any resting order is cancelled on stop. This is a manual
    integration probe with no alpha; it exists to exercise the full NT -> gateway
    -> Shioaji order path for the Taiwan-specific order semantics.

    """

    def __init__(self, config: OrderSemanticsScenarioConfig) -> None:
        super().__init__(config)
        self.instrument: Instrument | None = None
        self.instrument_id: InstrumentId | None = None
        self.order: Order | None = None
        self._submitted = False

    def on_start(self) -> None:
        """
        Resolve the scenario instrument and subscribe to its quotes.
        """
        self.instrument = self._resolve_instrument()
        if self.instrument is None:
            self.stop()
            return

        self.instrument_id = self.instrument.id
        self.log.info(
            f"Scenario '{self.config.scenario}' armed on {self.instrument_id} "
            f"(dry_run={self.config.dry_run})",
            LogColor.BLUE,
        )
        self.subscribe_quote_ticks(self.instrument_id)

    def _resolve_instrument(self) -> Instrument | None:
        """
        Resolve the instrument to trade for the active scenario.

        For a fixed-id scenario this is a direct cache lookup. For a futures
        scenario it dynamically selects the front-month contract for the configured
        product root from the instruments loaded into the cache, because the gateway
        lists resolved month codes (e.g. ``MXFF6``) rather than the ``C0`` alias.

        Returns
        -------
        Instrument or ``None``
            The resolved instrument, or ``None`` (with a logged error) when it
            cannot be resolved -- the caller stops the strategy in that case.

        """
        if self.config.front_month_root is not None:
            root = self.config.front_month_root
            instruments = self.cache.instruments(venue=SINOPAC_VENUE)
            front = _resolve_front_month(instruments, root, self.clock.timestamp_ns())
            if front is None:
                self.log.error(
                    f"No non-expired {root} futures found on {SINOPAC_VENUE} "
                    f"(checked {len(instruments)} loaded instruments)",
                )
                return None
            return front

        instrument = self.cache.instrument(self.config.instrument_id)
        if instrument is None:
            self.log.error(f"Could not find instrument for {self.config.instrument_id}")
        return instrument

    def on_quote_tick(self, quote: QuoteTick) -> None:
        """
        Submit the single scenario order on the first quote received.
        """
        if self._submitted:
            return
        self._submitted = True  # Guard before building so a failure does not loop

        if self.config.scenario == SCENARIO_BRACKET:
            # Bracket is an OrderList, not a single order, so handle it here
            # before the single-order path.  All three legs (entry LIMIT, SL
            # STOP_MARKET, TP LIMIT) carry emulation_trigger=LAST_PRICE, so all
            # are held by the OrderEmulator; SL/TP activate only after the entry
            # fills. Sinopac has no native conditional orders; no leg rests at the
            # venue natively.
            entry = self.instrument.make_price(float(quote.ask_price))
            bracket = self.order_factory.bracket(
                instrument_id=self.instrument_id,
                order_side=OrderSide.BUY,
                quantity=self.instrument.make_qty(2000),
                entry_price=entry,
                sl_trigger_price=self.instrument.make_price(float(entry) - 5.0),
                tp_price=self.instrument.make_price(float(entry) + 5.0),
                entry_order_type=OrderType.LIMIT,
                emulation_trigger=TriggerType.LAST_PRICE,
                time_in_force=TimeInForce.DAY,
            )
            self.log.info(f"Built bracket: {bracket}", LogColor.CYAN)
            if not self.config.dry_run:
                self.submit_order_list(bracket)
            return

        order = self._build_order(quote)
        if order is None:
            return

        self.order = order
        self.log.info(
            f"Built {self.config.scenario} order: {order!r} tags={order.tags}",
            LogColor.GREEN,
        )

        if self.config.dry_run:
            self.log.warning("Dry run, skipping submit")
            return

        self.submit_order(order)

    def _build_order(self, quote: QuoteTick) -> Order | None:
        """
        Build the single order for the active scenario.

        Parameters
        ----------
        quote : QuoteTick
            The latest quote, used to price the order at the bid.

        Returns
        -------
        Order or ``None``
            The scenario order, or ``None`` if the scenario name is unhandled.

        """
        instrument = self.instrument
        if instrument is None or self.instrument_id is None:
            self.log.error("No instrument loaded")
            return None
        instrument_id = self.instrument_id

        if self.config.scenario == SCENARIO_INTRADAY_ODD:
            # 37-share intraday odd lot, LIMIT @ bid, ROD. The adapter validates
            # LMT+ROD+1..999 shares+Cash locally before the gateway.
            return self.order_factory.limit(
                instrument_id=instrument_id,
                order_side=OrderSide.BUY,
                quantity=instrument.make_qty(Decimal(37)),
                price=quote.bid_price,
                time_in_force=TimeInForce.DAY,  # mapped to ROD by the adapter
                tags=[SinopacOrderTags(order_lot="IntradayOdd").value],
            )

        if self.config.scenario == SCENARIO_MKP:
            # MARKET_TO_LIMIT -> Shioaji MKP (range market). MKP is futures/options
            # only, so this targets the MXF front-month future. The adapter coerces
            # the default GTC TIF to IOC for marketable order types.
            return self.order_factory.market_to_limit(
                instrument_id=instrument_id,
                order_side=OrderSide.BUY,
                quantity=instrument.make_qty(Decimal(1)),  # 1 futures contract
            )

        if self.config.scenario == SCENARIO_FUTURES_OCTYPE:
            # 1x MXF front-month LIMIT @ bid tagged octype=Cover. With no open
            # position a Cover order is rejected by the venue; that terminal
            # rejection is the expected observable.
            return self.order_factory.limit(
                instrument_id=instrument_id,
                order_side=OrderSide.SELL,
                quantity=instrument.make_qty(Decimal(1)),
                price=quote.bid_price,
                time_in_force=TimeInForce.DAY,
                tags=[SinopacOrderTags(octype="Cover").value],
            )

        if self.config.scenario == SCENARIO_STOP_MARKET:
            # Emulated stop market: the OrderEmulator holds this order until the
            # last-trade price crosses the trigger, then releases a plain MARKET
            # order.  Set the trigger a tick above the ask so it can fire quickly
            # in sim (buy stop fires when price rises through the trigger).
            trigger = instrument.make_price(float(quote.ask_price) + 1.0)
            return self.order_factory.stop_market(
                instrument_id=instrument_id,
                order_side=OrderSide.BUY,
                quantity=instrument.make_qty(2000),
                trigger_price=trigger,
                trigger_type=TriggerType.LAST_PRICE,
                emulation_trigger=TriggerType.LAST_PRICE,
            )

        self.log.error(f"Unhandled scenario '{self.config.scenario}'")
        return None

    def on_order_event(self, event: OrderEvent) -> None:
        """
        Log every order event so the terminal state is observable in the log.
        """
        self.log.info(f"ORDER EVENT: {event!r}", LogColor.MAGENTA)

    def on_stop(self) -> None:
        """
        Cancel any resting scenario order and unsubscribe.
        """
        if self.instrument_id is None:
            return  # Instrument never resolved; nothing was subscribed or submitted
        if not self.config.dry_run:
            self.cancel_all_orders(self.instrument_id)
        self.unsubscribe_quote_ticks(self.instrument_id)


# --- Node assembly -------------------------------------------------------------


def _build_scenario_config(scenario: str, dry_run: bool) -> OrderSemanticsScenarioConfig:
    """
    Build the scenario-strategy config for a non-``common`` scenario.

    The futures scenarios (``mkp``, ``futures_octype``) trade the MXF front month,
    which is resolved dynamically at start because the gateway lists resolved month
    codes, not the ``C0`` lookup alias; their config carries ``front_month_root``
    instead of a fixed id (so no fixed ``external_order_claims`` either). The
    ``intraday_odd`` scenario trades a fixed stock id.

    Parameters
    ----------
    scenario : str
        One of ``intraday_odd``, ``mkp``, ``futures_octype``, ``stop_market``,
        ``bracket``.
    dry_run : bool
        Whether orders are built and logged but not submitted.

    Returns
    -------
    OrderSemanticsScenarioConfig

    """
    if scenario in (SCENARIO_MKP, SCENARIO_FUTURES_OCTYPE):
        return OrderSemanticsScenarioConfig(
            scenario=scenario,
            front_month_root=MXF_FUTURES_ROOT,
            dry_run=dry_run,
        )
    return OrderSemanticsScenarioConfig(
        scenario=scenario,
        instrument_id=STOCK_INSTRUMENT_ID,
        external_order_claims=[STOCK_INSTRUMENT_ID],
        dry_run=dry_run,
    )


def build_node(scenario: str) -> TradingNode:
    """
    Build the trading node and attach the strategy for the given scenario.

    Parameters
    ----------
    scenario : str
        One of ``SCENARIOS``.

    Returns
    -------
    TradingNode
        A built node ready to run.

    """
    config_node = TradingNodeConfig(
        trader_id=TraderId("TESTER-001"),
        logging=LoggingConfig(log_level="INFO", use_pyo3=True),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
        ),
        cache=CacheConfig(
            encoding="msgpack",
            timestamps_as_iso8601=True,
            buffer_interval_ms=100,
        ),
        data_clients={
            SINOPAC: SinopacDataClientConfig(
                gateway_host=GATEWAY_HOST,
                gateway_port=GATEWAY_PORT,
                instrument_provider=InstrumentProviderConfig(load_all=True),
            ),
        },
        exec_clients={
            SINOPAC: SinopacExecClientConfig(
                gateway_host=GATEWAY_HOST,
                gateway_port=GATEWAY_PORT,
                account_id=SINOPAC_ACCOUNT_ID,
                instrument_provider=InstrumentProviderConfig(load_all=True),
            ),
        },
        timeout_connection=30.0,
        timeout_reconciliation=20.0,
        timeout_portfolio=10.0,
        timeout_disconnection=5.0,
        timeout_post_stop=5.0,
    )

    node = TradingNode(config=config_node)
    node.add_data_client_factory(SINOPAC, SinopacLiveDataClientFactory)
    node.add_exec_client_factory(SINOPAC, SinopacLiveExecClientFactory)

    dry_run = _env_dry_run()

    if scenario == SCENARIO_COMMON:
        # Unchanged stock ExecTester behavior.
        config_tester = ExecTesterConfig(
            instrument_id=STOCK_INSTRUMENT_ID,
            external_order_claims=[STOCK_INSTRUMENT_ID],
            order_qty=TRADE_SIZE,
            tob_offset_ticks=OFFSET_TICKS,
            subscribe_quotes=True,
            subscribe_trades=True,
            enable_stop_buys=False,  # use the stop_market scenario for emulated stops
            enable_stop_sells=False,
            enable_brackets=False,  # use the bracket scenario for emulated bracket orders
            use_post_only=False,  # Not applicable to Taiwan exchange
            close_positions_time_in_force=TimeInForce.DAY,  # Taiwan uses ROD (rest of day)
            dry_run=dry_run,  # DRY_RUN_DEFAULT (False) unless SINOPAC_EXEC_DRY_RUN set
            log_data=True,
        )
        strategy: Strategy = ExecTester(config=config_tester)
    else:
        config_scenario = _build_scenario_config(scenario, dry_run)
        strategy = OrderSemanticsScenarioStrategy(config=config_scenario)

    node.trader.add_strategy(strategy)
    node.build()
    return node


def main() -> None:
    """
    Build and run the tester node for the resolved scenario.
    """
    scenario = _resolve_scenario()
    node = build_node(scenario)
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
