"""Tests for p1rec.watch: the soak host's checks, its state files, and the p1watch entry point."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, NoReturn
from zoneinfo import ZoneInfo

import httpx
import pytest
from helpers import SECRET_ENV_VARS
from stub_server import StubServer

from p1rec import watch
from p1rec.config import P1Config, load_config
from p1rec.watch import SystemProbes, latest_data_frame_ns, run_watch, session_open

_CONFIG = Path(__file__).resolve().parent / "data" / "p1.toml"

# The console script uv installs beside the interpreter running the tests
_P1WATCH = Path(sys.executable).with_name("p1watch")

_TAIPEI = ZoneInfo("Asia/Taipei")
_NS_PER_S = 1_000_000_000
_GIB = 1024**3
_MIB = 1024**2

# The egress IPs the tests' secrets list, and one outside them, all from documentation ranges
_EGRESS_IPS = ("203.0.113.1",)
_OTHER_IP = "198.51.100.20"

_SERVER = "p1-server.service"
_HEARTBEAT = "heartbeat"
_PROCESS_TIMEOUT_S = 30.0


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    """Return a Taipei time in October 2026, the month of the soak."""
    return datetime(2026, 10, day, hour, minute, tzinfo=_TAIPEI)


# A Thursday between the day and night sessions, when no recording is expected
_BETWEEN_SESSIONS = _at(15, 14, 30)


def _ns(moment: datetime) -> int:
    return int(moment.timestamp()) * _NS_PER_S


def _health_body(**fields: object) -> dict[str, object]:
    """Return a HealthResponse (openapi.json) of a logged-in server, with fields replaced."""
    body: dict[str, object] = {
        "status": "healthy",
        "version": "1.7.6",
        "timestamp": "2026-10-15T09:00:00+08:00",
        "session_recovering": False,
        "session_recovery_attempts": 0,
        "token_stale": False,
        "token_expires_in_seconds": 80000,
        "next_maintenance": "2026-10-16T08:21:07+08:00",
        "last_session_recovery_error": None,
        "last_maintenance_error": None,
        "agent_harness": {"enabled": False, "mode": "off"},
    }
    body.update(fields)
    return body


def _healthy_units() -> dict[str, dict[str, str]]:
    return {
        _SERVER: {"ActiveState": "active", "NRestarts": "0", "MemoryCurrent": "209715200"},
        "p1-stream.service": {"ActiveState": "active", "NRestarts": "0", "MemoryCurrent": "52428800"},
    }


@dataclass
class _FakeProbes:
    """Probes whose answers a test sets, describing a healthy host until it changes them."""

    http_status: int | None = 200
    body: Mapping[str, object] | None = field(default_factory=_health_body)
    units: dict[str, dict[str, str]] = field(default_factory=_healthy_units)
    egress: str | None = _EGRESS_IPS[0]
    disk_free: int = 40 * _GIB
    memory: dict[str, int] = field(
        default_factory=lambda: {"MemAvailable": 512 * _MIB, "SwapTotal": 2 * _GIB, "SwapFree": 2 * _GIB}
    )
    disk_error: OSError | None = None
    memory_error: OSError | None = None
    egress_calls: int = 0

    def health(self) -> tuple[int | None, Mapping[str, object] | None]:
        return self.http_status, self.body

    def unit(self, name: str) -> Mapping[str, str]:
        return self.units.get(name, {})

    def egress_ip(self) -> str | None:
        self.egress_calls += 1
        return self.egress

    def disk_free_bytes(self, path: Path) -> int:
        if self.disk_error is not None:
            raise self.disk_error
        return self.disk_free

    def meminfo(self) -> Mapping[str, int]:
        if self.memory_error is not None:
            raise self.memory_error
        return self.memory


@dataclass(frozen=True)
class _Host:
    cfg: P1Config
    state_dir: Path
    server_log: Path

    def run(self, probes: _FakeProbes, now: datetime, holidays: frozenset[date] = frozenset()) -> dict[str, Any]:
        """Run the watch once and return the status.json it wrote."""
        code = run_watch(self.cfg, _EGRESS_IPS, self.state_dir, self.server_log, holidays, self.state_dir, now, probes)
        assert code == 0
        return json.loads((self.state_dir / "status.json").read_text(encoding="utf-8"))

    def written(self) -> str:
        """Return every file in the state directory, concatenated."""
        return "".join(path.read_text(encoding="utf-8") for path in sorted(self.state_dir.iterdir()))


@pytest.fixture
def host(tmp_path: Path) -> _Host:
    """Return the rehearsal config, with results_root under tmp_path, and an empty server log."""
    cfg = dataclasses.replace(load_config(_CONFIG), results_root=tmp_path / "results")
    server_log = tmp_path / "server.log"
    server_log.write_text("", encoding="utf-8")
    return _Host(cfg=cfg, state_dir=tmp_path / "watch", server_log=server_log)


def _names(status: Mapping[str, Any]) -> list[str]:
    return [anomaly["name"] for anomaly in status["anomalies"]]


def _anomaly(status: Mapping[str, Any], name: str) -> dict[str, Any]:
    [found] = [anomaly for anomaly in status["anomalies"] if anomaly["name"] == name]
    return found


def _sse_line(recv_ts_ns: int, event: str, client: str = "a") -> str:
    """Return one frame line of an SSE recording, as SseRecorder writes it."""
    data = {"type": event, "connection_id": "conn-1"} if event == _HEARTBEAT else {"code": "TMFJ6", "close": "23000"}
    raw = f"event:{event}\ndata:{json.dumps(data, separators=(',', ':'))}\n\n"
    record = {"client": client, "stream": "data", "kind": "frame", "recv_ts_ns": recv_ts_ns, "raw": raw}
    return json.dumps(record) + "\n"


class _CountingFile:
    """A binary file that counts the bytes read through it."""

    def __init__(self, raw: BinaryIO) -> None:
        self._raw = raw
        self.bytes_read = 0

    def __enter__(self) -> _CountingFile:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._raw.close()

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._raw.seek(offset, whence)

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self.bytes_read += len(data)
        return data


def test_session_open_day_and_night(host: _Host) -> None:
    watch_cfg = host.cfg.watch
    assert watch_cfg is not None
    holidays = frozenset({date(2026, 10, 9)})

    # Thursday's day and night sessions, the night running into Friday, and Friday's night into Saturday
    for moment in (_at(15, 9), _at(15, 23), _at(16, 3), _at(17, 3)):
        assert session_open(moment, holidays, watch_cfg), moment

    # Between Thursday's sessions, a Saturday, Monday before its first session, and a holiday
    for moment in (_at(15, 14, 30), _at(17, 10), _at(19, 3), _at(9, 10)):
        assert not session_open(moment, holidays, watch_cfg), moment


def test_latest_data_frame_skips_heartbeats(tmp_path: Path) -> None:
    tick_ns = _ns(_at(15, 9))
    path = tmp_path / "sse_a_data.jsonl"

    # The last line is still being written
    path.write_text(
        _sse_line(tick_ns, "tick_fop") + _sse_line(tick_ns + _NS_PER_S, _HEARTBEAT) + '{"client": "a", "ki',
        encoding="utf-8",
    )

    assert latest_data_frame_ns(path, _HEARTBEAT, tick_ns - 60 * _NS_PER_S) == tick_ns


def test_latest_data_frame_none_when_only_heartbeats_since_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cutoff_ns = _ns(_at(15, 9))
    before = [_sse_line(cutoff_ns - (count + 1) * _NS_PER_S, "tick_fop") for count in reversed(range(200))]
    since = "".join(_sse_line(cutoff_ns + count * _NS_PER_S, _HEARTBEAT) for count in range(3))
    path = tmp_path / "sse_a_data.jsonl"
    path.write_text("".join(before) + since, encoding="utf-8")

    block_bytes = 256
    monkeypatch.setattr(watch, "_TAIL_BLOCK_BYTES", block_bytes)
    opened: list[_CountingFile] = []
    real_open = Path.open

    def counting_open(self: Path, *args: Any, **kwargs: Any) -> _CountingFile:
        opened.append(_CountingFile(real_open(self, *args, **kwargs)))
        return opened[-1]

    monkeypatch.setattr(Path, "open", counting_open)

    # Every tick is older than the cutoff, so finding one would mean reading past it
    assert latest_data_frame_ns(path, _HEARTBEAT, cutoff_ns) is None

    # Reading stopped at the newest record before the cutoff, within one block of it
    [reader] = opened
    assert reader.bytes_read <= len(since) + len(before[-1]) + block_bytes
    assert reader.bytes_read < path.stat().st_size // 10


@pytest.mark.parametrize(
    ("http_status", "body"),
    [
        pytest.param(503, _health_body(status="unhealthy", session_recovery_attempts=10), id="unhealthy"),
        pytest.param(None, None, id="unreachable"),
    ],
)
def test_health_unhealthy_is_immediate(
    host: _Host, http_status: int | None, body: Mapping[str, object] | None
) -> None:
    status = host.run(_FakeProbes(http_status=http_status, body=body), _BETWEEN_SESSIONS)

    assert status["ok"] is False
    assert _names(status) == ["health_not_healthy"]
    detail = _anomaly(status, "health_not_healthy")["detail"]
    assert detail["http_status"] == http_status
    if body is not None:
        assert detail["status"] == "unhealthy"
        assert detail["session_recovery_attempts"] == 10
        assert status["health"]["status"] == "unhealthy"
    else:
        assert status["health"] is None


def test_health_degraded_tolerated_in_maintenance_window(host: _Host) -> None:
    probes = _FakeProbes(body=_health_body(status="degraded", token_stale=True))

    assert _names(host.run(probes, _at(15, 8, 20))) == []
    assert _names(host.run(probes, _at(15, 14))) == []
    status = host.run(probes, _at(15, 14, 5))

    assert _names(status) == ["health_not_healthy"]
    detail = _anomaly(status, "health_not_healthy")["detail"]
    assert detail["degraded_checks"] == 2
    assert detail["token_stale"] is True


def test_anomaly_since_kept_while_it_holds_and_dropped_on_recovery(host: _Host) -> None:
    down = _FakeProbes(http_status=503, body=_health_body(status="unhealthy"))
    up = _FakeProbes()

    host.run(down, _at(15, 14, 20))
    held = host.run(down, _at(15, 14, 25))
    assert _anomaly(held, "health_not_healthy")["since"] == "2026-10-15T06:20:00Z"

    recovered = host.run(up, _at(15, 14, 30))
    assert recovered["ok"] is True
    assert recovered["anomalies"] == []

    again = host.run(down, _at(15, 14, 35))
    assert _anomaly(again, "health_not_healthy")["since"] == "2026-10-15T06:35:00Z"


def test_status_never_copies_recovery_error_text(host: _Host) -> None:
    leaked = "203.0.113.9"
    body = _health_body(
        status="unhealthy",
        last_session_recovery_error=f"ip: {leaked} not allow",
        last_maintenance_error=f"Daily maintenance failed: ip: {leaked} not allow",
    )

    status = host.run(_FakeProbes(http_status=503, body=body), _at(15, 10))

    assert status["health"]["has_recovery_error"] is True
    detail = _anomaly(status, "health_not_healthy")["detail"]
    assert detail["has_session_recovery_error"] is True
    assert detail["has_maintenance_error"] is True
    assert {path.name for path in host.state_dir.iterdir()} >= {"status.json", "watch.jsonl"}
    assert leaked not in host.written()


def test_failure_patterns_counted_once(host: _Host) -> None:
    probes = _FakeProbes()
    log = host.server_log
    log.write_text(
        "\x1b[32m INFO\x1b[0m server started\n"
        "\x1b[31mERROR\x1b[0m login rejected: ip: 203.0.113.9 not allow\n"
        # Only the text without its escape sequences holds the pattern
        "\x1b[31mERROR\x1b[0m Daily \x1b[1mmaintenance\x1b[0m failed, retrying\n",
        encoding="utf-8",
    )

    first = host.run(probes, _at(15, 8, 20))
    assert _anomaly(first, "relogin_failed")["detail"] == {"lines": 2, "log_unreadable": False}

    assert "relogin_failed" not in _names(host.run(probes, _at(15, 8, 25)))

    # A line still being written is read once it is whole
    with log.open("a", encoding="utf-8") as fh:
        fh.write("ERROR login rejected: not al")
    assert "relogin_failed" not in _names(host.run(probes, _at(15, 8, 30)))
    with log.open("a", encoding="utf-8") as fh:
        fh.write("low\n")
    completed = host.run(probes, _at(15, 8, 35))
    assert _anomaly(completed, "relogin_failed")["detail"] == {"lines": 1, "log_unreadable": False}

    # A shorter file is a new log, read from its start
    log.write_text("ERROR Daily maintenance exhausted\n", encoding="utf-8")
    shortened = host.run(probes, _at(15, 8, 40))
    assert _anomaly(shortened, "relogin_failed")["detail"] == {"lines": 1, "log_unreadable": False}

    assert "203.0.113.9" not in host.written()


def test_failure_patterns_read_from_start_of_new_log_path(host: _Host, tmp_path: Path) -> None:
    probes = _FakeProbes()
    host.server_log.write_text("INFO up\n", encoding="utf-8")
    host.run(probes, _at(15, 7, 30))

    # Longer than the old log, so only the new path explains reading from the start
    new_log = tmp_path / "2026-10-15-server" / "server.log"
    new_log.parent.mkdir()
    new_log.write_text("ERROR not allow\nINFO later\n", encoding="utf-8")
    moved = dataclasses.replace(host, server_log=new_log)

    status = moved.run(probes, _at(15, 7, 35))
    assert _anomaly(status, "relogin_failed")["detail"] == {"lines": 1, "log_unreadable": False}


@pytest.mark.parametrize("fault", ["missing", "mode-000"])
def test_unreadable_server_log_is_an_anomaly(host: _Host, fault: str) -> None:
    if fault == "missing":
        host.server_log.unlink()
    else:
        if os.geteuid() == 0:
            pytest.skip("root reads a file whatever its mode")
        host.server_log.write_text("ERROR not allow\n", encoding="utf-8")
        host.server_log.chmod(0)

    status = host.run(_FakeProbes(), _BETWEEN_SESSIONS)

    assert _names(status) == ["relogin_failed"]
    assert _anomaly(status, "relogin_failed")["detail"] == {"lines": 0, "log_unreadable": True}


def test_service_down_on_restart_count_increase(host: _Host) -> None:
    probes = _FakeProbes()

    first = host.run(probes, _at(15, 10))
    assert "service_down" not in _names(first)
    assert first["units"][_SERVER] == {"active": True, "n_restarts": 0, "memory_bytes": 209715200}

    probes.units[_SERVER]["NRestarts"] = "1"
    restarted = host.run(probes, _at(15, 10, 5))
    assert _anomaly(restarted, "service_down")["detail"] == {"inactive": 0, "restarted": 1}

    assert "service_down" not in _names(host.run(probes, _at(15, 10, 10)))


def test_service_down_when_unit_inactive_or_unknown(host: _Host) -> None:
    probes = _FakeProbes()
    probes.units[_SERVER] = {"ActiveState": "failed", "NRestarts": "0", "MemoryCurrent": "[not set]"}
    del probes.units["p1-stream.service"]

    status = host.run(probes, _at(15, 10))

    assert _anomaly(status, "service_down")["detail"] == {"inactive": 2, "restarted": 0}
    assert status["units"][_SERVER] == {"active": False, "n_restarts": 0, "memory_bytes": None}
    assert status["units"]["p1-stream.service"] == {"active": False, "n_restarts": None, "memory_bytes": None}


def test_egress_checked_once_per_interval(host: _Host) -> None:
    probes = _FakeProbes(egress=_OTHER_IP)

    first = host.run(probes, _at(15, 10))
    assert probes.egress_calls == 1
    assert first["egress_matches"] is False
    assert "egress_changed" in _names(first)

    # Inside the interval the last answer stands
    second = host.run(probes, _at(15, 10, 5))
    assert probes.egress_calls == 1
    assert _anomaly(second, "egress_changed")["since"] == _anomaly(first, "egress_changed")["since"]

    probes.egress = _EGRESS_IPS[0]
    third = host.run(probes, _at(15, 11))
    assert probes.egress_calls == 2
    assert third["egress_matches"] is True
    assert "egress_changed" not in _names(third)

    assert _OTHER_IP not in host.written()
    assert _EGRESS_IPS[0] not in host.written()


def test_egress_query_failure_is_unknown(host: _Host) -> None:
    status = host.run(_FakeProbes(egress=None), _at(15, 10))

    assert status["egress_matches"] is None
    assert "egress_changed" not in _names(status)


@pytest.mark.parametrize("recording", ["no-run", "empty-run", "no-data-stream"])
def test_no_recording_in_session_is_stale(host: _Host, recording: str) -> None:
    run_dir = host.cfg.results_root / "20261015T000000Z-stream"
    if recording != "no-run":
        run_dir.mkdir(parents=True)
    if recording == "no-data-stream":
        (run_dir / "sse_a_order_event.jsonl").write_text(_sse_line(_ns(_at(15, 10)), "order_event"), encoding="utf-8")

    in_session = host.run(_FakeProbes(), _at(15, 10))

    assert _anomaly(in_session, "quotes_stale")["detail"]["no_recording"] is True
    assert in_session["stream_run"] == (None if recording == "no-run" else run_dir.name)
    assert "quotes_stale" not in _names(host.run(_FakeProbes(), _BETWEEN_SESSIONS))


def test_quotes_stale_only_in_session(host: _Host) -> None:
    now = _at(15, 9)
    now_ns = _ns(now)
    results = host.cfg.results_root

    # An older run whose quotes are fresh must not stand in for the latest one
    older = results / "20261014T000000Z-stream"
    older.mkdir(parents=True)
    (older / "sse_a_data.jsonl").write_text(_sse_line(now_ns, "tick_fop"), encoding="utf-8")

    latest = results / "20261015T000000Z-stream"
    latest.mkdir()
    (latest / "sse_a_data.jsonl").write_text(
        _sse_line(now_ns - 400 * _NS_PER_S, "tick_fop")
        + "".join(_sse_line(now_ns - seconds * _NS_PER_S, _HEARTBEAT) for seconds in (90, 60, 30)),
        encoding="utf-8",
    )
    (latest / "sse_b_data.jsonl").write_text(
        _sse_line(now_ns - 10 * _NS_PER_S, "tick_fop", client="b")
        + _sse_line(now_ns - 5 * _NS_PER_S, _HEARTBEAT, client="b"),
        encoding="utf-8",
    )
    (latest / "sse_b_order_event.jsonl").write_text(
        _sse_line(now_ns, "order_event", client="b"), encoding="utf-8"
    )

    status = host.run(_FakeProbes(), now)

    assert status["stream_run"] == latest.name
    assert status["data_frame_age_s"] == {"a": None, "b": 10.0}
    assert _anomaly(status, "quotes_stale")["detail"] == {"stale_clients": 1, "no_recording": False}

    # Hours later, between sessions, both are silent and that is expected
    closed = host.run(_FakeProbes(), _at(15, 14, 30))
    assert closed["data_frame_age_s"] == {"a": None, "b": None}
    assert "quotes_stale" not in _names(closed)


@pytest.mark.parametrize(
    ("probes", "measured"),
    [
        pytest.param(_FakeProbes(disk_free=4 * _GIB), ("disk_free_gib", 4.0), id="disk"),
        pytest.param(
            _FakeProbes(memory={"MemAvailable": 64 * _MIB, "SwapTotal": 2 * _GIB, "SwapFree": 2 * _GIB}),
            ("mem_available_mib", 64.0),
            id="memory",
        ),
        pytest.param(
            _FakeProbes(memory={"MemAvailable": 512 * _MIB, "SwapTotal": 2 * _GIB, "SwapFree": _GIB}),
            ("swap_used_mib", 1024.0),
            id="swap",
        ),
    ],
)
def test_resources_past_threshold(host: _Host, probes: _FakeProbes, measured: tuple[str, float]) -> None:
    roomy = host.run(_FakeProbes(), _at(15, 10))
    assert roomy["state_reset"] is False
    assert "resources" not in _names(roomy)
    assert roomy["resources"] == {"disk_free_bytes": 40 * _GIB, "mem_available_bytes": 512 * _MIB, "swap_used_bytes": 0}

    status = host.run(probes, _at(15, 10, 5))

    key, value = measured
    detail = _anomaly(status, "resources")["detail"]
    assert detail[key] == value
    assert detail["probe_failed"] is False
    assert detail["failed_probes"] == []


@pytest.mark.parametrize(
    ("probes", "failed", "unmeasured"),
    [
        pytest.param(
            _FakeProbes(disk_error=PermissionError("Permission denied")), ["disk"], ["disk_free_bytes"], id="disk"
        ),
        pytest.param(
            _FakeProbes(memory_error=FileNotFoundError("No such file or directory")),
            ["meminfo"],
            ["mem_available_bytes", "swap_used_bytes"],
            id="meminfo-unreadable",
        ),
        pytest.param(
            _FakeProbes(memory={"MemTotal": _GIB}),
            ["meminfo"],
            ["mem_available_bytes", "swap_used_bytes"],
            id="meminfo-incomplete",
        ),
    ],
)
def test_resources_probe_failure_is_an_anomaly(
    host: _Host, probes: _FakeProbes, failed: list[str], unmeasured: list[str]
) -> None:
    status = host.run(probes, _at(15, 10))

    detail = _anomaly(status, "resources")["detail"]
    assert detail["probe_failed"] is True
    assert detail["failed_probes"] == failed
    assert [key for key, value in status["resources"].items() if value is None] == unmeasured

    # A probe is named, never the path it measured
    assert "/" not in json.dumps(detail)


def test_corrupt_state_is_reported(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.state_dir.mkdir()
    (host.state_dir / "state.json").write_text("{not json", encoding="utf-8")
    synced: list[int] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(watch.os, "fsync", fsync)

    reset = host.run(_FakeProbes(), _BETWEEN_SESSIONS)

    assert reset["state_reset"] is True
    assert reset["ok"] is True
    assert json.loads((host.state_dir / "state.json").read_text(encoding="utf-8"))["server_log_offset"] == 0

    # status.json and state.json, each file and then its directory
    assert len(synced) == 4

    assert host.run(_FakeProbes(), _BETWEEN_SESSIONS + timedelta(minutes=5))["state_reset"] is False


def test_unwritable_state_exits_1(host: _Host) -> None:
    # A file where the state directory should be cannot be made into one
    host.state_dir.write_text("", encoding="utf-8")

    code = run_watch(
        host.cfg, _EGRESS_IPS, host.state_dir, host.server_log, frozenset(), host.state_dir, _at(15, 10), _FakeProbes()
    )

    assert code == 1


def test_system_probes_over_http(host: _Host) -> None:
    egress_answer = httpx.Response(200, text=f"{_OTHER_IP}\n")
    timeouts: list[dict[str, float | None]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        if request.url.path == "/api/v1/health":
            return httpx.Response(503, json=_health_body(status="unhealthy"))
        return egress_answer

    probes = SystemProbes(host.cfg, transport=httpx.MockTransport(answer))

    http_status, body = probes.health()
    assert http_status == 503
    assert body is not None and body["status"] == "unhealthy"
    assert probes.egress_ip() == _OTHER_IP

    # Every phase of both requests is bounded by http_timeout_s
    assert len(timeouts) == 2
    assert all(set(timeout.values()) == {host.cfg.http_timeout_s} for timeout in timeouts)

    egress_answer = httpx.Response(200, text="<html>rate limited</html>")
    assert probes.egress_ip() is None

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    unreachable = SystemProbes(host.cfg, transport=httpx.MockTransport(refuse))
    assert unreachable.health() == (None, None)
    assert unreachable.egress_ip() is None


def test_system_probes_read_systemctl_and_meminfo(
    host: _Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def systemctl(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="NRestarts=2\nMemoryCurrent=[not set]\nActiveState=active\n")

    monkeypatch.setattr(watch.subprocess, "run", systemctl)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:  1000 kB\nMemAvailable:    2048 kB\nHugePages_Total:       0\n", encoding="ascii")
    monkeypatch.setattr(watch, "_MEMINFO", meminfo)
    probes = SystemProbes(host.cfg)

    assert probes.unit(_SERVER) == {"NRestarts": "2", "MemoryCurrent": "[not set]", "ActiveState": "active"}
    assert commands[0][-2:] == ["--", _SERVER]
    assert probes.meminfo() == {"MemTotal": 1024000, "MemAvailable": 2097152, "HugePages_Total": 0}


def test_systemctl_timeout_reads_as_down(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float | None] = []

    def hang(command: list[str], *, timeout: float | None = None, **_kwargs: object) -> NoReturn:
        timeouts.append(timeout)
        raise subprocess.TimeoutExpired(command, timeout or 0)

    monkeypatch.setattr(watch.subprocess, "run", hang)
    system = SystemProbes(host.cfg)

    @dataclass
    class _HungSystemctl(_FakeProbes):
        def unit(self, name: str) -> Mapping[str, str]:
            return system.unit(name)

    status = host.run(_HungSystemctl(), _BETWEEN_SESSIONS)

    assert timeouts == [30, 30]
    assert _anomaly(status, "service_down")["detail"] == {"inactive": 2, "restarted": 0}


def _config_text(base_url: str) -> str:
    """Return tests/data/p1.toml pointed at the stub, the egress query too, so nothing leaves the host."""
    lines = _CONFIG.read_text(encoding="utf-8").splitlines()
    replacements = {"base_url = ": f'"{base_url}"', "egress_url = ": f'"{base_url}/checkip"'}
    for prefix, value in replacements.items():
        at = [index for index, line in enumerate(lines) if line.startswith(prefix)]
        assert len(at) == 1, f"expected one {prefix!r} line in {_CONFIG}"
        lines[at[0]] = f"{prefix}{value}"
    return "\n".join(lines) + "\n"


def test_main_against_stub_server(tmp_path: Path) -> None:
    assert _P1WATCH.is_file(), f"no p1watch console script beside {sys.executable}; run the tests with uv run"
    egress_ip = "203.0.113.77"

    # A stand-in systemctl ahead of the real one on PATH, so the units do not depend on the test host
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\nprintf 'ActiveState=active\\nNRestarts=0\\nMemoryCurrent=1048576\\n'\n", encoding="utf-8"
    )
    systemctl.chmod(0o755)

    # Only the egress role is set, since p1watch must not need the others
    env = {name: value for name, value in os.environ.items() if name not in SECRET_ENV_VARS}
    env["P1_EGRESS_IP"] = egress_ip
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

    # Neither the state directory nor its parent exists yet
    state_dir = tmp_path / "home" / "watch"
    server_log = tmp_path / "server.log"
    server_log.write_text("INFO server started\n", encoding="utf-8")

    # A quote just received under results_root, so quotes_stale holds at no time of day
    stream_run = tmp_path / "results" / "20261015T000000Z-stream"
    stream_run.mkdir(parents=True)
    (stream_run / "sse_a_data.jsonl").write_text(_sse_line(time.time_ns(), "tick_fop"), encoding="utf-8")

    with StubServer() as stub:
        config = tmp_path / "p1.toml"
        config.write_text(_config_text(stub.base_url), encoding="utf-8")
        command = [
            str(_P1WATCH),
            "--config",
            str(config),
            "--state-dir",
            str(state_dir),
            "--server-log",
            str(server_log),
            "--holidays",
            "",
            "--disk-path",
            str(tmp_path),
        ]

        def p1watch() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                command, env=env, capture_output=True, text=True, timeout=_PROCESS_TIMEOUT_S, check=False
            )

        first = p1watch()
        assert first.returncode == 0, first.stderr
        healthy = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))

        stub.broker.health_status, stub.broker.health_http_status = "unhealthy", 503
        second = p1watch()
        assert second.returncode == 0, second.stderr
        unhealthy = json.loads((state_dir / "status.json").read_text(encoding="utf-8"))

    assert healthy["health"]["status"] == "healthy"
    assert healthy["units"] == dict.fromkeys(
        ("p1-server.service", "p1-stream.service"), {"active": True, "n_restarts": 0, "memory_bytes": 1048576}
    )

    # The disk and memory are the test host's own, so only resources may vary from host to host
    assert set(_names(healthy)) <= {"resources"}
    assert isinstance(healthy["resources"]["disk_free_bytes"], int)
    assert isinstance(healthy["resources"]["mem_available_bytes"], int)
    assert healthy["egress_matches"] is None
    assert healthy["stream_run"] == stream_run.name
    assert healthy["data_frame_age_s"]["a"] < _PROCESS_TIMEOUT_S
    assert healthy["state_reset"] is False
    assert _anomaly(unhealthy, "health_not_healthy")["detail"]["http_status"] == 503

    assert len((state_dir / "watch.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert (state_dir / "state.json").is_file()
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_dir.parent.stat().st_mode) == 0o700

    written = "".join(path.read_text(encoding="utf-8") for path in state_dir.iterdir())
    assert egress_ip not in written
    assert egress_ip not in first.stderr + second.stderr


def _main_args(tmp_path: Path, holidays: str) -> list[str]:
    return [
        "--config",
        str(_CONFIG),
        "--state-dir",
        str(tmp_path / "watch"),
        "--server-log",
        str(tmp_path / "server.log"),
        "--holidays",
        holidays,
        "--disk-path",
        str(tmp_path),
    ]


def test_main_rejects_malformed_holidays(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        watch.main(_main_args(tmp_path, "2026-10-09,2026-10-1"))

    assert exit_info.value.code == 1


def test_main_needs_the_egress_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("P1_EGRESS_IP", raising=False)

    assert watch.main(_main_args(tmp_path, "")) == 1
    assert not (tmp_path / "watch").exists()


def test_data_frame_age_keeps_sub_second_precision(host: _Host) -> None:
    now = _at(15, 9) + timedelta(microseconds=250_000)
    run_dir = host.cfg.results_root / "20261015T000000Z-stream"
    run_dir.mkdir(parents=True)
    (run_dir / "sse_a_data.jsonl").write_text(_sse_line(_ns(_at(15, 9)), "tick_fop"), encoding="utf-8")

    status = host.run(_FakeProbes(), now)

    assert status["data_frame_age_s"] == {"a": 0.25}
