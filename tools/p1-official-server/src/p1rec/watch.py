"""Check the soak host once and record what is wrong in a few status files.

p1watch runs from a systemd timer on the soak host. Each run checks the
official server's health, the server log written since the last run, how
recently the latest stream run received quotes, the systemd units, the
host's disk and memory and, at most once per egress_check_interval_s, the
public egress IP. It then overwrites status.json, appends the same document
to watch.jsonl, and keeps what the next run needs in state.json, all in
--state-dir; it never creates a run directory.

Nothing it writes or logs holds an IP address, a key or an account: the
egress IP is only compared with [secrets] egress_ip, server log lines are
only counted, and the health response's error texts are reduced to whether
they are present. It exits 0 whatever the checks find, and 1 only when it
is started wrongly or cannot load its configuration, write its state or
finish at all, so the timer's journal shows a failure only when the watch
itself is broken.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, NoReturn, Protocol

import httpx

from p1rec.capture import append_jsonl, describe_exception, make_http_client
from p1rec.config import ConfigError, P1Config, WatchConfig, load_config, read_secret_role
from p1rec.records import NS_PER_S, NS_PER_US, UNIX_EPOCH, AnalysisError, discover_sse_files, exchange_tz
from p1rec.redact import RedactingFormatter
from p1rec.sse import parse_frame

logger = logging.getLogger(__name__)

ANOMALY_CLASSES = (
    "health_not_healthy",
    "relogin_failed",
    "quotes_stale",
    "service_down",
    "resources",
    "egress_changed",
)

EXIT_OK = 0
EXIT_FAILED = 1

STATE_FILE = "state.json"
STATUS_FILE = "status.json"
HISTORY_FILE = "watch.jsonl"

# The state directory sits beside the host's secrets, so only its owner may enter it
_PRIVATE_DIR_MODE = 0o700

# The HealthResponse status of a server whose token is stale but whose
# session still answers (docs/official-server-contract.md section 2).
_DEGRADED = "degraded"

# status.json's health fields that health_not_healthy's detail repeats
_HEALTH_DETAIL_KEYS = ("status", "token_stale", "session_recovering", "session_recovery_attempts")

_EGRESS_ROLE = "egress_ip"

_BYTES_PER_KIB = 1024
_BYTES_PER_MIB = 1024**2
_BYTES_PER_GIB = 1024**3

_UNIT_PROPERTIES = ("ActiveState", "NRestarts", "MemoryCurrent")
_ACTIVE = "active"

_MEMINFO = Path("/proc/meminfo")

# Terminal control sequences, such as the colour codes around the server's log text
_ANSI_ESCAPE_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")

# How much of an SSE recording latest_data_frame_ns reads at a time, from the end backwards
_TAIL_BLOCK_BYTES = 64 * 1024

# Run directories that rundir.new_run names for the stream subcommand, tagged or not
_STREAM_RUN_RE = re.compile(r"\d{8}T\d{6}Z-stream(-[A-Za-z0-9._-]+)?")

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# The journal stamps each line itself
_LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"
_QUIET_THIRD_PARTY_LOGGERS = ("httpx", "httpcore")


@dataclass(frozen=True)
class Anomaly:
    """One anomaly class that the latest run found."""

    name: str  # One of ANOMALY_CLASSES
    since: datetime  # The first run of the unbroken series of runs that found it
    detail: Mapping[str, object]  # Counts, ages and flags only, never text from the server or its log


class Probes(Protocol):
    """What run_watch asks the outside world, so a test can answer instead."""

    def health(self) -> tuple[int | None, Mapping[str, object] | None]:
        """Return the health check's HTTP status and JSON object body, None for what never arrived."""
        ...

    def unit(self, name: str) -> Mapping[str, str]:
        """Return the unit's ActiveState, NRestarts and MemoryCurrent as systemctl shows them, {} if unknown."""
        ...

    def egress_ip(self) -> str | None:
        """Return the host's public IPv4 address, or None if it could not be learned."""
        ...

    def disk_free_bytes(self, path: Path) -> int:
        """Return the bytes an unprivileged user may still write on path's file system."""
        ...

    def meminfo(self) -> Mapping[str, int]:
        """Return /proc/meminfo's fields, in bytes where the kernel gives kB."""
        ...


class SystemProbes:
    """Probes that ask the official server, systemctl, the egress service and the kernel.

    transport is None for real network I/O; a test passes an
    httpx.MockTransport to answer both HTTP probes in-process.
    """

    def __init__(self, cfg: P1Config, *, transport: httpx.BaseTransport | None = None) -> None:
        watch = _watch_config(cfg)
        self._base_url = cfg.base_url
        self._health = cfg.endpoints[watch.health_op]
        self._egress_url = watch.egress_url
        self._timeout = httpx.Timeout(cfg.http_timeout_s)
        self._transport = transport

    def health(self) -> tuple[int | None, Mapping[str, object] | None]:
        try:
            with make_http_client(self._base_url, self._timeout, transport=self._transport) as client:
                response = client.request(self._health.method, self._health.path)
        except httpx.HTTPError as exc:
            logger.warning("health check failed: %s", describe_exception(exc))
            return None, None
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body if isinstance(body, dict) else None

    def unit(self, name: str) -> Mapping[str, str]:
        properties = [arg for prop in _UNIT_PROPERTIES for arg in ("-p", prop)]
        try:
            result = subprocess.run(
                ["systemctl", "show", *properties, "--", name], capture_output=True, text=True, check=False
            )
        except OSError as exc:
            logger.warning("cannot run systemctl for %s: %s", name, describe_exception(exc))
            return {}
        if result.returncode != 0:
            logger.warning("systemctl show %s exited %d", name, result.returncode)
            return {}
        return _parse_unit_properties(result.stdout)

    def egress_ip(self) -> str | None:
        try:
            with httpx.Client(timeout=self._timeout, trust_env=False, transport=self._transport) as client:
                response = client.get(self._egress_url)
        except httpx.HTTPError as exc:
            logger.warning("egress query failed: %s", describe_exception(exc))
            return None
        text = response.text.strip()
        if response.status_code != httpx.codes.OK or not _is_ipv4(text):
            logger.warning("egress query answered HTTP %d without an IPv4 address", response.status_code)
            return None
        return text

    def disk_free_bytes(self, path: Path) -> int:
        return shutil.disk_usage(path).free

    def meminfo(self) -> Mapping[str, int]:
        return _parse_meminfo(_MEMINFO.read_text(encoding="ascii"))


def _parse_unit_properties(text: str) -> dict[str, str]:
    """Return systemctl show's Name=value lines as a mapping."""
    properties: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep:
            properties[name] = value
    return properties


def _parse_meminfo(text: str) -> dict[str, int]:
    """Return /proc/meminfo's "Name: value [kB]" lines as a mapping, kB values in bytes."""
    fields: dict[str, int] = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        parts = rest.split()
        if not sep or not parts or not parts[0].isdigit():
            continue
        scale = _BYTES_PER_KIB if parts[1:] == ["kB"] else 1
        fields[name] = int(parts[0]) * scale
    return fields


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def session_open(now: datetime, holidays: frozenset[date], cfg: WatchConfig) -> bool:
    """Return whether now falls in a trading session, when quotes should keep arriving.

    Definition: Whether the Taipei wall-clock time of now lies in the day
                session or the night session of a business day.
    Formula:    open = (B(d) and t in day_session) or (B(d) and t >= night
                start) or (B(d - 1) and t < night end), where d and t are
                now's Taipei date and time, B(x) = x is Monday to Friday
                and not in holidays, and a session is [start, end).
    Domain:     now is timezone-aware. A session whose end comes before its
                start runs past midnight into the next calendar day, which
                need not be a business day itself; one whose start comes
                first opens on business days alone.
    Returns:    True inside a session, False otherwise.
    """
    local = now.astimezone(exchange_tz())
    today, moment = local.date(), local.time()
    for start, end in (cfg.day_session, cfg.night_session):
        if start < end:
            if _is_business_day(today, holidays) and start <= moment < end:
                return True
        elif (_is_business_day(today, holidays) and moment >= start) or (
            _is_business_day(today - timedelta(days=1), holidays) and moment < end
        ):
            return True
    return False


def _is_business_day(day: date, holidays: frozenset[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def _within(moment: time, window: tuple[time, time]) -> bool:
    """Return whether moment lies in the [start, end) window, which may run past midnight."""
    start, end = window
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def latest_data_frame_ns(path: Path, heartbeat_event: str, not_before_ns: int) -> int | None:
    """Return the recv_ts_ns of the last frame in an SSE recording that is not a heartbeat, or None.

    The file is read backwards from its end, and reading stops at the first
    record received before not_before_ns, so None means no such frame since
    then, and a long recording costs only its recent tail. A last line
    still being written, without its newline, and any line that is not a
    JSON object with an integer recv_ts_ns, are skipped.
    """
    with path.open("rb") as fh:
        for line in _lines_backwards(fh):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            recv_ts = record.get("recv_ts_ns") if isinstance(record, dict) else None
            if isinstance(recv_ts, bool) or not isinstance(recv_ts, int):
                continue
            if recv_ts < not_before_ns:
                return None
            raw = record.get("raw")
            if record.get("kind") == "frame" and isinstance(raw, str) and parse_frame(raw).event != heartbeat_event:
                return recv_ts
    return None


def _lines_backwards(fh: BinaryIO) -> Iterator[bytes]:
    """Yield fh's complete lines from the last to the first, without their newlines.

    The text after the last newline is a line still being written, so it is
    never yielded.
    """
    position = fh.seek(0, os.SEEK_END)
    pending = b""  # The start of a line whose end has already been read
    seen_newline = False
    while position > 0:
        size = min(_TAIL_BLOCK_BYTES, position)
        position -= size
        fh.seek(position)
        pieces = (fh.read(size) + pending).split(b"\n")
        pending, complete = pieces[0], pieces[1:]
        if not seen_newline:
            if not complete:
                pending = b""
                continue
            complete = complete[:-1]
            seen_newline = True
        yield from reversed(complete)
    if seen_newline:
        yield pending


@dataclass
class _State:
    """What one run leaves for the next in state.json."""

    server_log: str | None = None
    server_log_offset: int = 0

    # Consecutive runs whose health was neither healthy nor degraded inside the maintenance window
    degraded_checks: int = 0

    n_restarts: dict[str, int] = field(default_factory=dict)
    egress_checked_at: datetime | None = None
    egress_matches: bool | None = None
    since: dict[str, datetime] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "server_log": self.server_log,
            "server_log_offset": self.server_log_offset,
            "degraded_checks": self.degraded_checks,
            "n_restarts": self.n_restarts,
            "egress_checked_at": None if self.egress_checked_at is None else _iso(self.egress_checked_at),
            "egress_matches": self.egress_matches,
            "since": {name: _iso(moment) for name, moment in self.since.items()},
        }

    @classmethod
    def from_json(cls, document: object) -> _State:
        """Return the state document holds, raising ValueError if anything in it has the wrong type."""
        if not isinstance(document, dict):
            raise ValueError("not a JSON object")
        server_log = document.get("server_log")
        offset = document.get("server_log_offset", 0)
        checks = document.get("degraded_checks", 0)
        restarts = document.get("n_restarts", {})
        checked_at = document.get("egress_checked_at")
        matches = document.get("egress_matches")
        since = document.get("since", {})
        if (
            not (server_log is None or isinstance(server_log, str))
            or not _is_count(offset)
            or not _is_count(checks)
            or not (isinstance(restarts, dict) and all(_is_count(value) for value in restarts.values()))
            or not (checked_at is None or isinstance(checked_at, str))
            or not (matches is None or isinstance(matches, bool))
            or not (isinstance(since, dict) and all(isinstance(value, str) for value in since.values()))
        ):
            raise ValueError("a field has the wrong type")
        return cls(
            server_log=server_log,
            server_log_offset=offset,
            degraded_checks=checks,
            n_restarts=dict(restarts),
            egress_checked_at=None if checked_at is None else _parse_instant(checked_at),
            egress_matches=matches,
            since={name: _parse_instant(value) for name, value in since.items()},
        )


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_instant(text: str) -> datetime:
    """Parse an ISO 8601 time, raising ValueError unless it carries a UTC offset."""
    moment = datetime.fromisoformat(text)
    if moment.utcoffset() is None:
        raise ValueError(f"time without a UTC offset: {text!r}")
    return moment


def _epoch_ns(moment: datetime) -> int:
    """Return an aware datetime on the epoch-nanosecond scale of recv_ts_ns.

    Definition: The same instant as integer nanoseconds since the epoch.
    Formula:    ns = floor((moment - 1970-01-01T00:00Z) / 1 us) * 1000.
    Domain:     moment is timezone-aware; integer arithmetic keeps its
                microseconds exact, where float seconds would blur them.
    Returns:    Integer nanoseconds since 1970-01-01T00:00Z.
    """
    return (moment - UNIX_EPOCH) // timedelta(microseconds=1) * NS_PER_US


def _iso(moment: datetime) -> str:
    """Return moment as an ISO 8601 UTC time to the second, such as 2026-10-15T01:00:00Z."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_state(path: Path) -> _State:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _State()
    except OSError as exc:
        logger.warning("cannot read %s, starting from an empty state: %s", path, describe_exception(exc))
        return _State()
    try:
        return _State.from_json(json.loads(text))
    except ValueError as exc:
        logger.warning("%s is unreadable, starting from an empty state: %s", path, exc)
        return _State()


def run_watch(
    cfg: P1Config,
    egress_ips: Sequence[str],
    state_dir: Path,
    server_log: Path,
    holidays: frozenset[date],
    disk_path: Path,
    now: datetime,
    probes: Probes,
) -> int:
    """Run every check once, record the result in state_dir, and return the exit code.

    state_dir and any missing parent are created for their owner alone.
    now must be timezone-aware. Returns 0 whatever the checks find, and 1
    if state_dir cannot be created or a file in it cannot be written.
    """
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    watch = _watch_config(cfg)
    try:
        _make_private_dirs(state_dir)
    except OSError as exc:
        logger.error("cannot create the state directory %s: %s", state_dir, describe_exception(exc))
        return EXIT_FAILED
    state = _load_state(state_dir / STATE_FILE)

    health_detail, health = _check_health(probes, watch, now, state)
    stale_detail, stream_run, ages = _check_quotes(cfg, watch, holidays, now)
    units_detail, units = _check_units(probes, watch.services, state)
    found: dict[str, Mapping[str, object] | None] = {
        "health_not_healthy": health_detail,
        "relogin_failed": _check_server_log(server_log, watch.failure_patterns, state),
        "quotes_stale": stale_detail,
        "service_down": units_detail,
        "resources": _check_resources(probes, watch, disk_path),
        "egress_changed": _check_egress(probes, watch, egress_ips, now, state),
    }

    anomalies = [
        Anomaly(name, state.since.get(name, now), detail)
        for name in ANOMALY_CLASSES
        if (detail := found[name]) is not None
    ]
    state.since = {anomaly.name: anomaly.since for anomaly in anomalies}
    status: dict[str, object] = {
        "checked_at": _iso(now),
        "ok": not anomalies,
        "anomalies": [
            {"name": anomaly.name, "since": _iso(anomaly.since), "detail": dict(anomaly.detail)}
            for anomaly in anomalies
        ],
        "health": health,
        "units": units,
        "stream_run": stream_run,
        "data_frame_age_s": ages,
        "egress_matches": state.egress_matches,
    }

    try:
        _write_json_atomically(state_dir / STATUS_FILE, status)
        append_jsonl(state_dir / HISTORY_FILE, status)
        _write_json_atomically(state_dir / STATE_FILE, state.to_json())
    except OSError as exc:
        logger.error("cannot write the watch state in %s: %s", state_dir, describe_exception(exc))
        return EXIT_FAILED
    logger.info("anomalies: %s", ", ".join(anomaly.name for anomaly in anomalies) or "none")
    return EXIT_OK


def _watch_config(cfg: P1Config) -> WatchConfig:
    if cfg.watch is None:
        raise ConfigError("p1watch needs a [watch] section in p1.toml")
    return cfg.watch


def _make_private_dirs(directory: Path) -> None:
    """Create directory and each missing parent for their owner alone, and make directory itself private."""
    missing = [path for path in directory.parents if not path.exists()]
    for path in reversed(missing):
        path.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=True)
    directory.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=True)

    # chmod as well as mkdir's mode, which a umask can narrow and which an existing directory ignores
    directory.chmod(_PRIVATE_DIR_MODE)


def _write_json_atomically(path: Path, document: Mapping[str, object]) -> None:
    """Replace path with document as indented JSON through a temporary file, so no reader sees half of it."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _check_health(
    probes: Probes, watch: WatchConfig, now: datetime, state: _State
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Return health_not_healthy's detail if it holds, and status.json's health, counting degraded runs in state.

    Anything but healthy or degraded holds at once. Degraded is expected
    inside the maintenance window and holds only after
    degraded_after_checks consecutive runs outside it.
    """
    http_status, body = probes.health()
    reported = body.get("status") if body is not None else None
    answered = http_status == httpx.codes.OK
    degraded = answered and reported == _DEGRADED
    if answered and reported == watch.healthy_status:
        state.degraded_checks, holds = 0, False
    elif degraded and _within(now.astimezone(exchange_tz()).time(), watch.maintenance_window):
        state.degraded_checks, holds = 0, False
    else:
        state.degraded_checks += 1
        holds = not degraded or state.degraded_checks >= watch.degraded_after_checks

    detail: dict[str, object] = {"http_status": http_status, "degraded_checks": state.degraded_checks}
    if body is None:
        return (detail if holds else None), None
    health = _health_summary(body)
    if not holds:
        return None, health
    detail.update({key: health[key] for key in _HEALTH_DETAIL_KEYS})
    detail.update(
        has_session_recovery_error=bool(body.get("last_session_recovery_error")),
        has_maintenance_error=bool(body.get("last_maintenance_error")),
    )
    return detail, health


def _health_summary(body: Mapping[str, object]) -> dict[str, object]:
    """Return status.json's health: the fields worth watching, each None unless it has its schema's type.

    The error texts may quote the egress IP, so only whether either is set is kept.
    """
    return {
        "status": _typed(body, "status", str),
        "token_stale": _typed(body, "token_stale", bool),
        "session_recovering": _typed(body, "session_recovering", bool),
        "session_recovery_attempts": _typed(body, "session_recovery_attempts", int),
        "next_maintenance": _typed(body, "next_maintenance", str),
        "token_expires_in_seconds": _typed(body, "token_expires_in_seconds", int),
        "has_recovery_error": bool(body.get("last_session_recovery_error") or body.get("last_maintenance_error")),
    }


def _typed(body: Mapping[str, object], key: str, kind: type) -> object:
    value = body.get(key)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        return None
    return value


def _check_server_log(path: Path, patterns: Sequence[str], state: _State) -> dict[str, object] | None:
    """Return relogin_failed's detail if a line added since the last run holds a failure pattern.

    Reading starts where the last run stopped, or at the start when the
    path has changed or the file has become shorter, and stops before a
    last line still being written, which the next run reads whole. ANSI
    escape sequences are removed before matching.
    """
    offset = state.server_log_offset if state.server_log == str(path) else 0
    needles = [pattern.encode("utf-8") for pattern in patterns]
    matched = 0
    try:
        with path.open("rb") as fh:
            if fh.seek(0, os.SEEK_END) < offset:
                offset = 0
            fh.seek(offset)
            for line in fh:
                if not line.endswith(b"\n"):
                    break
                offset += len(line)
                text = _ANSI_ESCAPE_RE.sub(b"", line)
                matched += any(needle in text for needle in needles)
    except FileNotFoundError:
        logger.warning("server log %s does not exist", path)
        offset = 0
    except OSError as exc:
        logger.warning("cannot read server log %s: %s", path, describe_exception(exc))
    state.server_log, state.server_log_offset = str(path), offset
    return {"lines": matched} if matched else None


def _check_quotes(
    cfg: P1Config, watch: WatchConfig, holidays: frozenset[date], now: datetime
) -> tuple[dict[str, object] | None, str | None, dict[str, float | None]]:
    """Return quotes_stale's detail if it holds, the latest stream run's name, and each client's data frame age.

    An age is None when the client has had no data frame for
    quotes_stale_s, which makes quotes_stale hold inside a session.
    """
    run_dir = _latest_stream_run(cfg.results_root)
    if run_dir is None:
        return None, None, {}
    try:
        clients = discover_sse_files(run_dir)
    except (AnalysisError, OSError) as exc:
        logger.warning("no recording to check in %s: %s", run_dir.name, exc)
        clients = {}

    now_ns = _epoch_ns(now)
    not_before_ns = now_ns - int(watch.quotes_stale_s * NS_PER_S)
    ages: dict[str, float | None] = {}
    for client, paths in sorted(clients.items()):
        path = run_dir / f"sse_{client}_{watch.data_stream}.jsonl"
        latest = None
        if path in paths:
            try:
                latest = latest_data_frame_ns(path, cfg.events.heartbeat_event, not_before_ns)
            except OSError as exc:
                logger.warning("cannot read %s: %s", path, describe_exception(exc))
        ages[client] = None if latest is None else (now_ns - latest) / NS_PER_S

    stale = sum(age is None for age in ages.values())
    if stale and session_open(now, holidays, watch):
        return {"stale_clients": stale}, run_dir.name, ages
    return None, run_dir.name, ages


def _latest_stream_run(results_root: Path) -> Path | None:
    try:
        runs = [path for path in results_root.iterdir() if _STREAM_RUN_RE.fullmatch(path.name) and path.is_dir()]
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("cannot list %s: %s", results_root, describe_exception(exc))
        return None
    return max(runs, key=lambda path: path.name, default=None)


def _check_units(
    probes: Probes, services: Sequence[str], state: _State
) -> tuple[dict[str, object] | None, dict[str, dict[str, object]]]:
    """Return service_down's detail if a unit is not active or has restarted since the last run, and status.json's units."""
    units: dict[str, dict[str, object]] = {}
    inactive = restarted = 0
    for name in services:
        properties = probes.unit(name)
        active = properties.get("ActiveState") == _ACTIVE
        n_restarts = _count_or_none(properties.get("NRestarts"))
        if n_restarts is not None:
            previous = state.n_restarts.get(name)
            restarted += previous is not None and n_restarts > previous
            state.n_restarts[name] = n_restarts
        inactive += not active
        units[name] = {
            "active": active,
            "n_restarts": n_restarts,
            "memory_bytes": _count_or_none(properties.get("MemoryCurrent")),
        }
    if inactive or restarted:
        return {"inactive": inactive, "restarted": restarted}, units
    return None, units


def _count_or_none(text: str | None) -> int | None:
    """Return a systemctl property's decimal value, or None for one it leaves unset, such as [not set]."""
    return int(text) if text is not None and text.isdigit() else None


def _check_resources(probes: Probes, watch: WatchConfig, disk_path: Path) -> dict[str, object] | None:
    """Return resources' detail if free disk, available memory or swap use is past its threshold.

    A measurement that cannot be taken is logged and left out of the check.
    """
    disk_free: int | None
    try:
        disk_free = probes.disk_free_bytes(disk_path)
    except OSError as exc:
        logger.warning("cannot measure free disk at %s: %s", disk_path, describe_exception(exc))
        disk_free = None
    try:
        meminfo = probes.meminfo()
    except OSError as exc:
        logger.warning("cannot read memory use: %s", describe_exception(exc))
        meminfo = {}
    mem_available = meminfo.get("MemAvailable")
    swap_total, swap_free = meminfo.get("SwapTotal"), meminfo.get("SwapFree")
    swap_used = None if swap_total is None or swap_free is None else swap_total - swap_free

    if not (
        (disk_free is not None and disk_free < watch.disk_free_min_gib * _BYTES_PER_GIB)
        or (mem_available is not None and mem_available < watch.mem_available_min_mib * _BYTES_PER_MIB)
        or (swap_used is not None and swap_used > watch.swap_used_max_mib * _BYTES_PER_MIB)
    ):
        return None
    return {
        "disk_free_gib": None if disk_free is None else disk_free / _BYTES_PER_GIB,
        "mem_available_mib": None if mem_available is None else mem_available / _BYTES_PER_MIB,
        "swap_used_mib": None if swap_used is None else swap_used / _BYTES_PER_MIB,
    }


def _check_egress(
    probes: Probes, watch: WatchConfig, egress_ips: Sequence[str], now: datetime, state: _State
) -> dict[str, object] | None:
    """Return egress_changed's detail if the last egress query found an IP outside egress_ips.

    The query is made only once egress_check_interval_s has passed since
    the last one, failed or not, and its outcome is kept in state until the
    next: whether the IP matched, or None if the query failed.
    """
    last = state.egress_checked_at
    if last is None or not 0 <= (now - last).total_seconds() < watch.egress_check_interval_s:
        current = probes.egress_ip()
        state.egress_matches = None if current is None else current in egress_ips
        state.egress_checked_at = last = now
    if state.egress_matches is False:
        return {"query_age_s": (now - last).total_seconds()}
    return None


def _parse_holidays(value: str) -> frozenset[date]:
    """Parse --holidays: comma-separated YYYY-MM-DD dates, or an empty string for none."""
    if not value.strip():
        return frozenset()
    days: set[date] = set()
    for item in value.split(","):
        text = item.strip()
        try:
            if not _DATE_RE.fullmatch(text):
                raise ValueError(text)
            days.add(date.fromisoformat(text))
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid --holidays date {text!r}: expected YYYY-MM-DD") from None
    return frozenset(days)


class _ArgumentParser(argparse.ArgumentParser):
    """An ArgumentParser whose usage errors exit 1 like every other failure of p1watch, not 2."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_FAILED, f"{self.prog}: error: {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="p1watch",
        description="Check the soak host once and write status.json, watch.jsonl and state.json.",
    )
    parser.add_argument("--config", default="p1.toml", help="path to p1.toml (default: p1.toml)")
    parser.add_argument("--state-dir", required=True, help="directory for status.json, watch.jsonl and state.json")
    parser.add_argument("--server-log", required=True, help="the official server's log file")
    parser.add_argument(
        "--holidays",
        required=True,
        type=_parse_holidays,
        help="comma-separated YYYY-MM-DD market holidays on weekdays, or an empty string for none",
    )
    parser.add_argument("--disk-path", required=True, help="a path on the file system whose free space is checked")
    return parser


def _setup_logging() -> RedactingFormatter:
    """Send log records to stderr, which the journal keeps, through a formatter that redacts what it is given."""
    formatter = RedactingFormatter((), _LOG_FORMAT)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in _QUIET_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return formatter


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    formatter = _setup_logging()
    try:
        cfg = load_config(Path(args.config))
        _watch_config(cfg)
        egress_ips = read_secret_role(cfg, _EGRESS_ROLE)
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_FAILED
    formatter.add_secrets(egress_ips)
    try:
        return run_watch(
            cfg,
            egress_ips,
            Path(args.state_dir),
            Path(args.server_log),
            args.holidays,
            Path(args.disk_path),
            datetime.now(timezone.utc),
            SystemProbes(cfg),
        )
    except Exception:
        logger.exception("p1watch failed")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
