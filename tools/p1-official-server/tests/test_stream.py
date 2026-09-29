"""Tests for p1rec.stream: several recorded SSE clients sharing server-global subscriptions.

End-to-end tests run run_stream in the main thread against a real HTTP
server on a free loopback port. Tests of the subscription logic drive
stream._Session directly, with recorders the test connects by hand and a
CapturingClient answered in-process, and wait for the control thread with
wait_until_handled before checking that a connection sent nothing.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from datetime import time as wall_time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest
from helpers import (
    MINIMAL_CONFIG_LINES,
    SECRET_ENV_VARS,
    UNUSED_EVENT_FIELDS,
    UNUSED_ORDERS_CONFIG,
    UNUSED_TRADE_FIELDS,
    WriteConfig,
)

from p1rec import capture, cli, sse, stream
from p1rec.capture import CapturingClient
from p1rec.config import (
    ConfigError,
    ContractFields,
    Endpoint,
    FundsConfig,
    HistoryConfig,
    Instrument,
    InterlockConfig,
    P1Config,
    ScrubConfig,
    StartupRequest,
    StreamAction,
    StreamConfig,
    load_config,
)
from p1rec.interlock import EXIT_INTERLOCK_ABORT
from p1rec.records import EXIT_CHECK_FAILED
from p1rec.redact import redaction_values, setup_logging
from p1rec.stream import render_template, run_stream

INFO_PATH = "/api/v1/info"
ACCOUNTS_PATH = "/api/v1/auth/accounts"
SUBSCRIBE_TRADE_PATH = "/api/v1/auth/subscribe_trade"
SUBSCRIBE_PATH = "/api/v1/stream/subscribe"
UNSUBSCRIBE_PATH = "/api/v1/stream/unsubscribe"

STREAMS = {
    "data": Endpoint(method="GET", path="/api/v1/stream/data"),
    "order_event": Endpoint(method="GET", path="/api/v1/stream/data/order_event"),
    "contract_event": Endpoint(method="GET", path="/api/v1/stream/data/contract_event"),
}
SSE_PATHS = frozenset(endpoint.path for endpoint in STREAMS.values())
KEYS = [f"{client}/{name}" for client in ("a", "b") for name in STREAMS]

BODY_TEMPLATE = json.dumps(
    {
        "security_type": "{security_type}",
        "exchange": "{exchange}",
        "code": "{code}",
        "quote_type": "{quote_type}",
        "intraday_odd": "{intraday_odd}",
    }
)

INSTRUMENTS = (
    Instrument(code="2330", exchange="TSE", security_type="STK", quote_types=("Tick", "BidAsk"), intraday_odd=False),
    Instrument(code="00981A", exchange="TSE", security_type="STK", quote_types=("Tick",), intraday_odd=True),
    Instrument(code="TMFJ6", exchange="TAIFEX", security_type="FUT", quote_types=("Tick",), intraday_odd=False),
)


def _body(code: str, exchange: str, security_type: str, quote_type: str, intraday_odd: bool) -> dict[str, object]:
    return {
        "security_type": security_type,
        "exchange": exchange,
        "code": code,
        "quote_type": quote_type,
        "intraday_odd": intraday_odd,
    }


SUBSCRIPTION_BODIES = [
    _body("2330", "TSE", "STK", "Tick", False),
    _body("2330", "TSE", "STK", "BidAsk", False),
    _body("00981A", "TSE", "STK", "Tick", True),
    _body("TMFJ6", "TAIFEX", "FUT", "Tick", False),
]
SUBSCRIPTION_POSTS = [(SUBSCRIBE_PATH, body) for body in SUBSCRIPTION_BODIES]

# Fake accounts shaped like the official server's Account objects
ACCOUNTS = [
    {
        "account_type": "S",
        "broker_id": "9A00",
        "account_id": "0000001",
        "person_id": "Z100000000",
        "signed": True,
        "username": "tester",
    },
    {
        "account_type": "F",
        "broker_id": "F000000",
        "account_id": "0000002",
        "person_id": "Z100000000",
        "signed": False,
        "username": "tester",
    },
]
ACCOUNT_REQUEST_FIELDS = ("account_type", "broker_id", "account_id", "person_id")
STARTUP_POSTS = [
    (SUBSCRIBE_TRADE_PATH, {name: account[name] for name in ACCOUNT_REQUEST_FIELDS}) for account in ACCOUNTS
]
INITIAL_POSTS = STARTUP_POSTS + SUBSCRIPTION_POSTS

HEARTBEAT = b'event:heartbeat\ndata:{"type":"heartbeat","connection_id":"1"}\n\n'

# Generous bounds for events that normally take milliseconds, so only a real bug waits them out
WAIT_S = 5.0
SAFETY_UNTIL_S = 30.0

Log = list[tuple[str, str, object]]


def _cfg(
    base_url: str,
    actions: tuple[StreamAction, ...] = (),
    *,
    request_retry_s: float = 60.0,
    daily_resend_at: wall_time | None = None,
) -> P1Config:
    endpoints = {
        "mode": Endpoint(method="GET", path=INFO_PATH),
        "accounts": Endpoint(method="GET", path=ACCOUNTS_PATH),
        "subscribe_trade": Endpoint(method="POST", path=SUBSCRIBE_TRADE_PATH),
        "stream_subscribe": Endpoint(method="POST", path=SUBSCRIBE_PATH),
        "stream_unsubscribe": Endpoint(method="POST", path=UNSUBSCRIBE_PATH),
    }
    return P1Config(
        base_url=base_url,
        http_timeout_s=WAIT_S,
        results_root=Path("results"),
        fixtures_root=Path("fixtures"),
        secret_env={},
        endpoints=endpoints,
        interlock=InterlockConfig(endpoint_op="mode", pointer="/simulation"),
        contracts=ContractFields(code="", limit_up="", limit_down="", reference="", futures_tick="", etf_codes=()),
        funds=FundsConfig(
            stock_available_op="",
            stock_available_pointer="",
            stock_report_only={},
            futures_available_pointer="",
            futures_margin_twd={},
        ),
        stream=StreamConfig(
            streams=dict(STREAMS),
            subscribe=endpoints["stream_subscribe"],
            unsubscribe=endpoints["stream_unsubscribe"],
            subscribe_body=BODY_TEMPLATE,
            unsubscribe_body=BODY_TEMPLATE,
            startup_requests=(
                StartupRequest(op="subscribe_trade", body=None, account_type="S"),
                StartupRequest(op="subscribe_trade", body=None, account_type="F"),
            ),
            retry_s=0.05,
            request_retry_s=request_retry_s,
            daily_resend_at=daily_resend_at,
            instruments=INSTRUMENTS,
            actions=actions,
        ),
        orders=UNUSED_ORDERS_CONFIG,
        trades=UNUSED_TRADE_FIELDS,
        events=UNUSED_EVENT_FIELDS,
        history=HistoryConfig(queries=(), usage_bytes_pointer="/bytes"),
        scrub=ScrubConfig(
            account_id_pointers=(),
            ca_pointers=(),
            account_id_keys=(),
            map_path=Path("."),
            selection=Path("."),
            contract_doc=Path("."),
            extra_scan_paths=(),
        ),
    )


def _posts(log: Log) -> list[tuple[str, object]]:
    return [(path, body) for method, path, body in log if method == "POST"]


class _FakeApi:
    """Answer the REST operations the stream subcommand uses, logging every request in arrival order.

    SSE connections are logged too, as ("SSE", path, None), when the
    end-to-end server receives them. trade_confirms gives, call by call,
    the subscribe_trade field of each subscribe_trade response; calls
    beyond it are confirmed. refuse_times[code] subscription requests for
    code are answered with success false before the next ones succeed.
    While gate is clear, every POST waits for it after being logged.
    """

    def __init__(
        self,
        *,
        refuse: frozenset[str] = frozenset(),
        fail: frozenset[str] = frozenset(),
        accounts_status: int = 200,
        trade_confirms: tuple[bool, ...] = (),
        refuse_times: Mapping[str, int] | None = None,
        simulation: bool = True,
    ) -> None:
        self._cond = threading.Condition()
        self._log: Log = []
        self._refuse = refuse  # Codes whose subscription is answered with success false
        self._fail = fail  # Codes whose subscription is answered with HTTP 500
        self._accounts_status = accounts_status
        self._trade_confirms = list(trade_confirms)
        self._refuse_times = dict(refuse_times or {})
        self._simulation = simulation
        self.gate = threading.Event()
        self.gate.set()

    def record(self, method: str, path: str, body: object) -> None:
        with self._cond:
            self._log.append((method, path, body))
            self._cond.notify_all()

    def log(self) -> Log:
        with self._cond:
            return list(self._log)

    def wait_until(self, predicate: Callable[[Log], bool]) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: predicate(self._log), WAIT_S)

    def answer(self, method: str, path: str, raw_body: bytes) -> tuple[int, object]:
        body = json.loads(raw_body) if raw_body else None
        self.record(method, path, body)
        if method == "POST":
            self.gate.wait(WAIT_S)
        if (method, path) == ("GET", INFO_PATH):
            return 200, {"simulation": self._simulation}
        if (method, path) == ("GET", ACCOUNTS_PATH):
            return self._accounts_status, ACCOUNTS if self._accounts_status == 200 else {"detail": "fake failure"}
        if (method, path) == ("POST", SUBSCRIBE_TRADE_PATH):
            with self._cond:
                confirmed = self._trade_confirms.pop(0) if self._trade_confirms else True
            return 200, {"account": body, "subscribe_trade": confirmed, "ts": 0}
        if method == "POST" and path in (SUBSCRIBE_PATH, UNSUBSCRIBE_PATH):
            code = str(cast(dict[str, object], body)["code"])
            if code in self._fail:
                return 500, {"detail": "fake failure"}
            with self._cond:
                refused_once = self._refuse_times.get(code, 0) > 0
                if refused_once:
                    self._refuse_times[code] -= 1
            success = code not in self._refuse and not refused_once
            return 200, {"success": success, "message": "fake", "subscription": body}
        return 404, {"detail": "not found"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        status, body = self.answer(request.method, request.url.path, request.content)
        return httpx.Response(status, json=body)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        server = cast(_FakeServer, self.server)
        if self.path in SSE_PATHS:
            server.api.record("SSE", self.path, None)
            generation = server.generation(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(HEARTBEAT)
            self.wfile.flush()
            server.hold(self.path, generation)
            return
        self._reply(*server.api.answer("GET", self.path, b""))

    def do_POST(self) -> None:
        server = cast(_FakeServer, self.server)
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._reply(*server.api.answer("POST", self.path, raw_body))

    def _reply(self, status: int, body: object) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _FakeServer(ThreadingHTTPServer):
    """A real HTTP server on a free loopback port.

    Each SSE stream sends one heartbeat, then stays silent until drop() ends
    every open stream on its path, or the server closes.
    """

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.api = _FakeApi()
        self._cond = threading.Condition()
        self._generations: dict[str, int] = {}
        self._closed = False

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host!s}:{port}"

    def generation(self, path: str) -> int:
        with self._cond:
            return self._generations.get(path, 0)

    def hold(self, path: str, generation: int) -> None:
        with self._cond:
            self._cond.wait_for(lambda: self._closed or self._generations.get(path, 0) != generation)

    def drop(self, path: str) -> None:
        with self._cond:
            self._generations[path] = self._generations.get(path, 0) + 1
            self._cond.notify_all()

    def close_streams(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def handle_error(self, request: object, client_address: object) -> None:
        # A recorder hanging up on a silent stream is expected here
        pass


@pytest.fixture
def fake_server() -> Iterator[_FakeServer]:
    server = _FakeServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.close_streams()
        server.shutdown()
        server.server_close()


@pytest.fixture
def allow_test_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let run_stream start although the fake server's threads are running.

    Those threads keep the stop signals unblocked, which is safe here only
    because these tests send them to the main thread alone.
    """
    monkeypatch.setattr(stream, "_check_no_other_threads", lambda: None)


def _run_until_ready(cfg: P1Config, run_dir: Path, api: _FakeApi, ready: Callable[[Log], bool]) -> int:
    """Run run_stream for clients a and b in this thread, sending it SIGTERM once ready(log) holds.

    The test keeps the stop signals blocked around the whole run, so a
    SIGTERM that lands after run_stream has returned stays pending and is
    discarded instead of killing the test process.
    """
    main_thread_id = threading.main_thread().ident
    assert main_thread_id is not None
    finished = threading.Event()

    def _stop_when_ready() -> None:
        api.wait_until(ready)
        if not finished.is_set():
            signal.pthread_kill(main_thread_id, signal.SIGTERM)

    with stream._stop_signals_blocked():
        stopper = threading.Thread(target=_stop_when_ready, daemon=True)
        stopper.start()
        try:
            return run_stream(cfg, ["a", "b"], datetime.now(timezone.utc) + timedelta(seconds=SAFETY_UNTIL_S), run_dir)
        finally:
            finished.set()
            stopper.join()


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_every_recorder_stopped(run_dir: Path) -> None:
    files = sorted(run_dir.glob("sse_*.jsonl"))
    assert len(files) == 6
    for path in files:
        assert _jsonl(path)[-1]["kind"] == "stopped"


class _FakeRecorder(threading.Thread):
    """Stand in for SseRecorder without a server: the test calls connect() to play a 200 response."""

    def __init__(
        self,
        base_url: str,
        client_name: str,
        stream_name: str,
        connect: Endpoint,
        out: Path,
        retry_s: float,
        stop: threading.Event,
        on_connected: Callable[[], None],
    ) -> None:
        super().__init__(name=f"fake-{client_name}-{stream_name}", daemon=True)
        self.key = f"{client_name}/{stream_name}"
        self._stop_event = stop
        self._on_connected = on_connected

    def run(self) -> None:
        self._stop_event.wait()

    def connect(self) -> None:
        self._on_connected()


class _StuckRecorder(_FakeRecorder):
    """A recorder that ignores stop, like one blocked waiting for response headers, until released."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.released = threading.Event()

    def run(self) -> None:
        self.released.wait()


@dataclass
class _Run:
    session: stream._Session
    recorders: dict[str, _FakeRecorder]
    stop: threading.Event

    def connect(self, key: str) -> None:
        """Play a connection of recorder key and wait until the control thread has acted on it."""
        self.recorders[key].connect()
        assert self.session.wait_until_handled(WAIT_S)


@contextmanager
def _session(
    tmp_path: Path,
    api: _FakeApi,
    cfg: P1Config,
    recorder_types: Mapping[str, type[_FakeRecorder]] | None = None,
    clock: stream.Clock = stream._utc_now,
) -> Iterator[_Run]:
    """Start a stream session for clients a and b with fake recorders."""
    client = CapturingClient(
        cfg.base_url, "stream", tmp_path / "rest.jsonl", WAIT_S, transport=httpx.MockTransport(api.handle)
    )
    recorders: dict[str, _FakeRecorder] = {}

    def factory(*args: object) -> _FakeRecorder:
        key = f"{args[1]}/{args[2]}"
        recorder = (recorder_types or {}).get(key, _FakeRecorder)(*args)  # type: ignore[arg-type]
        recorders[recorder.key] = recorder
        return recorder

    stop = threading.Event()
    session = stream._Session(cfg, client, ["a", "b"], tmp_path, stop, recorder_factory=factory, clock=clock)
    session.start(clock())
    try:
        yield _Run(session, recorders, stop)
    finally:
        session.shutdown()
        client.close()


def _connect_all(run: _Run, skip: frozenset[str] = frozenset()) -> None:
    for key, recorder in run.recorders.items():
        if key not in skip:
            recorder.connect()


def _initial_sent(log: Log) -> bool:
    return len(_posts(log)) >= len(INITIAL_POSTS)


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


def test_one_recorder_per_client_and_stream(fake_server: _FakeServer, allow_test_threads: None, tmp_path: Path) -> None:
    exit_code = _run_until_ready(
        _cfg(fake_server.base_url), tmp_path, fake_server.api, lambda log: sum(m == "SSE" for m, _, _ in log) == 6
    )

    assert exit_code == 0
    expected = {f"sse_{client}_{name}.jsonl" for client in ("a", "b") for name in STREAMS}
    assert {path.name for path in tmp_path.glob("sse_*.jsonl")} == expected
    for client in ("a", "b"):
        for name, endpoint in STREAMS.items():
            lines = _jsonl(tmp_path / f"sse_{client}_{name}.jsonl")

            # Whether the heartbeat was read before the stop depends on timing
            assert (lines[0]["kind"], lines[0]["status"], lines[-1]["kind"]) == ("connected", 200, "stopped")
            assert all(line["client"] == client and line["stream"] == name for line in lines)
            assert str(lines[0]["url"]).endswith(endpoint.path)


def test_startup_then_subscriptions_sent_once(
    fake_server: _FakeServer, allow_test_threads: None, tmp_path: Path
) -> None:
    api = fake_server.api

    exit_code = _run_until_ready(_cfg(fake_server.base_url), tmp_path, api, _initial_sent)

    assert exit_code == 0
    log = api.log()

    # The interlock check, then all six first connections, then one startup
    # sequence and one subscription each, not one set per client.
    assert log[0] == ("GET", INFO_PATH, None)
    assert sorted(path for method, path, _ in log[1:7] if method == "SSE") == sorted(
        endpoint.path for _ in ("a", "b") for endpoint in STREAMS.values()
    )
    assert log[7] == ("GET", ACCOUNTS_PATH, None)
    assert _posts(log) == INITIAL_POSTS
    assert len(log) == 8 + len(INITIAL_POSTS)

    rest = _jsonl(tmp_path / "rest.jsonl")
    assert [line["op"] for line in rest] == ["mode", "accounts"] + ["subscribe_trade"] * 2 + ["stream_subscribe"] * 4
    assert {line["client"] for line in rest} == {"stream"}


def test_leader_reconnect_resends(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    api = _FakeApi()

    with (
        caplog.at_level(logging.INFO, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake")) as run,
    ):
        _connect_all(run)
        assert api.wait_until(_initial_sent)

        # Every other recorder reconnecting, including client b's first stream, sends nothing
        for key in KEYS[1:]:
            run.connect(key)
        assert _posts(api.log()) == INITIAL_POSTS

        run.connect("a/data")
        resent = _posts(api.log())[len(INITIAL_POSTS) :]

    assert resent == INITIAL_POSTS

    # The account list fetched for the first send is reused
    assert sum(path == ACCOUNTS_PATH for _, path, _ in api.log()) == 1
    resend_logs = [record for record in caplog.records if "resending" in record.getMessage()]
    assert len(resend_logs) == 1
    assert resend_logs[0].levelno == logging.INFO
    assert "a/data" in resend_logs[0].getMessage()


def test_scheduled_unsubscribe_updates_active_set(tmp_path: Path) -> None:
    api = _FakeApi()
    action = StreamAction(
        at=datetime.now(timezone.utc) + timedelta(seconds=0.2), op="unsubscribe", code="2330", quote_type="Tick"
    )

    with _session(tmp_path, api, _cfg("http://fake", actions=(action,))) as run:
        _connect_all(run)
        assert api.wait_until(lambda log: any(path == UNSUBSCRIBE_PATH for path, _ in _posts(log)))
        run.connect("a/data")
        posts = _posts(api.log())

    assert posts[len(INITIAL_POSTS)] == (UNSUBSCRIBE_PATH, _body("2330", "TSE", "STK", "Tick", False))
    resent = [body for path, body in posts[len(INITIAL_POSTS) + 1 :] if path == SUBSCRIBE_PATH]
    assert resent == [body for body in SUBSCRIPTION_BODIES if body != _body("2330", "TSE", "STK", "Tick", False)]


def test_past_action_skipped_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    api = _FakeApi()
    action = StreamAction(
        at=datetime.now(timezone.utc) - timedelta(minutes=1), op="unsubscribe", code="2330", quote_type="Tick"
    )

    with (
        caplog.at_level(logging.WARNING, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake", actions=(action,))) as run,
    ):
        _connect_all(run)
        assert api.wait_until(_initial_sent)
        run.connect("a/data")

    assert _posts(api.log()) == INITIAL_POSTS + INITIAL_POSTS
    [warning] = _messages(caplog, logging.WARNING)
    assert "skipping scheduled unsubscribe of 2330 Tick" in warning


def test_until_stops_all_clients(fake_server: _FakeServer, allow_test_threads: None, tmp_path: Path) -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=0.5)

    exit_code = run_stream(_cfg(fake_server.base_url), ["a", "b"], until, tmp_path)

    assert exit_code == 0
    assert datetime.now(timezone.utc) >= until
    _assert_every_recorder_stopped(tmp_path)


def test_until_deadline_ignores_wall_clock_steps(
    fake_server: _FakeServer, allow_test_threads: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step_back_s = 3.0
    until = datetime.now(timezone.utc) + timedelta(seconds=0.3)
    calls: list[None] = []

    class _SteppedBack(datetime):
        # The wall clock steps back by step_back_s right after run_stream first reads it
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            calls.append(None)
            shift = timedelta(0) if len(calls) == 1 else timedelta(seconds=step_back_s)
            return datetime.now(timezone.utc) - shift

    monkeypatch.setattr(stream, "datetime", _SteppedBack)
    started = time.monotonic()

    exit_code = run_stream(_cfg(fake_server.base_url), ["a", "b"], until, tmp_path)

    assert exit_code == 0
    assert time.monotonic() - started < step_back_s


def test_first_send_waits_at_most_the_read_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sse, "SSE_READ_TIMEOUT_S", 0.2)
    api = _FakeApi()

    with (
        caplog.at_level(logging.WARNING, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake")) as run,
    ):
        _connect_all(run, skip=frozenset({"b/contract_event"}))
        assert api.wait_until(_initial_sent)

    assert _posts(api.log()) == INITIAL_POSTS
    [warning] = _messages(caplog, logging.WARNING)
    assert warning.endswith("first connection from: b/contract_event")


def test_failed_subscriptions_logged_and_rest_sent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    api = _FakeApi(refuse=frozenset({"2330"}), fail=frozenset({"00981A"}))

    with (
        caplog.at_level(logging.ERROR, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake")) as run,
    ):
        _connect_all(run)
        assert api.wait_until(_initial_sent)

    assert not run.session.failed
    assert _posts(api.log()) == INITIAL_POSTS
    assert _messages(caplog, logging.ERROR) == [
        "subscribe 2330 Tick failed: success is not true (the response has a message; see rest.jsonl)",
        "subscribe 2330 BidAsk failed: success is not true (the response has a message; see rest.jsonl)",
        "subscribe 00981A Tick odd-lot failed: HTTP 500",
    ]


def test_unconfirmed_trade_reports_stop_the_first_send(
    fake_server: _FakeServer, allow_test_threads: None, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake_server.api = _FakeApi(trade_confirms=(True, False))

    with caplog.at_level(logging.ERROR, logger="p1rec.stream"):
        exit_code = run_stream(
            _cfg(fake_server.base_url),
            ["a", "b"],
            datetime.now(timezone.utc) + timedelta(seconds=SAFETY_UNTIL_S),
            tmp_path,
        )

    assert exit_code == EXIT_CHECK_FAILED

    # Both startup requests went out, then no subscription
    assert _posts(fake_server.api.log()) == STARTUP_POSTS
    _assert_every_recorder_stopped(tmp_path)
    errors = _messages(caplog, logging.ERROR)
    assert errors[0] == "subscribe_trade for account type F failed: subscribe_trade is not true"
    assert errors[1].startswith("first send failed for subscribe_trade for account type F; stopping")


def test_account_lookup_failure_stops_the_first_send(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    api = _FakeApi(accounts_status=500)

    with (
        caplog.at_level(logging.ERROR, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake")) as run,
    ):
        _connect_all(run)
        assert run.stop.wait(WAIT_S)

    assert run.session.failed
    assert _posts(api.log()) == []
    assert sum(path == ACCOUNTS_PATH for _, path, _ in api.log()) == 1
    assert _messages(caplog, logging.ERROR) == [
        "subscribe_trade for account type S failed: the account list is unavailable (accounts returned HTTP 500)",
        "subscribe_trade for account type F failed: the account list is unavailable (accounts returned HTTP 500)",
        "first send failed for subscribe_trade for account type S, subscribe_trade for account type F; "
        "stopping so that the run can be restarted once the cause is fixed",
    ]


class _AccountQuotingApi(_FakeApi):
    """Refuse each subscribe_trade with a message quoting the account's id and username."""

    def answer(self, method: str, path: str, raw_body: bytes) -> tuple[int, object]:
        status, body = super().answer(method, path, raw_body)
        if (method, path) != ("POST", SUBSCRIBE_TRADE_PATH):
            return status, body
        request = cast(dict[str, object], json.loads(raw_body))
        [account] = [account for account in ACCOUNTS if account["account_id"] == request["account_id"]]
        message = f"account {account['account_id']} of {account['username']} is not signed"
        return status, {"account": request, "subscribe_trade": False, "message": message, "ts": 0}


def test_subscribe_trade_failure_message_not_logged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    account_id, username = str(ACCOUNTS[0]["account_id"]), str(ACCOUNTS[0]["username"])
    made_up_secrets = {
        "api_key": "FakeApiKey0123456789abcdef",
        "sec_key": "FakeSecKey9876543210zyxwv",
        "ca_passwd": "fake-ca-passwd-q7",
        "egress_ip": "203.0.113.77",
    }
    setup_logging(tmp_path, redaction_values(made_up_secrets), "INFO")
    api = _AccountQuotingApi()

    with _session(tmp_path, api, _cfg("http://fake")) as run:
        _connect_all(run)
        assert run.stop.wait(WAIT_S)

    # Anything logged later that quotes an account the stream looked up is redacted
    logging.getLogger("p1rec.stream").error("quoting %s and %s", account_id, username)
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert run.session.failed
    assert "is not signed" in (tmp_path / "rest.jsonl").read_text(encoding="utf-8")
    log_text = (tmp_path / "recorder.log").read_text(encoding="utf-8")
    stderr = capsys.readouterr().err
    for output in (log_text, stderr):
        assert account_id not in output
        assert username not in output
        assert "is not signed" not in output
        assert "subscribe_trade is not true (the response has a message; see rest.jsonl)" in output
        assert "quoting *** and ***" in output


def test_failed_startup_request_retried_on_any_next_connection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The first send and the leader's resend confirm S; the resend refuses F
    api = _FakeApi(trade_confirms=(True, True, True, False))

    with (
        caplog.at_level(logging.ERROR, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake")) as run,
    ):
        _connect_all(run)
        assert api.wait_until(_initial_sent)
        run.connect("a/data")
        assert _posts(api.log()) == INITIAL_POSTS + INITIAL_POSTS

        # Any recorder's next connection retries only the failed request, once
        run.connect("b/order_event")
        run.connect("b/contract_event")

    assert not run.session.failed
    assert _posts(api.log())[2 * len(INITIAL_POSTS) :] == [STARTUP_POSTS[1]]
    assert _messages(caplog, logging.ERROR) == [
        "subscribe_trade for account type F failed: subscribe_trade is not true"
    ]


def test_failed_subscription_retried_on_a_timer(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    retry_s = 0.1
    api = _FakeApi(refuse_times={"TMFJ6": 1})
    tmfj6 = (SUBSCRIBE_PATH, _body("TMFJ6", "TAIFEX", "FUT", "Tick", False))

    with (
        caplog.at_level(logging.INFO, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake", request_retry_s=retry_s)) as run,
    ):
        _connect_all(run)

        # Every stream stays connected, so only the timer can send it again
        assert api.wait_until(lambda log: len(_posts(log)) == len(INITIAL_POSTS) + 1)

    assert not run.session.failed
    assert _posts(api.log())[len(INITIAL_POSTS) :] == [tmfj6]
    sent = [line for line in _jsonl(tmp_path / "rest.jsonl") if '"TMFJ6"' in str(line["req_body"])]
    assert cast(int, sent[1]["sent_ts_ns"]) - cast(int, sent[0]["recv_ts_ns"]) >= retry_s * 1e9
    assert _messages(caplog, logging.ERROR) == [
        "subscribe TMFJ6 Tick failed: success is not true (the response has a message; see rest.jsonl)"
    ]
    assert "subscribe TMFJ6 Tick succeeded after 1 failed attempt(s)" in _messages(caplog, logging.INFO)


def test_refused_trade_reports_on_resend_retried_without_a_new_connection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The first send confirms both accounts; the leader's resend and the first timed retry refuse F
    api = _FakeApi(trade_confirms=(True, True, True, False, False))

    with (
        caplog.at_level(logging.DEBUG, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake", request_retry_s=0.05)) as run,
    ):
        _connect_all(run)
        assert api.wait_until(_initial_sent)
        run.connect("a/data")
        assert api.wait_until(lambda log: len(_posts(log)) == 2 * len(INITIAL_POSTS) + 2)

    assert _posts(api.log())[2 * len(INITIAL_POSTS) :] == [STARTUP_POSTS[1], STARTUP_POSTS[1]]
    f_logs = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.getMessage().startswith("subscribe_trade for account type F")
    ]
    assert f_logs == [
        (logging.ERROR, "subscribe_trade for account type F failed: subscribe_trade is not true"),
        (
            logging.DEBUG,
            "subscribe_trade for account type F failed again, 2 attempts in a row: subscribe_trade is not true",
        ),
        (logging.INFO, "subscribe_trade for account type F succeeded after 2 failed attempt(s)"),
    ]


def test_daily_resend_after_the_maintenance_window(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    api = _FakeApi()
    resend_at = wall_time(8, 35)
    real_start = datetime.now(timezone.utc)
    taipei = ZoneInfo("Asia/Taipei")
    due = datetime.combine(real_start.astimezone(taipei).date(), resend_at, tzinfo=taipei)

    # A clock that reads a moment before the resend time and then runs at the real rate
    offset = due - timedelta(seconds=0.3) - real_start

    def clock() -> datetime:
        return datetime.now(timezone.utc) + offset

    with (
        caplog.at_level(logging.INFO, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake", daily_resend_at=resend_at), clock=clock) as run,
    ):
        _connect_all(run)
        assert api.wait_until(_initial_sent)

        # No recorder connects again; the clock passing the resend time alone sends everything
        assert api.wait_until(lambda log: len(_posts(log)) == 2 * len(INITIAL_POSTS))
        assert clock() >= due

        # The next day's resend is a day away, so acting on a later connection sends nothing
        run.connect("b/order_event")

    assert _posts(api.log()) == INITIAL_POSTS + INITIAL_POSTS
    daily = [message for message in _messages(caplog, logging.INFO) if "daily resend" in message]
    assert daily == [
        "daily resend at 08:35 Asia/Taipei, after the maintenance window; "
        "resending 2 startup request(s) and 4 subscription(s)"
    ]


def test_control_failure_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _broken(_self: object) -> list[str]:
        raise RuntimeError("fake control failure")

    monkeypatch.setattr(stream._Coordinator, "send_initial", _broken)
    api = _FakeApi()

    with (
        caplog.at_level(logging.ERROR, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake")) as run,
    ):
        _connect_all(run)
        assert run.stop.wait(WAIT_S)

    assert run.session.failed
    assert _messages(caplog, logging.ERROR) == ["stream control failed; stopping the run"]


def test_control_error_during_shutdown_keeps_a_normal_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sending = threading.Event()

    def _fails_once_stopping(self: stream._Coordinator) -> list[str]:
        # Like a request cut short when shutdown closes the client under it
        sending.set()
        self._stop.wait()
        raise httpx.ReadError("client closed")

    monkeypatch.setattr(stream._Coordinator, "send_initial", _fails_once_stopping)
    api = _FakeApi()

    with (
        caplog.at_level(logging.WARNING, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake")) as run,
    ):
        _connect_all(run)
        assert sending.wait(WAIT_S)

    assert not run.session.failed
    assert _messages(caplog, logging.ERROR) == []

    # A failure reported by any path once shutdown has begun leaves the outcome alone
    run.session._fail()
    assert not run.session.failed


def test_resend_does_not_delay_leader_frames(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames: dict[str, int] = {}
    frames_cond = threading.Condition()
    append_jsonl = capture.append_jsonl

    def spy_append(path: Path, record: Mapping[str, object]) -> None:
        append_jsonl(path, record)
        if record.get("kind") == "frame":
            with frames_cond:
                frames[path.name] = frames.get(path.name, 0) + 1
                frames_cond.notify_all()

    monkeypatch.setattr(capture, "append_jsonl", spy_append)
    api = _FakeApi()
    cfg = _cfg(fake_server.base_url)
    client = CapturingClient(
        cfg.base_url, "stream", tmp_path / "rest.jsonl", WAIT_S, transport=httpx.MockTransport(api.handle)
    )
    session = stream._Session(cfg, client, ["a"], tmp_path, threading.Event())
    session.start(datetime.now(timezone.utc))
    try:
        assert api.wait_until(lambda log: len(_posts(log)) == len(INITIAL_POSTS))

        # The resend's first request now hangs until the gate opens again
        api.gate.clear()
        fake_server.drop(STREAMS["data"].path)
        assert api.wait_until(lambda log: len(_posts(log)) == len(INITIAL_POSTS) + 1)

        # The leader has reconnected and records its new heartbeat while the resend is stuck
        with frames_cond:
            assert frames_cond.wait_for(lambda: frames.get("sse_a_data.jsonl", 0) == 2, WAIT_S)
        assert not api.gate.is_set()
    finally:
        api.gate.set()
        session.shutdown()
        client.close()


def test_stuck_recorder_named_at_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sse, "SSE_READ_TIMEOUT_S", 0.1)
    api = _FakeApi()

    stuck: list[_StuckRecorder] = []

    class _Tracked(_StuckRecorder):
        def __init__(self, *args: Any) -> None:
            super().__init__(*args)
            stuck.append(self)

    try:
        with (
            caplog.at_level(logging.WARNING, logger="p1rec.stream"),
            _session(tmp_path, api, _cfg("http://fake"), {"b/order_event": _Tracked}) as run,
        ):
            _connect_all(run)
            assert api.wait_until(_initial_sent)
    finally:
        for recorder in stuck:
            recorder.released.set()

    [warning] = _messages(caplog, logging.WARNING)
    assert warning.endswith("left behind: b/order_event")


def test_other_threads_refused(fake_server: _FakeServer, tmp_path: Path) -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=SAFETY_UNTIL_S)

    with pytest.raises(RuntimeError, match="before any other thread"):
        run_stream(_cfg(fake_server.base_url), ["a", "b"], until, tmp_path)

    assert [method for method, _, _ in fake_server.api.log()] == ["GET"]  # The interlock check only
    assert list(tmp_path.glob("sse_*.jsonl")) == []


def test_render_template_keeps_json_types() -> None:
    template = json.loads(BODY_TEMPLATE) | {"note": "{literal braces", "nested": ["{code}"]}
    values = {"code": "00981A", "exchange": "TSE", "security_type": "STK", "quote_type": "Tick", "intraday_odd": True}

    assert render_template(template, values) == {
        **_body("00981A", "TSE", "STK", "Tick", True),
        "note": "{literal braces",
        "nested": ["00981A"],
    }


STREAM_CONFIG_LINES = [
    "",
    "[endpoints.accounts]",
    'method = "GET"',
    f'path = "{ACCOUNTS_PATH}"',
    "",
    "[endpoints.subscribe_trade]",
    'method = "POST"',
    f'path = "{SUBSCRIBE_TRADE_PATH}"',
    "",
    "[endpoints.stream_subscribe]",
    'method = "POST"',
    f'path = "{SUBSCRIBE_PATH}"',
    "",
    "[endpoints.stream_unsubscribe]",
    'method = "POST"',
    f'path = "{UNSUBSCRIBE_PATH}"',
    "",
    "[stream]",
    "retry_s = 1.0",
    "request_retry_s = 60.0",
    'daily_resend_at = "08:35"',
    f"subscribe_body = '{BODY_TEMPLATE}'",
    f"unsubscribe_body = '{BODY_TEMPLATE}'",
    'startup_requests = [["subscribe_trade", "@account:S"]]',
    "",
    "[stream.streams.data]",
    'method = "GET"',
    'path = "/api/v1/stream/data"',
    "",
    "[stream.streams.order_event]",
    'method = "GET"',
    'path = "/api/v1/stream/data/order_event"',
    "",
    "[[stream.instrument]]",
    'code = "2330"',
    'exchange = "TSE"',
    'security_type = "STK"',
    'quote_types = ["Tick", "BidAsk"]',
    "intraday_odd = false",
    "",
    "[[stream.action]]",
    "at = 2026-10-02T10:30:00+08:00",
    'op = "unsubscribe"',
    'code = "2330"',
    'quote_type = "Tick"',
]


@pytest.mark.parametrize(("body", "account_type"), [(None, None), ('{"a": 1}', "S")])
def test_startup_request_needs_exactly_one_body_source(body: str | None, account_type: str | None) -> None:
    with pytest.raises(ValueError, match="exactly one of body and account_type"):
        StartupRequest(op="subscribe_trade", body=body, account_type=account_type)


def test_stream_section_loads(write_config: WriteConfig) -> None:
    cfg = load_config(write_config([*MINIMAL_CONFIG_LINES, *STREAM_CONFIG_LINES])).stream

    assert list(cfg.streams) == ["data", "order_event"]
    assert cfg.subscribe == Endpoint(method="POST", path=SUBSCRIBE_PATH)
    assert cfg.startup_requests == (StartupRequest(op="subscribe_trade", body=None, account_type="S"),)
    assert (cfg.retry_s, cfg.request_retry_s, cfg.daily_resend_at) == (1.0, 60.0, wall_time(8, 35))
    assert cfg.instruments == (INSTRUMENTS[0],)
    [action] = cfg.actions
    assert action.at == datetime(2026, 10, 2, 2, 30, tzinfo=timezone.utc)
    assert (action.op, action.code, action.quote_type) == ("unsubscribe", "2330", "Tick")


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ("at = 2026-10-02T10:30:00+08:00", "at = 2026-10-02T10:30:00", r"stream\.action\[0\]\.at"),
        ('code = "2330"\nquote_type', 'code = "2890"\nquote_type', r"stream\.action\[0\]\.code"),
        ('"code": "{code}"', '"code": "x{code}"', r"stream\.subscribe_body"),
        ('"code": "{code}"', '"code": "{ticker}"', r"stream\.subscribe_body"),
        ('["subscribe_trade", "@account:S"]', '["no_such_op", "@account:S"]', r"stream\.startup_requests\[0\]"),
        ("[endpoints.stream_subscribe]", "[endpoints.renamed]", r"endpoints\.stream_subscribe"),
        ('quote_types = ["Tick", "BidAsk"]', 'quote_types = ["Tik"]', r"stream\.instrument\[0\]\.quote_types"),
        # Intervals must be positive and finite, the resend time a zero-padded HH:MM
        ("retry_s = 1.0", "retry_s = inf", r"stream\.retry_s"),
        ("request_retry_s = 60.0", "request_retry_s = nan", r"stream\.request_retry_s"),
        ("request_retry_s = 60.0", "request_retry_s = 0", r"stream\.request_retry_s"),
        ('daily_resend_at = "08:35"', 'daily_resend_at = "8:35"', r"stream\.daily_resend_at"),
        ('daily_resend_at = "08:35"', 'daily_resend_at = "24:00"', r"stream\.daily_resend_at"),
        # Misspelt keys, at every level of the section
        ("[[stream.action]]", "[[stream.actions]]", r"unknown config key: stream\.actions"),
        ("retry_s = 1.0", "retry_s = 1.0\nretries = 3", r"unknown config key: stream\.retries"),
        ('path = "/api/v1/stream/data"', 'path = "/api/v1/stream/data"\nquery = "x"', r"stream\.streams\.data\.query"),
        ("intraday_odd = false", "intraday_odd = false\nodd = true", r"stream\.instrument\[0\]\.odd"),
        ('quote_type = "Tick"', 'quote_type = "Tick"\nwhen = 1', r"stream\.action\[0\]\.when"),
        # Account identifiers, however deep in a configured body
        ('"@account:S"', '"{\\"account\\": {\\"account_id\\": \\"0000001\\"}}"', r"stream\.startup_requests\[0\]"),
        ('"@account:S"', '"[{\\"username\\": \\"tester\\"}]"', r"stream\.startup_requests\[0\].*username"),
        ('"code": "{code}"', '"code": "{code}", "person_id": "Z100000000"', r"stream\.subscribe_body.*person_id"),
    ],
)
def test_stream_section_rejected(write_config: WriteConfig, old: str, new: str, key: str) -> None:
    # Only the stream lines are edited, since MINIMAL_CONFIG_LINES' order
    # templates hold some of the same text.
    stream_text = "\n".join(STREAM_CONFIG_LINES)
    assert old in stream_text
    lines = [*MINIMAL_CONFIG_LINES, *stream_text.replace(old, new, 1).split("\n")]

    with pytest.raises(ConfigError, match=key):
        load_config(write_config(lines))


def test_real_p1_toml_stream_section() -> None:
    cfg = load_config(Path(__file__).resolve().parent.parent / "p1.toml").stream

    assert list(cfg.streams) == ["data", "order_event", "contract_event"]
    assert [request.account_type for request in cfg.startup_requests] == ["S", "F"]
    assert cfg.daily_resend_at == wall_time(8, 35)
    assert any(instrument.intraday_odd for instrument in cfg.instruments)
    assert cfg.actions == ()


def _set_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")


def test_cli_passes_clients_and_until(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_secret_env(monkeypatch)
    calls: list[tuple[list[str], datetime]] = []

    def _fake_run_stream(_cfg: P1Config, clients: list[str], until: datetime, _run_dir: Path) -> int:
        calls.append((clients, until))
        return 0

    monkeypatch.setattr(cli, "run_stream", _fake_run_stream)
    config_path = write_config()

    exit_code = cli.main(
        ["stream", "--config", str(config_path), "--clients", "a,b", "--until", "2026-10-06T09:30:00+08:00"]
    )

    assert exit_code == 0
    assert calls == [(["a", "b"], datetime(2026, 10, 6, 1, 30, tzinfo=timezone.utc))]


def test_cli_interlock_abort_exits_3_without_recorders(
    fake_server: _FakeServer, write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_secret_env(monkeypatch)
    fake_server.api = _FakeApi(simulation=False)
    lines = [*MINIMAL_CONFIG_LINES, *STREAM_CONFIG_LINES]
    lines = [f'base_url = "{fake_server.base_url}"' if line.startswith("base_url") else line for line in lines]
    until = (datetime.now(timezone.utc) + timedelta(seconds=SAFETY_UNTIL_S)).isoformat()

    exit_code = cli.main(["stream", "--config", str(write_config(lines)), "--clients", "a,b", "--until", until])

    assert exit_code == EXIT_INTERLOCK_ABORT
    assert fake_server.api.log() == [("GET", INFO_PATH, None)]
    [run_dir] = (tmp_path / "results").iterdir()
    assert list(run_dir.glob("sse_*.jsonl")) == []


def test_stream_process_stops_on_process_sigterm(
    fake_server: _FakeServer, write_config: WriteConfig, tmp_path: Path
) -> None:
    lines = [*MINIMAL_CONFIG_LINES, *STREAM_CONFIG_LINES]
    lines = [f'base_url = "{fake_server.base_url}"' if line.startswith("base_url") else line for line in lines]
    env = {**os.environ, **dict.fromkeys(SECRET_ENV_VARS, "fake-value-0123456789")}
    until = (datetime.now(timezone.utc) + timedelta(seconds=SAFETY_UNTIL_S)).isoformat()
    command = [sys.executable, "-m", "p1rec.cli", "stream", "--config", str(write_config(lines))]
    process = subprocess.Popen(
        [*command, "--clients", "a,b", "--until", until], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        # One startup request and two subscriptions, per STREAM_CONFIG_LINES
        assert fake_server.api.wait_until(lambda log: len(_posts(log)) == 3)
        os.kill(process.pid, signal.SIGTERM)
        _, stderr = process.communicate(timeout=WAIT_S)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == 0, stderr.decode("utf-8", "replace")
    [run_dir] = (tmp_path / "results").iterdir()
    files = sorted(run_dir.glob("sse_*.jsonl"))
    assert len(files) == 4
    assert all(_jsonl(path)[-1]["kind"] == "stopped" for path in files)
    assert "stopping: received SIGTERM" in (run_dir / "recorder.log").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("clients", "until"),
    [
        ("a,b", "2026-10-06T09:30:00"),  # No UTC offset
        ("a_1,b", "2026-10-06T09:30:00+08:00"),  # Would make the file name ambiguous
        ("a,a", "2026-10-06T09:30:00+08:00"),
    ],
)
def test_cli_rejects_bad_stream_arguments(write_config: WriteConfig, clients: str, until: str) -> None:
    config_path = write_config()

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["stream", "--config", str(config_path), "--clients", clients, "--until", until])

    assert exc_info.value.code == 1
