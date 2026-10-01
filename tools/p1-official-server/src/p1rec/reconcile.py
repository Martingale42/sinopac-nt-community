"""Reconcile the order reports every SSE client received against the REST trades list.

Official order events carry no status string, so each order's final
status on the SSE side is derived from its operations and deals by
derive_status. The REST side is the union of the trades lists of every
account type trades.account_types names, taken from an earlier run's
rest.jsonl or queried live after the simulation-mode check.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import httpx

from p1rec.broker import BrokerError, account_request, fetch_accounts
from p1rec.capture import CapturingClient
from p1rec.config import ACCOUNTS_OP, EventFields, P1Config, TradeFields
from p1rec.interlock import assert_simulation
from p1rec.jsonpointer import JsonPointerError, resolve_pointer
from p1rec.order_status import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_FILLED,
    STATUS_PART_FILLED,
    STATUS_SUBMITTED,
    normalize_status,
)
from p1rec.records import (
    EXIT_ANALYSIS_ERROR,
    EXIT_CHECK_FAILED,
    EXIT_OK,
    NS_PER_S,
    AnalysisError,
    discover_sse_files,
    epoch_s_to_ns,
    exchange_tz,
    iter_frames,
    read_jsonl,
    resolve_run_dir,
    taipei_date,
    to_decimal,
    write_json,
)
from p1rec.sse import SseFrame

logger = logging.getLogger(__name__)

# The OpenAPI OpType values derive_status reads: New places the order,
# UpdateQty and Cancel each remove quantity from it, and UpdatePrice
# changes nothing it reads.
_OP_NEW = "New"
_OP_UPDATE_QTY = "UpdateQty"
_OP_CANCEL = "Cancel"
_OP_UPDATE_PRICE = "UpdatePrice"

# The status derive_status gives an order whose events cannot place it,
# which is no value of the OpenAPI Status enum and so never matches REST.
STATUS_UNKNOWN = "Unknown"

# Each trades request names one account, since without one trades lists
# the stock account's orders alone, and spells out refresh so REST is
# brought up to date before it is compared.
_TRADES_OP = "trades"
_TRADES_REFRESH: dict[str, object] = {"refresh": True}

# TAIFEX's night session runs from 15:00 to 05:00 the next day, Taipei
# time, and its orders belong to the next TAIFEX trading day rather than
# to their calendar date.
_NIGHT_SESSION_START = time(15, 0)
_NIGHT_SESSION_END = time(5, 0)


@dataclass(frozen=True)
class OrderView:
    """One order's status and cumulative filled quantity, as one side reports it."""

    key: str
    status: str
    filled_qty: Decimal


@dataclass(frozen=True)
class Mismatch:
    """An order whose status or filled quantity differs between one client and REST."""

    client: str
    sse: OrderView
    rest: OrderView


@dataclass(frozen=True)
class ClientOrders:
    """What one client's order event reports say about each order it saw."""

    orders: dict[str, OrderView]

    # Identities of the reports behind orders: the event_id, or a digest of
    # the data text for a report without one.
    event_ids: frozenset[str]

    # Orders left out because they fall on another calendar date
    excluded_other_days: int = 0


@dataclass(frozen=True)
class RestOrders:
    """The orders a trades response lists."""

    orders: dict[str, OrderView]

    # Orders whose order time falls on another calendar date
    excluded_other_days: int = 0


@dataclass(frozen=True)
class ReconcileReport:
    """How the orders every client saw on SSE compare with the REST trades list."""

    # How many distinct orders REST lists, and each client saw, on the day compared
    rest_order_count: int
    sse_order_count_by_client: dict[str, int]

    # REST orders at least one client missed, and orders some client saw
    # that REST does not list.
    only_in_rest: list[str]
    only_in_sse: list[str]

    mismatched: list[Mismatch]

    # REST orders each client missed, and the order keys each client saw
    missing_by_client: dict[str, list[str]]
    per_client_keys: dict[str, list[str]]

    # Report identities some client saw and this one did not
    missing_events_by_client: dict[str, list[str]]

    # Orders each client, and REST, listed on other calendar dates, left
    # out of the comparison.
    excluded_other_days: dict[str, int]
    rest_excluded_other_days: int

    @property
    def clean(self) -> bool:
        """Return whether REST lists an order, every client saw every report, and SSE and REST agree on every order.

        With no REST order nothing was compared, so a server that lost the
        day's orders, and clients that never saw them, cannot pass.
        """
        missed_events = any(self.missing_events_by_client.values())
        compared = self.rest_order_count > 0
        return compared and not (self.only_in_rest or self.only_in_sse or self.mismatched or missed_events)

    def to_json(self) -> dict[str, object]:
        """Return the report as the reconcile.json document, quantities as decimal strings."""
        return {
            "clean": self.clean,
            "rest_order_count": self.rest_order_count,
            "sse_order_count_by_client": self.sse_order_count_by_client,
            "only_in_rest": self.only_in_rest,
            "only_in_sse": self.only_in_sse,
            "mismatched": [
                {
                    "client": item.client,
                    "key": item.sse.key,
                    "sse": {"status": item.sse.status, "filled_qty": str(item.sse.filled_qty)},
                    "rest": {"status": item.rest.status, "filled_qty": str(item.rest.filled_qty)},
                }
                for item in self.mismatched
            ],
            "missing_by_client": self.missing_by_client,
            "missing_events_by_client": self.missing_events_by_client,
            "per_client_keys": self.per_client_keys,
            "excluded_other_days": self.excluded_other_days,
            "rest_excluded_other_days": self.rest_excluded_other_days,
        }


def _pointer_str(doc: object, pointer: str) -> str:
    value = resolve_pointer(doc, pointer)
    if not isinstance(value, str):
        raise ValueError(f"value at {pointer!r} is {type(value).__name__}, expected a string")
    return value


def _optional_quantity(doc: object, pointer: str) -> Decimal | None:
    try:
        value = resolve_pointer(doc, pointer)
    except JsonPointerError:
        return None
    return None if value is None else to_decimal(value)


def derive_status(
    *,
    new_ok: bool | None,
    cancel_ok: bool,
    order_qty: Decimal | None,
    reduced_qty: Decimal,
    cancel_qty: Decimal,
    filled_qty: Decimal,
) -> str:
    """Return the final status an order's recorded operations and deals imply.

    Definition: The status of one order derived from its order and deal
                events, since the official order events carry no status
                string.
    Formula:    The first rule that holds wins:
                Failed      if a New operation was rejected;
                Unknown     if no accepted New was seen, or order_qty is None;
                Filled      if filled_qty > 0 and filled_qty >= order_qty - reduced_qty;
                Cancelled   if cancel_ok and cancel_qty + filled_qty >= order_qty;
                PartFilled  if filled_qty > 0;
                Submitted   otherwise.
    Domain:     new_ok is None when no New event was seen, else whether it
                was accepted; cancel_ok is whether any Cancel was
                accepted; order_qty is the placed size, the accepted New's
                order.quantity, None if it reported none above 0;
                reduced_qty is the sum of cancel_quantity over accepted
                UpdateQty operations, and cancel_qty the same sum over
                accepted UpdateQty and Cancel operations, since each
                operation's cancel_quantity is what that operation alone
                removed; filled_qty is the sum of deal quantities.
                Quantities are non-negative and in the events' own units;
                an overfill also reads Filled, and the filled quantity
                compared with REST still shows it. Without the accepted
                New the rest of an order's history is incomplete, so no
                status is claimed for it.
                calibrate: these rules rest on assumptions about events and
                REST statuses not yet recorded, each of which shows up as a
                mismatch if wrong: REST reads Filled once the filled
                quantity reaches the placed quantity less what accepted
                UpdateQty operations removed, in either order; an IOC
                or FOK order that expires arrives as an accepted Cancel; and
                a Reject operation leaves the status unchanged.
    Returns:    One of Failed, Filled, Cancelled, PartFilled, Submitted or
                Unknown. All but Unknown are REST Status values; Unknown
                never matches REST, so an order the events cannot place is
                always reported.
    """
    if new_ok is False:
        return STATUS_FAILED
    if not new_ok or order_qty is None:
        return STATUS_UNKNOWN
    if filled_qty > 0 and filled_qty >= order_qty - reduced_qty:
        return STATUS_FILLED
    if cancel_ok and cancel_qty + filled_qty >= order_qty:
        return STATUS_CANCELLED
    if filled_qty > 0:
        return STATUS_PART_FILLED
    return STATUS_SUBMITTED


@dataclass
class _OrderProgress:
    """What one client's events have said so far about one order."""

    new_ok: bool | None = None
    cancel_ok: bool = False
    order_qty: Decimal | None = None

    # Quantity removed by accepted UpdateQty operations, and by those and
    # accepted Cancels together.
    reduced_qty: Decimal = Decimal(0)
    cancel_qty: Decimal = Decimal(0)

    filled_qty: Decimal = Decimal(0)
    identities: set[str] = field(default_factory=set)

    # Exchange time of the first report that carried one, else the first
    # report's receive time.
    first_time_ns: int | None = None
    first_time_is_exchange: bool = False

    def view(self, key: str) -> OrderView:
        status = derive_status(
            new_ok=self.new_ok,
            cancel_ok=self.cancel_ok,
            order_qty=self.order_qty,
            reduced_qty=self.reduced_qty,
            cancel_qty=self.cancel_qty,
            filled_qty=self.filled_qty,
        )
        return OrderView(key=key, status=status, filled_qty=self.filled_qty)


@dataclass(frozen=True)
class _Report:
    """One order or deal report taken from an order event frame."""

    recv_ts_ns: int
    seq: int
    is_deal: bool
    body: Mapping[str, object]
    identity: str
    where: str


def _reports(paths: Sequence[Path], fields: EventFields) -> list[_Report]:
    """Return one client's order and deal reports across its files, deduplicated and in arrival order.

    The same event can arrive on more than one of a client's streams, so
    reports are deduplicated by event_id, or by their data text when the
    event_id is empty, keeping the earliest arrival; an event_id repeated
    with different content is logged as a WARNING. Only lines containing
    the event name are decoded.
    """
    needle = json.dumps(fields.order_event_name, ensure_ascii=False)[1:-1].encode("utf-8")
    by_identity: dict[str, _Report] = {}
    seq = 0
    for path in paths:
        for number, recv_ts_ns, frame in iter_frames(path, needle):
            if frame.event != fields.order_event_name:
                continue
            where = f"{path}:{number}"
            parsed = _parse_report(frame, fields, where)
            if parsed is None:
                continue
            is_deal, body = parsed
            identity = _report_identity(body, frame, fields)
            report = _Report(
                recv_ts_ns=recv_ts_ns, seq=seq, is_deal=is_deal, body=body, identity=identity, where=where
            )
            seq += 1
            kept = by_identity.get(identity)
            if kept is not None and kept.body != body:
                earlier = kept if kept.recv_ts_ns <= recv_ts_ns else report
                logger.warning(
                    "%s: event_id %s repeats %s with different content; keeping %s",
                    where,
                    identity,
                    kept.where,
                    earlier.where,
                )
            if kept is None or recv_ts_ns < kept.recv_ts_ns:
                by_identity[identity] = report
    return sorted(by_identity.values(), key=lambda report: (report.recv_ts_ns, report.seq))


def _parse_report(frame: SseFrame, fields: EventFields, where: str) -> tuple[bool, Mapping[str, object]] | None:
    """Return (is_deal, report) from an OrderEvent frame, or None with a WARNING if it has neither.

    An OrderEvent's data is {"state": <state>, "data": {<state>: report}}
    (OpenAPI OrderEvent), and the configured pointers are relative to that
    report.
    """
    try:
        doc = json.loads(frame.data or "")
    except ValueError as exc:
        logger.warning("%s: %s data is not JSON: %s", where, fields.order_event_name, exc)
        return None
    state = doc.get("state") if isinstance(doc, dict) else None
    container = doc.get("data") if isinstance(doc, dict) else None
    body = container.get(state) if isinstance(container, dict) and isinstance(state, str) else None
    if not isinstance(body, dict):
        logger.warning("%s: %s has no report under data/<state>", where, fields.order_event_name)
        return None
    if state in fields.order_states:
        return False, body
    if state in fields.deal_states:
        return True, body
    logger.warning("%s: skipping %s with unconfigured state %r", where, fields.order_event_name, state)
    return None


def _report_identity(body: Mapping[str, object], frame: SseFrame, fields: EventFields) -> str:
    try:
        event_id = resolve_pointer(body, fields.event_id)
    except JsonPointerError:
        event_id = None
    if isinstance(event_id, str) and event_id:
        return event_id
    return "sha256:" + hashlib.sha256((frame.data or "").encode("utf-8")).hexdigest()


def _apply_report(orders: dict[str, _OrderProgress], report: _Report, fields: EventFields) -> None:
    """Fold one report into its order's progress, logging a WARNING instead if its fields are unusable.

    Definition: One step of the running state derive_status reads.
    Formula:    deal: filled_qty += deal_qty.
                New: new_ok = accepted and no New was rejected before;
                if accepted, order_qty = its order_qty if that is > 0.
                UpdateQty, if accepted: reduced_qty += its cancel_qty and
                cancel_qty += its cancel_qty.
                Cancel: cancel_ok = cancel_ok or accepted; if accepted,
                cancel_qty += its cancel_qty.
                Accepted means op_code == op_code_success.
    Domain:     Reports arrive in recv_ts order, deduplicated by identity,
                so each operation's cancel_quantity, which is what that
                operation alone removed, is added once. order.quantity
                stays the placed size through every operation, so only the
                New sets order_qty, and status.order_quantity, what remains
                open after the operation, is not read. A rejected operation
                changes nothing, since it may carry the size it asked for
                rather than the order's. A zero order size is ignored, since
                no live order has size 0, and a missing or null quantity
                leaves the running value unchanged.
    Returns:    None; orders is updated in place.
    """
    try:
        if report.is_deal:
            key = _pointer_str(report.body, fields.deal_key)
            quantity = to_decimal(resolve_pointer(report.body, fields.deal_qty))
            deal_progress = orders.setdefault(key, _OrderProgress())
            deal_progress.filled_qty += quantity
            deal_progress.identities.add(report.identity)
            _note_time(deal_progress, report, fields.deal_exchange_ts)
            return

        key = _pointer_str(report.body, fields.order_key)
        op_type = _pointer_str(report.body, fields.op_type)
        accepted = _pointer_str(report.body, fields.op_code) == fields.op_code_success
        order_qty = _optional_quantity(report.body, fields.order_qty)
        cancel_qty = _optional_quantity(report.body, fields.cancel_qty)
    except (JsonPointerError, ValueError) as exc:
        logger.warning("%s: skipping unusable report: %s", report.where, exc)
        return

    progress = orders.setdefault(key, _OrderProgress())
    progress.identities.add(report.identity)
    _note_time(progress, report, fields.order_exchange_ts)
    removed = cancel_qty if accepted and cancel_qty is not None else Decimal(0)

    if op_type == _OP_NEW:
        # A rejection is final even if an accepted duplicate also arrives
        progress.new_ok = accepted and progress.new_ok is not False
        if accepted and order_qty is not None and order_qty > 0:
            progress.order_qty = order_qty
    elif op_type == _OP_UPDATE_QTY:
        progress.reduced_qty += removed
        progress.cancel_qty += removed
    elif op_type == _OP_CANCEL:
        progress.cancel_ok = progress.cancel_ok or accepted
        progress.cancel_qty += removed
    elif op_type != _OP_UPDATE_PRICE:
        logger.warning("%s: op_type %r of order %s does not change its derived status", report.where, op_type, key)


def _note_time(progress: _OrderProgress, report: _Report, pointer: str) -> None:
    """Keep the exchange time of an order's first report that has one, else its first receive time."""
    if progress.first_time_is_exchange:
        return
    try:
        exchange_ns = epoch_s_to_ns(resolve_pointer(report.body, pointer))
    except (JsonPointerError, ValueError):
        exchange_ns = None
    if exchange_ns is not None and exchange_ns > 0:
        progress.first_time_ns = exchange_ns
        progress.first_time_is_exchange = True
    elif progress.first_time_ns is None:
        progress.first_time_ns = report.recv_ts_ns


def orders_from_sse(
    paths: Mapping[str, Sequence[Path]], fields: EventFields, trading_day: date | None = None
) -> dict[str, ClientOrders]:
    """Return, for each client, every order it saw keyed by order key, and the reports behind them.

    paths maps a client to all of its SSE files. Reports are read from
    frames named fields.order_event_name, deduplicated per client, and
    folded in arrival order; see derive_status for how the status follows.
    With trading_day, an Asia/Taipei calendar date rather than a TAIFEX
    trading day, only orders whose first report's exchange time falls on
    it take part, and the rest are counted in excluded_other_days; an
    order none of whose reports carries an exchange time is dated by its
    first receive time, and one timed in the night session is noted, each
    with a WARNING.
    """
    result: dict[str, ClientOrders] = {}
    for client, client_paths in paths.items():
        orders: dict[str, _OrderProgress] = {}
        for report in _reports(client_paths, fields):
            _apply_report(orders, report, fields)

        kept: dict[str, _OrderProgress] = {}
        for key, progress in orders.items():
            if trading_day is not None and not progress.first_time_is_exchange:
                logger.warning("client %s order %s has no exchange time; dating it by receive time", client, key)
            elif trading_day is not None and progress.first_time_ns is not None:
                _warn_if_night_session(f"client {client} order {key}", progress.first_time_ns)
            if trading_day is None or (
                progress.first_time_ns is not None and taipei_date(progress.first_time_ns) == trading_day
            ):
                kept[key] = progress
        result[client] = ClientOrders(
            orders={key: progress.view(key) for key, progress in kept.items()},
            event_ids=frozenset(identity for progress in kept.values() for identity in progress.identities),
            excluded_other_days=len(orders) - len(kept),
        )
    return result


def _in_night_session(ts_ns: int) -> bool:
    """Return whether an instant falls in TAIFEX's night session, 15:00 to 05:00 Taipei time."""
    local = datetime.fromtimestamp(ts_ns // NS_PER_S, tz=exchange_tz()).time()
    return local >= _NIGHT_SESSION_START or local < _NIGHT_SESSION_END


def _warn_if_night_session(what: str, ts_ns: int) -> None:
    if _in_night_session(ts_ns):
        logger.warning(
            "%s is timed in the night session, which belongs to the next TAIFEX trading day, "
            "while --trading-day selects by calendar date",
            what,
        )


def _rest_items(payload: object, fields: TradeFields) -> list[object]:
    try:
        items = resolve_pointer(payload, fields.list)
    except JsonPointerError as exc:
        raise AnalysisError(f"trades response has no list at {fields.list!r}: {exc}") from exc
    if not isinstance(items, list):
        raise AnalysisError(f"trades response at {fields.list!r} is {type(items).__name__}, expected a list")
    return items


def _rest_order_ns(item: object, fields: TradeFields) -> int | None:
    """Return a trades item's order time in epoch nanoseconds, or None if it has no usable one.

    A missing, null, non-numeric or non-positive order time is treated as
    absent, as an order report's exchange time is.
    """
    try:
        order_ns = epoch_s_to_ns(resolve_pointer(item, fields.order_ts))
    except (JsonPointerError, ValueError):
        return None
    return order_ns if order_ns > 0 else None


def orders_from_rest(payload: object, fields: TradeFields, trading_day: date | None = None) -> RestOrders:
    """Return the orders in a trades response keyed by order key.

    A missing or null filled quantity counts as zero, since the schema
    makes it optional and nullable for an order without deals. With
    trading_day, an Asia/Taipei calendar date rather than a TAIFEX trading
    day, an order whose order time falls on another date is counted in
    excluded_other_days instead, an order without an order time is kept,
    and one timed in the night session is noted, each with a WARNING.
    Raises AnalysisError if the list or any item's key or status cannot
    be read, or its filled quantity is not a number, since the comparison
    is only meaningful against the whole list; a repeated key keeps its
    last item, with a WARNING.
    """
    return _orders_from_items(_rest_items(payload, fields), fields, trading_day)


def _orders_from_items(items: Sequence[object], fields: TradeFields, trading_day: date | None) -> RestOrders:
    """Return the orders among Trade items keyed by order key, as orders_from_rest describes."""
    orders: dict[str, OrderView] = {}
    excluded = 0
    for index, item in enumerate(items):
        try:
            key = _pointer_str(item, fields.id)
            status = normalize_status(_pointer_str(item, fields.status))
            reported = _optional_quantity(item, fields.deal_quantity)
            filled = reported if reported is not None else Decimal(0)
        except (JsonPointerError, ValueError) as exc:
            raise AnalysisError(f"trades item {index}: {exc}") from exc

        if trading_day is not None:
            order_ns = _rest_order_ns(item, fields)
            if order_ns is None:
                logger.warning("trades order %s has no order time; keeping it on %s", key, trading_day)
            else:
                _warn_if_night_session(f"trades order {key}", order_ns)
                if taipei_date(order_ns) != trading_day:
                    excluded += 1
                    continue

        if key in orders:
            logger.warning("trades lists order %s more than once; keeping item %d", key, index)
        orders[key] = OrderView(key=key, status=status, filled_qty=filled)
    return RestOrders(orders=orders, excluded_other_days=excluded)


def trading_day_of(payload: object, fields: TradeFields) -> date:
    """Return the Asia/Taipei calendar date every order time in a trades response falls on.

    This is a calendar date, not a TAIFEX trading day. Items without a
    usable order time are passed over. Raises AnalysisError when no item
    has one, or when they span more than one date, since the snapshot
    then names no single date and one must be given.
    """
    return _trading_day_of_items(_rest_items(payload, fields), fields)


def _trading_day_of_items(items: Sequence[object], fields: TradeFields) -> date:
    """Return the Asia/Taipei calendar date every order time among Trade items falls on, as trading_day_of describes."""
    days = {taipei_date(order_ns) for item in items if (order_ns := _rest_order_ns(item, fields)) is not None}
    if len(days) != 1:
        found = ", ".join(sorted(day.isoformat() for day in days)) or "none"
        raise AnalysisError(f"trades order times give no single date (found: {found}); pass --trading-day")
    return days.pop()


def reconcile(sse: Mapping[str, ClientOrders], rest_orders: RestOrders) -> ReconcileReport:
    """Compare every client's orders and reports with REST and with each other.

    Every client must see every REST order and every report, since each
    client is a consumer that would otherwise miss one: an order is only
    in REST when at least one client missed it (every REST order, when
    there is no client at all), and missing_by_client says which; a report
    some client saw and another did not is listed under the one that
    missed it in missing_events_by_client. An order is only in SSE when
    some client saw it but REST does not list it. Each client that saw an
    order REST also lists must agree on both status and filled quantity,
    and every client that does not adds a Mismatch.
    """
    rest = rest_orders.orders
    clients = sorted(sse)
    seen = {key for client in clients for key in sse[client].orders}
    all_events = frozenset().union(*(sse[client].event_ids for client in clients))
    missing_by_client = {client: sorted(rest.keys() - sse[client].orders.keys()) for client in clients}
    missed = {key for keys in missing_by_client.values() for key in keys} if sse else set(rest)
    mismatched = [
        Mismatch(client=client, sse=view, rest=rest[key])
        for client in clients
        for key, view in sorted(sse[client].orders.items())
        if key in rest
        and (
            normalize_status(view.status) != normalize_status(rest[key].status)
            or view.filled_qty != rest[key].filled_qty
        )
    ]
    return ReconcileReport(
        rest_order_count=len(rest),
        sse_order_count_by_client={client: len(sse[client].orders) for client in clients},
        only_in_rest=sorted(missed),
        only_in_sse=sorted(seen - rest.keys()),
        mismatched=mismatched,
        missing_by_client=missing_by_client,
        per_client_keys={client: sorted(sse[client].orders) for client in clients},
        missing_events_by_client={client: sorted(all_events - sse[client].event_ids) for client in clients},
        excluded_other_days={client: sse[client].excluded_other_days for client in clients},
        rest_excluded_other_days=rest_orders.excluded_other_days,
    )


def last_trades_payloads(rest_log: Path, account_types: Sequence[str]) -> dict[str, object]:
    """Return, for each of account_types, the parsed body of the last trades response rest_log records for it.

    Each response belongs to the account type its recorded request body
    names. Lines for requests that got no response are passed over; each
    account type's last response must be a 2xx JSON body, since falling
    back to an older snapshot would compare SSE against a stale list.
    Raises AnalysisError if no trades response is recorded, if any names
    no account, since such a request lists the stock account's orders
    alone, or if an account type has none.
    """
    last: dict[str, tuple[int, dict[str, object]]] = {}
    recorded = False
    for number, record in read_jsonl(rest_log):
        if record.get("op") != _TRADES_OP or "status" not in record:
            continue
        recorded = True
        account_type = _requested_account_type(record)
        if account_type is None:
            raise AnalysisError(
                f"{rest_log}:{number}: {_TRADES_OP} request names no account, the single-query shape that lists "
                "the stock account's orders alone; reconcile without --rest-run to query every account"
            )
        last[account_type] = (number, record)
    if not recorded:
        raise AnalysisError(f"{rest_log}: no {_TRADES_OP} response recorded")
    missing = [account_type for account_type in account_types if account_type not in last]
    if missing:
        raise AnalysisError(f"{rest_log}: no {_TRADES_OP} response recorded for account type {', '.join(missing)}")
    return {
        account_type: _recorded_trades_body(rest_log, *last[account_type], account_type)
        for account_type in account_types
    }


def _requested_account_type(record: Mapping[str, object]) -> str | None:
    """Return the account_type a recorded request body names, or None if it names none."""
    text = record.get("req_body")
    if not isinstance(text, str):
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    account_type = body.get("account_type") if isinstance(body, dict) else None
    return account_type if isinstance(account_type, str) and account_type else None


def _recorded_trades_body(rest_log: Path, number: int, record: Mapping[str, object], account_type: str) -> object:
    """Return the parsed body of the trades response at rest_log:number, which must be a 2xx JSON body."""
    what = f"{rest_log}:{number}: last {_TRADES_OP} response for account type {account_type}"
    status = record["status"]
    if isinstance(status, bool) or not isinstance(status, int) or not httpx.codes.is_success(status):
        raise AnalysisError(f"{what} is HTTP {status}")
    try:
        if isinstance(record.get("resp_body"), str):
            return json.loads(str(record["resp_body"]))
        return json.loads(base64.b64decode(str(record["resp_body_b64"]), validate=True))
    except (KeyError, ValueError) as exc:
        raise AnalysisError(f"{what} is not JSON: {exc}") from exc


def _query_trades(cfg: P1Config, run_dir: Path, transport: httpx.BaseTransport | None) -> dict[str, object]:
    """Check simulation mode, then fetch the trades list of each of trades.account_types.

    Each request carries that account's AccountRequest, taken from the
    live accounts response, and every exchange is recorded in
    run_dir/rest.jsonl. Raises AnalysisError if an endpoint is not
    configured, accounts lists no account of a configured type, or a
    trades response is not a 2xx JSON body.
    """
    endpoint = cfg.endpoints.get(_TRADES_OP)
    if endpoint is None:
        raise AnalysisError(f"no [endpoints.{_TRADES_OP}] configured")
    if ACCOUNTS_OP not in cfg.endpoints:
        raise AnalysisError(f"no [endpoints.{ACCOUNTS_OP}] configured")

    payloads: dict[str, object] = {}
    with CapturingClient(
        cfg.base_url, "reconcile", run_dir / "rest.jsonl", cfg.http_timeout_s, transport=transport
    ) as client:
        assert_simulation(client, cfg)
        try:
            accounts = fetch_accounts(client, cfg)
        except BrokerError as exc:
            raise AnalysisError(str(exc)) from exc
        bodies = {
            account_type: {**account_request(_account_of_type(accounts, account_type)), **_TRADES_REFRESH}
            for account_type in cfg.trades.account_types
        }
        for account_type, body in bodies.items():
            response = client.request(_TRADES_OP, endpoint, json_body=body)
            if not httpx.codes.is_success(response.status):
                raise AnalysisError(f"{_TRADES_OP} for account type {account_type} returned HTTP {response.status}")
            try:
                payloads[account_type] = response.json()
            except ValueError as exc:
                what = f"{_TRADES_OP} response for account type {account_type}"
                raise AnalysisError(f"{what} is not JSON: {exc}") from exc
    return payloads


def _account_of_type(accounts: Sequence[Mapping[str, object]], account_type: str) -> Mapping[str, object]:
    """Return the first account of account_type, raising AnalysisError if accounts lists none."""
    for account in accounts:
        if account.get("account_type") == account_type:
            return account
    raise AnalysisError(f"{ACCOUNTS_OP} lists no account of type {account_type!r}, which trades.account_types names")


def run_reconcile(
    cfg: P1Config,
    sse_run: str,
    rest_run: str | None,
    run_dir: Path,
    *,
    trading_day: date | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Write run_dir/reconcile.json and return 0 if SSE and REST agree, 2 if not or if nothing was compared.

    REST is the union of the trades lists of every account type in
    trades.account_types. Without rest_run, each is fetched live after
    the simulation-mode check, and recorded in run_dir; with it, each
    account type's last trades response in that run's rest.jsonl is used.
    REST is read before the SSE files, so an order placed meanwhile shows
    up as only in SSE rather than silently widening the comparison. On
    both sides only the orders of trading_day take part: an Asia/Taipei
    calendar date, not a TAIFEX trading day, so a night-session order
    counts on the date it was placed. It defaults to the date of the
    union's order times, except that an empty union gives no date, so no
    SSE order is compared either and reconcile.json records trading_day
    as null. When REST lists no order on the day, nothing was compared,
    which is logged as an ERROR and returns 2. InterlockError propagates;
    any other failure to analyse is logged and returns 1.
    """
    try:
        sse_dir = resolve_run_dir(cfg.results_root, sse_run)
        if rest_run is None:
            payloads = _query_trades(cfg, run_dir, transport)
        else:
            rest_log = resolve_run_dir(cfg.results_root, rest_run) / "rest.jsonl"
            payloads = last_trades_payloads(rest_log, cfg.trades.account_types)
        items = [item for payload in payloads.values() for item in _rest_items(payload, cfg.trades)]
        day = trading_day
        if day is None and items:
            day = _trading_day_of_items(items, cfg.trades)
        rest = _orders_from_items(items, cfg.trades, day)
        sse_files = discover_sse_files(sse_dir)

        # With no day to window SSE by, no SSE order takes part either
        if day is None:
            sse = {client: ClientOrders(orders={}, event_ids=frozenset()) for client in sse_files}
        else:
            sse = orders_from_sse(sse_files, cfg.events, day)
        report = reconcile(sse, rest)
    except (AnalysisError, OSError) as exc:
        logger.error("reconcile failed: %s", exc)
        return EXIT_ANALYSIS_ERROR

    document: dict[str, object] = {
        "sse_run": sse_run,
        "rest_run": rest_run,
        "trading_day": day.isoformat() if day is not None else None,
        **report.to_json(),
    }
    write_json(run_dir / "reconcile.json", document)
    if report.rest_order_count == 0:
        where = f"on {day.isoformat()}" if day is not None else "at all"
        logger.error("reconcile: nothing was compared, since trades lists no order %s", where)
    logger.info(
        "reconcile: %d REST orders, SSE orders %s; %d only in REST, %d only in SSE, %d mismatched, "
        "missing orders %s, missing reports %s",
        report.rest_order_count,
        ", ".join(f"{client}={count}" for client, count in report.sse_order_count_by_client.items()),
        len(report.only_in_rest),
        len(report.only_in_sse),
        len(report.mismatched),
        ", ".join(f"{client}={len(keys)}" for client, keys in report.missing_by_client.items()),
        ", ".join(f"{client}={len(ids)}" for client, ids in report.missing_events_by_client.items()),
    )
    return EXIT_OK if report.clean else EXIT_CHECK_FAILED
