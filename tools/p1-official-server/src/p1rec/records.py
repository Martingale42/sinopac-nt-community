"""Read recorded JSONL and SSE files, and the numbers and times inside them.

Shared by the reconcile and stats analyses, which only read what stream
and orders runs already wrote, along with their exit codes, run
directory lookup and JSON output.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Iterator, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from p1rec.sse import SseFrame, parse_frame

logger = logging.getLogger(__name__)

# Exit codes of the analysis subcommands: success, input that cannot be
# analysed, and a check that found a difference.
EXIT_OK = 0
EXIT_ANALYSIS_ERROR = 1
EXIT_CHECK_FAILED = 2

NS_PER_S = 1_000_000_000
NS_PER_US = 1_000

# Exchange time strings carry no zone and are Taipei local time
# (docs/official-server-contract.md section 2).
EXCHANGE_TZ_NAME = "Asia/Taipei"

UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class AnalysisError(ValueError):
    """Raised when recorded input cannot be analysed as configured."""


def read_jsonl(path: Path, contains: bytes | None = None) -> Iterator[tuple[int, dict[str, object]]]:
    """Yield (line number, record) for each JSON object line of path.

    Blank lines are skipped. An unreadable last line is skipped with a
    WARNING, since appends are not fsynced and a power loss mid-write
    leaves a torn or NUL-padded tail; any earlier unreadable line raises
    AnalysisError, since a crash explains only the tail. With contains,
    lines without those bytes are skipped undecoded, which spares a large
    file's JSON parsing when only a few lines matter, so only lines that
    contain them are checked.
    """
    bad_line: tuple[int, str] | None = None
    with path.open("rb") as fh:
        for number, raw_line in enumerate(fh, start=1):
            text = raw_line.strip()
            if not text:
                continue
            if bad_line is not None:
                raise AnalysisError(f"{path}:{bad_line[0]}: unreadable line before the end of the file: {bad_line[1]}")
            if contains is not None and contains not in text:
                continue
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


def recv_ts_of(record: Mapping[str, object], path: Path, number: int) -> int:
    """Return a recorded line's recv_ts_ns, raising AnalysisError naming path:number if it is not an integer."""
    value = record.get("recv_ts_ns")
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnalysisError(f"{path}:{number}: recv_ts_ns is missing or not an integer")
    return value


def frame_of(record: Mapping[str, object], path: Path, number: int) -> SseFrame:
    """Parse a frame line's raw text, raising AnalysisError naming path:number if it has none."""
    raw = record.get("raw")
    if not isinstance(raw, str):
        raise AnalysisError(f"{path}:{number}: frame line has no raw text")
    return parse_frame(raw)


def iter_frames(path: Path, contains: bytes | None = None) -> Iterator[tuple[int, int, SseFrame]]:
    """Yield (line number, recv_ts_ns, parsed frame) for each frame line of an SSE file, as read_jsonl filters them."""
    for number, record in read_jsonl(path, contains):
        if record.get("kind") == "frame":
            yield number, recv_ts_of(record, path, number), frame_of(record, path, number)


def to_decimal(value: object) -> Decimal:
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
    return int((to_decimal(value) * NS_PER_S).to_integral_value())


def taipei_date(ts_ns: int) -> date:
    """Return the Asia/Taipei calendar date of an epoch-nanosecond instant.

    Definition: The local calendar date in Taiwan at that instant.
    Formula:    date(floor(ts_ns / 1e9) seconds after the epoch, in
                Asia/Taipei).
    Domain:     Integer nanoseconds since 1970-01-01T00:00Z; Asia/Taipei
                is UTC+8 with no daylight saving time.
    Returns:    A date; from 16:00Z on, an instant falls on the next day's date.
    """
    return datetime.fromtimestamp(ts_ns // NS_PER_S, tz=exchange_tz()).date()


@functools.cache
def exchange_tz() -> ZoneInfo:
    """Return the exchange's zone, loaded on first use so a host without tzdata fails only here."""
    return ZoneInfo(EXCHANGE_TZ_NAME)


def iso_utc(ts_ns: int, timespec: str = "microseconds") -> str:
    """Return epoch nanoseconds as an ISO 8601 UTC string.

    Definition: The wall-clock time an epoch-nanosecond stamp names.
    Formula:    s, r = divmod(ts_ns, 1e9); UTC datetime of s with
                microsecond = r // NS_PER_US, formatted with timespec.
    Domain:     Integer nanoseconds since 1970-01-01T00:00Z; sub-microsecond
                digits are truncated, not rounded.
    Returns:    For example "2026-10-01T01:00:00.000000Z".
    """
    seconds, remainder = divmod(ts_ns, NS_PER_S)
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=remainder // NS_PER_US)
    return moment.isoformat(timespec=timespec).replace("+00:00", "Z")


def resolve_run_dir(results_root: Path, run_id: str) -> Path:
    """Return results_root/run_id, raising AnalysisError unless it is an existing run directory."""
    run_dir = results_root / run_id
    if not run_dir.is_dir():
        raise AnalysisError(f"run {run_id!r} not found under {results_root}")
    return run_dir


def write_json(path: Path, document: Mapping[str, object]) -> None:
    """Write document to path as indented UTF-8 JSON ending in a newline."""
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
