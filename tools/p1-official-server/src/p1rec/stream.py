"""Record several SSE clients at once while holding the server's subscriptions.

run_stream opens one SseRecorder per client and configured stream, each
writing sse_<client>_<stream>.jsonl in the run directory. Every REST
request is sent by one control thread through one CapturingClient into
rest.jsonl; recorders only report their connections to it, so a slow
request never holds up their reading.

Subscriptions on the official server are global, not per connection
(docs/official-server-contract.md section 1), so they are sent once for all
clients: when every recorder has connected, or after sse.SSE_READ_TIMEOUT_S,
whichever comes first, the startup requests go out, then one subscription
per instrument and quote type. If a startup request fails on that first
send, the run stops and returns 2, so that the operator can fix the cause
and start again. After that, each new connection of the leader recorder,
the first client's first stream, sends everything again, since it is the
sign that the server may have restarted and lost the subscriptions, and a
new connection of any recorder retries the startup requests that failed
last time. Scheduled actions change the active subscriptions at their set
time. The run ends at --until or on SIGINT or SIGTERM.
"""

from __future__ import annotations

import collections
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

# The exit code shared by every subcommand for a check that did not pass
EXIT_CHECK_FAILED = 2

# Stream names may contain "_", so a client name may not, or the client and
# stream in a file name sse_<client>_<stream>.jsonl could not be told apart.
_CLIENT_NAME_RE = re.compile(r"[A-Za-z0-9-]+")

_REST_CLIENT_NAME = "stream"

# AccountRequest fields (openapi.json), which each Account from accounts also carries
_ACCOUNT_REQUEST_FIELDS = ("account_type", "broker_id", "account_id", "person_id")

# Response fields that confirm an operation took effect, each required by its
# response schema in openapi.json (SubscribeTradeOut, SubscriptionResponse).
_STARTUP_CONFIRMATIONS = {"subscribe_trade": "subscribe_trade"}
_SUBSCRIPTION_CONFIRMATION = "success"

_STOP_SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM})

# Sent by the control thread to the thread waiting in run_stream when the run stops itself
_WAKE_SIGNAL = signal.SIGUSR1
_BLOCKED_SIGNALS = _STOP_SIGNALS | {_WAKE_SIGNAL}

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


@dataclass(frozen=True)
class _Work:
    """What woke the control thread: connections to act on, and whether the leader's is among them."""

    reported: int  # Connections reported so far, to pass back to mark_handled
    new_connection: bool
    leader_connected: bool


class _Coordinator:
    """Track recorder connections and send the startup requests and subscriptions.

    on_connected runs on recorder threads and only records the connection
    under _cond. Every request is sent by the control thread through the
    other methods, so the active subscriptions and the failed startup
    requests are touched by that thread alone. Each sequence of requests
    stops before its next request once stop is set.
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
        self._leader_pending = False
        self._reported = 0
        self._handled = 0

        # Used as an ordered set, so every send goes out in configuration order
        self._active: dict[Subscription, None] = {
            Subscription.of(instrument, quote_type): None
            for instrument in stream_cfg.instruments
            for quote_type in instrument.quote_types
        }
        self._failed_startup: list[tuple[str, str]] = []

        # Kept once fetched successfully; a failed lookup is retried, but only
        # by the next sequence, not by each startup request in this one.
        self._accounts: list[Mapping[str, object]] | None = None
        self._account_lookup_failed = False

    def on_connected(self, key: str) -> None:
        """Record that recorder key got a 200 response and wake the control thread.

        A leader connection asks for a resend only once the initial send has
        begun; before that, the initial send covers it.
        """
        with self._cond:
            self._connected.add(key)
            self._reported += 1
            if key == self._leader and self._initial_started:
                self._leader_pending = True
            self._cond.notify_all()

    def wake(self) -> None:
        """Wake the control thread so that it sees that stop is set."""
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
                remaining_s = deadline - time.monotonic()
                if remaining_s <= 0:
                    break
                self._cond.wait(remaining_s)
            if self._stop.is_set():
                return None
            return [key for key in self._keys if key not in self._connected]

    def wait_for_work(self, next_action_at: datetime | None) -> _Work | None:
        """Wait for a connection not yet acted on, or until next_action_at; return None once stop is set."""
        with self._cond:
            while not self._stop.is_set() and self._reported == self._handled:
                if next_action_at is None:
                    self._cond.wait()
                    continue
                delay_s = (next_action_at - datetime.now(timezone.utc)).total_seconds()
                if delay_s <= 0:
                    break
                self._cond.wait(delay_s)
            if self._stop.is_set():
                return None
            leader_connected, self._leader_pending = self._leader_pending, False
            return _Work(self._reported, self._reported > self._handled, leader_connected)

    def mark_handled(self, reported: int) -> None:
        """Record that the control thread has acted on the first reported connections."""
        with self._cond:
            self._handled = reported
            self._cond.notify_all()

    def wait_until_handled(self, timeout_s: float) -> bool:
        """Block until the control thread has acted on every connection reported so far.

        Return False if timeout_s passed first. Tests use it to see that a
        connection caused no request.
        """
        with self._cond:
            target = self._reported
            return self._cond.wait_for(lambda: self._handled >= target or self._stop.is_set(), timeout_s)

    def send_initial(self) -> list[str]:
        """Send every startup request and then, if all succeeded, every subscription.

        Return a description of each startup request that failed or was
        skipped, in which case no subscription is sent. The list is empty
        also when stop was set before the send finished.
        """
        with self._cond:
            self._initial_started = True
        if self._stop.is_set():
            return []
        logger.info(
            "sending %d startup request(s) and %d subscription(s) for all clients",
            len(self._startup),
            len(self._active),
        )
        failed = self._send_startup(self._startup)
        if self._stop.is_set():
            return []
        if failed:
            return [self._describe_startup(op, body) for op, body in failed]
        self._send_subscriptions()
        return []

    def resend(self) -> None:
        """Send every startup request and active subscription again, keeping the startup failures for a retry."""
        logger.info(
            "leader stream %s connected again; resending %d startup request(s) and %d subscription(s)",
            self._leader,
            len(self._startup),
            len(self._active),
        )
        self._failed_startup = self._send_startup(self._startup)
        self._send_subscriptions()

    def retry_failed_startup(self) -> None:
        """Send again the startup requests that failed last time, if there are any."""
        if not self._failed_startup:
            return
        logger.info("retrying %d failed startup request(s) after a new connection", len(self._failed_startup))
        self._failed_startup = self._send_startup(self._failed_startup)

    def apply(self, action: StreamAction) -> None:
        """Update the active subscriptions for action, then send its request for each matching instrument."""
        subscriptions = [
            Subscription.of(instrument, action.quote_type)
            for instrument in self._instruments
            if instrument.code == action.code
        ]
        for subscription in subscriptions:
            if action.op == "subscribe":
                self._active[subscription] = None
            else:
                self._active.pop(subscription, None)
        logger.info("scheduled %s of %s %s due %s", action.op, action.code, action.quote_type, action.at.isoformat())
        for subscription in subscriptions:
            if self._stop.is_set():
                return
            self._send_subscription(action.op, subscription)

    def _send_startup(self, requests: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
        """Send requests in order and return those that failed or were skipped."""
        self._account_lookup_failed = False
        failed: list[tuple[str, str]] = []
        for op, body in requests:
            if self._stop.is_set():
                break
            if not self._send_startup_request(op, body):
                failed.append((op, body))
        return failed

    def _send_startup_request(self, op: str, body: str) -> bool:
        what = self._describe_startup(op, body)
        account = ACCOUNT_BODY_RE.fullmatch(body)
        if account is not None:
            json_body: object = self._account_request(account.group(1), what)
            if json_body is None:
                return False
        else:
            json_body = json.loads(body)

        response = self._request(op, self._endpoints[op], json_body, what)
        return response is not None and self._confirmed(response, _STARTUP_CONFIRMATIONS.get(op), what)

    @staticmethod
    def _describe_startup(op: str, body: str) -> str:
        account = ACCOUNT_BODY_RE.fullmatch(body)
        return f"{op} for account type {account.group(1)}" if account else op

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

    def _send_subscriptions(self) -> None:
        for subscription in list(self._active):
            if self._stop.is_set():
                return
            self._send_subscription("subscribe", subscription)

    def _send_subscription(self, verb: str, subscription: Subscription) -> None:
        op, endpoint, template = self._ops[verb]
        what = f"{verb} {subscription.describe()}"
        response = self._request(op, endpoint, render_template(template, subscription.fields()), what)
        if response is not None:
            self._confirmed(response, _SUBSCRIPTION_CONFIRMATION, what)

    @staticmethod
    def _confirmed(response: CapturedResponse, field: str | None, what: str) -> bool:
        """Return whether response is a 2xx whose field, if one is named, is JSON true, logging an ERROR if not."""
        if not _is_success(response.status):
            logger.error("%s failed: HTTP %d", what, response.status)
            return False
        if field is None:
            return True
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get(field) is True:
            return True
        detail = f" (message: {body['message']!r})" if isinstance(body, dict) and "message" in body else ""
        logger.error("%s refused: %s is not true%s", what, field, detail)
        return False

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
    subscriptions, then acts on new connections and scheduled actions until
    stop is set. If the first send fails, it sets startup_failed and stop
    and calls on_self_stop, unless shutdown has already begun. shutdown sets
    stop and joins every thread within one shared bound.
    """

    def __init__(
        self,
        cfg: P1Config,
        client: CapturingClient,
        clients: Sequence[str],
        run_dir: Path,
        stop: threading.Event,
        recorder_factory: RecorderFactory = sse.SseRecorder,
        on_self_stop: Callable[[], None] = lambda: None,
    ) -> None:
        stream_cfg = cfg.stream
        self.startup_failed = False
        self._stop = stop
        self._on_self_stop = on_self_stop
        self._stop_lock = threading.Lock()
        self._shutting_down = False
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

    def wait_until_handled(self, timeout_s: float) -> bool:
        """Block until the control thread has acted on every connection reported so far, as tests need."""
        return self._coordinator.wait_until_handled(timeout_s)

    def shutdown(self) -> None:
        """Set stop and join every started thread, logging a WARNING that names any still running.

        A recorder can take up to sse.SSE_READ_TIMEOUT_S to notice stop while
        it waits for response headers, counted from its connection attempt,
        which may begin just before stop is set; retry_s covers that. The
        control thread shares the bound, since at worst it finishes one
        request, bounded by the shorter http_timeout_s.
        """
        with self._stop_lock:
            self._shutting_down = True
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

    def _stop_self(self) -> None:
        with self._stop_lock:
            self._stop.set()
            if not self._shutting_down:
                self._on_self_stop()

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
            failed = self._coordinator.send_initial()
            if failed:
                logger.error(
                    "first send failed for %s; stopping so that the run can be restarted once the cause is fixed",
                    ", ".join(failed),
                )
                self.startup_failed = True
                self._stop_self()
                return
            self._serve(collections.deque(self._pending))
        except Exception:
            logger.exception("stream control failed; no further requests or scheduled actions will be sent")

    def _serve(self, actions: collections.deque[StreamAction]) -> None:
        """Act on new connections and due actions until stop is set."""
        while True:
            work = self._coordinator.wait_for_work(actions[0].at if actions else None)
            if work is None:
                return
            if work.leader_connected:
                self._coordinator.resend()
            elif work.new_connection:
                self._coordinator.retry_failed_startup()
            while actions and actions[0].at <= datetime.now(timezone.utc) and not self._stop.is_set():
                self._coordinator.apply(actions.popleft())
            self._coordinator.mark_handled(work.reported)


def _check_no_other_threads() -> None:
    """Raise RuntimeError if any thread besides the caller is running.

    A thread started before the stop signals are blocked keeps them
    unblocked, so the kernel could deliver SIGINT or SIGTERM to it, where
    sigtimedwait would never see it.
    """
    others = [thread.name for thread in threading.enumerate() if thread is not threading.current_thread()]
    if others:
        raise RuntimeError(f"run_stream must start before any other thread, but these are running: {', '.join(others)}")


@contextlib.contextmanager
def _stop_signals_blocked() -> Iterator[None]:
    """Block the stop and wake signals in this thread and every thread it starts, restoring the mask on exit.

    Blocked, the signals are taken only by _wait_for_stop's sigtimedwait, so
    no handler runs at an arbitrary point in this thread, where setting a
    threading.Event could deadlock on a lock the thread itself holds. A
    signal still pending on exit, such as a second Ctrl-C during shutdown,
    is discarded rather than delivered once the mask is lifted, where it
    would turn a clean exit into a KeyboardInterrupt.
    """
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, _BLOCKED_SIGNALS)
    try:
        yield
    finally:
        while signal.sigtimedwait(_BLOCKED_SIGNALS, 0) is not None:
            pass
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _wait_for_stop(deadline: float, until: datetime, stop: threading.Event) -> str:
    """Block until the time.monotonic() deadline, SIGINT or SIGTERM, or the run stopping itself; say which.

    The wake signal only makes the loop look at stop again, so one sent
    from outside the process changes nothing.
    """
    while not stop.is_set():
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            return f"reached --until {until.isoformat()}"
        received = signal.sigtimedwait(_BLOCKED_SIGNALS, remaining_s)
        if received is not None and received.si_signo != _WAKE_SIGNAL:
            return f"received {signal.Signals(received.si_signo).name}"
    return "the run stopped itself"


def run_stream(cfg: P1Config, clients: list[str], until: datetime, run_dir: Path) -> int:
    """Record every configured stream for each client until until or SIGINT/SIGTERM.

    Return 0 after a normal stop, or EXIT_CHECK_FAILED after the run stopped
    itself because a startup request failed on the first send. Returns 1
    without connecting if p1.toml has no [stream] section or until is not
    in the future. Runs the interlock check first and lets its
    InterlockError propagate, then raises RuntimeError if any other thread
    is running, since every thread must inherit the blocked stop signals.
    """
    check_client_names(clients)
    if until.utcoffset() is None:
        raise ValueError("until must be timezone-aware")
    if not cfg.stream.streams:
        logger.error("p1.toml has no [stream] section; nothing to record")
        return 1
    now = datetime.now(timezone.utc)
    remaining_s = (until - now).total_seconds()
    if remaining_s <= 0:
        logger.error("--until %s is not in the future", until.isoformat())
        return 1

    # Measured on the monotonic clock, so a wall-clock step cannot move the stop
    deadline = time.monotonic() + remaining_s

    with CapturingClient(cfg.base_url, _REST_CLIENT_NAME, run_dir / "rest.jsonl", cfg.http_timeout_s) as client:
        assert_simulation(client, cfg)
        _check_no_other_threads()
        stop = threading.Event()
        wake_runner = functools.partial(signal.pthread_kill, threading.get_ident(), _WAKE_SIGNAL)
        with _stop_signals_blocked():
            session = _Session(cfg, client, clients, run_dir, stop, on_self_stop=wake_runner)
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
                logger.info("stopping: %s", _wait_for_stop(deadline, until, stop))
            finally:
                session.shutdown()
    return EXIT_CHECK_FAILED if session.startup_failed else 0
