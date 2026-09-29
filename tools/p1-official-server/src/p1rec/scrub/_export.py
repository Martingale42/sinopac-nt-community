"""Read fixtures.toml, scrub the lines it selects, and write them with their MANIFEST.toml."""

from __future__ import annotations

import base64
import json
import re
import shutil
import tempfile
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from p1rec.config import ConfigError, P1Config
from p1rec.scrub._common import REST_SOURCE, ScrubError, decode_b64, logger
from p1rec.scrub._discovery import key_categories
from p1rec.scrub._matching import Leak, Replacer, cached_replacer, original_pattern, scan
from p1rec.sse import parse_frame

# CSI escape sequences, such as the colour codes a log written for a terminal carries
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# A leading ISO-8601 date and time with optional fraction and UTC offset,
# as the official server's log (2026-09-29T10:04:55.724337Z) and the
# recorder's own log format (2026-09-29 19:41:05,203, local time) begin.
_LOG_TIMESTAMP_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?"
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_NS_PER_S = 1_000_000_000
_NS_PER_US = 1_000
_NS_FRACTION_DIGITS = 9

_SSE_SOURCE_RE = re.compile(r"sse_.+\.jsonl")
_MANIFEST_NAME = "MANIFEST.toml"
_ENTRY_KEYS = ("name", "run_id", "source", "lines", "checklist", "instrument")
_OPTIONAL_ENTRY_KEYS = ("recorded_at",)


class LeakError(ScrubError):
    """Raised by export_fixtures when its scrubbed output still holds an original value."""

    def __init__(self, leaks: Sequence[Leak], staging_dir: Path) -> None:
        super().__init__(f"{len(leaks)} original values remain in the scrubbed output staged at {staging_dir}")
        self.leaks = tuple(leaks)
        self.staging_dir = staging_dir


@dataclass(frozen=True)
class FixtureEntry:
    """One [[fixture]] table of fixtures.toml."""

    index: int  # Position in fixtures.toml, which names the entry in errors raised before its name is checked
    name: str
    run_id: str
    source: str
    lines: tuple[int, ...]
    checklist: tuple[str, ...]
    instrument: str
    recorded_at_ns: int | None  # Given explicitly, for a source whose first selected line has no timestamp


def _plain_name(value: str) -> bool:
    """Return True if value is usable as a single file or directory name."""
    return (
        bool(value)
        and not value.startswith(".")
        and value.isprintable()
        and not any(separator in value for separator in ("/", "\\"))
    )


def _explicit_recorded_at(label: str, value: object) -> int:
    """Return an entry's explicit recorded_at, a TOML or ISO-8601 date-time with a UTC offset, as ns."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            value = None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ConfigError(f"{label}: recorded_at must be a date-time with a UTC offset")
    return _datetime_ns(value)


def _parse_entry(selection: Path, index: int, raw: object) -> FixtureEntry:
    # Named by index alone: a value checked only later, such as the name, may hold an original
    label = f"{selection}: fixture {index}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be a table")

    unknown = sorted(set(raw) - set(_ENTRY_KEYS) - set(_OPTIONAL_ENTRY_KEYS))
    if unknown:
        raise ConfigError(f"{label}: unknown keys {', '.join(unknown)}")
    missing = [key for key in _ENTRY_KEYS if key not in raw]
    if missing:
        raise ConfigError(f"{label}: missing keys {', '.join(missing)}")

    for key in ("name", "run_id", "source"):
        value = raw[key]
        if not isinstance(value, str) or not _plain_name(value):
            raise ConfigError(f"{label}: {key} must be a single file name, without path separators")
    lines = raw["lines"]
    if (
        not isinstance(lines, list)
        or not lines
        or not all(isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in lines)
        or len(set(lines)) != len(lines)
    ):
        raise ConfigError(f"{label}: lines must be a non-empty array of distinct 1-based line numbers")
    checklist = raw["checklist"]
    if not isinstance(checklist, list) or not checklist or not all(isinstance(i, str) and i for i in checklist):
        raise ConfigError(f"{label}: checklist must be a non-empty array of checklist item names")
    if not isinstance(raw["instrument"], str):
        raise ConfigError(f"{label}: instrument must be a string")

    return FixtureEntry(
        index=index,
        name=raw["name"],
        run_id=raw["run_id"],
        source=raw["source"],
        lines=tuple(lines),
        checklist=tuple(checklist),
        instrument=raw["instrument"],
        recorded_at_ns=_explicit_recorded_at(label, raw["recorded_at"]) if "recorded_at" in raw else None,
    )


def load_selection(path: Path) -> list[FixtureEntry]:
    """Load fixtures.toml, whose [[fixture]] tables list what to export; none means an empty list."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read fixture selection {path} ({exc})") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    unknown = sorted(set(data) - {"fixture"})
    if unknown:
        raise ConfigError(f"{path}: unknown keys {', '.join(unknown)}; list fixtures as [[fixture]] tables")
    raw_entries = data.get("fixture", [])
    if not isinstance(raw_entries, list):
        raise ConfigError(f"{path}: fixture must be an array of tables")

    entries: list[FixtureEntry] = []
    first_index: dict[str, int] = {}
    for index, raw in enumerate(raw_entries):
        entry = _parse_entry(path, index, raw)
        if entry.name in first_index:
            raise ConfigError(f"{path}: fixture {index} repeats the name of fixture {first_index[entry.name]}")
        first_index[entry.name] = index
        entries.append(entry)
    return entries


def _check_entry_fields(selection: Path, entry: FixtureEntry, mapping: Mapping[str, str]) -> None:
    """Raise ConfigError, naming the entry by index only, if a field that goes public holds an original."""
    fields = {"name": entry.name, "run_id": entry.run_id, "source": entry.source, "instrument": entry.instrument}
    fields.update({f"checklist[{i}]": item for i, item in enumerate(entry.checklist)})
    for original in mapping:
        if not original:
            continue
        pattern = original_pattern(original)
        for key, value in fields.items():
            if pattern.search(value):
                raise ConfigError(f"{selection}: fixture {entry.index}: {key} contains an original value")


def _select_lines(source_path: Path, entry: FixtureEntry) -> list[bytes]:
    """Return the entry's lines of source_path, in the entry's order, reading no further than needed."""
    if not source_path.is_file():
        raise ConfigError(f"fixture {entry.name!r}: source {source_path} does not exist")
    wanted = set(entry.lines)
    last = max(wanted)
    found: dict[int, bytes] = {}
    with source_path.open("rb") as fh:
        for number, line in enumerate(fh, start=1):
            if number in wanted:
                found[number] = line
            if number >= last:
                break
    missing = [number for number in entry.lines if number not in found]
    if missing:
        raise ConfigError(f"fixture {entry.name!r}: {entry.run_id}/{entry.source} has no line {missing[0]}")
    return [found[number] for number in entry.lines]


def _parse_record(entry: FixtureEntry, number: int, line: bytes) -> dict[str, Any]:
    try:
        record = json.loads(line)
    except ValueError:
        record = None
    if not isinstance(record, dict):
        raise ConfigError(f"fixture {entry.name!r}: line {number} of {entry.source} is not a JSON record")
    return record


@dataclass(frozen=True)
class _FixtureScrubber:
    """Scrub one fixture's content through a replacer, counting replacements by category."""

    replacer: Replacer
    counts: Counter[str]

    def scrub_str(self, text: str) -> str:
        return self.replacer.scrub_str(text, self.counts)

    def scrub_bytes(self, data: bytes) -> bytes:
        return self.replacer.scrub_bytes(data, self.counts)


def _scrub_b64(key: str, value: str, scrubber: _FixtureScrubber) -> str:
    data = decode_b64(value)
    if data is None:
        raise ScrubError(f"field {key} is not valid base64, so it cannot be scrubbed")
    return base64.b64encode(scrubber.scrub_bytes(data)).decode("ascii")


def _scrub_record(record: Mapping[str, object], scrubber: _FixtureScrubber) -> dict[str, object]:
    """Scrub every key, string and base64 field in a parsed record, leaving numbers alone.

    The numbers in a record are the recorder's own timestamps and status
    codes, and replacing digits inside them would corrupt the JSON.
    """
    return {
        scrubber.scrub_str(key): (
            _scrub_b64(key, value, scrubber)
            if key.endswith("_b64") and isinstance(value, str)
            else _scrub_json(value, scrubber)
        )
        for key, value in record.items()
    }


def _scrub_json(value: object, scrubber: _FixtureScrubber) -> object:
    if isinstance(value, str):
        return scrubber.scrub_str(value)
    if isinstance(value, list):
        return [_scrub_json(item, scrubber) for item in value]
    if isinstance(value, dict):
        return _scrub_record(value, scrubber)
    return value


def _check_json_kept(before: object, after: object, where: str) -> None:
    """Raise ScrubError if scrubbing turned valid JSON text into invalid JSON."""
    if not isinstance(before, str) or not isinstance(after, str) or before == after:
        return
    try:
        json.loads(before)
    except ValueError:
        return
    try:
        json.loads(after)
    except ValueError:
        raise ScrubError(
            f"scrubbing broke the JSON in {where}: an original also occurs outside a JSON string, "
            "such as inside a number"
        ) from None


@dataclass(frozen=True)
class _Selection:
    """One entry's selected lines, and their parsed records for a REST or SSE source."""

    entry: FixtureEntry
    lines: list[bytes]
    records: list[dict[str, Any]]  # Empty for a log source


def _select(cfg: P1Config, entry: FixtureEntry) -> _Selection:
    """Read and parse one entry's selected lines, requiring frame records from an SSE source."""
    lines = _select_lines(cfg.results_root / entry.run_id / entry.source, entry)
    if entry.source != REST_SOURCE and not _SSE_SOURCE_RE.fullmatch(entry.source):
        return _Selection(entry=entry, lines=lines, records=[])
    records = [_parse_record(entry, number, line) for number, line in zip(entry.lines, lines, strict=True)]
    if _SSE_SOURCE_RE.fullmatch(entry.source):
        for number, record in zip(entry.lines, records, strict=True):
            if record.get("kind") != "frame" or not isinstance(record.get("raw"), str):
                raise ConfigError(f"fixture {entry.name!r}: line {number} of {entry.source} is not a frame record")
    return _Selection(entry=entry, lines=lines, records=records)


def _export_rest(selection: _Selection, scrubber: _FixtureScrubber) -> tuple[bytes, int | None]:
    """Return the selected REST records as a scrubbed, pretty-printed JSON array."""
    entry = selection.entry
    scrubbed_records = []
    for number, record in zip(entry.lines, selection.records, strict=True):
        scrubbed = _scrub_record(record, scrubber)
        for key in ("req_body", "resp_body"):
            _check_json_kept(record.get(key), scrubbed.get(key), f"{key} of line {number} of {entry.source}")
        scrubbed_records.append(scrubbed)
    text = json.dumps(scrubbed_records, ensure_ascii=False, indent=2) + "\n"
    return text.encode("utf-8"), _record_ns(selection.records[0])


def _export_sse(selection: _Selection, scrubber: _FixtureScrubber) -> tuple[bytes, int | None]:
    """Return the selected frames' exact bytes, scrubbed and concatenated into stream text."""
    entry = selection.entry
    frames: list[bytes] = []
    for number, record in zip(entry.lines, selection.records, strict=True):
        raw_b64 = record.get("raw_b64")
        data = record["raw"].encode("utf-8") if not isinstance(raw_b64, str) else decode_b64(raw_b64)
        if data is None:
            raise ScrubError(f"line {number} of {entry.source} has invalid base64")
        scrubbed = scrubber.scrub_bytes(data)
        _check_json_kept(
            parse_frame(data.decode("utf-8", "replace")).data,
            parse_frame(scrubbed.decode("utf-8", "replace")).data,
            f"the data of line {number} of {entry.source}",
        )
        frames.append(scrubbed)
    return b"".join(frames), _record_ns(selection.records[0])


def _export_selection(selection: _Selection, scrubber: _FixtureScrubber) -> tuple[str, bytes, int | None]:
    """Return the output file name, scrubbed content and the first selected line's time, if it has one."""
    entry = selection.entry
    try:
        if entry.source == REST_SOURCE:
            data, recorded_ns = _export_rest(selection, scrubber)
            return f"{entry.name}.json", data, recorded_ns
        if _SSE_SOURCE_RE.fullmatch(entry.source):
            data, recorded_ns = _export_sse(selection, scrubber)
            return f"{entry.name}.sse", data, recorded_ns
        lines = selection.lines
        return f"{entry.name}.log", b"".join(scrubber.scrub_bytes(line) for line in lines), _log_line_ns(lines[0])
    except ScrubError as exc:
        raise ScrubError(f"fixture {entry.name!r}: {exc}") from None


def _datetime_ns(moment: datetime) -> int:
    """Return an aware datetime as nanoseconds since the Unix epoch, exactly."""
    return (moment - _EPOCH) // timedelta(microseconds=1) * _NS_PER_US


def _log_line_ns(line: bytes) -> int | None:
    """Return the time a log line starts with, after any colour codes, as ns, or None.

    A time without a UTC offset, as the recorder's own log writes, is local
    time, read in this host's time zone, since scrub runs on the host that
    recorded the log.
    """
    text = _ANSI_CSI_RE.sub("", line.decode("utf-8", "replace")).lstrip()
    match = _LOG_TIMESTAMP_RE.match(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(group) for group in match.groups()[:6])
    fraction, offset = match.group(7) or "", match.group(8)
    try:
        moment = datetime(year, month, day, hour, minute, second, tzinfo=_utc_offset(offset))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone(timezone.utc)
    return _datetime_ns(moment) + int(fraction.ljust(_NS_FRACTION_DIGITS, "0"))


def _utc_offset(text: str | None) -> timezone | None:
    """Parse Z, +hh:mm or +hhmm into a timezone, or None for no offset."""
    if text is None:
        return None
    if text == "Z":
        return timezone.utc
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))


def _record_ns(record: Mapping[str, object]) -> int | None:
    """Return when the recorder received a record's reply, or recorded its failure, as ns."""
    for key in ("recv_ts_ns", "error_ts_ns"):
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _recorded_at(entry: FixtureEntry, derived_ns: int | None) -> int:
    """Return the entry's recording time: from its first selected line, or else given explicitly."""
    if derived_ns is not None and entry.recorded_at_ns is not None:
        raise ConfigError(
            f"fixture {entry.name!r}: recorded_at is only for a source whose first selected line has no timestamp"
        )
    if derived_ns is not None:
        return derived_ns
    if entry.recorded_at_ns is not None:
        return entry.recorded_at_ns
    raise ConfigError(f"fixture {entry.name!r}: the first selected line has no timestamp, so give recorded_at")


def _iso_utc(ns: int) -> str:
    """Format nanoseconds since the Unix epoch as ISO-8601 UTC, keeping every digit."""
    seconds, fraction = divmod(ns, _NS_PER_S)
    return f"{datetime.fromtimestamp(seconds, tz=timezone.utc):%Y-%m-%dT%H:%M:%S}.{fraction:0{_NS_FRACTION_DIGITS}d}Z"


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        # JSON string escapes are all valid TOML ones, except that TOML also forbids a raw DEL
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to MANIFEST.toml")


def _render_manifest(entries: Sequence[Mapping[str, object]]) -> str:
    out = ["# Written by `p1rec scrub` from fixtures.toml, one [[fixture]] per exported file.", ""]
    for entry in entries:
        out.append("[[fixture]]")
        out.extend(f"{key} = {_toml_value(value)}" for key, value in entry.items())
        out.append("")
    return "\n".join(out)


def export_fixtures(cfg: P1Config, mapping: Mapping[str, str]) -> list[Path]:
    """Scrub the lines fixtures.toml selects from results_root and write them to fixtures_root.

    A rest.jsonl source becomes <name>.json, the records as a JSON array;
    an sse_*.jsonl source becomes <name>.sse, its frames' raw text; any
    other file becomes <name>.log, its lines. MANIFEST.toml records each
    file's run_id, source, lines, recording time, instrument and checklist
    items; the recording time comes from the first selected line (its
    recv_ts_ns or error_ts_ns, or the time a log line starts with), or
    else from the entry's explicit recorded_at, never from file times.
    mapping must hold every original, "ca" signatures and
    scrub.account_id_keys values included, as build_map over discover's
    result does; one it lacks is caught by the scan as a leak. The number
    of replacements per category is logged for each fixture. Every entry
    is validated, and the output staged and scanned under results_root,
    before anything reaches fixtures_root: an invalid entry, or one with an
    original in a field that goes public, raises ConfigError, and output
    that still holds an original raises LeakError, leaving fixtures_root
    untouched and the staged copy in place for inspection. Returns the
    written paths, MANIFEST.toml last.
    """
    entries = load_selection(cfg.scrub.selection)
    for entry in entries:
        _check_entry_fields(cfg.scrub.selection, entry, mapping)
    selections = [_select(cfg, entry) for entry in entries]
    replacer = cached_replacer(mapping)

    outputs: dict[str, bytes] = {}
    manifest: list[dict[str, object]] = []
    for selection in selections:
        entry = selection.entry
        scrubber = _FixtureScrubber(replacer=replacer, counts=Counter())
        filename, data, derived_ns = _export_selection(selection, scrubber)
        logger.info(
            "fixture %r: replaced %s",
            entry.name,
            ", ".join(f"{category}={count}" for category, count in sorted(scrubber.counts.items())) or "nothing",
        )
        outputs[filename] = data
        manifest.append(
            {
                "file": filename,
                "run_id": entry.run_id,
                "source": entry.source,
                "lines": list(entry.lines),
                "recorded_at": _iso_utc(_recorded_at(entry, derived_ns)),
                "instrument": entry.instrument,
                "checklist": list(entry.checklist),
            }
        )
    outputs[_MANIFEST_NAME] = _render_manifest(manifest).encode("utf-8")

    cfg.results_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="scrub-staging-", dir=cfg.results_root))
    staged = []
    for filename, data in outputs.items():
        (staging / filename).write_bytes(data)
        staged.append(staging / filename)
    leaks = scan(staged, mapping, key_categories(cfg))
    if leaks:
        raise LeakError(leaks, staging)

    cfg.fixtures_root.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, data in outputs.items():
        (cfg.fixtures_root / filename).write_bytes(data)
        written.append(cfg.fixtures_root / filename)
    shutil.rmtree(staging)
    return written
