"""Reconcile recorded order events against REST trades, and summarise SSE streams.

Both analyses only read what stream and orders runs already wrote: the
sse_<client>_<stream>.jsonl files of a stream run and, unless the trades
list is queried live, the rest.jsonl of an earlier run. Official order
events carry no status string, so each order's final status on the SSE
side is derived from its operations and deals by derive_status.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import logging
import math
import statistics
from array import array
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from p1rec.capture import CapturingClient
from p1rec.config import EventFields, P1Config
from p1rec.interlock import assert_simulation
from p1rec.jsonpointer import JsonPointerError, resolve_pointer
from p1rec.sse import SseFrame, parse_frame

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ANALYSIS_ERROR = 1
EXIT_CHECK_FAILED = 2

NS_PER_S = 1_000_000_000

# How many of the busiest seconds stats lists, enough to show the shape
# of the opening spike without flooding the report.
PEAK_SECONDS = 10

# Nearest-rank percentiles reported for receive latency
LATENCY_PERCENTILES = (50, 99)

# Exchange time strings carry no zone and are Taipei local time
# (docs/official-server-contract.md section 2).
EXCHANGE_TZ_NAME = "Asia/Taipei"

# The OpenAPI OpType values that change an order's derived status; the
# update operations only refresh its quantities.
_OP_NEW = "New"
_OP_CANCEL = "Cancel"
_OPS_QUANTITY_ONLY = frozenset({"UpdatePrice", "UpdateQty"})

# REST statuses may be spelled with the enum's qualified name
_STATUS_PREFIX = "Status."

# Derived statuses; all but UNKNOWN are values of the OpenAPI Status enum
STATUS_FAILED = "Failed"
STATUS_FILLED = "Filled"
STATUS_CANCELLED = "Cancelled"
STATUS_PART_FILLED = "PartFilled"
STATUS_SUBMITTED = "Submitted"
STATUS_UNKNOWN = "Unknown"

# SSE's default event type for a frame without an event field, and the
# name counted for a frame made only of comment lines.
_DEFAULT_EVENT_NAME = "message"
_COMMENT_EVENT_NAME = ":comment"

# The trades request names no account, since every AccountRequest field
# is optional and the endpoint returns all trades, and spells out refresh
# so REST is brought up to date before it is compared.
_TRADES_OP = "trades"
_TRADES_BODY: dict[str, object] = {"refresh": True}

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class AnalysisError(ValueError):
    """Raised when recorded input cannot be analysed as configured."""


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

    # Orders left out because they belong to another trading day
    excluded_other_days: int = 0


@dataclass(frozen=True)
class ReconcileReport:
    """How the orders every client saw on SSE compare with the REST trades list."""

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

    # Orders each client saw on other trading days, left out of the comparison
    excluded_other_days: dict[str, int]

    @property
    def clean(self) -> bool:
        """Return whether every client saw every report and SSE and REST agree on every order."""
        missed_events = any(self.missing_events_by_client.values())
        return not (self.only_in_rest or self.only_in_sse or self.mismatched or missed_events)

    def to_json(self) -> dict[str, object]:
        """Return the report as the reconcile.json document, quantities as decimal strings."""
        return {
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
        }


def read_jsonl(path: Path) -> Iterator[tuple[int, dict[str, object]]]:
    """Yield (line number, record) for each JSON object line of path.

    Blank lines are skipped. An unreadable last line is skipped with a
    WARNING, since appends are not fsynced and the recording host has lost
    power mid-write before, leaving a torn or NUL-padded tail; any earlier
    unreadable line raises AnalysisError, since a crash explains only the
    tail.
    """
    bad_line: tuple[int, str] | None = None
    with path.open("rb") as fh:
        for number, raw_line in enumerate(fh, start=1):
            text = raw_line.strip()
            if not text:
                continue
            if bad_line is not None:
                raise AnalysisError(f"{path}:{bad_line[0]}: unreadable line before the end of the file: {bad_line[1]}")
            try:
                record = json.loads(text)
            except ValueError as exc:
                bad_line = (number, str(exc))
                continue
            if not isinstance(record, dict):
                bad_line = (number, f"expected a JSON object, got {type(record).__name__}")
                continue
            yield number, record
    if bad_line is not None:
        logger.warning("%s:%d: skipping unreadable last line (%s)", path, bad_line[0], bad_line[1])


def discover_sse_files(run_dir: Path) -> dict[str, list[Path]]:
    """Return a stream run's sse_*.jsonl files grouped by the client that recorded them.

    The client comes from the first record's client field, since stream
    names contain underscores and make the file name ambiguous; a file
    whose first line is unreadable falls back to the name segment after
    "sse_", with a WARNING. Raises AnalysisError if run_dir holds no such
    file.
    """
    files = sorted(run_dir.glob("sse_*.jsonl"))
    if not files:
        raise AnalysisError(f"no sse_*.jsonl files in {run_dir}")
    grouped: dict[str, list[Path]] = {}
    for path in files:
        client = _first_record_client(path)
        if client is None:
            client = path.name.removeprefix("sse_").split("_", 1)[0]
            logger.warning("%s: no readable first record, assuming client %r from the file name", path, client)
        grouped.setdefault(client, []).append(path)
    return grouped


def _first_record_client(path: Path) -> str | None:
    with path.open("rb") as fh:
        for raw_line in fh:
            text = raw_line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except ValueError:
                return None
            client = record.get("client") if isinstance(record, dict) else None
            return client if isinstance(client, str) and client else None
    return None


def _recv_ts_ns(record: Mapping[str, object], path: Path, number: int) -> int:
    value = record.get("recv_ts_ns")
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnalysisError(f"{path}:{number}: recv_ts_ns is missing or not an integer")
    return value


def _frame_of(record: Mapping[str, object], path: Path, number: int) -> SseFrame:
    raw = record.get("raw")
    if not isinstance(raw, str):
        raise AnalysisError(f"{path}:{number}: frame line has no raw text")
    return parse_frame(raw)


def _frames(path: Path) -> Iterator[tuple[int, int, SseFrame]]:
    """Yield (line number, recv_ts_ns, parsed frame) for each frame line of an SSE file."""
    for number, record in read_jsonl(path):
        if record.get("kind") == "frame":
            yield number, _recv_ts_ns(record, path, number), _frame_of(record, path, number)


def _to_decimal(value: object) -> Decimal:
    """Convert a JSON quantity to Decimal, raising ValueError for anything but a finite number or numeric string."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected a number, got {type(value).__name__}")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return result


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
    return None if value is None else _to_decimal(value)


def normalize_status(status: str) -> str:
    """Return status without the enum's "Status." qualifier, so both spellings compare equal."""
    return status.removeprefix(_STATUS_PREFIX)


def derive_status(
    *,
    new_ok: bool | None,
    cancel_ok: bool,
    order_qty: Decimal | None,
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
                Filled      if filled_qty > 0 and filled_qty >= order_qty;
                Cancelled   if cancel_ok and cancel_qty >= order_qty - filled_qty;
                PartFilled  if filled_qty > 0;
                Submitted   otherwise.
    Domain:     new_ok is None when no New event was seen, else whether it
                was accepted; cancel_ok is whether any Cancel was
                accepted; order_qty is the latest non-zero order size an
                accepted operation reported, None if none did; cancel_qty
                is the largest cancel_quantity an accepted operation
                reported; filled_qty is the sum of deal quantities.
                Quantities are non-negative and in the events' own units;
                an overfill also reads Filled, and the filled quantity
                compared with REST still shows it. Without the accepted
                New the rest of an order's history is incomplete, so no
                status is claimed for it.
                calibrate: these rules rest on assumptions about events not
                yet recorded, each of which shows up as a mismatch if wrong:
                cancel_quantity is cumulative over an order's operations,
                not per operation; after an accepted UpdateQty,
                order.quantity reports the reduced size, so a reduced order
                that fills its remainder reads Filled; an IOC or FOK order
                that expires arrives as an accepted Cancel; and a Reject
                operation leaves the status unchanged.
    Returns:    One of Failed, Filled, Cancelled, PartFilled, Submitted or
                Unknown. All but Unknown are REST Status values; Unknown
                never matches REST, so an order the events cannot place is
                always reported.
    """
    if new_ok is False:
        return STATUS_FAILED
    if not new_ok or order_qty is None:
        return STATUS_UNKNOWN
    if filled_qty > 0 and filled_qty >= order_qty:
        return STATUS_FILLED
    if cancel_ok and cancel_qty >= order_qty - filled_qty:
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
    event_id is empty, keeping the earliest arrival.
    """
    by_identity: dict[str, _Report] = {}
    seq = 0
    for path in paths:
        for number, recv_ts_ns, frame in _frames(path):
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
                order operation accepted (op_code == op_code_success):
                order_qty = its order_qty if that is > 0, else unchanged;
                cancel_qty = max(cancel_qty, its cancel_qty).
                New: new_ok = accepted and no New was rejected before.
                Cancel: cancel_ok = cancel_ok or accepted.
    Domain:     Reports arrive in recv_ts order, deduplicated. A rejected
                operation changes no quantity, since it may carry the size
                it asked for rather than the order's. A zero order size is
                ignored, as a size field can read 0 on a Cancel (the
                recorded one has status.order_quantity 0), and a missing or
                null quantity leaves the running value unchanged.
    Returns:    None; orders is updated in place.
    """
    try:
        if report.is_deal:
            key = _pointer_str(report.body, fields.deal_key)
            quantity = _to_decimal(resolve_pointer(report.body, fields.deal_qty))
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
    if accepted and order_qty is not None and order_qty > 0:
        progress.order_qty = order_qty
    if accepted and cancel_qty is not None:
        progress.cancel_qty = max(progress.cancel_qty, cancel_qty)

    if op_type == _OP_NEW:
        # A rejection is final even if an accepted duplicate also arrives
        progress.new_ok = accepted and progress.new_ok is not False
    elif op_type == _OP_CANCEL:
        progress.cancel_ok = progress.cancel_ok or accepted
    elif op_type not in _OPS_QUANTITY_ONLY:
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


def epoch_s_to_ns(value: object) -> int:
    """Return a JSON number of epoch seconds as integer epoch nanoseconds.

    Definition: The same instant on the recorder's nanosecond scale.
    Formula:    ns = round(Decimal(text of value) * 1e9), half to even.
    Domain:     A JSON number (not a boolean) of seconds since
                1970-01-01T00:00Z; going through its decimal text keeps it
                exact, where float multiplication would blur it by
                hundreds of nanoseconds at this magnitude. Raises
                ValueError for anything else.
    Returns:    Integer nanoseconds since the epoch.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{value!r} is not a number of epoch seconds")
    return int((_to_decimal(value) * NS_PER_S).to_integral_value())


def taipei_date(ts_ns: int) -> date:
    """Return the Asia/Taipei calendar date of an epoch-nanosecond instant.

    Definition: The local calendar date in Taiwan at that instant.
    Formula:    date(floor(ts_ns / 1e9) seconds after the epoch, in
                Asia/Taipei).
    Domain:     Integer nanoseconds since 1970-01-01T00:00Z; Asia/Taipei
                is UTC+8 with no daylight saving time.
    Returns:    A date; an instant after 16:00Z falls on the next UTC day's date.
    """
    return datetime.fromtimestamp(ts_ns // NS_PER_S, tz=_exchange_tz()).date()


def orders_from_sse(
    paths: Mapping[str, Sequence[Path]], fields: EventFields, trading_day: date | None = None
) -> dict[str, ClientOrders]:
    """Return, for each client, every order it saw keyed by order key, and the reports behind them.

    paths maps a client to all of its SSE files. Reports are read from
    frames named fields.order_event_name, deduplicated per client, and
    folded in arrival order; see derive_status for how the status follows.
    With trading_day, only orders whose first report's exchange time falls
    on that Asia/Taipei date take part, and the rest are counted in
    excluded_other_days; an order none of whose reports carries an
    exchange time is dated by its first receive time, with a WARNING.
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


def orders_from_rest(payload: object, fields: EventFields) -> dict[str, OrderView]:
    """Return the orders in a trades response keyed by order key.

    A null filled quantity counts as zero, since the schema allows null
    for an order without deals. Raises AnalysisError if the list or any
    item's key, status or filled quantity cannot be read, since the
    comparison is only meaningful against the whole list; a repeated key
    keeps its last item, with a WARNING.
    """
    try:
        items = resolve_pointer(payload, fields.rest_list)
    except JsonPointerError as exc:
        raise AnalysisError(f"trades response has no list at {fields.rest_list!r}: {exc}") from exc
    if not isinstance(items, list):
        raise AnalysisError(f"trades response at {fields.rest_list!r} is {type(items).__name__}, expected a list")

    orders: dict[str, OrderView] = {}
    for index, item in enumerate(items):
        try:
            key = _pointer_str(item, fields.rest_key)
            status = normalize_status(_pointer_str(item, fields.rest_status))
            raw_filled = resolve_pointer(item, fields.rest_filled_qty)
            filled = Decimal(0) if raw_filled is None else _to_decimal(raw_filled)
        except (JsonPointerError, ValueError) as exc:
            raise AnalysisError(f"trades item {index}: {exc}") from exc
        if key in orders:
            logger.warning("trades lists order %s more than once; keeping item %d", key, index)
        orders[key] = OrderView(key=key, status=status, filled_qty=filled)
    return orders


def trading_day_of(payload: object, fields: EventFields) -> date:
    """Return the Asia/Taipei date every order time in a trades response falls on.

    Items without an order time are passed over. Raises AnalysisError
    when no item has one, or when they span more than one date, since the
    snapshot then names no single trading day and one must be given.
    """
    try:
        items = resolve_pointer(payload, fields.rest_list)
    except JsonPointerError:
        items = []
    days: set[date] = set()
    for item in items if isinstance(items, list) else []:
        try:
            days.add(taipei_date(epoch_s_to_ns(resolve_pointer(item, fields.rest_order_ts))))
        except (JsonPointerError, ValueError):
            continue
    if len(days) != 1:
        found = ", ".join(sorted(day.isoformat() for day in days)) or "none"
        raise AnalysisError(f"trades order times give no single trading day (found: {found}); pass --trading-day")
    return days.pop()


def reconcile(sse: Mapping[str, ClientOrders], rest: dict[str, OrderView]) -> ReconcileReport:
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
        only_in_rest=sorted(missed),
        only_in_sse=sorted(seen - rest.keys()),
        mismatched=mismatched,
        missing_by_client=missing_by_client,
        per_client_keys={client: sorted(sse[client].orders) for client in clients},
        missing_events_by_client={client: sorted(all_events - sse[client].event_ids) for client in clients},
        excluded_other_days={client: sse[client].excluded_other_days for client in clients},
    )


def percentile_nearest_rank(sorted_values: Sequence[float], p: float) -> float:
    """Return the p-th percentile of sorted_values by the nearest-rank method.

    Definition: The smallest recorded value such that at least p percent
                of the values are less than or equal to it.
    Formula:    P_p = x_(k), k = max(1, ceil(p / 100 * n)), 1-based over x
                sorted ascending; computed with exact fractions.
    Domain:     sorted_values is sorted ascending and non-empty; 0 <= p <=
                100. No interpolation, so small samples round up to an
                observed value.
    Returns:    An element of sorted_values, in its unit.
    """
    if not sorted_values:
        raise ValueError("percentile of an empty sequence")
    if not 0 <= p <= 100:
        raise ValueError(f"percentile must be within [0, 100], got {p}")
    rank = max(1, math.ceil(Fraction(p) * len(sorted_values) / 100))
    return sorted_values[rank - 1]


def interval_summary(intervals: Sequence[float]) -> dict[str, object] | None:
    """Summarise heartbeat intervals by their count, minimum, median and maximum.

    Definition: The spread of the time between consecutive heartbeats.
    Formula:    n = len(x); min(x); median(x) (statistics.median, the mean
                of the two middle values when n is even); max(x).
    Domain:     Intervals in seconds, each measured within one connection;
                empty when a file had fewer than two heartbeats on one
                connection.
    Returns:    {"n", "min", "median", "max"} in seconds, or None when
                there are no intervals.
    """
    if not intervals:
        return None
    return {"n": len(intervals), "min": min(intervals), "median": statistics.median(intervals), "max": max(intervals)}


def latency_summary(latencies: Sequence[float], unparsable: int) -> dict[str, object]:
    """Summarise receive latency by nearest-rank p50 and p99 and the maximum.

    Definition: How long after the exchange time a frame was recorded.
    Formula:    latency = recv_ts - exchange_ts per frame; p50 and p99 by
                percentile_nearest_rank; max(latency).
    Domain:     Seconds; negative when the local and exchange clocks
                disagree, so the local NTP state belongs next to it.
                unparsable counts frames whose exchange time fields exist
                but do not parse.
    Returns:    {"n", "p50", "p99", "max", "unparsable"} in seconds; the
                statistics are None when n is 0.
    """
    ordered = sorted(latencies)
    summary: dict[str, object] = {"n": len(ordered)}
    for p in LATENCY_PERCENTILES:
        summary[f"p{p}"] = percentile_nearest_rank(ordered, p) if ordered else None
    summary["max"] = ordered[-1] if ordered else None
    summary["unparsable"] = unparsable
    return summary


def exchange_ts_ns(doc: object, fields: EventFields) -> int | None:
    """Return the exchange time in a frame's data as epoch nanoseconds.

    Definition: The instant the exchange stamped on a frame, on the same
                scale as recv_ts_ns.
    Formula:    Without exchange_ts_format: ns = epoch_s_to_ns(v), v the
                single pointer's value in epoch seconds. With it: t = strptime(" ".join(values),
                exchange_ts_format) as Asia/Taipei local time, ns =
                (t - 1970-01-01T00:00Z) in whole microseconds * 1000.
    Domain:     doc is a frame's parsed data. Frames where a pointer does
                not resolve, such as heartbeats, have no exchange time.
                The format must not parse a zone. Raises ValueError when
                the values resolve but are not a number or do not parse.
    Returns:    Epoch nanoseconds (UTC), microsecond resolution for parsed
                strings, or None when a pointer does not resolve.
    """
    try:
        values = [resolve_pointer(doc, pointer) for pointer in fields.exchange_ts_pointers]
    except JsonPointerError:
        return None

    if fields.exchange_ts_format is None:
        (value,) = values
        return epoch_s_to_ns(value)

    texts = [value for value in values if isinstance(value, str)]
    if len(texts) != len(values):
        raise ValueError("exchange time fields are not all strings")
    parsed = datetime.strptime(" ".join(texts), fields.exchange_ts_format)
    if parsed.tzinfo is not None:
        raise ValueError("exchange_ts_format must not parse a zone, since exchange time is Taipei local")
    return (parsed.replace(tzinfo=_exchange_tz()) - _UNIX_EPOCH) // timedelta(microseconds=1) * 1000


@functools.cache
def _exchange_tz() -> ZoneInfo:
    """Return the exchange's zone, loaded on first use so a host without tzdata fails only here."""
    return ZoneInfo(EXCHANGE_TZ_NAME)


def _iso_utc(ts_ns: int, timespec: str = "microseconds") -> str:
    """Return epoch nanoseconds as an ISO 8601 UTC string.

    Definition: The wall-clock time an epoch-nanosecond stamp names.
    Formula:    s, r = divmod(ts_ns, 1e9); UTC datetime of s with
                microsecond = r // 1000, formatted with timespec.
    Domain:     Integer nanoseconds since 1970-01-01T00:00Z; sub-microsecond
                digits are truncated, not rounded.
    Returns:    For example "2026-10-01T01:00:00.000000Z".
    """
    seconds, remainder = divmod(ts_ns, NS_PER_S)
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=remainder // 1000)
    return moment.isoformat(timespec=timespec).replace("+00:00", "Z")


def _event_name(frame: SseFrame) -> str:
    if frame.event:
        return frame.event
    return _COMMENT_EVENT_NAME if frame.is_comment else _DEFAULT_EVENT_NAME


@dataclass
class _Outage:
    """A run of failed connection attempts, ended by the next 200 connect or by the file's end."""

    start_ns: int
    first_error: object
    attempts: int = 1
    end_ns: int | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "disconnected_at": _iso_utc(self.start_ns),
            "reconnected_at": _iso_utc(self.end_ns) if self.end_ns is not None else None,
            "outage_s": (self.end_ns - self.start_ns) / NS_PER_S if self.end_ns is not None else None,
            "attempts": self.attempts,
            "first_error": self.first_error,
        }


class _FileStats:
    """One SSE file's statistics, accumulated while its lines are read once."""

    def __init__(self, path: Path, fields: EventFields) -> None:
        self._path = path
        self._fields = fields
        self.stream: str | None = None
        self.frames = 0
        self.event_counts: Counter[str] = Counter()
        self._per_second: Counter[int] = Counter()
        self._prev_frame_ns: int | None = None
        self._gaps: list[dict[str, object]] = []
        self._prev_heartbeat_ns: int | None = None
        self._heartbeat_intervals: list[float] = []
        self._outages: list[_Outage] = []
        self._outage: _Outage | None = None
        self._latencies: array[float] = array("d")
        self._unparsable = 0

    def read(self) -> None:
        for number, record in read_jsonl(self._path):
            if self.stream is None and isinstance(record.get("stream"), str):
                self.stream = str(record["stream"])
            recv_ts_ns = _recv_ts_ns(record, self._path, number)
            kind = record.get("kind")
            if kind == "frame":
                self._frame(recv_ts_ns, _frame_of(record, self._path, number))
            elif kind == "connected":
                self._prev_heartbeat_ns = None
                if record.get("status") == httpx.codes.OK:
                    self._end_outage(recv_ts_ns)
            elif kind == "disconnected":
                self._prev_heartbeat_ns = None
                self._start_or_extend_outage(recv_ts_ns, record.get("error"))
            elif kind == "stopped":
                self._prev_heartbeat_ns = None
                self._end_outage(None)
        self._end_outage(None)

    def _frame(self, recv_ts_ns: int, frame: SseFrame) -> None:
        self.frames += 1
        self.event_counts[_event_name(frame)] += 1
        self._per_second[recv_ts_ns // NS_PER_S] += 1

        if self._prev_frame_ns is not None:
            gap_s = (recv_ts_ns - self._prev_frame_ns) / NS_PER_S
            if gap_s > self._fields.gap_threshold_s:
                self._gaps.append({"from": _iso_utc(self._prev_frame_ns), "to": _iso_utc(recv_ts_ns), "gap_s": gap_s})
        self._prev_frame_ns = recv_ts_ns

        if frame.event == self._fields.heartbeat_event:
            if self._prev_heartbeat_ns is not None:
                self._heartbeat_intervals.append((recv_ts_ns - self._prev_heartbeat_ns) / NS_PER_S)
            self._prev_heartbeat_ns = recv_ts_ns

        if self._fields.exchange_ts_pointers and frame.data is not None:
            self._latency(recv_ts_ns, frame.data)

    def _latency(self, recv_ts_ns: int, data: str) -> None:
        try:
            doc = json.loads(data)
        except ValueError:
            return
        try:
            exchange_ns = exchange_ts_ns(doc, self._fields)
        except ValueError:
            self._unparsable += 1
            return
        if exchange_ns is not None:
            self._latencies.append((recv_ts_ns - exchange_ns) / NS_PER_S)

    def _start_or_extend_outage(self, recv_ts_ns: int, error: object) -> None:
        if self._outage is None:
            self._outage = _Outage(start_ns=recv_ts_ns, first_error=error)
        else:
            self._outage.attempts += 1

    def _end_outage(self, recv_ts_ns: int | None) -> None:
        if self._outage is None:
            return
        self._outage.end_ns = recv_ts_ns
        self._outages.append(self._outage)
        self._outage = None

    def to_json(self, client: str) -> dict[str, object]:
        busiest = sorted(self._per_second.items(), key=lambda item: (-item[1], item[0]))[:PEAK_SECONDS]
        return {
            "client": client,
            "stream": self.stream,
            "frames": self.frames,
            "event_counts": dict(sorted(self.event_counts.items())),
            "peak_seconds": [
                {"second": _iso_utc(second * NS_PER_S, "seconds"), "frames": count} for second, count in busiest
            ],
            "heartbeat_interval_s": interval_summary(self._heartbeat_intervals),
            "gaps": self._gaps,
            "outages": [outage.to_json() for outage in self._outages],
            "latency_s": self._latency_json(),
        }

    def _latency_json(self) -> dict[str, object] | None:
        if not self._fields.exchange_ts_pointers:
            return None
        return latency_summary(self._latencies, self._unparsable)


def stream_stats(paths: Mapping[str, Sequence[Path]], fields: EventFields) -> dict[str, object]:
    """Return the stats.json document for a stream run's SSE files.

    Definition: Per-client event counts, and per-file frame rate peaks,
                heartbeat spacing, silences, outages and receive latency.
    Formula:    event_counts[name] = frames with that event name;
                peak_seconds = the PEAK_SECONDS one-second buckets
                floor(recv_ts_ns / 1e9) with the most frames, ties broken
                by the earlier second; a gap is recv_ts[i] - recv_ts[i-1]
                > gap_threshold_s between consecutive frames of a file; a
                heartbeat interval is the same difference between
                consecutive heartbeat_event frames on one connection; an
                outage runs from a disconnect to the next 200 connect;
                latency as in latency_summary.
    Domain:     paths maps a client to all of its files; each file is one
                recorder, so intervals and gaps never span two files.
                Client event_counts sum that client's files, so an event
                recorded on both the combined and the order_event stream
                counts twice there; per-file counts are exact.
                recv_ts is the recorder's wall clock, which trails socket
                arrival whenever the recorder falls behind.
    Returns:    {"gap_threshold_s", "heartbeat_event", "clients": {client:
                {"event_counts"}}, "files": {file name: {"client",
                "stream", "frames", "event_counts", "peak_seconds",
                "heartbeat_interval_s", "gaps", "outages", "latency_s"}}},
                times as ISO 8601 UTC and durations in seconds.
    """
    clients: dict[str, dict[str, object]] = {}
    files: dict[str, dict[str, object]] = {}
    for client in sorted(paths):
        client_counts: Counter[str] = Counter()
        for path in paths[client]:
            stats = _FileStats(path, fields)
            stats.read()
            client_counts.update(stats.event_counts)
            files[path.name] = stats.to_json(client)
        clients[client] = {"event_counts": dict(sorted(client_counts.items()))}
    return {
        "gap_threshold_s": fields.gap_threshold_s,
        "heartbeat_event": fields.heartbeat_event,
        "clients": clients,
        "files": files,
    }


def resolve_run_dir(results_root: Path, run_id: str) -> Path:
    """Return results_root/run_id, raising AnalysisError unless it is an existing run directory."""
    run_dir = results_root / run_id
    if not run_dir.is_dir():
        raise AnalysisError(f"run {run_id!r} not found under {results_root}")
    return run_dir


def last_trades_payload(rest_log: Path) -> object:
    """Return the parsed body of the last trades response recorded in rest_log.

    Lines for requests that got no response are passed over; the last
    response must be a 2xx JSON body, since falling back to an older
    snapshot would compare SSE against a stale list.
    """
    last: tuple[int, dict[str, object]] | None = None
    for number, record in read_jsonl(rest_log):
        if record.get("op") == _TRADES_OP and "status" in record:
            last = (number, record)
    if last is None:
        raise AnalysisError(f"{rest_log}: no {_TRADES_OP} response recorded")

    number, record = last
    status = record["status"]
    if isinstance(status, bool) or not isinstance(status, int) or not httpx.codes.is_success(status):
        raise AnalysisError(f"{rest_log}:{number}: last {_TRADES_OP} response is HTTP {status}")
    try:
        if isinstance(record.get("resp_body"), str):
            return json.loads(str(record["resp_body"]))
        return json.loads(base64.b64decode(str(record["resp_body_b64"]), validate=True))
    except (KeyError, ValueError) as exc:
        raise AnalysisError(f"{rest_log}:{number}: last {_TRADES_OP} body is not JSON: {exc}") from exc


def _query_trades(cfg: P1Config, run_dir: Path, transport: httpx.BaseTransport | None) -> object:
    """Check simulation mode, then fetch and record the trades list in run_dir/rest.jsonl."""
    endpoint = cfg.endpoints.get(_TRADES_OP)
    if endpoint is None:
        raise AnalysisError(f"no [endpoints.{_TRADES_OP}] configured")
    with CapturingClient(
        cfg.base_url, "reconcile", run_dir / "rest.jsonl", cfg.http_timeout_s, transport=transport
    ) as client:
        assert_simulation(client, cfg)
        response = client.request(_TRADES_OP, endpoint, json_body=_TRADES_BODY)
    if not httpx.codes.is_success(response.status):
        raise AnalysisError(f"{_TRADES_OP} returned HTTP {response.status}")
    try:
        return response.json()
    except ValueError as exc:
        raise AnalysisError(f"{_TRADES_OP} response is not JSON: {exc}") from exc


def _write_json(path: Path, document: Mapping[str, object]) -> None:
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_reconcile(
    cfg: P1Config,
    sse_run: str,
    rest_run: str | None,
    run_dir: Path,
    *,
    trading_day: date | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Write run_dir/reconcile.json and return 0 if SSE and REST agree, 2 if not.

    Without rest_run, the trades list is fetched live after the
    simulation-mode check, and recorded in run_dir; with it, the last
    trades response in that run's rest.jsonl is used. REST is read
    before the SSE files, so an order placed meanwhile shows up as only
    in SSE rather than silently widening the comparison. Only SSE orders
    of trading_day take part, which defaults to the date of the
    snapshot's order times. InterlockError propagates; any other failure
    to analyse is logged and returns 1.
    """
    try:
        sse_dir = resolve_run_dir(cfg.results_root, sse_run)
        if rest_run is None:
            payload = _query_trades(cfg, run_dir, transport)
        else:
            payload = last_trades_payload(resolve_run_dir(cfg.results_root, rest_run) / "rest.jsonl")
        rest = orders_from_rest(payload, cfg.events)
        day = trading_day if trading_day is not None else trading_day_of(payload, cfg.events)
        sse_files = discover_sse_files(sse_dir)
        report = reconcile(orders_from_sse(sse_files, cfg.events, day), rest)
    except (AnalysisError, OSError) as exc:
        logger.error("reconcile failed: %s", exc)
        return EXIT_ANALYSIS_ERROR

    document: dict[str, object] = {
        "sse_run": sse_run,
        "rest_run": rest_run,
        "trading_day": day.isoformat(),
        **report.to_json(),
    }
    _write_json(run_dir / "reconcile.json", document)
    logger.info(
        "reconcile: %d only in REST, %d only in SSE, %d mismatched, missing orders %s, missing reports %s",
        len(report.only_in_rest),
        len(report.only_in_sse),
        len(report.mismatched),
        ", ".join(f"{client}={len(keys)}" for client, keys in report.missing_by_client.items()),
        ", ".join(f"{client}={len(ids)}" for client, ids in report.missing_events_by_client.items()),
    )
    return EXIT_OK if report.clean else EXIT_CHECK_FAILED


def run_stats(cfg: P1Config, sse_run: str, run_dir: Path) -> int:
    """Write run_dir/stats.json for a stream run and return 0, or log the failure and return 1."""
    try:
        sse_files = discover_sse_files(resolve_run_dir(cfg.results_root, sse_run))
        document = {"sse_run": sse_run, **stream_stats(sse_files, cfg.events)}
    except (AnalysisError, OSError) as exc:
        logger.error("stats failed: %s", exc)
        return EXIT_ANALYSIS_ERROR
    _write_json(run_dir / "stats.json", document)
    file_count = sum(len(client_paths) for client_paths in sse_files.values())
    logger.info("stats: %d file(s) from %d client(s)", file_count, len(sse_files))
    return EXIT_OK
