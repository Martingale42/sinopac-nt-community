"""Run order scenarios against the official server behind the simulation interlock.

Cases run one at a time in scenarios.toml order, each ending cancelled,
expired or closed before the next starts. Before a case's place, the live
accounts and the case's contract limits are queried, so the order carries
the full account object and prices derived from the day's limits; nothing
is queried between a case's steps, so step_wait_s is the only gap between
them. assert_simulation runs immediately before every place, update_price,
update_qty, cancel and close request, and an InterlockError is never
handled here: it ends the run before anything further is sent.

A request the server refuses with a 4xx is recorded, since the rejection
is itself corpus, and a rejected place skips the rest of its case. Any
other failure of a mutation, a 5xx or no response at all, leaves its
effect unknown and stops the run. Every accepted order is then polled
through trades until it ends or fill_timeout_s passes: a case expected to
fill has its fill closed at once with a marketable limit order the other
way, and a case not meant to fill must end with nothing filled. Whenever
a position may remain, the run logs CRITICAL and starts no further fill
case. orders.json summarises every case, without any account identifier,
and is rewritten after each one.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
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
from p1rec.config import PLACEHOLDER_RE, ConfigError, OrdersConfig, P1Config, TradeFields
from p1rec.interlock import EXIT_INTERLOCK_ABORT, InterlockError, assert_simulation
from p1rec.jsonpointer import JsonPointerError, resolve_pointer
from p1rec.order_status import STATUS_CANCELLED, STATUS_FAILED, STATUS_FILLED, normalize_status
from p1rec.prices import SHARES_PER_BOARD_LOT, format_decimal, resolve_price
from p1rec.records import EXIT_CHECK_FAILED
from p1rec.scenarios import EXCEED_FUNDS, Action, Case, Market, PriceRef, parse_step

logger = logging.getLogger(__name__)

EXIT_UNEXPECTED = 1

ORDERS_JSON = "orders.json"

_CLIENT_NAME = "orders"

# Queried before every case's place, beyond what p1.toml validation covers
_REQUIRED_OPS = ("accounts", "contracts_stock", "contracts_futures")

_TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class TradingSession:
    """A window of an exchange's trading day, in Asia/Taipei wall-clock time."""

    name: str
    open: time_of_day
    close: time_of_day


# The contract file's futures price limits hold for the TAIFEX day session
# only (docs/official-server-contract.md section 2). calibrate: confirm
# whether the day session ends with a closing call auction, which would
# end continuous trading, and so this window, before 13:45.
FUTURES_DAY_SESSION = TradingSession("TAIFEX day session", time_of_day(8, 45), time_of_day(13, 45))

# TWSE continuous trading, which ends when the closing call auction starts
# at 13:25, so a stock or odd-lot fill and its close both happen before a
# single auction price could decide them.
TWSE_CONTINUOUS_SESSION = TradingSession("TWSE continuous trading", time_of_day(9, 0), time_of_day(13, 25))

# ContractRequest.security_type per market (openapi.json SecurityType)
_SECURITY_TYPE: Mapping[Market, str] = {Market.STOCK: "STK", Market.ODD: "STK", Market.FUTURES: "FUT"}

# StockOrder.order_lot (openapi.json StockOrderLot), which makes an odd-lot
# order's quantity a number of shares rather than board lots.
_ORDER_LOT: Mapping[Market, str] = {Market.STOCK: "Common", Market.ODD: "IntradayOdd"}

# FuturesOrder.octype the futures API test sent and the server accepted,
# leaving open or cover to the broker, so a close offsets the open position.
_FUTURES_OCTYPE = "Auto"

# A close is a limit order at the far limit, so it matches any resting
# counter order, and ROD, so it waits for one rather than expiring.
_CLOSE_PRICE_TYPE = "LMT"
_CLOSE_ORDER_TYPE = "ROD"

# Prices a buy not meant to fill may use: far_below (limit_down for stock
# and odd lots, reference * 0.95 for futures), limit_down, or one tick
# above either, all far enough below the market to rest without a seller.
_RESTING_PRICE_REFS = frozenset(
    {PriceRef.FAR_BELOW, PriceRef.FAR_BELOW_PLUS_TICK, PriceRef.LIMIT_DOWN, PriceRef.LIMIT_DOWN_PLUS_TICK}
)


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
    UNKNOWN_OUTCOME = "unknown_outcome"
    INTERRUPTED = "interrupted"


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

# Outcomes whose case may have left an order or a position behind
_ATTENTION_OUTCOMES = frozenset(
    {Outcome.RESIDUAL_POSITION, Outcome.ORDER_MAY_REST, Outcome.UNKNOWN_OUTCOME, Outcome.INTERRUPTED}
)

# A case's outcome and the reason orders.json records for it
_Ending = tuple[Outcome, str | None]


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
    when a position or a resting order may remain, a case was refused
    outside its trading session, or a case could not be prepared; and
    EXIT_UNEXPECTED when a mutation's outcome is unknown, after a 5xx or no
    response, which stops the run. Raises ConfigError, before sending
    anything, if the cases break a rule in check_cases or only names an
    unknown case. InterlockError, and anything else that escapes a case,
    such as KeyboardInterrupt, propagates after a CRITICAL if that case
    may have left an order behind. orders.json is rewritten after every
    case and once more at the end, and a failure to write it never hides
    the exception that stopped the run. transport and clock are None for
    live use; tests pass an httpx.MockTransport and a fake clock.
    """
    selected = select_cases(cases, only)
    missing = [op for op in _REQUIRED_OPS if op not in cfg.endpoints]
    if missing:
        raise ConfigError(f"orders needs endpoints missing from p1.toml: {', '.join(missing)}")

    summary_path = run_dir / ORDERS_JSON
    records = [_CaseRecord(id=case.id) for case in selected]
    try:
        with CapturingClient(
            cfg.base_url, _CLIENT_NAME, run_dir / "rest.jsonl", cfg.http_timeout_s, transport=transport
        ) as client:
            runner = _Runner(client, cfg, clock or SystemClock())
            for case, record in zip(selected, records, strict=True):
                try:
                    runner.run_case(case, record)
                # Each outcome is recorded here, then the exception re-raised
                # so nothing further is sent.
                except InterlockError:
                    record.outcome = Outcome.INTERLOCK_ABORT
                    record.reason = "the interlock aborted before the next request"
                    if record.may_hold_order():
                        _log_residual(record, "the interlock aborted after an order was sent")
                    raise
                except _UnknownOutcome as exc:
                    record.outcome = Outcome.UNKNOWN_OUTCOME
                    record.reason = str(exc)
                    break
                except BaseException as exc:
                    record.outcome = Outcome.INTERRUPTED
                    record.reason = describe_exception(exc)
                    if record.may_hold_order():
                        _log_residual(record, "the run was interrupted after an order was sent")
                    raise
                _write_summary(summary_path, cfg, records)
    except BaseException as exc:
        _write_summary_or_log(summary_path, cfg, records)
        _log_run_summary(summary_path, records, _exit_code_for(exc))
        raise
    _write_summary(summary_path, cfg, records)

    if any(record.outcome is Outcome.UNKNOWN_OUTCOME for record in records):
        exit_code = EXIT_UNEXPECTED
    elif any(record.outcome in _FAILED_OUTCOMES for record in records):
        exit_code = EXIT_CHECK_FAILED
    else:
        exit_code = 0
    _log_run_summary(summary_path, records, exit_code)
    return exit_code


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
    """Raise ConfigError naming the case unless every case can run without leaving a position.

    A case that does not expect a fill must be a limit buy priced, at place
    and at every update_price, at far_below (limit_down for stock and odd
    lots, reference * 0.95 for futures), at limit_down, or one tick above
    either, so it rests without meeting a seller. If it is ROD it must end
    with cancel, since its order would otherwise still rest when the next
    case starts and the funds preflight counts one case at a time, while
    IOC and FOK orders expire by themselves. A case expected to fill must
    not update its quantity, since its close sends the quantity it placed.
    An exceed_funds quantity is sized in board lots at limit_down, so it
    needs a stock case priced at limit_down.
    """
    for case in cases:
        steps = [parse_step(raw) for raw in case.steps]
        if case.expect_fill:
            if any(step.verb == "update_qty" for step in steps):
                raise ConfigError(
                    f"case {case.id!r}: a case that expects a fill cannot update_qty, "
                    "since its close sends the quantity it placed"
                )
        else:
            refs = [case.price_ref, *(step.price_ref for step in steps if step.price_ref is not None)]
            if case.price_type != "LMT" or case.action != "Buy" or not set(refs) <= _RESTING_PRICE_REFS:
                raise ConfigError(
                    f"case {case.id!r}: a case that does not expect a fill must be an LMT Buy priced at "
                    f"{', '.join(sorted(_RESTING_PRICE_REFS))}, at place and at every update_price"
                )
            if case.order_type == "ROD" and case.steps[-1] != "cancel":
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
    Formula:    B = n * W + k * (T + P), where n is the number of steps, W is
                step_wait_s, T is fill_timeout_s, P is fill_poll_s, and k is
                2 for a case expected to fill and 1 otherwise.
    Domain:     W, P, T > 0 as p1.toml validation guarantees. Every step is
                followed by W; each wait for an order to end polls every P
                until T has passed, so it can overrun T by up to one P. A fill
                case waits for its order and then for its close; any other
                case waits once, to confirm its order ended unfilled.
    Returns:    Seconds.
    """
    waits = 2 if case.expect_fill else 1
    return len(case.steps) * orders.step_wait_s + waits * (orders.fill_timeout_s + orders.fill_poll_s)


def fits_session(start: datetime, budget_s: float, session: TradingSession) -> bool:
    """Return whether a case starting at start ends within session.

    Definition: Whether the interval [start, start + budget_s] lies inside
                session on start's Taipei date.
    Formula:    open <= start < close and start + budget_s <= close, in
                Asia/Taipei wall-clock time.
    Domain:     start is timezone-aware, in any zone; budget_s >= 0 from
                case_budget_s. Exchange holidays are not checked, since the
                exchange itself rejects orders on them.
    Returns:    True when the case may run.
    """
    local = start.astimezone(_TAIPEI)
    open_at = datetime.combine(local.date(), session.open, tzinfo=_TAIPEI)
    close_at = datetime.combine(local.date(), session.close, tzinfo=_TAIPEI)
    return open_at <= local < close_at and local + timedelta(seconds=budget_s) <= close_at


def session_for(case: Case) -> TradingSession | None:
    """Return the session case must run within, or None if it may run at any time.

    A futures order is priced from limits that hold in the day session
    only; a stock or odd-lot case expected to fill must also be closed
    while TWSE still trades continuously. A resting stock case is
    cancelled whenever it runs.
    """
    if case.market is Market.FUTURES:
        return FUTURES_DAY_SESSION
    if case.expect_fill:
        return TWSE_CONTINUOUS_SESSION
    return None


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
    """Return value as the int or float that json.dumps renders as value's fixed-point form.

    httpx serializes request bodies with json.dumps, which cannot take a
    Decimal and writes a float as its shortest repr, so an integral value
    becomes an int, and any other a float only when that repr is exactly
    format_decimal of value. Raises ValueError for a value whose repr
    would be inexact or in exponent notation, such as 0.00001 (1e-05).
    """
    if value == value.to_integral_value():
        return int(value)
    as_float = float(value)
    if repr(as_float) != format_decimal(value.normalize()):
        raise ValueError(f"{format_decimal(value)} has no exact fixed-point JSON rendering")
    return as_float


class _UnknownOutcome(Exception):
    """Raised when a mutation got a 5xx or no response, so its effect on the server is unknown."""


class _OrderState(StrEnum):
    PENDING = "pending"
    PARTLY_FILLED = "partly_filled"  # Not ended yet, with some quantity already filled
    FILLED = "filled"
    ENDED_UNFILLED = "ended_unfilled"
    ENDED_PARTIAL = "ended_partial"
    ENDED_UNKNOWN = "ended_unknown"
    TIMED_OUT = "timed_out"


@dataclass
class _StepRecord:
    step: str
    op: str
    status: int | None  # None until a response arrives, and for one that never does
    accepted: bool


@dataclass
class _CaseRecord:
    id: str
    trade_id: str | None = None
    close_trade_id: str | None = None
    steps: list[_StepRecord] = field(default_factory=list)
    outcome: Outcome = Outcome.NOT_RUN
    reason: str | None = None

    def may_hold_order(self) -> bool:
        """Return whether an order of this case may exist: one was accepted, or one got no answer."""
        return self.trade_id is not None or any(step.accepted or step.status is None for step in self.steps)


@dataclass(frozen=True)
class _Prepared:
    """Everything a case sends, derived before its place so no step can fail to price."""

    account: Mapping[str, object]
    quantity: int
    price: int | float
    step_prices: Mapping[PriceRef, int | float]
    close_price: int | float


class _Runner:
    """Run cases one at a time on one client, remembering whether a position may remain."""

    def __init__(self, client: CapturingClient, cfg: P1Config, clock: Clock) -> None:
        self._client = client
        self._cfg = cfg
        self._orders = cfg.orders
        self._trades = cfg.trades
        self._clock = clock

        # Set once a position may remain, after which no fill case starts
        self._residual = False

    def run_case(self, case: Case, record: _CaseRecord) -> None:
        """Run one case and set record's outcome and reason.

        Raises InterlockError, _UnknownOutcome, or whatever else interrupts
        the case, leaving record's outcome for the caller to set.
        """
        if case.expect_fill and self._residual:
            logger.error("case %s skipped: an earlier case may have left a position", case.id)
            record.outcome, record.reason = Outcome.SKIPPED_AFTER_RESIDUAL, "an earlier case may have left a position"
            return

        refusal = self._session_refusal(case)
        if refusal is None:
            prepared = self._prepare(case, record)
            if prepared is None:
                return

            # Preparation queries take time too, so the session is checked again before any mutation
            refusal = self._session_refusal(case)
            if refusal is None:
                record.outcome, record.reason = self._run_steps(case, record, prepared)
                return

        logger.error("case %s refused, no order sent: %s", case.id, refusal)
        record.outcome, record.reason = Outcome.REFUSED_OUTSIDE_SESSION, refusal

    def _session_refusal(self, case: Case) -> str | None:
        """Return why case cannot run now, or None if it has no session or fits in its session."""
        session = session_for(case)
        if session is None:
            return None
        budget_s = case_budget_s(case, self._orders)
        if fits_session(self._clock.now(), budget_s, session):
            return None
        return (
            f"it must start and end within {session.name}, {session.open:%H:%M}-{session.close:%H:%M} "
            f"Asia/Taipei, and it can take up to {budget_s:g} s"
        )

    def _prepare(self, case: Case, record: _CaseRecord) -> _Prepared | None:
        """Query the account and limits case needs and price every order it sends.

        Returns None, with record's outcome set to NOT_PREPARED, if a query
        or a price fails.
        """
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
            return _Prepared(
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
            record.outcome, record.reason = Outcome.NOT_PREPARED, describe_exception(exc)
            return None

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

    def _run_steps(self, case: Case, record: _CaseRecord, prepared: _Prepared) -> _Ending:
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

        # Recorded before the wait, so an interrupt during it still names the order
        if accepted:
            record.trade_id = self._trade_id(trade)
        self._clock.sleep(self._orders.step_wait_s)
        if not accepted:
            logger.warning("case %s: place rejected, remaining steps skipped", case.id)
            return Outcome.REJECTED, f"place rejected with HTTP {record.steps[-1].status}"

        trade_id = record.trade_id
        if trade_id is None:
            reason = f"place accepted but its response has no trade id at {self._trades.id!r}"
            if case.expect_fill:
                return self._residual_position(record, reason)
            return self._order_may_rest(record, reason)

        cancel_rejected = False
        for raw, step in zip(case.steps[1:], map(parse_step, case.steps[1:]), strict=True):
            op: str = step.verb
            step_values: dict[str, object] = {"trade_id": trade_id}
            if step.verb == "update_price":
                step_values["price"] = prepared.step_prices[cast(PriceRef, step.price_ref)]
            elif step.verb == "update_qty":
                # The reduction, as shioaji's update_order takes qty (inferred),
                # which for a 2-unit order reduced by 1 equals the remaining
                # quantity too, so the lifecycle case holds under either reading.
                step_values["quantity"] = step.reduce_by
            step_accepted, _ = self._mutate(case, record, raw, op, step_values)
            cancel_rejected = cancel_rejected or (op == "cancel" and not step_accepted)
            self._clock.sleep(self._orders.step_wait_s)

        if case.expect_fill:
            return self._settle_fill(case, record, prepared, trade_id)
        return self._confirm_unfilled(case, record, trade_id, prepared.account, cancel_rejected=cancel_rejected)

    def _confirm_unfilled(
        self, case: Case, record: _CaseRecord, trade_id: str, account: Mapping[str, object], *, cancel_rejected: bool
    ) -> _Ending:
        """Wait for the order of a case not meant to fill to end, and check that nothing filled."""
        state = self._wait_for_end(trade_id, account)
        after = "its cancel was rejected and " if cancel_rejected else ""
        if state is _OrderState.ENDED_UNFILLED:
            if cancel_rejected:
                logger.warning("case %s: cancel rejected, but order %s had already ended unfilled", case.id, trade_id)
                return Outcome.COMPLETED, "cancel rejected, but the order had already ended unfilled"
            return Outcome.COMPLETED, None
        if state is _OrderState.TIMED_OUT:
            return self._order_may_rest(record, f"{after}order {trade_id} did not end within fill_timeout_s")
        return self._residual_position(
            record, f"{after}order {trade_id}, not meant to fill, shows a fill ({state.value})"
        )

    def _settle_fill(self, case: Case, record: _CaseRecord, prepared: _Prepared, trade_id: str) -> _Ending:
        """Wait for a fill case's order to end, then close whatever it filled."""
        state = self._wait_for_end(trade_id, prepared.account)
        if state is _OrderState.ENDED_UNFILLED:
            logger.warning("case %s: order ended without a fill, nothing to close", case.id)
            return Outcome.ENDED_UNFILLED, f"order {trade_id} ended without a fill"
        if state is not _OrderState.FILLED:
            return self._residual_position(record, f"order {trade_id} did not fill completely ({state.value})")

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
            return self._residual_position(record, "close rejected")
        close_id = self._trade_id(trade)
        record.close_trade_id = close_id
        if close_id is None:
            return self._residual_position(record, "close accepted but its response has no trade id")

        state = self._wait_for_end(close_id, prepared.account)
        if state is not _OrderState.FILLED:
            return self._residual_position(record, f"close {close_id} did not fill ({state.value})")
        logger.info("case %s: filled and closed", case.id)
        return Outcome.CLOSED, None

    def _residual_position(self, record: _CaseRecord, reason: str) -> _Ending:
        _log_residual(record, reason)
        self._residual = True
        return Outcome.RESIDUAL_POSITION, reason

    def _order_may_rest(self, record: _CaseRecord, reason: str) -> _Ending:
        logger.error("case %s: %s; the order may still rest or have filled: %s", record.id, reason, _what_to_do(record))
        return Outcome.ORDER_MAY_REST, reason

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

        A 4xx is a rejection, and a 2xx is accepted unless the body's status
        is Failed. Raises InterlockError, sending nothing, if the interlock
        aborts, and _UnknownOutcome, after a CRITICAL, for any other status
        or no response, since the server may have acted on the request.
        """
        body = render_template(self._orders.body_templates[op], values)
        assert_simulation(self._client, self._cfg)

        # On record before sending, so a send that never returns still counts as sent
        sent = _StepRecord(step=step, op=op, status=None, accepted=False)
        record.steps.append(sent)
        try:
            response = self._client.request(op, self._cfg.endpoints[op], json_body=body)
        except httpx.RequestError as exc:
            reason = f"{step} got no response ({describe_exception(exc)}), so its outcome is unknown"
            _log_residual(record, reason)
            raise _UnknownOutcome(reason) from exc
        sent.status = response.status

        # A synchronous refusal is a 4xx (docs/official-server-contract.md
        # section 2), while a 5xx may come after the server already acted.
        if not (200 <= response.status < 300 or 400 <= response.status < 500):
            reason = f"{step} got HTTP {response.status}, so its outcome is unknown"
            _log_residual(record, reason)
            raise _UnknownOutcome(reason)

        accepted, parsed = _judge(response, self._trades)
        sent.accepted = accepted
        if accepted:
            logger.info("case %s: %s accepted (HTTP %d)", case.id, step, response.status)
        else:
            logger.warning("case %s: %s rejected (HTTP %d), recorded", case.id, step, response.status)
        return accepted, parsed

    def _trade_id(self, trade: object) -> str | None:
        """Return the non-empty string trade id a Trade carries at trades.id, or None."""
        value = _value_or_none(trade, self._trades.id)
        return value if isinstance(value, str) and value else None

    def _wait_for_end(self, trade_id: str, account: Mapping[str, object]) -> _OrderState:
        """Poll trades every fill_poll_s until the order fills or ends, or fill_timeout_s passes.

        At the deadline, returns TIMED_OUT if nothing had filled yet, or
        PARTLY_FILLED if some quantity had.
        """
        deadline = self._clock.monotonic() + self._orders.fill_timeout_s
        while True:
            self._clock.sleep(self._orders.fill_poll_s)
            state = self._poll(trade_id, account)
            if state not in (_OrderState.PENDING, _OrderState.PARTLY_FILLED):
                return state
            if self._clock.monotonic() >= deadline:
                return _OrderState.TIMED_OUT if state is _OrderState.PENDING else state

    def _poll(self, trade_id: str, account: Mapping[str, object]) -> _OrderState:
        """Query trades once and return the state of the order trade_id, PENDING if unknown."""
        body = {**account_request(account), "refresh": True}
        try:
            response = self._client.request("trades", self._cfg.endpoints["trades"], json_body=body)
        except httpx.RequestError as exc:
            logger.warning("trades poll for order %s got no response: %s", trade_id, describe_exception(exc))
            return _OrderState.PENDING
        try:
            trades = parse_json_exact("trades", response)
        except Exception as exc:
            # Broad on purpose, since a deeply nested body raises RecursionError
            logger.warning("trades poll for order %s failed: %s", trade_id, describe_exception(exc))
            return _OrderState.PENDING
        items = _value_or_none(trades, self._trades.list)
        if not isinstance(items, list):
            logger.warning("trades poll for order %s: response has no array at %r", trade_id, self._trades.list)
            return _OrderState.PENDING
        for item in items:
            if _value_or_none(item, self._trades.id) == trade_id:
                return _order_state(item, self._trades)
        return _OrderState.PENDING


def _judge(response: CapturedResponse, fields: TradeFields) -> tuple[bool, object]:
    """Return whether a 2xx or 4xx response to a mutation accepted it, and the parsed body.

    A 2xx is accepted unless the Trade in its body has the status Failed.
    A 2xx whose body cannot be parsed still counts as accepted, since the
    server took the request, and its body is returned as None.
    """
    if response.status >= 400:
        return False, None
    try:
        parsed = json.loads(response.body, parse_float=Decimal)
    except Exception:
        # Broad on purpose, since a deeply nested body raises RecursionError
        return True, None
    return _status(parsed, fields) != STATUS_FAILED, parsed


def _order_state(trade: object, fields: TradeFields) -> _OrderState:
    """Classify one trades element by its status and filled quantity.

    A Failed order was refused, so it holds nothing unless it reports a
    filled quantity. A Cancelled order holds nothing only when its filled
    quantity is known to be zero, since it may have filled in part before
    it was cancelled, so a missing quantity makes it ENDED_UNKNOWN. An
    order that has not ended yet but reports a filled quantity is
    PARTLY_FILLED.
    """
    status = _status(trade, fields)
    if status == STATUS_FILLED:
        return _OrderState.FILLED
    filled = _value_or_none(trade, fields.deal_quantity)
    known = isinstance(filled, int) and not isinstance(filled, bool)
    if status not in (STATUS_FAILED, STATUS_CANCELLED):
        return _OrderState.PARTLY_FILLED if known and filled else _OrderState.PENDING
    if known and filled:
        return _OrderState.ENDED_PARTIAL
    if known or (status == STATUS_FAILED and filled is None):
        return _OrderState.ENDED_UNFILLED
    return _OrderState.ENDED_UNKNOWN


def _status(trade: object, fields: TradeFields) -> str | None:
    """Return the normalized status a Trade carries, or None if it has no string status."""
    value = _value_or_none(trade, fields.status)
    return normalize_status(value) if isinstance(value, str) else None


def _what_to_do(record: _CaseRecord) -> str:
    return (
        f"check trades for trade_id {record.trade_id or 'unknown'}, close_trade_id "
        f"{record.close_trade_id or 'none'}; cancel or close it in the simulator before rerunning"
    )


def _log_residual(record: _CaseRecord, reason: str) -> None:
    """Log CRITICAL that record's case may have left a position, naming its orders and what to do."""
    logger.critical(
        "case %s: %s; possible residual position, check manually: %s", record.id, reason, _what_to_do(record)
    )


def _value_or_none(doc: object, pointer: str) -> object:
    try:
        return resolve_pointer(doc, pointer)
    except JsonPointerError:
        return None


def _write_summary(path: Path, cfg: P1Config, records: Sequence[_CaseRecord]) -> None:
    """Write orders.json atomically, so a reader never sees a half-written file.

    Holds the server and the [orders] timings the run used, then every
    selected case in order: its trade ids, each step's HTTP status and
    whether it was accepted, its outcome and the reason for it.
    """
    doc = {
        "base_url": cfg.base_url,
        "timings": {
            "step_wait_s": cfg.orders.step_wait_s,
            "fill_poll_s": cfg.orders.fill_poll_s,
            "fill_timeout_s": cfg.orders.fill_timeout_s,
        },
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
                "reason": record.reason,
            }
            for record in records
        ],
    }
    temporary = path.with_name(f"{path.name}.tmp")

    # Synced before and after the rename, since a power loss could
    # otherwise leave the new name pointing at unwritten blocks.
    with temporary.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_summary_or_log(path: Path, cfg: P1Config, records: Sequence[_CaseRecord]) -> None:
    """Write orders.json while another exception is propagating, logging a failure instead of raising it."""
    try:
        _write_summary(path, cfg, records)
    except OSError as exc:
        logger.error("could not write %s: %s", path, describe_exception(exc))


def _exit_code_for(exc: BaseException) -> int | None:
    """Return the exit code the CLI turns exc into, or None for one it does not catch, such as KeyboardInterrupt."""
    if isinstance(exc, InterlockError):
        return EXIT_INTERLOCK_ABORT
    return EXIT_UNEXPECTED if isinstance(exc, Exception) else None


def _log_run_summary(path: Path, records: Sequence[_CaseRecord], exit_code: int | None) -> None:
    """Log the run's exit code, its outcome counts, the cases needing attention and where orders.json is.

    A case needs attention when its outcome says an order or a position may
    remain, and so does one the interlock stopped after its order was sent.
    """
    counts = Counter(record.outcome.value for record in records)
    attention = [
        record.id
        for record in records
        if record.outcome in _ATTENTION_OUTCOMES
        or (record.outcome is Outcome.INTERLOCK_ABORT and record.may_hold_order())
    ]
    logger.info(
        "orders: exit code %s; %d cases: %s; needing attention: %s; summary in %s",
        "none, interrupted" if exit_code is None else exit_code,
        len(records),
        ", ".join(f"{count} {outcome}" for outcome, count in sorted(counts.items())),
        ", ".join(attention) or "none",
        path,
    )
