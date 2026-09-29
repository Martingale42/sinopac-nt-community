"""Offline rehearsal: the whole p1rec pipeline, through its real entry points, against a stub official server.

Each test starts tests/stub_server.py on a free loopback port and copies
tests/data/p1.toml, pointed at it, into a scratch git repository, since
scrub refuses to run unless git ignores results_root. The full pipeline
then runs as a live day would: self-test-interlock, preflight, stream in
the background, orders, every SSE connection dropped and refused for a
while, history, stream stopped, reconcile, stats, a scrub export of two
fixtures and a scan. Every subcommand runs as a subprocess of the p1rec
console script with made-up secrets in its environment, except orders.

orders runs in this process through p1rec.cli.main instead, with the
clock that run_orders takes injected the way tests/test_orders.py injects
it: orders refuses futures and filling stock cases outside the TAIFEX and
TWSE sessions, and production code keeps no bypass for tests, so only an
injected clock lets the rehearsal run at any hour. That clock shifts the
wall time alone; its sleeps and elapsed time are real. stream has to be a
subprocess regardless, since it blocks its stop signals and refuses to
start while any other thread, such as the stub's, is running.
"""

from __future__ import annotations

import functools
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from stub_server import (
    STUB_PERSON_ID,
    STUB_STOCK_ACCOUNT,
    SUBSCRIBE_TRADE_PATH,
    StubBroker,
    StubServer,
    session_anchor,
)

from p1rec import cli, orders
from p1rec.config import P1Config, load_config
from p1rec.interlock import EXIT_INTERLOCK_ABORT
from p1rec.scenarios import load_scenarios

_DATA_DIR = Path(__file__).resolve().parent / "data"
_CONFIG_TEMPLATE = _DATA_DIR / "p1.toml"
_SCENARIOS = _DATA_DIR / "scenarios.toml"

# The console script uv installs beside the interpreter running the tests
_P1REC = Path(sys.executable).with_name("p1rec")

_CLIENTS = ("a", "b")

# Made-up secrets. The person is the one the stub's accounts name, so scrub
# meets it both in the environment and in recorded responses.
_SECRETS = {
    "SJ_API_KEY": "FakeRehearsalApiKey0123456789",
    "SJ_SEC_KEY": "FakeRehearsalSecKey9876543210",
    "SJ_CA_PASSWD": "fake-rehearsal-ca-pw",
    "P1_PERSON_ID": STUB_PERSON_ID,
    "P1_EGRESS_IP": "203.0.113.77",
}

# Generous bounds for what normally takes milliseconds, so only a real fault waits them out
_WAIT_S = 10.0
_PROCESS_TIMEOUT_S = 30.0

# How often to look at a recording that another process is still writing
_POLL_S = 0.05

# Far beyond the rehearsal's length, so stream ends by SIGTERM unless something hangs
_STREAM_SAFETY_S = 120.0

# How long the stub refuses streams after dropping them: several of the
# rehearsal config's retry_s, and longer than its gap_threshold_s.
_STREAM_DOWN_S = 1.5

# How each rehearsal case ends against the stub (tests/data/scenarios.toml)
_EXPECTED_OUTCOMES = {
    "far_stock_2890_lmt_rod": "completed",
    "far_stock_2890_lmt_ioc": "completed",
    "lifecycle_stock_2890": "completed",
    "market_stock_2890_mkt_ioc": "closed",
    "market_odd_00981a_mkt_ioc": "rejected",
    "market_futures_tmf_mkt_ioc": "closed",
    "exceed_funds_stock_2890": "completed",
    "custom_field_base62_6": "completed",
}

# The case during which the mode flips to production, just after its place
_FLIP_CASE = "lifecycle_stock_2890"


class _SessionClock:
    """The real clock, except that wall time reads as if the run began at start."""

    def __init__(self, start: datetime) -> None:
        self._start = start
        self._began = time.monotonic()

    def now(self) -> datetime:
        return self._start + timedelta(seconds=time.monotonic() - self._began)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class _Rehearsal:
    root: Path
    config: Path
    cfg: P1Config
    stub: StubServer
    session_start: datetime

    def p1rec(self, subcommand: str, *args: str, tag: str | None = None) -> subprocess.CompletedProcess[str]:
        """Run one subcommand of the p1rec console script against the stub and wait for it."""
        tag_args = ["--tag", tag] if tag else []
        return subprocess.run(
            [str(_P1REC), subcommand, "--config", str(self.config), *tag_args, *args],
            cwd=self.root,
            env=_process_env(),
            capture_output=True,
            text=True,
            timeout=_PROCESS_TIMEOUT_S,
            check=False,
        )

    def run_dir(self, subcommand: str, tag: str | None = None) -> Path:
        """Return the one run directory of subcommand with tag under results_root."""
        label = f"{subcommand}-{tag}" if tag else subcommand
        runs = [path for path in self.cfg.results_root.iterdir() if path.name.partition("-")[2] == label]
        assert len(runs) == 1, f"expected one {label} run, found {runs}"
        return runs[0]

    def stream_count(self) -> int:
        return len(_CLIENTS) * len(self.cfg.stream.streams)


def _process_env() -> dict[str, str]:
    return {**os.environ, **_SECRETS}


def _config_text(base_url: str) -> str:
    """Return tests/data/p1.toml with base_url pointing at the stub."""
    lines = _CONFIG_TEMPLATE.read_text(encoding="utf-8").splitlines()
    at = [index for index, line in enumerate(lines) if line.startswith("base_url = ")]
    assert len(at) == 1
    lines[at[0]] = f'base_url = "{base_url}"'
    return "\n".join(lines) + "\n"


@pytest.fixture
def rehearsal(tmp_path: Path) -> Iterator[_Rehearsal]:
    """A running stub, the rehearsal config pointed at it, and a scratch git repository around both."""
    assert _P1REC.is_file(), f"no p1rec console script beside {sys.executable}; run the tests with uv run"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("/results/\n", encoding="utf-8")
    (tmp_path / "fixtures.toml").write_text("", encoding="utf-8")
    (tmp_path / "contract.md").write_text("Fixtures name every identifier by placeholder.\n", encoding="utf-8")

    start = session_anchor()
    with StubServer(StubBroker(exchange_clock_start=start)) as stub:
        config = tmp_path / "p1.toml"
        config.write_text(_config_text(stub.base_url), encoding="utf-8")
        cfg = load_config(config)
        cfg.fixtures_root.parent.mkdir(parents=True)
        yield _Rehearsal(root=tmp_path, config=config, cfg=cfg, stub=stub, session_start=start)


def _expect(result: subprocess.CompletedProcess[str], exit_code: int) -> None:
    assert result.returncode == exit_code, (
        f"p1rec {result.args[1]} exited {result.returncode}, expected {exit_code}:\n{result.stderr[-4000:]}"
    )


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _first_line(path: Path, matches: Callable[[dict[str, Any]], bool]) -> int:
    """Return the 1-based number of the first JSONL line of path whose record matches."""
    for number, record in enumerate(_records(path), start=1):
        if matches(record):
            return number
    raise AssertionError(f"no matching line in {path}")


def _run_orders(rehearsal: _Rehearsal, monkeypatch: pytest.MonkeyPatch) -> int:
    """Run `p1rec orders` in this process on a clock that starts inside the trading sessions."""
    for name, value in _SECRETS.items():
        monkeypatch.setenv(name, value)
    clock = _SessionClock(rehearsal.session_start)
    monkeypatch.setattr(cli, "run_orders", functools.partial(orders.run_orders, clock=clock))
    return cli.main(["orders", "--config", str(rehearsal.config), "--scenarios", str(_SCENARIOS)])


@contextmanager
def _streaming(rehearsal: _Rehearsal) -> Iterator[subprocess.Popen[bytes]]:
    """Run `p1rec stream` for every client in the background, killing it if the block ends with it running."""
    until = (datetime.now(timezone.utc) + timedelta(seconds=_STREAM_SAFETY_S)).isoformat()
    command = [str(_P1REC), "stream", "--config", str(rehearsal.config), "--clients", ",".join(_CLIENTS)]
    with (rehearsal.root / "stream.stderr").open("wb") as stderr:
        process = subprocess.Popen(
            [*command, "--until", until],
            cwd=rehearsal.root,
            env=_process_env(),
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def _stream_log(rehearsal: _Rehearsal) -> str:
    return (rehearsal.root / "stream.stderr").read_text(encoding="utf-8", errors="replace")[-4000:]


def _wait_until_streaming(rehearsal: _Rehearsal) -> None:
    """Wait until every client's streams are open and stream has sent its startup requests and subscriptions."""
    expected = {
        (instrument.code, quote_type)
        for instrument in rehearsal.cfg.stream.instruments
        for quote_type in instrument.quote_types
    }
    broker = rehearsal.stub.broker
    assert rehearsal.stub.hub.wait_for_streams(rehearsal.stream_count(), _WAIT_S), _stream_log(rehearsal)
    assert broker.wait_until(
        lambda stub: stub.subscriptions() == expected and stub.trade_subscribed() == {"S", "F"}, _WAIT_S
    ), _stream_log(rehearsal)


def _frame_since_connect(path: Path) -> bool:
    """Return whether an SSE recording holds a frame after its last 200 connection, reading complete lines only."""
    seen = False
    for line in path.read_bytes().splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        record = json.loads(line)
        if record["kind"] == "connected" and record["status"] == 200:
            seen = False
        elif record["kind"] == "frame":
            seen = True
    return seen


def _wait_for_frames_since_connect(stream_run: Path) -> bool:
    """Wait until every recorder of a running stream has recorded a frame since it last connected."""
    deadline = time.monotonic() + _WAIT_S
    while not all(_frame_since_connect(path) for path in stream_run.glob("sse_*.jsonl")):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_S)
    return True


def _fixture_table(name: str, run_id: str, source: str, line: int, checklist: list[str], instrument: str) -> str:
    return "\n".join(
        [
            "[[fixture]]",
            f'name = "{name}"',
            f'run_id = "{run_id}"',
            f'source = "{source}"',
            f"lines = [{line}]",
            f"checklist = {json.dumps(checklist)}",
            f'instrument = "{instrument}"',
            "",
        ]
    )


def test_full_pipeline_against_stub(rehearsal: _Rehearsal, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = rehearsal.stub
    _expect(rehearsal.p1rec("self-test-interlock"), 0)
    _expect(rehearsal.p1rec("preflight", "--scenarios", str(_SCENARIOS)), 0)
    assert _json(rehearsal.run_dir("preflight") / "preflight.json")["funds_ok"] is True

    with _streaming(rehearsal) as stream:
        _wait_until_streaming(rehearsal)
        assert _run_orders(rehearsal, monkeypatch) == 0

        # Every report is already queued, and a drop delivers what each stream holds before it ends
        startup_sends = stub.broker.count(SUBSCRIBE_TRADE_PATH)
        stub.hub.drop(_STREAM_DOWN_S)
        assert stub.hub.wait_for_streams(rehearsal.stream_count(), _STREAM_DOWN_S + _WAIT_S), _stream_log(rehearsal)

        # The leader stream's return makes stream resend its startup requests
        assert stub.broker.wait_until(lambda broker: broker.count(SUBSCRIBE_TRADE_PATH) >= 2 * startup_sends, _WAIT_S)

        _expect(rehearsal.p1rec("history"), 0)

        # Stopped only once each recorder has a frame after the outage, which is what bounds the gap
        assert _wait_for_frames_since_connect(rehearsal.run_dir("stream")), _stream_log(rehearsal)
        stream.send_signal(signal.SIGTERM)
        assert stream.wait(timeout=_PROCESS_TIMEOUT_S) == 0, _stream_log(rehearsal)

    orders_run = rehearsal.run_dir("orders")
    cases = _json(orders_run / "orders.json")["cases"]
    assert {case["id"]: case["outcome"] for case in cases} == _EXPECTED_OUTCOMES
    order_keys = sorted(key for case in cases for key in (case["trade_id"], case["close_trade_id"]) if key)

    history = _json(rehearsal.run_dir("history") / "history.json")
    assert history["failed_count"] == 0
    assert all(query["usage_delta"] > 0 for query in history["queries"])

    stream_run = rehearsal.run_dir("stream")
    sse_files = sorted(stream_run.glob("sse_*.jsonl"))
    assert len(sse_files) == rehearsal.stream_count()
    assert all(_records(path)[-1]["kind"] == "stopped" for path in sse_files)

    _expect(rehearsal.p1rec("reconcile", "--sse-run", stream_run.name), 0)
    reconciled = _json(rehearsal.run_dir("reconcile") / "reconcile.json")
    assert reconciled["trading_day"] == rehearsal.session_start.date().isoformat()
    assert reconciled["only_in_rest"] == []
    assert reconciled["only_in_sse"] == []
    assert reconciled["mismatched"] == []
    assert reconciled["missing_events_by_client"] == {client: [] for client in _CLIENTS}
    assert reconciled["per_client_keys"] == {client: order_keys for client in _CLIENTS}

    _expect(rehearsal.p1rec("stats", "--sse-run", stream_run.name), 0)
    stats = _json(rehearsal.run_dir("stats") / "stats.json")
    files = stats["files"]
    assert len(files) == rehearsal.stream_count()
    for name, file_stats in files.items():
        # The drop, then refused reconnects, then the stream's return
        outage = file_stats["outages"][0]
        assert outage["first_error"] == "stream ended by server", name
        assert outage["attempts"] > 1, name
        assert outage["reconnected_at"] is not None, name
        assert file_stats["gaps"], name
        assert file_stats["heartbeat_interval_s"] is not None, name
    for client in _CLIENTS:
        counts = files[f"sse_{client}_data.jsonl"]["event_counts"]
        assert {"tick_stk", "bidask_stk", "tick_fop", "order_event"} <= counts.keys()

    place_line = _first_line(orders_run / "rest.jsonl", lambda record: record.get("op") == "place_stock")
    frame_line = _first_line(
        stream_run / "sse_a_order_event.jsonl",
        lambda record: str(record.get("raw", "")).startswith("event:order_event"),
    )
    selection = [
        _fixture_table("C1_place_stock", orders_run.name, "rest.jsonl", place_line, ["C1", "C3"], "2890"),
        _fixture_table("C1_order_event", stream_run.name, "sse_a_order_event.jsonl", frame_line, ["C1", "D1"], "2890"),
    ]
    rehearsal.cfg.scrub.selection.write_text("".join(selection), encoding="utf-8")
    _expect(rehearsal.p1rec("scrub", tag="export"), 0)
    fixtures_root = rehearsal.cfg.fixtures_root
    manifest = tomllib.loads((fixtures_root / "MANIFEST.toml").read_text(encoding="utf-8"))
    assert [entry["file"] for entry in manifest["fixture"]] == ["C1_place_stock.json", "C1_order_event.sse"]
    assert "CA_SIGNATURE_" in (fixtures_root / "C1_place_stock.json").read_text(encoding="utf-8")
    assert "ACCOUNT_ID_" in (fixtures_root / "C1_order_event.sse").read_text(encoding="utf-8")

    _expect(rehearsal.p1rec("scrub", "--scan-only", tag="scan"), 0)


def test_mode_flip_stops_orders(rehearsal: _Rehearsal, monkeypatch: pytest.MonkeyPatch) -> None:
    cases = load_scenarios(_SCENARIOS)
    flip_at = [case.id for case in cases].index(_FLIP_CASE)

    # The cases before it never fill, so each sends one mutation per step and the flip follows this case's place
    assert not any(case.expect_fill for case in cases[:flip_at])
    flip_after = sum(len(case.steps) for case in cases[:flip_at]) + 1
    broker = rehearsal.stub.broker
    broker.production_after_mutations = flip_after

    assert _run_orders(rehearsal, monkeypatch) == EXIT_INTERLOCK_ABORT

    assert broker.mutation_count() == flip_after
    orders_run = rehearsal.run_dir("orders")
    outcomes = [case["outcome"] for case in _json(orders_run / "orders.json")["cases"]]
    assert outcomes == ["completed"] * flip_at + ["interlock_abort"] + ["not_run"] * (len(cases) - flip_at - 1)

    # The last request orders sent was the mode query that found production, and the order left resting is flagged
    last = _records(orders_run / "rest.jsonl")[-1]
    assert last["op"] == "mode"
    assert json.loads(str(last["resp_body"]))["simulation"] is False
    log = (orders_run / "recorder.log").read_text(encoding="utf-8")
    assert f"CRITICAL p1rec.orders: case {_FLIP_CASE}:" in log
    assert "possible residual position, check manually" in log


def test_scan_catches_planted_leak(rehearsal: _Rehearsal) -> None:
    _expect(rehearsal.p1rec("preflight", "--scenarios", str(_SCENARIOS)), 0)
    preflight_run = rehearsal.run_dir("preflight")
    accounts_line = _first_line(preflight_run / "rest.jsonl", lambda record: record.get("op") == "accounts")
    rehearsal.cfg.scrub.selection.write_text(
        _fixture_table("C3_accounts", preflight_run.name, "rest.jsonl", accounts_line, ["C3"], ""), encoding="utf-8"
    )
    _expect(rehearsal.p1rec("scrub", tag="export"), 0)
    _expect(rehearsal.p1rec("scrub", "--scan-only", tag="clean"), 0)

    # Undo the scrubbing of one account id, as a hand edit of a fixture might
    fixture = rehearsal.cfg.fixtures_root / "C3_accounts.json"
    lines = fixture.read_text(encoding="utf-8").splitlines(keepends=True)
    planted_at = next(index for index, line in enumerate(lines) if "ACCOUNT_ID_1" in line)
    account_id = str(STUB_STOCK_ACCOUNT["account_id"])
    lines[planted_at] = lines[planted_at].replace("ACCOUNT_ID_1", account_id, 1)
    fixture.write_text("".join(lines), encoding="utf-8")

    result = rehearsal.p1rec("scrub", "--scan-only", tag="planted")

    _expect(result, 2)
    log = (rehearsal.run_dir("scrub", "planted") / "recorder.log").read_text(encoding="utf-8")
    assert f"C3_accounts.json:{planted_at + 1}:ACCOUNT_ID" in log
    assert account_id not in log
    assert account_id not in result.stderr
