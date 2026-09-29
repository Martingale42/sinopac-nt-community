"""Run order scenarios against the official server behind the simulation interlock.

Cases run one at a time in scenarios.toml order, each ending cancelled,
expired or closed before the next starts. Before a case's place, the live
accounts and the case's contract limits are queried, so the order carries
the full account object and prices derived from the day's limits; nothing
is queried between a case's steps, so step_wait_s is the only gap between
them. assert_simulation runs immediately before every place, update_price,
update_qty, cancel and close request, and an InterlockError is never
handled here: it ends the run before anything further is sent.

A rejected request is recorded, since the rejection is itself corpus, and a
rejected place skips the rest of its case. A case expected to fill is
polled through trades until it fills, ends unfilled, or fill_timeout_s
passes, and a fill is closed at once with a marketable limit order the
other way. Whenever a position may remain, the run logs CRITICAL and starts
no further fill case. orders.json summarises every case, without any
account identifier.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from datetime import time as time_of_day
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from zoneinfo import ZoneInfo

import httpx

from p1rec.broker import (
    BrokerError,
    account_for_market,
    account_request,
    fetch_accounts,
    fetch_funds_value,
    fetch_limits,
    parse_json_exact,
)
from p1rec.capture import CapturedResponse, CapturingClient, describe_exception
from p1rec.config import PLACEHOLDER_RE, ConfigError, OrdersConfig, P1Config
from p1rec.interlock import InterlockError, assert_simulation
from p1rec.jsonpointer import JsonPointerError, resolve_pointer
from p1rec.prices import SHARES_PER_BOARD_LOT, format_decimal, resolve_price
from p1rec.records import EXIT_CHECK_FAILED
from p1rec.scenarios import EXCEED_FUNDS, Action, Case, Market, PriceRef, parse_step

logger = logging.getLogger(__name__)

EXIT_UNEXPECTED = 1

ORDERS_JSON = "orders.json"

_CLIENT_NAME = "orders"

# Queried before every case's place, beyond what p1.toml validation covers
_REQUIRED_OPS = ("accounts", "contracts_stock", "contracts_futures")

# TAIFEX day session in Taipei time, the only session the contract file's
# price limits hold for (docs/official-server-contract.md section 2).
_TAIPEI = ZoneInfo("Asia/Taipei")
_FUTURES_DAY_OPEN = time_of_day(8, 45)
_FUTURES_DAY_CLOSE = time_of_day(13, 45)

# ContractRequest.security_type per market (openapi.json SecurityType)
_SECURITY_TYPE: Mapping[Market, str] = {Market.STOCK: "STK", Market.ODD: "STK", Market.FUTURES: "FUT"}

# StockOrder.order_lot (openapi.json StockOrderLot), which makes an odd-lot
# order's quantity a number of shares rather than board lots.
_ORDER_LOT: Mapping[Market, str] = {Market.STOCK: "Common", Market.ODD: "IntradayOdd"}

# FuturesOrder.octype the futures API test sent and the server accepted,
# leaving open or cover to the broker, so a close offsets the open position.
_FUTURES_OCTYPE = "Auto"

# OrderStatusInfo.status values (openapi.json Status)
_STATUS_FILLED = "Filled"
_STATUS_FAILED = "Failed"
_ENDED_STATUSES = frozenset({"Failed", "Cancelled"})

# A close is a limit order at the far limit, so it matches any resting
# counter order, and ROD, so it waits for one rather than expiring.
_CLOSE_PRICE_TYPE = "LMT"
_CLOSE_ORDER_TYPE = "ROD"


class Outcome(StrEnum):
    """How one case ended, as orders.json records it."""

    NOT_RUN = "not_run"
    COMPLETED = "completed"
    REJECTED = "rejected"
    ENDED_UNFILLED = "ended_unfilled"
    CLOSED = "closed"
    RESIDUAL_POSITION = "residual_position"
    ORDER_MAY_REST = "order_may_rest"
    REFUSED_OUTSIDE_SESSION = "refused_outside_session"
    NOT_PREPARED = "not_prepared"
    SKIPPED_AFTER_RESIDUAL = "skipped_after_residual"
    INTERLOCK_ABORT = "interlock_abort"
    NO_RESPONSE = "no_response"


# Outcomes that make the run exit EXIT_CHECK_FAILED
_FAILED_OUTCOMES = frozenset(
    {
        Outcome.RESIDUAL_POSITION,
        Outcome.ORDER_MAY_REST,
        Outcome.REFUSED_OUTSIDE_SESSION,
        Outcome.NOT_PREPARED,
        Outcome.SKIPPED_AFTER_RESIDUAL,
    }
)


class Clock(Protocol):
    """Wall time, monotonic time and sleeping, injectable so tests never wait."""

    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def run_orders(
    cfg: P1Config,
    cases: Sequence[Case],
    run_dir: Path,
    only: set[str] | None,
    *,
    transport: httpx.BaseTransport | None = None,
    clock: Clock | None = None,
) -> int:
    """Run the selected cases in order and return the run's exit code.

    Returns 0 when every case ran, rejections included; EXIT_CHECK_FAILED
    when a position or a resting order may remain, a futures case was
    refused outside the day session, or a case could not be prepared; and
    EXIT_UNEXPECTED when a mutation got no response, which stops the run.
    Raises ConfigError, before sending anything, if the cases break a rule
    in check_cases or only names an unknown case, and lets InterlockError
    propagate once the interlock aborts. orders.json is written whenever a
    request may have been sent. transport and clock are None for live use;
    tests pass an httpx.MockTransport and a fake clock.
    """
    selected = select_cases(cases, only)
    missing = [op for op in _REQUIRED_OPS if op not in cfg.endpoints]
    if missing:
        raise ConfigError(f"orders needs endpoints missing from p1.toml: {', '.join(missing)}")

    records = [_CaseRecord(id=case.id) for case in selected]
    try:
        with CapturingClient(
            cfg.base_url, _CLIENT_NAME, run_dir / "rest.jsonl", cfg.http_timeout_s, transport=transport
        ) as client:
            runner = _Runner(client, cfg, clock or SystemClock())
            for case, record in zip(selected, records, strict=True):
                try:
                    runner.run_case(case, record)
                except InterlockError:
                    # Recorded here, then re-raised so nothing further is sent
                    record.outcome = Outcome.INTERLOCK_ABORT
                    if record.trade_id is not None:
                        logger.critical(
                            "case %s: interlock aborted after order %s was sent; possible residual order or "
                            "position, check manually",
                            case.id,
                            record.trade_id,
                        )
                    raise
                except _NoResponse:
                    record.outcome = Outcome.NO_RESPONSE
                    break
    finally:
        _write_summary(run_dir / ORDERS_JSON, records)

    if any(record.outcome is Outcome.NO_RESPONSE for record in records):
        return EXIT_UNEXPECTED
    if any(record.outcome in _FAILED_OUTCOMES for record in records):
        return EXIT_CHECK_FAILED
    return 0


def select_cases(cases: Sequence[Case], only: Collection[str] | None) -> list[Case]:
    """Return the cases to run, in file order, after checking every case with check_cases.

    Raises ConfigError if only is empty or names an id no case has.
    """
    check_cases(cases)
    if only is None:
        return list(cases)
    if not only:
        raise ConfigError("--only names no case")
    unknown = sorted(set(only) - {case.id for case in cases})
    if unknown:
        raise ConfigError(f"--only names unknown case ids: {', '.join(unknown)}")
    return [case for case in cases if case.id in only]


def check_cases(cases: Sequence[Case]) -> None:
    """Raise ConfigError naming the case unless every case can run on its own.

    A ROD case that does not expect a fill must end with cancel, since its
    order would otherwise still rest when the next case starts and the
    funds preflight counts one case at a time; IOC and FOK orders expire by
    themselves. An exceed_funds quantity is sized in board lots at
    limit_down, so it needs a stock case priced at limit_down.
    """
    for case in cases:
        if not case.expect_fill and case.order_type == "ROD" and case.steps[-1] != "cancel":
            raise ConfigError(
                f"case {case.id!r}: a ROD case that does not expect a fill must end with 'cancel', "
                "so its order never rests into the next case"
            )
        if case.quantity == EXCEED_FUNDS and (
            case.market is not Market.STOCK or case.price_ref is not PriceRef.LIMIT_DOWN
        ):
            raise ConfigError(
                f"case {case.id!r}: quantity {EXCEED_FUNDS!r} needs market 'stock' and price_ref 'limit_down'"
            )


def exceed_funds_lots(max_available_twd: Decimal, limit_down: Decimal) -> int:
    """Return how many board lots a buy at limit_down needs to cost more than the funds available.

    Definition: The fewest whole board lots whose value at limit_down is at
                least the largest available-funds figure, plus one lot.
    Formula:    lots = ceil(A / (1000 * P)) + 1, where A is the largest
                available stock funds figure and P is limit_down.
    Domain:     A >= 0 in TWD, the maximum over every stock funds source, so
                the order exceeds whichever one the simulator checks;
                P > 0 in TWD per share. Raises ValueError otherwise.
    Returns:    Board lots, at least 1; the order's value lots * 1000 * P
                exceeds A by at least one lot's value.
    """
    if max_available_twd < 0 or limit_down <= 0:
        raise ValueError(
            f"exceed_funds needs available funds >= 0 and limit_down > 0, got "
            f"{format_decimal(max_available_twd)} and {format_decimal(limit_down)}"
        )
    covering = (max_available_twd / (SHARES_PER_BOARD_LOT * limit_down)).to_integral_value(rounding=ROUND_CEILING)
    return int(covering) + 1


def case_budget_s(case: Case, orders: OrdersConfig) -> float:
    """Return the longest a case can take from its place to its last request.

    Definition: An upper bound on a case's duration, excluding HTTP round trips.
    Formula:    B = n * W + (2 * (T + P) if expect_fill else 0), where n is
                the number of steps, W is step_wait_s, T is fill_timeout_s
                and P is fill_poll_s.
    Domain:     W, P, T > 0 as p1.toml validation guarantees. Every step is
                followed by W; a fill wait polls every P until T has passed,
                so it can overrun T by up to one P, and a fill case waits
                twice, once for its order and once for its close.
    Returns:    Seconds.
    """
    budget = len(case.steps) * orders.step_wait_s
    if case.expect_fill:
        budget += 2 * (orders.fill_timeout_s + orders.fill_poll_s)
    return budget


def fits_futures_day_session(start: datetime, budget_s: float) -> bool:
    """Return whether a futures case starting at start ends within the TAIFEX day session.

    Definition: Whether the interval [start, start + budget_s] lies inside
                the day session on start's Taipei date.
    Formula:    open <= start < close and start + budget_s <= close, with
                open 08:45 and close 13:45 Asia/Taipei.
    Domain:     start is timezone-aware; budget_s >= 0 from case_budget_s.
                Exchange holidays are not checked, since the exchange itself
                rejects orders on them.
    Returns:    True when the case may run.
    """
    local = start.astimezone(_TAIPEI)
    open_at = datetime.combine(local.date(), _FUTURES_DAY_OPEN, tzinfo=_TAIPEI)
    close_at = datetime.combine(local.date(), _FUTURES_DAY_CLOSE, tzinfo=_TAIPEI)
    return open_at <= local < close_at and local + timedelta(seconds=budget_s) <= close_at


def render_template(template: str, values: Mapping[str, object]) -> object:
    """Parse a JSON body template and replace every "{name}" string value with values[name].

    Only a string value that is exactly one placeholder is replaced, and by
    the typed value itself, so a number stays a JSON number and the account
    stays an object. Raises KeyError for a placeholder values lacks.
    """

    def substitute(node: object) -> object:
        if isinstance(node, dict):
            return {key: substitute(child) for key, child in node.items()}
        if isinstance(node, list):
            return [substitute(child) for child in node]
        if isinstance(node, str) and (match := PLACEHOLDER_RE.fullmatch(node)):
            return values[match.group(1)]
        return node

    return substitute(json.loads(template))


def json_number(value: Decimal) -> int | float:
    """Return value as the int or float that json.dumps renders as exactly the same number.

    httpx serializes request bodies with json.dumps, which cannot take a
    Decimal, so an integral value becomes an int, and any other a float
    whose shortest representation is checked to read back as value. Raises
    ValueError if no float does.
    """
    if value == value.to_integral_value():
        return int(value)
    as_float = float(value)
    if Decimal(repr(as_float)) != value:
        raise ValueError(f"{format_decimal(value)} has no exact JSON number rendering")
    return as_float


class _NoResponse(Exception):
    """Raised when a mutation got no response, so its effect on the server is unknown."""


class _OrderState(StrEnum):
    PENDING = "pending"
    FILLED = "filled"
    ENDED_UNFILLED = "ended_unfilled"
    ENDED_PARTIAL = "ended_partial"
    TIMED_OUT = "timed_out"


@dataclass
class _StepRecord:
    step: str
    op: str
    status: int | None  # None when the request got no response
    accepted: bool


@dataclass
class _CaseRecord:
    id: str
    trade_id: str | None = None
    close_trade_id: str | None = None
    steps: list[_StepRecord] = field(default_factory=list)
    outcome: Outcome = Outcome.NOT_RUN


@dataclass(frozen=True)
class _Prepared:
    """Everything a case sends, derived before its place so no step can fail to price."""

    account: Mapping[str, object]
    quantity: int
    price: int | float
    step_prices: Mapping[PriceRef, int | float]
    close_price: int | float


class _Runner:
    def __init__(self, client: CapturingClient, cfg: P1Config, clock: Clock) -> None:
        self._client = client
        self._cfg = cfg
        self._orders = cfg.orders
        self._clock = clock

        # Set once a position may remain, after which no fill case starts
        self._residual = False

    def run_case(self, case: Case, record: _CaseRecord) -> None:
        if case.expect_fill and self._residual:
            logger.error("case %s skipped: an earlier case may have left a position", case.id)
            record.outcome = Outcome.SKIPPED_AFTER_RESIDUAL
            return

        if case.market is Market.FUTURES:
            budget_s = case_budget_s(case, self._orders)
            if not fits_futures_day_session(self._clock.now(), budget_s):
                logger.error(
                    "case %s refused, nothing sent: a futures case must start and end within the TAIFEX day "
                    "session 08:45-13:45 Asia/Taipei, and this one can take up to %g s",
                    case.id,
                    budget_s,
                )
                record.outcome = Outcome.REFUSED_OUTSIDE_SESSION
                return

        prepared = self._prepare(case)
        if prepared is None:
            record.outcome = Outcome.NOT_PREPARED
            return

        record.outcome = self._run_steps(case, record, prepared)

    def _prepare(self, case: Case) -> _Prepared | None:
        """Query the account and limits case needs and price every order it sends, or return None."""
        is_etf = case.code in self._cfg.contracts.etf_codes
        try:
            account = account_for_market(fetch_accounts(self._client, self._cfg), case.market)
            limits = fetch_limits(self._client, self._cfg, [case])[case.code]
            if case.quantity == EXCEED_FUNDS:
                available = self._max_stock_funds(case, account)
                quantity = exceed_funds_lots(available, limits.limit_down)
            else:
                quantity = case.quantity
            step_refs = [step.price_ref for step in map(parse_step, case.steps) if step.price_ref is not None]
            prepared = _Prepared(
                account=account,
                quantity=quantity,
                price=json_number(resolve_price(case.price_ref, limits, case.market, is_etf=is_etf)),
                step_prices={
                    ref: json_number(resolve_price(ref, limits, case.market, is_etf=is_etf)) for ref in step_refs
                },
                close_price=json_number(limits.limit_down if case.action == "Buy" else limits.limit_up),
            )
        except (BrokerError, ValueError, httpx.RequestError) as exc:
            logger.error("case %s not prepared, nothing sent: %s", case.id, describe_exception(exc))
            return None
        return prepared

    def _max_stock_funds(self, case: Case, account: Mapping[str, object]) -> Decimal:
        """Return the largest figure across every stock funds source, raising BrokerError if one fails."""
        funds = self._cfg.funds
        sources = {funds.stock_available_op: funds.stock_available_pointer, **funds.stock_report_only}
        figures = {
            op: fetch_funds_value(self._client, self._cfg, op, pointer, account) for op, pointer in sources.items()
        }
        largest_op = max(figures, key=lambda op: figures[op])
        logger.info(
            "case %s: exceed_funds sized from %s, the largest stock funds figure at %s TWD",
            case.id,
            largest_op,
            format_decimal(figures[largest_op]),
        )
        return figures[largest_op]

    def _run_steps(self, case: Case, record: _CaseRecord, prepared: _Prepared) -> Outcome:
        place_op = "place_futures" if case.market is Market.FUTURES else "place_stock"
        values = self._order_values(
            case,
            prepared,
            action=case.action,
            price=prepared.price,
            price_type=case.price_type,
            order_type=case.order_type,
            custom_field=case.custom_field,
        )
        accepted, trade = self._mutate(case, record, "place", place_op, values)
        self._clock.sleep(self._orders.step_wait_s)
        if not accepted:
            logger.warning("case %s: place rejected, remaining steps skipped", case.id)
            return Outcome.REJECTED

        trade_id = self._trade_id(trade)
        record.trade_id = trade_id
        later_steps = [parse_step(raw) for raw in case.steps[1:]]
        if trade_id is None:
            reason = f"place accepted but its response has no trade id at {self._orders.trade_id_pointer!r}"
            if case.expect_fill:
                return self._residual_position(case, reason)
            if later_steps:
                logger.error("case %s: %s; the order may still rest, check manually", case.id, reason)
                return Outcome.ORDER_MAY_REST
            return Outcome.COMPLETED

        cancel_rejected = False
        for raw, step in zip(case.steps[1:], later_steps, strict=True):
            step_values: dict[str, object]
            if step.verb == "update_price":
                price = prepared.step_prices[cast(PriceRef, step.price_ref)]
                op, step_values = "update_price", {"trade_id": trade_id, "price": price}
            elif step.verb == "update_qty":
                # The reduction, as shioaji's update_order takes qty (inferred),
                # which for a 2-unit order reduced by 1 equals the remaining
                # quantity too, so the lifecycle case holds under either reading.
                op, step_values = "update_qty", {"trade_id": trade_id, "quantity": step.reduce_by}
            else:
                op, step_values = "cancel", {"trade_id": trade_id}
            step_accepted, _ = self._mutate(case, record, raw, op, step_values)
            cancel_rejected = cancel_rejected or (op == "cancel" and not step_accepted)
            self._clock.sleep(self._orders.step_wait_s)

        if cancel_rejected:
            return self._after_rejected_cancel(case, trade_id, prepared.account)
        if case.expect_fill:
            return self._settle_fill(case, record, prepared, trade_id)
        return Outcome.COMPLETED

    def _settle_fill(self, case: Case, record: _CaseRecord, prepared: _Prepared, trade_id: str) -> Outcome:
        """Wait for a fill case's order to end, then close whatever it filled."""
        state = self._wait_for_end(trade_id, prepared.account)
        if state is _OrderState.ENDED_UNFILLED:
            logger.warning("case %s: order ended without a fill, nothing to close", case.id)
            return Outcome.ENDED_UNFILLED
        if state is not _OrderState.FILLED:
            return self._residual_position(case, f"order {trade_id} did not fill completely ({state.value})")

        place_op = "place_futures" if case.market is Market.FUTURES else "place_stock"
        close_action: Action = "Sell" if case.action == "Buy" else "Buy"
        values = self._order_values(
            case,
            prepared,
            action=close_action,
            price=prepared.close_price,
            price_type=_CLOSE_PRICE_TYPE,
            order_type=_CLOSE_ORDER_TYPE,
            custom_field=None,
        )
        accepted, trade = self._mutate(case, record, "close", place_op, values)
        if not accepted:
            return self._residual_position(case, "close rejected")
        close_id = self._trade_id(trade)
        record.close_trade_id = close_id
        if close_id is None:
            return self._residual_position(case, "close accepted but its response has no trade id")

        state = self._wait_for_end(close_id, prepared.account)
        if state is not _OrderState.FILLED:
            return self._residual_position(case, f"close {close_id} did not fill ({state.value})")
        logger.info("case %s: filled and closed", case.id)
        return Outcome.CLOSED

    def _after_rejected_cancel(self, case: Case, trade_id: str, account: Mapping[str, object]) -> Outcome:
        """Classify a case whose cancel was rejected from one trades query after its last step."""
        state = self._poll(trade_id, account)
        if state is _OrderState.ENDED_UNFILLED:
            logger.warning("case %s: cancel rejected, but order %s had already ended unfilled", case.id, trade_id)
            return Outcome.COMPLETED
        if state in (_OrderState.FILLED, _OrderState.ENDED_PARTIAL):
            return self._residual_position(case, f"cancel rejected and order {trade_id} has filled")
        logger.error("case %s: cancel rejected and order %s may still rest, check manually", case.id, trade_id)
        return Outcome.ORDER_MAY_REST

    def _residual_position(self, case: Case, reason: str) -> Outcome:
        logger.critical("case %s: %s; possible residual position, check manually", case.id, reason)
        self._residual = True
        return Outcome.RESIDUAL_POSITION

    def _order_values(
        self,
        case: Case,
        prepared: _Prepared,
        *,
        action: Action,
        price: int | float,
        price_type: str,
        order_type: str,
        custom_field: str | None,
    ) -> dict[str, object]:
        """Return every value a place template may name, for one order of case."""
        return {
            "code": case.code,
            "exchange": self._orders.exchange[case.market.value],
            "security_type": _SECURITY_TYPE[case.market],
            "action": action,
            "price": price,
            "quantity": prepared.quantity,
            "price_type": price_type,
            "order_type": order_type,
            "order_lot": _ORDER_LOT.get(case.market),
            "octype": _FUTURES_OCTYPE if case.market is Market.FUTURES else None,
            "custom_field": custom_field,
            "account": dict(prepared.account),
        }

    def _mutate(
        self, case: Case, record: _CaseRecord, step: str, op: str, values: Mapping[str, object]
    ) -> tuple[bool, object]:
        """Send one mutation behind the interlock, record it, and return (accepted, parsed body).

        A request is accepted when it gets a 2xx whose status, if the body
        has one, is not Failed. Raises InterlockError, sending nothing, if
        the interlock aborts, and _NoResponse if the request got no response.
        """
        body = render_template(self._orders.body_templates[op], values)
        assert_simulation(self._client, self._cfg)
        try:
            response = self._client.request(op, self._cfg.endpoints[op], json_body=body)
        except httpx.RequestError as exc:
            record.steps.append(_StepRecord(step=step, op=op, status=None, accepted=False))
            logger.critical(
                "case %s: %s got no response (%s); the order's state is unknown, possible residual order or "
                "position, check manually",
                case.id,
                step,
                describe_exception(exc),
            )
            raise _NoResponse from exc

        accepted, parsed = self._judge(response)
        record.steps.append(_StepRecord(step=step, op=op, status=response.status, accepted=accepted))
        if accepted:
            logger.info("case %s: %s accepted (HTTP %d)", case.id, step, response.status)
        else:
            logger.warning("case %s: %s rejected (HTTP %d), recorded", case.id, step, response.status)
        return accepted, parsed

    def _judge(self, response: CapturedResponse) -> tuple[bool, object]:
        if not 200 <= response.status < 300:
            return False, None
        try:
            parsed = json.loads(response.body, parse_float=Decimal)
        except ValueError:
            return True, None
        return _value_or_none(parsed, self._orders.filled_pointer) != _STATUS_FAILED, parsed

    def _trade_id(self, trade: object) -> str | None:
        value = _value_or_none(trade, self._orders.trade_id_pointer)
        return value if isinstance(value, str) and value else None

    def _wait_for_end(self, trade_id: str, account: Mapping[str, object]) -> _OrderState:
        """Poll trades every fill_poll_s until the order fills or ends, or fill_timeout_s passes."""
        deadline = self._clock.monotonic() + self._orders.fill_timeout_s
        while True:
            self._clock.sleep(self._orders.fill_poll_s)
            state = self._poll(trade_id, account)
            if state is not _OrderState.PENDING:
                return state
            if self._clock.monotonic() >= deadline:
                return _OrderState.TIMED_OUT

    def _poll(self, trade_id: str, account: Mapping[str, object]) -> _OrderState:
        """Query trades once and return the state of the order trade_id, PENDING if unknown."""
        body = {**account_request(account), "refresh": True}
        try:
            response = self._client.request("trades", self._cfg.endpoints["trades"], json_body=body)
            trades = parse_json_exact("trades", response)
        except (BrokerError, httpx.RequestError) as exc:
            logger.warning("trades poll for order %s failed: %s", trade_id, describe_exception(exc))
            return _OrderState.PENDING
        if not isinstance(trades, list):
            logger.warning("trades poll for order %s: response is not an array", trade_id)
            return _OrderState.PENDING
        for item in trades:
            if _value_or_none(item, self._orders.trade_id_pointer) == trade_id:
                return _order_state(item, self._orders)
        return _OrderState.PENDING


def _order_state(trade: object, orders: OrdersConfig) -> _OrderState:
    """Classify one trades element by its status and filled quantity.

    An order that ended as Failed or Cancelled holds no position only when
    its filled quantity is known to be zero; an unknown quantity keeps it
    pending, so an order is never taken as flat on missing evidence.
    """
    status = _value_or_none(trade, orders.filled_pointer)
    if status == _STATUS_FILLED:
        return _OrderState.FILLED
    if status in _ENDED_STATUSES:
        filled = _value_or_none(trade, orders.deal_quantity_pointer)
        if isinstance(filled, int) and not isinstance(filled, bool):
            return _OrderState.ENDED_UNFILLED if filled == 0 else _OrderState.ENDED_PARTIAL
    return _OrderState.PENDING


def _value_or_none(doc: object, pointer: str) -> object:
    try:
        return resolve_pointer(doc, pointer)
    except JsonPointerError:
        return None


def _write_summary(path: Path, records: Sequence[_CaseRecord]) -> None:
    doc = {
        "cases": [
            {
                "id": record.id,
                "trade_id": record.trade_id,
                "close_trade_id": record.close_trade_id,
                "steps": [
                    {"step": step.step, "op": step.op, "status": step.status, "accepted": step.accepted}
                    for step in record.steps
                ],
                "outcome": record.outcome.value,
            }
            for record in records
        ]
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
