"""A stub of the official shioaji server 1.7.6 on a real loopback port, for the offline rehearsal.

StubServer serves a StubBroker over a ThreadingHTTPServer bound to a free
port on 127.0.0.1, so p1rec's own entry points, run as subprocesses, reach
it exactly as they reach the official server. StubBroker extends
mock_official.FakeServer, which already answers every preflight query,
with the rest of what the recorder sends: the mode switch, the order book
and trades, trade report and quote subscriptions, history queries and
usage, and an SseHub that pushes quotes, order and deal reports and
heartbeats, and can drop every stream for a while.

Field names follow openapi.json and the shapes recorded in
docs/official-server-contract.md section 2; every value is made up. Where
the recording is silent the stub follows the shapes derive_status in
p1rec.reconcile assumes: order reports go out on the combined stream as
well as on order_event, an accepted UpdateQty reports the reduced size in
order.quantity and a cumulative status.cancel_quantity, and an IOC or FOK
order that cannot fill ends with an accepted Cancel. Every order settles
before its place response is sent, which still reads PendingSubmit as the
official server's does, so the reports and trades are complete by the time
the caller moves on.

Order and deal times come from an exchange clock that starts at
exchange_clock_start, inside the trading day, so a rehearsal's orders fall
on one Taipei date in the day session whatever the wall-clock time; quote
times use the real clock, so receive latency stays meaningful.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import queue
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as time_of_day
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Self, TypeGuard, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from mock_official import FakeServer, stock_record

from p1rec.prices import SHARES_PER_BOARD_LOT

logger = logging.getLogger(__name__)

TAIPEI = ZoneInfo("Asia/Taipei")

INFO_PATH = "/api/v1/info"
USAGE_PATH = "/api/v1/auth/usage"
SUBSCRIBE_TRADE_PATH = "/api/v1/auth/subscribe_trade"
SUBSCRIBE_PATH = "/api/v1/stream/subscribe"
UNSUBSCRIBE_PATH = "/api/v1/stream/unsubscribe"
PLACE_PATH = "/api/v1/order/place_order"
UPDATE_PRICE_PATH = "/api/v1/order/update_price"
UPDATE_QTY_PATH = "/api/v1/order/update_qty"
CANCEL_PATH = "/api/v1/order/cancel_order"
TRADES_PATH = "/api/v1/order/trades"
TICKS_PATH = "/api/v1/data/ticks"
KBARS_PATH = "/api/v1/data/kbars"
SNAPSHOTS_PATH = "/api/v1/data/snapshots"
MUTATION_PATHS = frozenset({PLACE_PATH, UPDATE_PRICE_PATH, UPDATE_QTY_PATH, CANCEL_PATH})

# The combined stream carries every event; /<event name> beneath it carries that event alone
STREAM_PATH = "/api/v1/stream/data"
ORDER_EVENT = "order_event"
HEARTBEAT_EVENT = "heartbeat"

# The official server's heartbeat and quote flow, scaled down so a
# rehearsal sees several of each per second.
DEFAULT_HEARTBEAT_S = 0.2
DEFAULT_QUOTE_INTERVAL_S = 0.25

# How often serve_forever looks for a shutdown request, which bounds how
# long closing the stub takes.
_SHUTDOWN_POLL_S = 0.05

# How long close waits for each of the stub's threads, which all end within
# one quote interval or shutdown poll unless something is stuck.
_JOIN_TIMEOUT_S = 5.0

# Inside both the TAIFEX day session and the TWSE session, the trading
# windows `p1rec orders` enforces, with hours to spare either side.
SESSION_TIME = time_of_day(10, 0)

# Made-up identities; the rehearsal's secret environment names the same person
STUB_PERSON_ID = "Z900000001"
STUB_USERNAME = "測試帳戶甲"
STUB_STOCK_ACCOUNT: dict[str, object] = {
    "account_type": "S",
    "broker_id": "0000",
    "account_id": "0977001",
    "person_id": STUB_PERSON_ID,
    "username": STUB_USERNAME,
    "signed": True,
}
STUB_FUTURES_ACCOUNT: dict[str, object] = {
    **STUB_STOCK_ACCOUNT,
    "account_type": "F",
    "broker_id": "F000000",
    "account_id": "0977002",
}

# The op_code of an accepted operation, and of the exchange rejection
# recorded in the futures API test (docs/official-server-contract.md section 2).
_OP_CODE_OK = "00"
_OP_CODE_REJECTED = "88"

_WEB_ID = "137"
_SUBMITTED = "Submitted"

# One official server instance's session and its daily data quota
# (docs/official-server-contract.md section 2).
_CONNECTIONS = 1
_DAILY_LIMIT_BYTES = 2 * 1024**3

# Margin's required fields (openapi.json), all zero unless set below
_MARGIN_FIELDS = (
    "yesterday_balance",
    "today_balance",
    "deposit_withdrawal",
    "fee",
    "tax",
    "initial_margin",
    "maintenance_margin",
    "margin_call",
    "risk_indicator",
    "royalty_revenue_expenditure",
    "equity",
    "equity_amount",
    "option_openbuy_market_value",
    "option_opensell_market_value",
    "option_open_position",
    "option_settle_profitloss",
    "future_open_position",
    "today_future_open_position",
    "future_settle_profitloss",
    "available_margin",
    "plus_margin",
    "plus_margin_indicator",
    "security_collateral_amount",
    "order_margin_premium",
    "collateral_amount",
)

_QUOTE_EVENT_PREFIX = {"Tick": "tick", "BidAsk": "bidask", "Quote": "quote"}
_BOOK_DEPTH = 5
_HISTORY_ROWS = 3
_NS_PER_S = 1_000_000_000


def session_anchor() -> datetime:
    """Return SESSION_TIME on today's Taipei date, where a rehearsal's exchange clock and orders clock start."""
    return datetime.combine(datetime.now(TAIPEI).date(), SESSION_TIME, tzinfo=TAIPEI)


def _error(status: int, message: str) -> httpx.Response:
    """Return an ErrorResponse (openapi.json) with status."""
    return httpx.Response(status, json={"code": status, "message": message, "details": {}})


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _digest(kind: str, serial: int, length: int) -> str:
    """Return a stable made-up hex identifier, such as an order id, for the serial-th of kind."""
    return hashlib.sha1(f"{kind}-{serial}".encode()).hexdigest()[:length]


def _frame(event: str, data: object) -> bytes:
    """Return one SSE frame as the official server writes it: no space after the colons, no id."""
    return f"event:{event}\ndata:{json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


@dataclass
class _Stream:
    id: int
    path: str
    generation: int
    frames: queue.SimpleQueue[bytes | None] = field(default_factory=queue.SimpleQueue)


class SseHub:
    """The stub's open SSE connections: publish to them, drop them all, or wait for them to return.

    Each connection is served on its own server thread, which writes a
    heartbeat every heartbeat_s and every frame published to its path. A
    drop ends every open connection once it has written what was already
    published to it, and answers new ones 503 until down_s has passed.
    """

    def __init__(self, heartbeat_s: float = DEFAULT_HEARTBEAT_S) -> None:
        self._heartbeat_s = heartbeat_s
        self._cond = threading.Condition()
        self._streams: dict[int, _Stream] = {}
        self._next_id = 0
        self._generation = 0
        self._refuse_until = 0.0
        self._closed = False

    @staticmethod
    def is_stream_path(path: str) -> bool:
        return path == STREAM_PATH or path.startswith(f"{STREAM_PATH}/")

    def publish(self, event: str, data: object) -> None:
        """Queue one frame for every open connection on the combined stream or on this event's own path."""
        frame = _frame(event, data)
        paths = {STREAM_PATH, f"{STREAM_PATH}/{event}"}
        with self._cond:
            for stream in self._streams.values():
                if stream.path in paths:
                    stream.frames.put(frame)

    def drop(self, down_s: float) -> None:
        """End every open connection after what it already holds, and refuse new ones for down_s."""
        with self._cond:
            self._generation += 1
            self._refuse_until = time.monotonic() + down_s
            for stream in self._streams.values():
                stream.frames.put(None)

    def wait_for_streams(self, count: int, timeout_s: float) -> bool:
        """Wait until count connections opened since the last drop are open; return whether they were."""
        with self._cond:
            return self._cond.wait_for(
                lambda: sum(stream.generation == self._generation for stream in self._streams.values()) >= count,
                timeout_s,
            )

    def close(self) -> None:
        """End every connection and refuse new ones for good."""
        with self._cond:
            self._closed = True
            for stream in self._streams.values():
                stream.frames.put(None)

    def serve(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        """Hold one SSE connection open on handler's thread until it is dropped, closed or hung up."""
        stream = self._open(path)
        if stream is None:
            _write_response(handler, _error(503, "stream unavailable"))
            return
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
            handler.wfile.flush()
            self._pump(handler, stream)
        except OSError:
            # The client hung up, which ends this connection like a drop
            pass
        finally:
            with self._cond:
                del self._streams[stream.id]
                self._cond.notify_all()

    def _open(self, path: str) -> _Stream | None:
        """Register a new connection on path, or return None while streams are refused."""
        with self._cond:
            if self._closed or time.monotonic() < self._refuse_until:
                return None
            self._next_id += 1
            stream = _Stream(self._next_id, path, self._generation)
            self._streams[stream.id] = stream
            self._cond.notify_all()
            return stream

    def _pump(self, handler: BaseHTTPRequestHandler, stream: _Stream) -> None:
        heartbeat = _frame(HEARTBEAT_EVENT, {"type": HEARTBEAT_EVENT, "connection_id": f"conn-{stream.id}"})
        next_beat = time.monotonic() + self._heartbeat_s
        while True:
            wait_s = next_beat - time.monotonic()
            if wait_s <= 0:
                handler.wfile.write(heartbeat)
                handler.wfile.flush()
                next_beat += self._heartbeat_s
                continue
            try:
                frame = stream.frames.get(timeout=wait_s)
            except queue.Empty:
                continue
            if frame is None:
                return
            handler.wfile.write(frame)
            handler.wfile.flush()


@dataclass(frozen=True)
class SubscriptionChange:
    """One accepted subscribe or unsubscribe, with the Taipei time the stub took it."""

    at: datetime
    verb: str
    code: str
    quote_type: str


@dataclass
class _Order:
    """One order in the stub's book, with its fields as the place request sent them."""

    id: str
    seqno: str
    ordno: str
    is_stock: bool
    contract: dict[str, Any]
    fields: dict[str, Any]
    reference: Decimal
    limit_up: Decimal
    order_ts: float
    ca: str
    quantity: int  # Current size, which an accepted UpdateQty reduces
    status: str = "PendingSubmit"
    status_code: str = _OP_CODE_OK
    msg: str = ""
    deal_quantity: int = 0
    cancel_quantity: int = 0
    modified_price: float | int = 0
    modified_ts: float | None = None
    deals: list[dict[str, Any]] = field(default_factory=list)

    @property
    def account(self) -> dict[str, Any]:
        return self.fields["account"]

    @property
    def state(self) -> str:
        return "StockOrder" if self.is_stock else "FuturesOrder"


def _default_trading_limits() -> dict[str, object]:
    limits: dict[str, object] = {
        f"{kind}_{part}": 0 for kind in ("trading", "margin", "short") for part in ("limit", "used", "available")
    }
    limits.update(trading_limit=500000, trading_available=500000)
    return limits


def _default_margin() -> dict[str, object]:
    margin: dict[str, object] = dict.fromkeys(_MARGIN_FIELDS, 0.0)
    margin.update(available_margin=500000.0, equity=500000.0, equity_amount=500000.0)
    return margin


@dataclass(eq=False)
class StubBroker(FakeServer):
    """FakeServer plus every other operation the rehearsed subcommands send; see the module docstring.

    Thread-safe: the HTTP server answers on many threads at once. A test
    reads what happened through count, mutation_count, subscriptions and
    trade_subscribed, or blocks on wait_until. Once production_after_mutations
    mutations have been answered, mode reports production.
    """

    accounts: list[dict[str, object]] = field(
        default_factory=lambda: [dict(STUB_STOCK_ACCOUNT), dict(STUB_FUTURES_ACCOUNT)]
    )
    trading_limits: dict[str, object] = field(default_factory=_default_trading_limits)
    margin: dict[str, object] = field(default_factory=_default_margin)
    stock_contracts: dict[str, dict[str, object]] = field(
        default_factory=lambda: {
            "2890": stock_record("2890", 25.3, 20.7, 23.0),
            "00981A": stock_record("00981A", 11.0, 9.0, 10.0),
        }
    )
    hub: SseHub = field(default_factory=SseHub)
    exchange_clock_start: datetime = field(default_factory=session_anchor)
    production_after_mutations: int | None = None

    _cond: threading.Condition = field(init=False, default_factory=threading.Condition, repr=False)
    _created: float = field(init=False, default_factory=time.monotonic, repr=False)
    _orders: dict[str, _Order] = field(init=False, default_factory=dict, repr=False)
    _subscriptions: dict[tuple[str, str, str, str, bool], None] = field(init=False, default_factory=dict, repr=False)
    _trade_subscribed: set[str] = field(init=False, default_factory=set, repr=False)
    _counts: Counter[str] = field(init=False, default_factory=Counter, repr=False)
    _usage_bytes: int = field(init=False, default=0, repr=False)
    _serial: Counter[str] = field(init=False, default_factory=Counter, repr=False)
    _subscription_log: list[SubscriptionChange] = field(init=False, default_factory=list, repr=False)

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one request, count it, and switch to production once production_after_mutations is reached."""
        path = request.url.path
        answer = self._route(request.method, path)
        with self._cond:
            self._counts[path] += 1
            if answer is None:
                response = super().handler(request)
            else:
                self.requests.append(request)
                try:
                    body = json.loads(request.content) if request.content else None
                except ValueError:
                    response = _error(400, "request body is not JSON")
                else:
                    response = answer(body)
            flip_at = self.production_after_mutations
            if path in MUTATION_PATHS and flip_at is not None and self.mutation_count() >= flip_at:
                self.simulation = False
            self._cond.notify_all()
        return response

    def count(self, path: str) -> int:
        """Return how many requests to path the stub has answered."""
        with self._cond:
            return self._counts[path]

    def mutation_count(self) -> int:
        """Return how many place, update_price, update_qty and cancel requests the stub has answered."""
        with self._cond:
            return sum(self._counts[path] for path in MUTATION_PATHS)

    def subscriptions(self) -> set[tuple[str, str]]:
        """Return the active quote subscriptions as (code, quote_type) pairs."""
        with self._cond:
            return {(code, quote_type) for code, _, _, quote_type, _ in self._subscriptions}

    def trade_subscribed(self) -> set[str]:
        """Return the account types whose order and deal reports are enabled."""
        with self._cond:
            return set(self._trade_subscribed)

    def wait_until(self, predicate: Callable[[StubBroker], bool], timeout_s: float) -> bool:
        """Block until predicate(self) holds after some request, or timeout_s passes; return whether it held."""
        with self._cond:
            return self._cond.wait_for(lambda: predicate(self), timeout_s)

    def subscription_log(self) -> list[SubscriptionChange]:
        """Return every subscribe and unsubscribe the stub has accepted, in order."""
        with self._cond:
            return list(self._subscription_log)

    def publish_quotes(self) -> None:
        """Publish one quote for every active subscription, stamped with the real Taipei time.

        Published under the lock that subscription changes take, so a quote
        stamped after a change reflects it and every quote stamped before it
        is queued ahead of the quotes that follow.
        """
        with self._cond:
            now = datetime.now(TAIPEI)
            for subscription in self._subscriptions:
                self._publish_quote(subscription, now)

    def _publish_quote(self, subscription: tuple[str, str, str, str, bool], now: datetime) -> None:
        code, _, security_type, quote_type, intraday_odd = subscription
        reference = self._reference(code, futures=security_type != "STK")
        if reference is None:
            return
        price = str(reference)
        data: dict[str, object] = {
            "code": code,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S.%f"),
            "simtrade": False,
            "intraday_odd": intraday_odd,
        }
        if quote_type == "BidAsk":
            data.update(
                bid_price=[price] * _BOOK_DEPTH,
                bid_volume=[1] * _BOOK_DEPTH,
                ask_price=[price] * _BOOK_DEPTH,
                ask_volume=[1] * _BOOK_DEPTH,
            )
        else:
            data.update(open=price, high=price, low=price, close=price, volume=1, tick_type=1)
        self.hub.publish(f"{_QUOTE_EVENT_PREFIX[quote_type]}_{'stk' if security_type == 'STK' else 'fop'}", data)

    def _route(self, method: str, path: str) -> Callable[[Any], httpx.Response] | None:
        routes: dict[tuple[str, str], Callable[[Any], httpx.Response]] = {
            ("GET", INFO_PATH): self._info,
            ("GET", USAGE_PATH): self._usage,
            ("POST", SUBSCRIBE_TRADE_PATH): self._subscribe_trade,
            ("POST", SUBSCRIBE_PATH): lambda body: self._subscribe(body, active=True),
            ("POST", UNSUBSCRIBE_PATH): lambda body: self._subscribe(body, active=False),
            ("POST", PLACE_PATH): self._place,
            ("POST", UPDATE_PRICE_PATH): self._update_price,
            ("POST", UPDATE_QTY_PATH): self._update_qty,
            ("POST", CANCEL_PATH): self._cancel,
            ("POST", TRADES_PATH): self._trades,
            ("POST", TICKS_PATH): self._ticks,
            ("POST", KBARS_PATH): self._kbars,
            ("POST", SNAPSHOTS_PATH): self._snapshots,
        }
        return routes.get((method, path))

    # Session and subscriptions

    def _info(self, _body: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "shioaji",
                "version": "1.7.6",
                "description": "stub of the official shioaji server",
                "protocols": ["http"],
                "scheme": "http",
                "uds_plaintext": False,
                "simulation": self.simulation,
                "agent_harness": {
                    "enabled": False,
                    "mode": "off",
                    "capability_version": 1,
                    "capability_header": "X-Shioaji-Agent-Capability",
                    "digest_scheme": "compact_keyed_blake3",
                    "max_ttl_seconds": 60,
                },
            },
        )

    def _usage(self, _body: object) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "connections": _CONNECTIONS,
                "bytes": self._usage_bytes,
                "limit_bytes": _DAILY_LIMIT_BYTES,
                "remaining_bytes": _DAILY_LIMIT_BYTES - self._usage_bytes,
            },
        )

    def _find_account(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, dict):
            return None
        return next(
            (
                account
                for account in self.accounts
                if account.get("account_type") == request.get("account_type")
                and account.get("account_id") == request.get("account_id")
            ),
            None,
        )

    def _subscribe_trade(self, body: object) -> httpx.Response:
        account = self._find_account(body)
        if account is None:
            return _error(422, "no such account")
        self._trade_subscribed.add(str(account["account_type"]))
        return httpx.Response(200, json={"account": account, "subscribe_trade": True, "ts": time.time_ns()})

    def _subscribe(self, body: object, *, active: bool) -> httpx.Response:
        if not isinstance(body, dict) or body.get("quote_type") not in _QUOTE_EVENT_PREFIX:
            return _error(422, "expected a BaseSubscriptionRequest")
        key = (
            str(body.get("code")),
            str(body.get("exchange")),
            str(body.get("security_type")),
            str(body["quote_type"]),
            body.get("intraday_odd") is True,
        )
        known = self._reference(key[0], futures=key[2] != "STK") is not None
        if known and active:
            self._subscriptions[key] = None
        elif known:
            self._subscriptions.pop(key, None)
        if known:
            change = SubscriptionChange(datetime.now(TAIPEI), "subscribe" if active else "unsubscribe", key[0], key[3])
            self._subscription_log.append(change)
        verb = "subscribed" if active else "unsubscribed"
        message = f"{verb} {key[0]} {key[3]}" if known else f"unknown contract {key[0]}"
        return httpx.Response(200, json={"success": known, "message": message, "subscription": body})

    # Contracts

    def _contract_record(self, code: object, *, futures: bool) -> dict[str, object] | None:
        if not futures:
            return self.stock_contracts.get(str(code))
        records = (record for records in self.futures_contracts.values() for record in records)
        return next((record for record in records if record.get("code") == code), None)

    def _reference(self, code: object, *, futures: bool) -> Decimal | None:
        record = self._contract_record(code, futures=futures)
        return None if record is None else Decimal(str(record["reference"]))

    # Orders

    def _exchange_now(self) -> float:
        """Return the exchange clock in epoch seconds: exchange_clock_start plus the time since the stub was made."""
        return round(self.exchange_clock_start.timestamp() + (time.monotonic() - self._created), 6)

    def _next(self, kind: str) -> int:
        self._serial[kind] += 1
        return self._serial[kind]

    def _place(self, body: object) -> httpx.Response:
        if not isinstance(body, dict):
            return _error(422, "expected a PlaceOrderRequest")
        stock_order, futures_order = body.get("stock_order"), body.get("futures_order")
        if (stock_order is None) == (futures_order is None):
            return _error(422, "exactly one of stock_order and futures_order is required")
        is_stock = stock_order is not None
        order_fields = stock_order if is_stock else futures_order
        contract = body.get("contract")
        problem = self._place_problem(contract, order_fields, is_stock=is_stock)
        if problem is not None:
            return _error(422, problem)
        assert isinstance(contract, dict) and isinstance(order_fields, dict)

        record = self._contract_record(contract["code"], futures=not is_stock)
        assert record is not None
        serial = self._next("order")
        order_id = _digest("order", serial, 8)
        order = _Order(
            id=order_id,
            seqno=f"{serial:06d}",
            ordno=f"S{serial:04d}",
            is_stock=is_stock,
            contract=dict(contract),
            fields=dict(order_fields),
            reference=Decimal(str(record["reference"])),
            limit_up=Decimal(str(record["limit_up"])),
            order_ts=self._exchange_now(),
            ca=base64.b64encode(f"FAKE-CA-SIGNATURE order {order_id}".encode()).decode("ascii"),
            quantity=int(order_fields["quantity"]),
        )
        self._orders[order.id] = order

        # The official server answers PendingSubmit before the exchange replies, so the stub does too
        response = httpx.Response(200, json=self._trade(order))
        self._settle_new(order)
        return response

    def _place_problem(self, contract: object, order: object, *, is_stock: bool) -> str | None:
        """Return why the server would refuse this place synchronously, or None."""
        if not isinstance(contract, dict) or not isinstance(order, dict):
            return "contract and order must be objects"
        if self._contract_record(contract.get("code"), futures=not is_stock) is None:
            return f"unknown contract {contract.get('code')!r}"
        if order.get("action") not in ("Buy", "Sell"):
            return "action must be Buy or Sell"
        if order.get("price_type") not in ("LMT", "MKT", "MKP"):
            return "unknown price_type"
        if order.get("order_type") not in ("ROD", "IOC", "FOK"):
            return "unknown order_type"
        quantity = order.get("quantity")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            return "quantity must be a positive integer"
        if not _is_number(order.get("price")):
            return "price must be a number"
        account_type = "S" if is_stock else "F"
        account = order.get("account")
        if not isinstance(account, dict) or account.get("account_type") != account_type:
            return f"account must be the full {account_type} account object"
        if self._find_account(account) is None:
            return "no such account"
        if is_stock and order.get("order_lot") == "IntradayOdd" and order["price_type"] != "LMT":
            return "intraday odd-lot orders take LMT only"
        return None

    def _settle_new(self, order: _Order) -> None:
        if self._exceeds_funds(order):
            order.status, order.status_code, order.msg = "Failed", _OP_CODE_REJECTED, "insufficient trading limit"
            self._report(order, "New", _OP_CODE_REJECTED, order.msg)
            return
        order.status = _SUBMITTED
        self._report(order, "New", _OP_CODE_OK, "")
        if self._marketable(order):
            self._fill(order)
        elif order.fields["order_type"] != "ROD":
            # An IOC or FOK order that cannot fill expires at once
            self._cancel_remaining(order)

    def _exceeds_funds(self, order: _Order) -> bool:
        """Return whether a stock buy holds more than trading_available, as the simulator's funds check would."""
        if not order.is_stock or order.fields["action"] != "Buy":
            return False
        price = Decimal(str(order.fields["price"])) if order.fields["price_type"] == "LMT" else order.limit_up
        shares = order.quantity * (1 if order.fields.get("order_lot") == "IntradayOdd" else SHARES_PER_BOARD_LOT)
        return shares * price > Decimal(str(self.trading_limits["trading_available"]))

    def _marketable(self, order: _Order) -> bool:
        """Return whether order meets the stub's market, which trades at the contract's reference price."""
        if order.fields["price_type"] != "LMT":
            return True
        price = Decimal(str(order.fields["price"]))
        return price >= order.reference if order.fields["action"] == "Buy" else price <= order.reference

    def _fill(self, order: _Order) -> None:
        quantity = order.quantity - order.deal_quantity
        price = float(order.reference)
        ts = self._exchange_now()
        seq = f"{self._next('deal'):06d}"
        order.deal_quantity += quantity
        order.deals.append({"seq": seq, "price": price, "quantity": quantity, "ts": ts})
        order.status = "Filled"
        self._deal_report(order, seq, price, quantity, ts)

    def _cancel_remaining(self, order: _Order) -> None:
        order.cancel_quantity += order.quantity - order.deal_quantity
        order.status = "Cancelled"

        # A recorded Cancel report reads status.order_quantity 0 (docs/official-server-contract.md section 2)
        self._report(order, "Cancel", _OP_CODE_OK, "", order_quantity=0)

    def _live_order(self, body: object) -> _Order | httpx.Response:
        """Return the live order body's trade_id names, or the rejection of a request naming another."""
        trade_id = body.get("trade_id") if isinstance(body, dict) else None
        order = self._orders.get(trade_id) if isinstance(trade_id, str) else None
        if order is None:
            return _error(404, f"no order {trade_id!r}")
        if order.status != _SUBMITTED:
            return _error(422, f"order {trade_id} is {order.status}")
        return order

    def _update_price(self, body: object) -> httpx.Response:
        order = self._live_order(body)
        if isinstance(order, httpx.Response):
            return order
        price = body.get("price") if isinstance(body, dict) else None
        if not _is_number(price):
            return _error(422, "price must be a number")
        order.fields["price"] = price
        order.modified_price = price
        order.modified_ts = self._exchange_now()
        self._report(order, "UpdatePrice", _OP_CODE_OK, "")
        if self._marketable(order):
            self._fill(order)
        return httpx.Response(200, json=self._trade(order))

    def _update_qty(self, body: object) -> httpx.Response:
        order = self._live_order(body)
        if isinstance(order, httpx.Response):
            return order
        reduce_by = body.get("quantity") if isinstance(body, dict) else None
        remaining = order.quantity - order.deal_quantity
        if not isinstance(reduce_by, int) or isinstance(reduce_by, bool) or not 0 < reduce_by < remaining:
            return _error(422, f"quantity must reduce the order by 1 to {remaining - 1}")
        order.quantity -= reduce_by
        order.cancel_quantity += reduce_by
        self._report(order, "UpdateQty", _OP_CODE_OK, "")
        return httpx.Response(200, json=self._trade(order))

    def _cancel(self, body: object) -> httpx.Response:
        order = self._live_order(body)
        if isinstance(order, httpx.Response):
            return order
        self._cancel_remaining(order)
        return httpx.Response(200, json=self._trade(order))

    def _trades(self, body: object) -> httpx.Response:
        """Answer TradesRequest with the orders of the account it names, or of the stock account if it names none.

        The official server lists the stock account's orders alone for a
        request without an account, so the stub does too.
        """
        request = body if isinstance(body, dict) else {}
        if request.get("account_type") is None:
            account = next((account for account in self.accounts if account.get("account_type") == "S"), None)
        else:
            account = self._find_account(request)
        if account is None:
            return _error(422, "no such account")
        return httpx.Response(
            200,
            json=[
                self._trade(order)
                for order in self._orders.values()
                if order.account.get("account_type") == account["account_type"]
                and order.account.get("account_id") == account["account_id"]
            ],
        )

    def _trade(self, order: _Order) -> dict[str, object]:
        """Return order as a Trade (openapi.json)."""
        return {
            "contract": {**order.contract, "region": "TW"},
            "order": {
                "id": order.id,
                "seqno": order.seqno,
                "ordno": order.ordno,
                **order.fields,
                "quantity": order.quantity,
                "ca": order.ca,
            },
            "status": {
                "id": order.id,
                "status": order.status,
                "status_code": order.status_code,
                "msg": order.msg,
                "order_ts": order.order_ts,
                "modified_ts": order.modified_ts,
                "modified_price": order.modified_price,
                "order_quantity": order.quantity,
                "deal_quantity": order.deal_quantity,
                "cancel_quantity": order.cancel_quantity,
                "deals": list(order.deals),
                "web_id": _WEB_ID,
            },
        }

    # Order and deal reports

    def _report(
        self, order: _Order, op_type: str, op_code: str, op_msg: str, *, order_quantity: int | None = None
    ) -> None:
        """Publish a StockOrderEvent or FuturesOrderEvent, if the order's account has trade reports on."""
        if order.account.get("account_type") not in self._trade_subscribed:
            return
        fields = order.fields
        detail: dict[str, object] = {
            "id": order.id,
            "seqno": order.seqno,
            "ordno": order.ordno,
            "account": order.account,
            "action": fields["action"],
            "price": fields["price"],
            "quantity": order.quantity,
            "order_type": fields["order_type"],
            "price_type": fields["price_type"],
        }
        if order.is_stock:
            detail.update(
                order_cond="Cash", order_lot=fields.get("order_lot"), custom_field=fields.get("custom_field") or ""
            )
            contract: dict[str, object] = {**order.contract, "currency": "TWD"}
        else:
            detail.update(oc_type=fields.get("octype") or "Auto", market_type="Day", combo=False, subaccount="")
            contract = {**order.contract, "full_code": order.contract["code"]}
        report = {
            "event_id": _digest("event", self._next("event"), 16),
            "operation": {"op_type": op_type, "op_code": op_code, "op_msg": op_msg},
            "order": detail,
            "status": {
                "id": order.id,
                "exchange_ts": self._exchange_now(),
                "order_quantity": order.quantity if order_quantity is None else order_quantity,
                "cancel_quantity": order.cancel_quantity,
                "modified_price": order.modified_price,
                "web_id": _WEB_ID,
            },
            "contract": contract,
        }
        self.hub.publish(ORDER_EVENT, {"state": order.state, "data": {order.state: report}})

    def _deal_report(self, order: _Order, seq: str, price: float, quantity: int, ts: float) -> None:
        """Publish a StockDealEvent or FuturesDealEvent, if the order's account has trade reports on."""
        if order.account.get("account_type") not in self._trade_subscribed:
            return
        state = "StockDeal" if order.is_stock else "FuturesDeal"
        report: dict[str, object] = {
            "event_id": _digest("event", self._next("event"), 16),
            "trade_id": order.id,
            "seqno": order.seqno,
            "ordno": order.ordno,
            "exchange_seq": seq,
            "action": order.fields["action"],
            "code": order.contract["code"],
            "price": price,
            "quantity": quantity,
            "ts": ts,
            "account_id": order.account.get("account_id"),
            "broker_id": order.account.get("broker_id"),
        }
        if order.is_stock:
            report.update(
                order_cond="Cash",
                order_lot=order.fields.get("order_lot"),
                custom_field=order.fields.get("custom_field") or "",
                web_id=_WEB_ID,
            )
        else:
            report.update(
                security_type="FUT", subaccount="", market_type="Day", full_code=order.contract["code"], combo=False
            )
        self.hub.publish(ORDER_EVENT, {"state": state, "data": {state: report}})

    # History

    def _requested_price(self, contract: object) -> float | None:
        """Return the reference price of a ContractRequest the stub knows, or None."""
        if not isinstance(contract, dict):
            return None
        reference = self._reference(contract.get("code"), futures=contract.get("security_type") != "STK")
        return None if reference is None else float(reference)

    def _counted(self, payload: object) -> httpx.Response:
        """Answer a history query and charge its body to usage, as the daily quota counts bytes served."""
        response = httpx.Response(200, json=payload)
        self._usage_bytes += len(response.content)
        return response

    def _history_times_ns(self, count: int) -> list[int]:
        """Return count whole seconds from exchange_clock_start, in epoch nanoseconds."""
        start_ns = int(self.exchange_clock_start.timestamp()) * _NS_PER_S
        return [start_ns + index * _NS_PER_S for index in range(count)]

    def _ticks(self, body: object) -> httpx.Response:
        price = self._requested_price(body.get("contract") if isinstance(body, dict) else None)
        if price is None:
            return _error(422, "unknown contract")
        return self._counted(
            {
                "datetime": self._history_times_ns(_HISTORY_ROWS),
                "close": [price] * _HISTORY_ROWS,
                "volume": [1] * _HISTORY_ROWS,
                "bid_price": [price] * _HISTORY_ROWS,
                "bid_volume": [1] * _HISTORY_ROWS,
                "ask_price": [price] * _HISTORY_ROWS,
                "ask_volume": [1] * _HISTORY_ROWS,
                "tick_type": [1] * _HISTORY_ROWS,
            }
        )

    def _kbars(self, body: object) -> httpx.Response:
        price = self._requested_price(body.get("contract") if isinstance(body, dict) else None)
        if price is None:
            return _error(422, "unknown contract")
        return self._counted(
            {
                "datetime": self._history_times_ns(_HISTORY_ROWS),
                "Open": [price] * _HISTORY_ROWS,
                "High": [price] * _HISTORY_ROWS,
                "Low": [price] * _HISTORY_ROWS,
                "Close": [price] * _HISTORY_ROWS,
                "Volume": [1] * _HISTORY_ROWS,
                "Amount": [price] * _HISTORY_ROWS,
            }
        )

    def _snapshots(self, body: object) -> httpx.Response:
        contracts = body.get("contracts") if isinstance(body, dict) else None
        if not isinstance(contracts, list) or not contracts:
            return _error(422, "expected a SnapshotRequest")
        snapshots: list[dict[str, object]] = []
        for index, contract in enumerate(contracts):
            price = self._requested_price(contract)
            if price is None:
                return _error(422, f"unknown contract at contracts[{index}]")
            snapshots.append(
                {
                    "datetime": self._history_times_ns(1)[0],
                    "code": contract["code"],
                    "exchange": contract.get("exchange"),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "tick_type": "Buy",
                    "change_price": 0.0,
                    "change_rate": 0.0,
                    "change_type": "Unchanged",
                    "average_price": price,
                    "volume": 1,
                    "total_volume": 1,
                    "amount": price,
                    "total_amount": price,
                    "yesterday_volume": 1,
                    "buy_price": price,
                    "buy_volume": 1,
                    "sell_price": price,
                    "sell_volume": 1,
                    "volume_ratio": 1.0,
                }
            )
        return self._counted(snapshots)


def _write_response(handler: BaseHTTPRequestHandler, response: httpx.Response) -> None:
    body = response.content
    handler.send_response(response.status_code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        """Keep one line per request out of the test output."""

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if SseHub.is_stream_path(path):
            self._broker.hub.serve(self, path)
        else:
            self._answer("GET")

    def do_POST(self) -> None:
        self._answer("POST")

    @property
    def _http_server(self) -> _HttpServer:
        return cast(_HttpServer, self.server)

    @property
    def _broker(self) -> StubBroker:
        return self._http_server.broker

    def _answer(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        content = self.rfile.read(length) if length else b""
        request = httpx.Request(method, f"{self._http_server.base_url}{self.path}", content=content)
        try:
            response = self._broker.handler(request)
        except httpx.TransportError:
            # FakeServer.refuse: hang up unanswered, which is what the client sees of a lost connection
            self.close_connection = True
            return
        except Exception:
            logger.exception("stub failed to answer %s %s", method, self.path)
            response = _error(500, "stub failure")
        _write_response(self, response)


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, broker: StubBroker) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.broker = broker

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host!s}:{port}"

    def handle_error(self, request: object, client_address: object) -> None:
        # A client hanging up mid-response, such as a recorder being stopped, is expected
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)  # type: ignore[arg-type]


class StubServer:
    """Serve a StubBroker, its SSE hub and a steady quote flow on 127.0.0.1 until closed.

    Use it as a context manager; base_url is where p1.toml's base_url points.
    """

    def __init__(self, broker: StubBroker | None = None, *, quote_interval_s: float = DEFAULT_QUOTE_INTERVAL_S) -> None:
        self.broker = broker if broker is not None else StubBroker()
        self._quote_interval_s = quote_interval_s
        self._http = _HttpServer(self.broker)
        self._closed = threading.Event()
        self._threads = [
            threading.Thread(
                target=self._http.serve_forever,
                kwargs={"poll_interval": _SHUTDOWN_POLL_S},
                name="stub-http",
                daemon=True,
            ),
            threading.Thread(target=self._publish_quotes, name="stub-quotes", daemon=True),
        ]

    @property
    def base_url(self) -> str:
        return self._http.base_url

    @property
    def hub(self) -> SseHub:
        return self.broker.hub

    def __enter__(self) -> Self:
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Stop serving, end every stream, and wait a bounded time for the stub's own threads."""
        self._closed.set()
        self.broker.hub.close()
        self._http.shutdown()
        self._http.server_close()
        for thread in self._threads:
            thread.join(_JOIN_TIMEOUT_S)
        stuck = [thread.name for thread in self._threads if thread.is_alive()]
        if stuck:
            logger.warning("stub threads still running %gs after close: %s", _JOIN_TIMEOUT_S, ", ".join(stuck))

    def _publish_quotes(self) -> None:
        while not self._closed.wait(self._quote_interval_s):
            self.broker.publish_quotes()
