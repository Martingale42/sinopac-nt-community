"""Tests for p1rec.stream: several recorded SSE clients sharing server-global subscriptions.

End-to-end tests run run_stream in the main thread against a real HTTP
server on a free loopback port. Tests of the subscription logic drive
stream._Session directly, with recorders the test connects by hand and a
CapturingClient answered in-process, so every step is synchronous.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import httpx
import pytest
from helpers import MINIMAL_CONFIG_LINES, SECRET_ENV_VARS, WriteConfig

from p1rec import cli, sse, stream
from p1rec.capture import CapturingClient
from p1rec.config import (
    ConfigError,
    ContractFields,
    Endpoint,
    EventFields,
    FundsConfig,
    HistoryConfig,
    Instrument,
    InterlockConfig,
    OrdersConfig,
    P1Config,
    ScrubConfig,
    StreamAction,
    StreamConfig,
    load_config,
)
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
INITIAL_POSTS = STARTUP_POSTS + [(SUBSCRIBE_PATH, body) for body in SUBSCRIPTION_BODIES]

HEARTBEAT = b'event:heartbeat\ndata:{"type":"heartbeat","connection_id":"1"}\n\n'

# Generous bounds for events that normally take milliseconds, so only a real bug waits them out
WAIT_S = 5.0
SAFETY_UNTIL_S = 30.0

Log = list[tuple[str, str, object]]


def _cfg(base_url: str, actions: tuple[StreamAction, ...] = ()) -> P1Config:
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
        contracts=ContractFields(),
        funds=FundsConfig(),
        stream=StreamConfig(
            streams=dict(STREAMS),
            subscribe=endpoints["stream_subscribe"],
            unsubscribe=endpoints["stream_unsubscribe"],
            subscribe_body=BODY_TEMPLATE,
            unsubscribe_body=BODY_TEMPLATE,
            startup_requests=(("subscribe_trade", "@account:S"), ("subscribe_trade", "@account:F")),
            retry_s=0.05,
            instruments=INSTRUMENTS,
            actions=actions,
        ),
        orders=OrdersConfig(),
        events=EventFields(),
        history=HistoryConfig(),
        scrub=ScrubConfig(),
    )


def _posts(log: Log) -> list[tuple[str, object]]:
    return [(path, body) for method, path, body in log if method == "POST"]


class _FakeApi:
    """Answer the REST operations the stream subcommand uses, logging every request in arrival order.

    SSE connections are logged too, as ("SSE", path, None), when the
    end-to-end server receives them.
    """

    def __init__(
        self, refuse: frozenset[str] = frozenset(), fail: frozenset[str] = frozenset(), accounts_status: int = 200
    ) -> None:
        self._cond = threading.Condition()
        self._log: Log = []
        self._refuse = refuse  # Codes whose subscription is answered with success false
        self._fail = fail  # Codes whose subscription is answered with HTTP 500
        self._accounts_status = accounts_status

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
        if (method, path) == ("GET", INFO_PATH):
            return 200, {"simulation": True}
        if (method, path) == ("GET", ACCOUNTS_PATH):
            return self._accounts_status, ACCOUNTS if self._accounts_status == 200 else {"detail": "fake failure"}
        if (method, path) == ("POST", SUBSCRIBE_TRADE_PATH):
            return 200, {"account": body, "subscribe_trade": True, "ts": 0}
        if method == "POST" and path in (SUBSCRIBE_PATH, UNSUBSCRIBE_PATH):
            code = cast(dict[str, object], body)["code"]
            if code in self._fail:
                return 500, {"detail": "fake failure"}
            return 200, {"success": code not in self._refuse, "message": "fake", "subscription": body}
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
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(HEARTBEAT)
            self.wfile.flush()
            server.done.wait()
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
    """A real HTTP server on a free loopback port whose SSE streams send one heartbeat, then go silent."""

    daemon_threads = True

    def __init__(self, api: _FakeApi) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.api = api
        self.done = threading.Event()

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host!s}:{port}"

    def handle_error(self, request: object, client_address: object) -> None:
        # A recorder hanging up on a silent stream is expected here
        pass


@pytest.fixture
def fake_server() -> Iterator[_FakeServer]:
    server = _FakeServer(_FakeApi())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.done.set()
        server.shutdown()
        server.server_close()


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
    """A recorder that ignores stop, like one blocked waiting for response headers."""

    released = threading.Event()

    def run(self) -> None:
        self.released.wait()


@contextmanager
def _session(
    tmp_path: Path,
    api: _FakeApi,
    cfg: P1Config,
    recorder_types: dict[str, type[_FakeRecorder]] | None = None,
) -> Iterator[dict[str, _FakeRecorder]]:
    """Start a stream session for clients a and b with fake recorders, yielding them by key."""
    client = CapturingClient(
        cfg.base_url, "stream", tmp_path / "rest.jsonl", WAIT_S, transport=httpx.MockTransport(api.handle)
    )
    recorders: dict[str, _FakeRecorder] = {}

    def factory(*args: object) -> _FakeRecorder:
        key = f"{args[1]}/{args[2]}"
        recorder = (recorder_types or {}).get(key, _FakeRecorder)(*args)  # type: ignore[arg-type]
        recorders[recorder.key] = recorder
        return recorder

    session = stream._Session(cfg, client, ["a", "b"], tmp_path, threading.Event(), recorder_factory=factory)
    session.start(datetime.now(timezone.utc))
    try:
        yield recorders
    finally:
        session.shutdown()
        client.close()


def _connect_all(recorders: dict[str, _FakeRecorder], skip: frozenset[str] = frozenset()) -> None:
    for key, recorder in recorders.items():
        if key not in skip:
            recorder.connect()


def _initial_sent(log: Log) -> bool:
    return len(_posts(log)) >= len(INITIAL_POSTS)


def test_one_recorder_per_client_and_stream(fake_server: _FakeServer, tmp_path: Path) -> None:
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


def test_startup_then_subscriptions_sent_once(fake_server: _FakeServer, tmp_path: Path) -> None:
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
        _session(tmp_path, api, _cfg("http://fake")) as recorders,
    ):
        _connect_all(recorders)
        assert api.wait_until(_initial_sent)

        # Every other recorder reconnecting, including client b's first stream, sends nothing
        for key in KEYS[1:]:
            recorders[key].connect()
        assert _posts(api.log()) == INITIAL_POSTS

        recorders["a/data"].connect()
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

    with _session(tmp_path, api, _cfg("http://fake", actions=(action,))) as recorders:
        _connect_all(recorders)
        assert api.wait_until(lambda log: any(path == UNSUBSCRIBE_PATH for path, _ in _posts(log)))
        before = len(_posts(api.log()))

        recorders["a/data"].connect()
        posts = _posts(api.log())

    assert posts[len(INITIAL_POSTS)] == (UNSUBSCRIBE_PATH, _body("2330", "TSE", "STK", "Tick", False))
    resent = [body for path, body in posts[before:] if path == SUBSCRIBE_PATH]
    assert resent == [body for body in SUBSCRIPTION_BODIES if body != _body("2330", "TSE", "STK", "Tick", False)]


def test_past_action_skipped_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    api = _FakeApi()
    action = StreamAction(
        at=datetime.now(timezone.utc) - timedelta(minutes=1), op="unsubscribe", code="2330", quote_type="Tick"
    )

    with (
        caplog.at_level(logging.WARNING, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake", actions=(action,))) as recorders,
    ):
        _connect_all(recorders)
        assert api.wait_until(_initial_sent)
        recorders["a/data"].connect()

    assert _posts(api.log()) == INITIAL_POSTS + INITIAL_POSTS
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "skipping scheduled unsubscribe of 2330 Tick" in warnings[0]


def test_until_stops_all_clients(fake_server: _FakeServer, tmp_path: Path) -> None:
    until = datetime.now(timezone.utc) + timedelta(seconds=0.5)

    exit_code = run_stream(_cfg(fake_server.base_url), ["a", "b"], until, tmp_path)

    assert exit_code == 0
    assert datetime.now(timezone.utc) >= until
    files = sorted(tmp_path.glob("sse_*.jsonl"))
    assert len(files) == 6
    for path in files:
        assert _jsonl(path)[-1]["kind"] == "stopped"


def test_first_send_waits_at_most_the_read_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sse, "SSE_READ_TIMEOUT_S", 0.2)
    api = _FakeApi()

    with (
        caplog.at_level(logging.WARNING, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake")) as recorders,
    ):
        _connect_all(recorders, skip=frozenset({"b/contract_event"}))
        assert api.wait_until(_initial_sent)

    assert _posts(api.log()) == INITIAL_POSTS
    [warning] = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert warning.endswith("first connection from: b/contract_event")


def test_failed_subscriptions_logged_and_rest_sent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    api = _FakeApi(refuse=frozenset({"2330"}), fail=frozenset({"00981A"}))

    with (
        caplog.at_level(logging.ERROR, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake")) as recorders,
    ):
        _connect_all(recorders)
        assert api.wait_until(_initial_sent)

    assert _posts(api.log()) == INITIAL_POSTS
    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert errors == [
        "subscribe 2330 Tick refused: success is not true (message: 'fake')",
        "subscribe 2330 BidAsk refused: success is not true (message: 'fake')",
        "subscribe 00981A Tick odd-lot failed: HTTP 500",
    ]


def test_account_lookup_failure_skips_startup_and_retries(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    api = _FakeApi(accounts_status=500)
    subscriptions = [(SUBSCRIBE_PATH, body) for body in SUBSCRIPTION_BODIES]

    with (
        caplog.at_level(logging.ERROR, logger="p1rec.stream"),
        _session(tmp_path, api, _cfg("http://fake")) as recorders,
    ):
        _connect_all(recorders)
        assert api.wait_until(lambda log: len(_posts(log)) == len(subscriptions))
        recorders["a/data"].connect()

    assert _posts(api.log()) == subscriptions + subscriptions

    # A failed lookup is not cached, so the resend asks again
    assert sum(path == ACCOUNTS_PATH for _, path, _ in api.log()) == 2
    skipped = [record.getMessage() for record in caplog.records if "skipped" in record.getMessage()]
    assert skipped == [
        f"subscribe_trade for account type {account_type} skipped: the account list is unavailable"
        for account_type in ("S", "F", "S", "F")
    ]
    assert all("0000001" not in record.getMessage() for record in caplog.records)


def test_stuck_recorder_named_at_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sse, "SSE_READ_TIMEOUT_S", 0.1)
    api = _FakeApi()

    try:
        with (
            caplog.at_level(logging.WARNING, logger="p1rec.stream"),
            _session(tmp_path, api, _cfg("http://fake"), {"b/order_event": _StuckRecorder}) as recorders,
        ):
            _connect_all(recorders)
            assert api.wait_until(_initial_sent)
    finally:
        _StuckRecorder.released.set()

    [warning] = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert warning.endswith("left behind: b/order_event")


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


def test_stream_section_loads(write_config: WriteConfig) -> None:
    cfg = load_config(write_config([*MINIMAL_CONFIG_LINES, *STREAM_CONFIG_LINES])).stream

    assert list(cfg.streams) == ["data", "order_event"]
    assert cfg.subscribe == Endpoint(method="POST", path=SUBSCRIBE_PATH)
    assert cfg.startup_requests == (("subscribe_trade", "@account:S"),)
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
        ('"@account:S"', '"{\\"account_id\\": \\"0000001\\"}"', r"stream\.startup_requests\[0\]"),
        ("[endpoints.stream_subscribe]", "[endpoints.renamed]", r"endpoints\.stream_subscribe"),
        ('quote_types = ["Tick", "BidAsk"]', 'quote_types = ["Tik"]', r"stream\.instrument\[0\]\.quote_types"),
    ],
)
def test_stream_section_rejected(write_config: WriteConfig, old: str, new: str, key: str) -> None:
    text = "\n".join([*MINIMAL_CONFIG_LINES, *STREAM_CONFIG_LINES])
    assert old in text
    lines = text.replace(old, new, 1).split("\n")

    with pytest.raises(ConfigError, match=key):
        load_config(write_config(lines))


def test_real_p1_toml_stream_section() -> None:
    cfg = load_config(Path(__file__).resolve().parent.parent / "p1.toml").stream

    assert list(cfg.streams) == ["data", "order_event", "contract_event"]
    assert all(body.startswith("@account:") for _, body in cfg.startup_requests)
    assert any(instrument.intraday_odd for instrument in cfg.instruments)
    assert cfg.actions == ()


def test_cli_passes_clients_and_until(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")
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
