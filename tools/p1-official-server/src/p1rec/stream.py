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
and start again.

After that everything is sent again whenever the leader recorder, the
first client's first stream, connects again, since that is the sign the
server may have restarted and lost the subscriptions, and once a day after
the server's maintenance window, which may drop them without closing any
stream. A request that fails is retried every request_retry_s and on any
recorder's next connection until it succeeds. Scheduled actions change the
active subscriptions at their set time. The run ends at --until or on
SIGINT or SIGTERM, or stops itself with 2 if the control thread fails.
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
from datetime import datetime, timedelta, timezone
from datetime import time as wall_time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from p1rec import sse
from p1rec.broker import BrokerError, account_request, fetch_accounts
from p1rec.capture import CapturedResponse, CapturingClient, describe_exception
from p1rec.config import (
    ACCOUNTS_OP,
    PLACEHOLDER_RE,
    STREAM_SUBSCRIBE_OP,
    STREAM_UNSUBSCRIBE_OP,
    Endpoint,
    Instrument,
    P1Config,
    StartupRequest,
    StreamAction,
)
from p1rec.interlock import assert_simulation
from p1rec.records import EXCHANGE_TZ_NAME, EXIT_CHECK_FAILED

logger = logging.getLogger(__name__)

# Stream names may contain "_", so a client name may not, or the client and
# stream in a file name sse_<client>_<stream>.jsonl could not be told apart.
_CLIENT_NAME_RE = re.compile(r"[A-Za-z0-9-]+")

_REST_CLIENT_NAME = "stream"

# The server's daily maintenance runs on Taipei time (docs/official-server-contract.md section 2)
_TAIPEI = ZoneInfo(EXCHANGE_TZ_NAME)

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

# The wall clock the control thread schedules by, which tests replace to reach a set time of day
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _failure_reason(response: CapturedResponse, field: str | None) -> str | None:
    """Return why response does not confirm its request, or None if it is a 2xx whose field, if named, is true."""
    if not _is_success(response.status):
        return f"HTTP {response.status}"
    if field is None:
        return None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get(field) is True:
        return None

    # The message may quote the account, so only its presence is logged
    detail = " (the response has a message; see rest.jsonl)" if isinstance(body, dict) and "message" in body else ""
    return f"{field} is not true{detail}"


def _account_request(accounts: Sequence[Mapping[str, object]] | str, account_type: str) -> dict[str, object] | str:
    """Return the AccountRequest for the account of account_type, or why there is none.

    accounts is the accounts response, or the reason it could not be had.
    """
    if isinstance(accounts, str):
        return f"the account list is unavailable ({accounts})"
    for account in accounts:
        if account.get("account_type") != account_type:
            continue
        request = account_request(account)
        missing = [name for name, value in request.items() if not isinstance(value, str)]
        if missing:
            return f"the account has no {', '.join(missing)}"
        return request
    return f"{ACCOUNTS_OP} lists no such account"


def _next_daily(now: datetime, at: wall_time) -> datetime:
    """Return the first moment after now at which the Taipei wall clock reads at.

    Taipei keeps no daylight saving time, so every day has the same length.
    """
    local_now = now.astimezone(_TAIPEI)
    candidate = datetime.combine(local_now.date(), at, tzinfo=_TAIPEI)
    return candidate if candidate > local_now else candidate + timedelta(days=1)


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
    other methods, so the active subscriptions and the failed requests are
    touched by that thread alone. No request is sent once stop is set.

    A request that fails is kept, with how many times in a row it has
    failed, until it succeeds: its first failure is logged as an ERROR, the
    ones after as DEBUG, and its success afterwards as INFO.
    """

    def __init__(
        self, cfg: P1Config, client: CapturingClient, keys: Sequence[str], stop: threading.Event, clock: Clock
    ) -> None:
        stream_cfg = cfg.stream
        if stream_cfg.subscribe is None or stream_cfg.unsubscribe is None:
            raise ValueError("p1.toml has no [stream] section")

        self._cfg = cfg
        self._client = client
        self._endpoints = cfg.endpoints
        self._startup = stream_cfg.startup_requests
        self._instruments = stream_cfg.instruments
        self._stop = stop
        self._clock = clock
        self._ops = {
            "subscribe": (STREAM_SUBSCRIBE_OP, stream_cfg.subscribe, json.loads(stream_cfg.subscribe_body)),
            "unsubscribe": (STREAM_UNSUBSCRIBE_OP, stream_cfg.unsubscribe, json.loads(stream_cfg.unsubscribe_body)),
        }

        self._keys = tuple(keys)
        self.leader = self._keys[0]
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
        self._failures: dict[StartupRequest | Subscription, int] = {}

        # Kept once fetched successfully; a failed lookup is retried by the next round
        self._accounts: list[dict[str, object]] | None = None

    def on_connected(self, key: str) -> None:
        """Record that recorder key got a 200 response and wake the control thread.

        A leader connection asks for a resend only once the initial send has
        begun; before that, the initial send covers it.
        """
        with self._cond:
            self._connected.add(key)
            self._reported += 1
            if key == self.leader and self._initial_started:
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

    def wait_for_work(self, wake_at: datetime | None) -> _Work | None:
        """Wait for a connection not yet acted on, or until wake_at; return None once stop is set."""
        with self._cond:
            while not self._stop.is_set() and self._reported == self._handled:
                if wake_at is None:
                    self._cond.wait()
                    continue
                delay_s = (wake_at - self._clock()).total_seconds()
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

    def has_failures(self) -> bool:
        return bool(self._failures)

    def send_initial(self) -> list[str]:
        """Send every startup request and then, if all succeeded, every subscription.

        The send acts on every connection reported so far. Return a
        description of each startup request that failed, in which case no
        subscription is sent; the list is empty also when stop was set
        before the send finished.
        """
        with self._cond:
            self._initial_started = True
            self._handled = self._reported
            self._cond.notify_all()
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
            return [request.describe() for request in failed]
        self._send_subscriptions()
        return []

    def resend(self, reason: str) -> None:
        """Send every startup request and active subscription again, and every failed unsubscription."""
        logger.info(
            "%s; resending %d startup request(s) and %d subscription(s)",
            reason,
            len(self._startup),
            len(self._active),
        )
        self._send_startup(self._startup)
        self._send_subscriptions()

        # The active set no longer holds a subscription whose unsubscription failed
        for key in list(self._failures):
            if isinstance(key, Subscription) and key not in self._active:
                self._send_subscription("unsubscribe", key)

    def retry_failed(self) -> bool:
        """Send again every request that has failed since it last succeeded; return whether there was any."""
        if not self._failures:
            return False
        logger.debug("retrying %d failed request(s)", len(self._failures))
        self._send_startup([request for request in self._startup if request in self._failures])
        for key in list(self._failures):
            if isinstance(key, Subscription):
                self._send_subscription("subscribe" if key in self._active else "unsubscribe", key)
        return True

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
            self._send_subscription(action.op, subscription)

    def _send_startup(self, requests: Sequence[StartupRequest]) -> list[StartupRequest]:
        """Send requests in order and return those that failed, looking the accounts up at most once."""
        accounts: list[dict[str, object]] | str | None = self._accounts
        failed: list[StartupRequest] = []
        for request in requests:
            if self._stop.is_set():
                break
            what = request.describe()
            outcome: bool | None = None
            if request.body is not None:
                outcome = self._send_startup_request(request, what, json.loads(request.body))
            elif request.account_type is not None:
                if accounts is None:
                    accounts = self._query_accounts()
                    if accounts is None:
                        break
                    if not isinstance(accounts, str):
                        self._accounts = accounts
                body = _account_request(accounts, request.account_type)
                if isinstance(body, str):
                    outcome = self._record(request, what, body)
                else:
                    outcome = self._send_startup_request(request, what, body)
            if outcome is False:
                failed.append(request)
        return failed

    def _send_startup_request(self, request: StartupRequest, what: str, json_body: object) -> bool | None:
        """Send one startup request; return whether it succeeded, or None if stop kept it from being sent."""
        outcome = self._request(request.op, self._endpoints[request.op], json_body)
        if outcome is None:
            return None
        confirmation = _STARTUP_CONFIRMATIONS.get(request.op)
        reason = outcome if isinstance(outcome, str) else _failure_reason(outcome, confirmation)
        return self._record(request, what, reason)

    def _query_accounts(self) -> list[dict[str, object]] | str | None:
        """Return the accounts response, why it could not be had, or None if stop kept it from being sent.

        The query goes through fetch_accounts, which adds every account
        identifier in the response to log redaction before this thread can
        log anything that quotes one.
        """
        if self._stop.is_set():
            return None
        try:
            return fetch_accounts(self._client, self._cfg)
        except httpx.HTTPError as exc:
            return f"{ACCOUNTS_OP}: {describe_exception(exc)}"
        except BrokerError as exc:
            return str(exc)

    def _send_subscriptions(self) -> None:
        for subscription in list(self._active):
            self._send_subscription("subscribe", subscription)

    def _send_subscription(self, verb: str, subscription: Subscription) -> bool | None:
        """Send one subscription request; return whether it succeeded, or None if stop kept it from being sent."""
        op, endpoint, template = self._ops[verb]
        outcome = self._request(op, endpoint, render_template(template, subscription.fields()))
        if outcome is None:
            return None
        reason = outcome if isinstance(outcome, str) else _failure_reason(outcome, _SUBSCRIPTION_CONFIRMATION)
        return self._record(subscription, f"{verb} {subscription.describe()}", reason)

    def _record(self, key: StartupRequest | Subscription, what: str, reason: str | None) -> bool:
        """Log and keep the outcome of key's request, which failed with reason unless it is None.

        Return whether the request succeeded.
        """
        failures = self._failures.get(key, 0)
        if reason is None:
            if failures:
                del self._failures[key]
                logger.info("%s succeeded after %d failed attempt(s)", what, failures)
            return True
        self._failures[key] = failures + 1
        if failures == 0:
            logger.error("%s failed: %s", what, reason)
        else:
            logger.debug("%s failed again, %d attempts in a row: %s", what, failures + 1, reason)
        return False

    def _request(self, op: str, endpoint: Endpoint, json_body: object) -> CapturedResponse | str | None:
        """Send one request and return its response or why it could not complete.

        Return None without sending once stop is set, so a control thread
        left running past shutdown never uses the closed client.
        """
        if self._stop.is_set():
            return None
        try:
            return self._client.request(op, endpoint, json_body=json_body)
        except httpx.HTTPError as exc:
            return describe_exception(exc)


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
    subscriptions, then acts on new connections, retries, the daily resend
    and scheduled actions until stop is set. If the first send or the
    control thread itself fails, it sets failed and stop and calls
    on_self_stop, unless shutdown has already begun. shutdown sets stop and
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
        on_self_stop: Callable[[], None] = lambda: None,
        clock: Clock = _utc_now,
    ) -> None:
        stream_cfg = cfg.stream
        self.failed = False
        self._stop = stop
        self._on_self_stop = on_self_stop
        self._clock = clock
        self._stop_lock = threading.Lock()
        self._shutting_down = False
        self._retry_s = stream_cfg.retry_s
        self._request_retry = timedelta(seconds=stream_cfg.request_retry_s)
        self._daily_resend_at = stream_cfg.daily_resend_at
        self._actions = stream_cfg.actions
        self._pending: list[StreamAction] = []

        keys = [f"{client_name}/{stream_name}" for client_name in clients for stream_name in stream_cfg.streams]
        self._coordinator = _Coordinator(cfg, client, keys, stop, clock)

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
        control thread shares the bound: once stop is set it sends nothing
        more, and the request it may be waiting on is bounded by
        http_timeout_s, so one outlasting the bound is named like any
        recorder.
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

    def _fail(self) -> None:
        """Mark the run failed, stop it and wake run_stream, unless shutdown has already begun.

        Checked under the lock shutdown takes, so a control thread still
        running after shutdown, failing on the closed client, cannot turn a
        normal stop into a failed run.
        """
        with self._stop_lock:
            if self._shutting_down:
                return
            self.failed = True
            self._stop.set()
            self._on_self_stop()

    def _is_shutting_down(self) -> bool:
        with self._stop_lock:
            return self._shutting_down

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
                self._fail()
                return
            self._serve(collections.deque(self._pending))
        except Exception:
            if self._is_shutting_down():
                # Expected once shutdown has closed the client under a request still in flight
                logger.debug("stream control ended with an error during shutdown", exc_info=True)
                return

            # Recording on without resends or retries would lose subscriptions unnoticed for days
            logger.exception("stream control failed; stopping the run")
            self._fail()

    def _serve(self, actions: collections.deque[StreamAction]) -> None:
        """Act on new connections, retries, the daily resend and due actions until stop is set."""
        daily_at = _next_daily(self._clock(), self._daily_resend_at) if self._daily_resend_at else None

        # Subscriptions that failed on the first send wait one interval, like any later failure
        retry_at = self._clock() + self._request_retry if self._coordinator.has_failures() else None
        while True:
            deadlines = [at for at in (actions[0].at if actions else None, retry_at, daily_at) if at is not None]
            work = self._coordinator.wait_for_work(min(deadlines, default=None))
            if work is None:
                return
            now = self._clock()
            daily_due = daily_at is not None and daily_at <= now
            retried = True
            if work.leader_connected:
                self._coordinator.resend(f"leader stream {self._coordinator.leader} connected again")
            elif daily_due and self._daily_resend_at is not None:
                self._coordinator.resend(
                    f"daily resend at {self._daily_resend_at:%H:%M} {EXCHANGE_TZ_NAME}, after the maintenance window"
                )
            elif work.new_connection or (retry_at is not None and retry_at <= now):
                retried = self._coordinator.retry_failed()
            else:
                retried = False
            if daily_due and self._daily_resend_at is not None:
                daily_at = _next_daily(now, self._daily_resend_at)
            while actions and actions[0].at <= self._clock() and not self._stop.is_set():
                self._coordinator.apply(actions.popleft())

            if not self._coordinator.has_failures():
                retry_at = None
            elif retried or retry_at is None:
                retry_at = self._clock() + self._request_retry
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
    itself because a startup request failed on the first send or the
    control thread failed. Returns 1 without connecting if p1.toml has no
    [stream] section or until is not in the future. Runs the interlock
    check first and lets its InterlockError propagate, then raises
    RuntimeError if any other thread is running, since every thread must
    inherit the blocked stop signals.
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
    return EXIT_CHECK_FAILED if session.failed else 0
