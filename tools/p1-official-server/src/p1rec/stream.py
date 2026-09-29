"""Record several SSE clients at once while holding the server's subscriptions.

run_stream opens one SseRecorder per client and configured stream, each
writing sse_<client>_<stream>.jsonl in the run directory, and sends every
REST request through one CapturingClient into rest.jsonl. Subscriptions on
the official server are global, not per connection
(docs/official-server-contract.md section 1), so they are sent once for all
clients: when every recorder has connected, or after sse.SSE_READ_TIMEOUT_S,
whichever comes first, the startup requests go out, then one subscription
per instrument and quote type. After that only the leader recorder, the
first client's first stream, sends them again, each time it connects, since
a new leader connection is the sign that the server may have restarted and
lost them; it does so in its on_connected, before reading any frame.
Scheduled actions change the active subscriptions at their set time. The
run ends at --until or on SIGINT or SIGTERM.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import logging
import re
import signal
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from p1rec import sse
from p1rec.capture import CapturedResponse, CapturingClient, describe_exception
from p1rec.config import (
    ACCOUNT_BODY_RE,
    ACCOUNTS_OP,
    PLACEHOLDER_RE,
    STREAM_SUBSCRIBE_OP,
    STREAM_UNSUBSCRIBE_OP,
    Endpoint,
    Instrument,
    P1Config,
    StreamAction,
)
from p1rec.interlock import assert_simulation

logger = logging.getLogger(__name__)

# Stream names may contain "_", so a client name may not, or the client and
# stream in a file name sse_<client>_<stream>.jsonl could not be told apart.
_CLIENT_NAME_RE = re.compile(r"[A-Za-z0-9-]+")

_REST_CLIENT_NAME = "stream"

# AccountRequest fields (openapi.json), which each Account from accounts also carries
_ACCOUNT_REQUEST_FIELDS = ("account_type", "broker_id", "account_id", "person_id")

_STOP_SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM})

# SseRecorder's constructor, which tests replace with a recorder that needs no server
RecorderFactory = Callable[
    [str, str, str, Endpoint, Path, float, threading.Event, Callable[[], None]], threading.Thread
]


def check_client_names(clients: Sequence[str]) -> None:
    """Raise ValueError unless clients is a non-empty list of distinct names safe in a file name."""
    if not clients:
        raise ValueError("at least one client name is required")
    for name in clients:
        if not _CLIENT_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid client name {name!r}: use only letters, digits and -")
    if len(set(clients)) != len(clients):
        raise ValueError(f"duplicate client name in {','.join(clients)}")


@dataclass(frozen=True)
class Subscription:
    """One server-global subscription: an instrument with one quote type.

    Its field names are the subscription body placeholders, so fields()
    is exactly what a body template is filled from.
    """

    code: str
    exchange: str
    security_type: str
    quote_type: str
    intraday_odd: bool

    @classmethod
    def of(cls, instrument: Instrument, quote_type: str) -> Subscription:
        return cls(instrument.code, instrument.exchange, instrument.security_type, quote_type, instrument.intraday_odd)

    def fields(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def describe(self) -> str:
        return f"{self.code} {self.quote_type}{' odd-lot' if self.intraday_odd else ''}"


def render_template(template: object, values: Mapping[str, object]) -> object:
    """Return template with each string value that is exactly "{name}" replaced by values[name].

    The substitution works on the parsed JSON rather than its text, so a
    value keeps its JSON type (intraday_odd stays a boolean) and a value
    containing quotes or braces cannot change the body's shape. Object keys
    and every other string are copied unchanged; load_config has already
    refused a placeholder anywhere substitution would not reach.
    """
    if isinstance(template, str):
        whole = PLACEHOLDER_RE.fullmatch(template)
        return values[whole.group(1)] if whole else template
    if isinstance(template, dict):
        return {key: render_template(item, values) for key, item in template.items()}
    if isinstance(template, list):
        return [render_template(item, values) for item in template]
    return template


def _is_success(status: int) -> bool:
    return 200 <= status < 300


class _Coordinator:
    """Track recorder connections and send the startup requests and subscriptions.

    _cond guards which recorders have connected and whether the initial
    send has begun. _send_lock serializes every sequence of requests and
    every change to the active subscriptions, so a resend never interleaves
    with the initial send or a scheduled action; it is always taken before
    _cond, never while holding it. Each sequence stops before its next
    request once stop is set.
    """

    def __init__(self, cfg: P1Config, client: CapturingClient, keys: Sequence[str], stop: threading.Event) -> None:
        stream_cfg = cfg.stream
        if stream_cfg.subscribe is None or stream_cfg.unsubscribe is None:
            raise ValueError("p1.toml has no [stream] section")

        self._client = client
        self._endpoints = cfg.endpoints
        self._startup = stream_cfg.startup_requests
        self._instruments = stream_cfg.instruments
        self._stop = stop
        self._ops = {
            "subscribe": (STREAM_SUBSCRIBE_OP, stream_cfg.subscribe, json.loads(stream_cfg.subscribe_body)),
            "unsubscribe": (STREAM_UNSUBSCRIBE_OP, stream_cfg.unsubscribe, json.loads(stream_cfg.unsubscribe_body)),
        }

        self._keys = tuple(keys)
        self._leader = self._keys[0]
        self._cond = threading.Condition()
        self._connected: set[str] = set()
        self._initial_started = False

        self._send_lock = threading.Lock()

        # Used as an ordered set, so every send goes out in configuration order
        self._active: dict[Subscription, None] = {
            Subscription.of(instrument, quote_type): None
            for instrument in stream_cfg.instruments
            for quote_type in instrument.quote_types
        }

        # Kept once fetched successfully; a failed lookup is retried, but only
        # by the next sequence, not by each startup request in this one.
        self._accounts: list[Mapping[str, object]] | None = None
        self._account_lookup_failed = False

    def on_connected(self, key: str) -> None:
        """Note that recorder key got a 200 response, resending everything if it is the leader.

        A leader connection counts as a reconnect only once the initial send
        has begun; before that, the initial send covers it. A resend that
        finds the initial send still running waits for it to finish.
        """
        with self._cond:
            self._connected.add(key)
            self._cond.notify_all()
            resend = key == self._leader and self._initial_started
        if not resend:
            return
        with self._send_lock:
            if self._stop.is_set():
                return
            logger.info(
                "leader stream %s connected again; resending %d startup request(s) and %d subscription(s)",
                key,
                len(self._startup),
                len(self._active),
            )
            self._send_all()

    def wake(self) -> None:
        """Wake wait_for_first_connections so it sees that stop is set."""
        with self._cond:
            self._cond.notify_all()

    def wait_for_first_connections(self, timeout_s: float) -> list[str] | None:
        """Wait until every recorder has connected once or timeout_s passes.

        Return the recorders that had not connected yet, or None if stop was
        set while waiting.
        """
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while not self._stop.is_set() and len(self._connected) < len(self._keys):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            if self._stop.is_set():
                return None
            return [key for key in self._keys if key not in self._connected]

    def send_initial(self) -> None:
        """Send every startup request, then every subscription, for all clients at once."""
        with self._send_lock:
            with self._cond:
                self._initial_started = True
            if self._stop.is_set():
                return
            logger.info(
                "sending %d startup request(s) and %d subscription(s) for all clients",
                len(self._startup),
                len(self._active),
            )
            self._send_all()

    def apply(self, action: StreamAction) -> None:
        """Update the active subscriptions for action, then send its request for each matching instrument."""
        subscriptions = [
            Subscription.of(instrument, action.quote_type)
            for instrument in self._instruments
            if instrument.code == action.code
        ]
        with self._send_lock:
            for subscription in subscriptions:
                if action.op == "subscribe":
                    self._active[subscription] = None
                else:
                    self._active.pop(subscription, None)
            if self._stop.is_set():
                return
            logger.info(
                "scheduled %s of %s %s due %s", action.op, action.code, action.quote_type, action.at.isoformat()
            )
            for subscription in subscriptions:
                if self._stop.is_set():
                    return
                self._send_subscription(action.op, subscription)

    def _send_all(self) -> None:
        """Send the startup requests, then subscribe to every active subscription; hold _send_lock."""
        self._account_lookup_failed = False
        for op, body in self._startup:
            if self._stop.is_set():
                return
            self._send_startup(op, body)
        for subscription in list(self._active):
            if self._stop.is_set():
                return
            self._send_subscription("subscribe", subscription)

    def _send_startup(self, op: str, body: str) -> None:
        account = ACCOUNT_BODY_RE.fullmatch(body)
        if account is not None:
            what = f"{op} for account type {account.group(1)}"
            json_body: object = self._account_request(account.group(1), what)
            if json_body is None:
                return
        else:
            what = op
            json_body = json.loads(body)

        response = self._request(op, self._endpoints[op], json_body, what)
        if response is not None and not _is_success(response.status):
            logger.error("%s failed: HTTP %d", what, response.status)

    def _account_request(self, account_type: str, what: str) -> dict[str, str] | None:
        """Return the AccountRequest for the account of account_type, or None after logging why not."""
        accounts = self._fetch_accounts()
        if accounts is None:
            logger.error("%s skipped: the account list is unavailable", what)
            return None
        for account in accounts:
            if account.get("account_type") != account_type:
                continue
            request = {name: account.get(name) for name in _ACCOUNT_REQUEST_FIELDS}
            missing = [name for name, value in request.items() if not isinstance(value, str)]
            if missing:
                logger.error("%s skipped: the account has no %s", what, ", ".join(missing))
                return None
            return {name: str(value) for name, value in request.items()}
        logger.error("%s skipped: %s lists no such account", what, ACCOUNTS_OP)
        return None

    def _fetch_accounts(self) -> list[Mapping[str, object]] | None:
        if self._accounts is None and not self._account_lookup_failed:
            self._accounts = self._query_accounts()
            self._account_lookup_failed = self._accounts is None
        return self._accounts

    def _query_accounts(self) -> list[Mapping[str, object]] | None:
        response = self._request(ACCOUNTS_OP, self._endpoints[ACCOUNTS_OP], None, ACCOUNTS_OP)
        if response is None:
            return None
        if not _is_success(response.status):
            logger.error("%s failed: HTTP %d", ACCOUNTS_OP, response.status)
            return None
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, list) or not all(isinstance(account, dict) for account in body):
            logger.error("%s response is not a JSON array of accounts", ACCOUNTS_OP)
            return None
        return body

    def _send_subscription(self, verb: str, subscription: Subscription) -> None:
        op, endpoint, template = self._ops[verb]
        what = f"{verb} {subscription.describe()}"
        response = self._request(op, endpoint, render_template(template, subscription.fields()), what)
        if response is None:
            return
        if not _is_success(response.status):
            logger.error("%s failed: HTTP %d", what, response.status)
            return
        try:
            body = response.json()
        except ValueError:
            body = None

        # SubscriptionResponse (openapi.json) reports the outcome in its required success field
        if not isinstance(body, dict) or body.get("success") is not True:
            message = body.get("message") if isinstance(body, dict) else None
            logger.error("%s refused: success is not true (message: %r)", what, message)

    def _request(self, op: str, endpoint: Endpoint, json_body: object, what: str) -> CapturedResponse | None:
        """Send one request, returning None after logging an ERROR if it could not complete."""
        try:
            return self._client.request(op, endpoint, json_body=json_body)
        except httpx.HTTPError as exc:
            logger.error("%s failed: %s", what, describe_exception(exc))
            return None


def _pending_actions(actions: Sequence[StreamAction], now: datetime) -> list[StreamAction]:
    """Return actions in time order, leaving out, with a WARNING each, those already past at now."""
    pending: list[StreamAction] = []
    for action in sorted(actions, key=lambda item: item.at):
        if action.at < now:
            logger.warning(
                "skipping scheduled %s of %s %s due %s: that time had passed at startup",
                action.op,
                action.code,
                action.quote_type,
                action.at.isoformat(),
            )
        else:
            pending.append(action)
    return pending


class _Session:
    """The recorders, their coordinator and the control thread of one stream run.

    The control thread waits for the first connections, sends the initial
    subscriptions, then runs the scheduled actions. shutdown sets stop and
    joins every thread within one shared bound.
    """

    def __init__(
        self,
        cfg: P1Config,
        client: CapturingClient,
        clients: Sequence[str],
        run_dir: Path,
        stop: threading.Event,
        recorder_factory: RecorderFactory = sse.SseRecorder,
    ) -> None:
        stream_cfg = cfg.stream
        self._stop = stop
        self._retry_s = stream_cfg.retry_s
        self._actions = stream_cfg.actions
        self._pending: list[StreamAction] = []

        keys = [f"{client_name}/{stream_name}" for client_name in clients for stream_name in stream_cfg.streams]
        self._coordinator = _Coordinator(cfg, client, keys, stop)

        self._recorders: dict[str, threading.Thread] = {}
        for client_name in clients:
            for stream_name, endpoint in stream_cfg.streams.items():
                key = f"{client_name}/{stream_name}"
                self._recorders[key] = recorder_factory(
                    cfg.base_url,
                    client_name,
                    stream_name,
                    endpoint,
                    run_dir / f"sse_{client_name}_{stream_name}.jsonl",
                    stream_cfg.retry_s,
                    stop,
                    functools.partial(self._coordinator.on_connected, key),
                )
        self._control = threading.Thread(target=self._run_control, name="stream-control", daemon=True)
        self._started: list[tuple[str, threading.Thread]] = []

    def start(self, now: datetime) -> None:
        """Start every recorder and the control thread, dropping actions already past at now."""
        self._pending = _pending_actions(self._actions, now)
        for key, recorder in self._recorders.items():
            recorder.start()
            self._started.append((key, recorder))
        self._control.start()
        self._started.append(("control", self._control))

    def shutdown(self) -> None:
        """Set stop and join every started thread, logging a WARNING that names any still running.

        A recorder can take up to sse.SSE_READ_TIMEOUT_S to notice stop while
        it waits for response headers, counted from its connection attempt,
        which may begin just before stop is set; retry_s covers that. The
        control thread shares the bound, since its longest wait is one request
        of its own after one the leader is making.
        """
        self._stop.set()
        self._coordinator.wake()
        bound_s = sse.SSE_READ_TIMEOUT_S + self._retry_s
        deadline = time.monotonic() + bound_s
        for _, thread in self._started:
            thread.join(max(0.0, deadline - time.monotonic()))
        stuck = [key for key, thread in self._started if thread.is_alive()]
        if stuck:
            logger.warning(
                "%d thread(s) still running %.1fs after stop, left behind: %s", len(stuck), bound_s, ", ".join(stuck)
            )

    def _run_control(self) -> None:
        try:
            missing = self._coordinator.wait_for_first_connections(sse.SSE_READ_TIMEOUT_S)
            if missing is None:
                return
            if missing:
                logger.warning(
                    "sending subscriptions without waiting further for a first connection from: %s",
                    ", ".join(missing),
                )
            self._coordinator.send_initial()
            for action in self._pending:
                delay_s = (action.at - datetime.now(timezone.utc)).total_seconds()
                if self._stop.wait(max(delay_s, 0.0)):
                    return
                self._coordinator.apply(action)
        except Exception:
            logger.exception("stream control failed; the rest of its initial send and scheduled actions will not run")


@contextlib.contextmanager
def _stop_signals_blocked() -> Iterator[None]:
    """Block SIGINT and SIGTERM in this thread and every thread it starts, restoring the mask on exit.

    Blocked, the signals are taken only by _wait_for_stop's sigtimedwait, so
    no handler runs at an arbitrary point in this thread, where setting a
    threading.Event could deadlock on a lock the thread itself holds. A
    signal still pending on exit, such as a second Ctrl-C during shutdown,
    is discarded rather than delivered once the mask is lifted, where it
    would turn a clean exit into a KeyboardInterrupt.
    """
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
    try:
        yield
    finally:
        while signal.sigtimedwait(_STOP_SIGNALS, 0) is not None:
            pass
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _wait_for_stop(until: datetime) -> str:
    """Block until until passes or SIGINT or SIGTERM arrives, and return which it was."""
    while True:
        remaining_s = (until - datetime.now(timezone.utc)).total_seconds()
        if remaining_s <= 0:
            return f"reached --until {until.isoformat()}"
        received = signal.sigtimedwait(_STOP_SIGNALS, remaining_s)
        if received is not None:
            return f"received {signal.Signals(received.si_signo).name}"


def run_stream(cfg: P1Config, clients: list[str], until: datetime, run_dir: Path) -> int:
    """Record every configured stream for each client until until or SIGINT/SIGTERM, then return 0.

    Runs the interlock check first and lets its InterlockError propagate.
    Returns 1 without connecting if p1.toml has no [stream] section or
    until is not in the future. Must be called from the main thread before
    any other thread is started, so that every thread inherits the blocked
    stop signals.
    """
    check_client_names(clients)
    if until.utcoffset() is None:
        raise ValueError("until must be timezone-aware")
    if not cfg.stream.streams:
        logger.error("p1.toml has no [stream] section; nothing to record")
        return 1
    now = datetime.now(timezone.utc)
    if until <= now:
        logger.error("--until %s is not in the future", until.isoformat())
        return 1

    with CapturingClient(cfg.base_url, _REST_CLIENT_NAME, run_dir / "rest.jsonl", cfg.http_timeout_s) as client:
        assert_simulation(client, cfg)
        stop = threading.Event()
        with _stop_signals_blocked():
            session = _Session(cfg, client, clients, run_dir, stop)
            logger.info(
                "recording streams %s for clients %s until %s; leader stream %s/%s",
                ", ".join(cfg.stream.streams),
                ", ".join(clients),
                until.isoformat(),
                clients[0],
                next(iter(cfg.stream.streams)),
            )
            try:
                session.start(now)
                logger.info("stopping: %s", _wait_for_stop(until))
            finally:
                session.shutdown()
    return 0
