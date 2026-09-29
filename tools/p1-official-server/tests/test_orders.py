"""Tests for p1rec.orders: order scenarios against an in-process fake official server."""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from helpers import MINIMAL_CONFIG_LINES, SECRET_ENV_VARS, WriteConfig
from mock_official import ENDPOINT_LINES, FUTURES_ACCOUNT, STOCK_ACCOUNT, FakeServer, preflight_config

from p1rec import capture, cli
from p1rec.cli import main
from p1rec.config import ORDER_MARKETS, ConfigError, P1Config, load_config
from p1rec.interlock import EXIT_INTERLOCK_ABORT, InterlockError
from p1rec.orders import (
    EXIT_UNEXPECTED,
    FUTURES_DAY_SESSION,
    ORDERS_JSON,
    TWSE_CONTINUOUS_SESSION,
    TradingSession,
    check_cases,
    exceed_funds_lots,
    fits_session,
    json_number,
    render_template,
    run_orders,
)
from p1rec.records import EXIT_CHECK_FAILED
from p1rec.scenarios import Case, Market, PriceRef, load_scenarios

_TOOL_DIR = Path(__file__).resolve().parent.parent
_TAIPEI = ZoneInfo("Asia/Taipei")

# Inside the TAIFEX day session, so futures cases run unless a test moves the clock
_IN_SESSION = datetime(2026, 10, 1, 10, 0, tzinfo=_TAIPEI)

_INFO_PATH = "/api/v1/info"
_PLACE_PATH = "/api/v1/order/place_order"
_UPDATE_PRICE_PATH = "/api/v1/order/update_price"
_UPDATE_QTY_PATH = "/api/v1/order/update_qty"
_CANCEL_PATH = "/api/v1/order/cancel_order"
_TRADES_PATH = "/api/v1/order/trades"
_MUTATION_PATHS = (_PLACE_PATH, _UPDATE_PRICE_PATH, _UPDATE_QTY_PATH, _CANCEL_PATH)

_ACCOUNT_IDENTIFIERS = ("0000001", "0000002", "FAKEPID001", "fake-user")


class FakeClock:
    """A clock whose sleep only advances its own time."""

    def __init__(self, start: datetime = _IN_SESSION) -> None:
        self.start = start
        self.elapsed = 0.0

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.elapsed)

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


class InterruptingClock(FakeClock):
    """A FakeClock that raises KeyboardInterrupt instead of sleeping for one given duration."""

    def __init__(self, interrupt_on_s: float) -> None:
        super().__init__()
        self.interrupt_on_s = interrupt_on_s

    def sleep(self, seconds: float) -> None:
        if seconds == self.interrupt_on_s:
            raise KeyboardInterrupt
        super().sleep(seconds)


class ScriptedClock(FakeClock):
    """A FakeClock whose now() returns the given times in turn, then keeps the last one."""

    def __init__(self, *nows: datetime) -> None:
        super().__init__(nows[0])
        self.nows = list(nows)

    def now(self) -> datetime:
        return self.nows.pop(0) if len(self.nows) > 1 else self.nows[0]


class CallbackClock(FakeClock):
    """A FakeClock that calls on_sleep before every sleep."""

    def __init__(self, on_sleep: Callable[[], None]) -> None:
        super().__init__()
        self.on_sleep = on_sleep

    def sleep(self, seconds: float) -> None:
        self.on_sleep()
        super().sleep(seconds)


# Given a mutation's path and JSON body, a response to send instead of handling it, or None
Answer = Callable[[str, dict[str, Any]], httpx.Response | None]


def _order_of(body: dict[str, Any]) -> dict[str, Any]:
    return body.get("stock_order") or body.get("futures_order") or {}


def _answer_with(status: int, path: str, *, action: str | None = None) -> Answer:
    """Answer every mutation to path, or only a place with action, with status."""

    def answer(request_path: str, body: dict[str, Any]) -> httpx.Response | None:
        if request_path != path or (action is not None and _order_of(body).get("action") != action):
            return None
        return httpx.Response(status, json={"code": status, "message": "fake", "details": {}})

    return answer


@dataclass
class OrderServer(FakeServer):
    """FakeServer plus the order endpoints; a marketable order fills at the first trades query after it."""

    modes: list[bool] = field(default_factory=list)  # Answers to the next mode queries, then simulation
    reject_place: set[str] = field(default_factory=set)  # Codes whose place gets a 422
    answer: Answer | None = None
    end_as: dict[str, str] = field(default_factory=dict)  # Code -> status its orders end with in trades
    end_deal_quantity: int = 0  # Quantity an order ending by end_as reports as filled
    null_deal_quantity: bool = False
    fills: bool = True  # Whether market-type buys fill
    close_fills: bool = True  # Whether sells fill
    limit_buys_fill: bool = False
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[tuple[str, str]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == _INFO_PATH and self.modes:
            self.requests.append(request)
            return httpx.Response(200, json={"simulation": self.modes.pop(0)})
        if path not in (*_MUTATION_PATHS, _TRADES_PATH):
            return super().handler(request)

        self.requests.append(request)
        if path in self.refuse:
            raise httpx.ConnectError("[Errno 111] Connection refused", request=request)
        body = json.loads(request.content)
        answer = self.answer(path, body) if self.answer is not None else None
        if answer is not None:
            return answer
        if path == _TRADES_PATH:
            return httpx.Response(200, json=[self._trade(trade_id, refresh=True) for trade_id in self.orders])
        if path == _PLACE_PATH:
            return self._place(body)

        order = self.orders[body["trade_id"]]
        if path == _UPDATE_PRICE_PATH:
            order["price"] = body["price"]
        elif path == _UPDATE_QTY_PATH:
            order["quantity"] = int(order["quantity"]) - body["quantity"]
        else:
            order["status"] = "Cancelled"
        return httpx.Response(200, json=self._trade(body["trade_id"]))

    def _place(self, body: dict[str, object]) -> httpx.Response:
        contract = body["contract"]
        assert isinstance(contract, dict)
        if contract["code"] in self.reject_place:
            return httpx.Response(422, json={"code": 422, "message": "rejected", "details": {}})
        order = body.get("stock_order") or body.get("futures_order")
        assert isinstance(order, dict)
        trade_id = f"{len(self.orders) + 1:06d}"
        self.orders[trade_id] = {**order, "contract": contract, "status": "PendingSubmit", "deal_quantity": 0}
        self.events.append(("place", f"{trade_id} {order['action']}"))
        return httpx.Response(200, json=self._trade(trade_id))

    def _trade(self, trade_id: str, *, refresh: bool = False) -> dict[str, object]:
        order = self.orders[trade_id]
        if refresh and order["status"] in ("PendingSubmit", "Submitted"):
            if order["contract"]["code"] in self.end_as:
                order["status"] = self.end_as[order["contract"]["code"]]
                order["deal_quantity"] = self.end_deal_quantity
            elif self._fills(order):
                order["status"] = "Filled"
                order["deal_quantity"] = order["quantity"]
                self.events.append(("filled", trade_id))
            else:
                order["status"] = "Submitted"
        fields = {key: order[key] for key in ("action", "price", "quantity", "price_type", "order_type", "account")}
        return {
            "contract": {**order["contract"], "region": "TW"},
            "order": {"id": trade_id, "seqno": trade_id, "ordno": f"X{trade_id}", **fields},
            "status": {
                "id": trade_id,
                "status": order["status"],
                "status_code": "00",
                "deal_quantity": None if self.null_deal_quantity else order["deal_quantity"],
            },
        }

    def _fills(self, order: dict[str, Any]) -> bool:
        if order["action"] == "Sell":
            return self.close_fills
        return self.fills if order["price_type"] != "LMT" else self.limit_buys_fill

    def mutations(self) -> list[tuple[str, Any]]:
        return [(r.url.path, json.loads(r.content)) for r in self.requests if r.url.path in _MUTATION_PATHS]

    def kinds(self) -> list[str]:
        names = {_INFO_PATH: "mode", _TRADES_PATH: "trades", **{p: "mutation" for p in _MUTATION_PATHS}}
        return [names.get(r.url.path, "query") for r in self.requests]


def _case(case_id: str, **overrides: object) -> Case:
    fields: dict[str, object] = {
        "id": case_id,
        "market": Market.STOCK,
        "code": "2890",
        "action": "Buy",
        "price_ref": PriceRef.FAR_BELOW,
        "price_type": "LMT",
        "order_type": "ROD",
        "quantity": 1,
        "custom_field": None,
        "expect_fill": False,
        "steps": ("place", "cancel"),
    }
    fields.update(overrides)
    return Case(**fields)  # type: ignore[arg-type]


def _fill_case(case_id: str, **overrides: object) -> Case:
    fill = {
        "price_ref": PriceRef.LIMIT_UP,
        "price_type": "MKT",
        "order_type": "IOC",
        "expect_fill": True,
        "steps": ("place",),
    }
    return _case(case_id, **{**fill, **overrides})


_LIFECYCLE_STEPS = ("place", "update_price:far_below_plus_tick", "update_qty:1", "cancel")


def _config(write_config: WriteConfig, **orders_overrides: object) -> P1Config:
    cfg = preflight_config(write_config)
    return dataclasses.replace(cfg, orders=dataclasses.replace(cfg.orders, **orders_overrides))


def _run(
    tmp_path: Path,
    cfg: P1Config,
    server: OrderServer,
    cases: list[Case],
    *,
    only: set[str] | None = None,
    clock: FakeClock | None = None,
) -> tuple[int, dict]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    exit_code = run_orders(cfg, cases, run_dir, only, transport=server.transport(), clock=clock or FakeClock())
    return exit_code, json.loads((run_dir / ORDERS_JSON).read_text(encoding="utf-8"))


def _outcomes(summary: dict) -> list[str]:
    return [case["outcome"] for case in summary["cases"]]


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def _set_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")


# Interlock


def test_interlock_before_every_mutation(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = OrderServer(modes=[True, False])
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(InterlockError):
        run_orders(
            _config(write_config),
            [_case("first"), _case("second")],
            run_dir,
            None,
            transport=server.transport(),
            clock=FakeClock(),
        )

    assert [path for path, _ in server.mutations()] == [_PLACE_PATH]
    summary = json.loads((run_dir / ORDERS_JSON).read_text(encoding="utf-8"))
    assert _outcomes(summary) == ["interlock_abort", "not_run"]

    # The first case's order was placed but never cancelled
    (critical,) = _messages(caplog, logging.CRITICAL)
    assert "first" in critical
    assert "possible residual position, check manually" in critical


def test_every_mutation_follows_its_own_mode_query(tmp_path: Path, write_config: WriteConfig) -> None:
    server = OrderServer()
    cases = [_case("lifecycle", quantity=2, steps=_LIFECYCLE_STEPS), _fill_case("fill")]

    exit_code, _ = _run(tmp_path, _config(write_config), server, cases)

    kinds = server.kinds()
    mutation_at = [index for index, kind in enumerate(kinds) if kind == "mutation"]
    assert exit_code == 0
    assert len(mutation_at) == 6  # 4 lifecycle steps, then a fill and its close
    assert kinds.count("mode") == len(mutation_at)
    assert all(kinds[index - 1] == "mode" for index in mutation_at)


def test_cli_interlock_abort_exits_3(
    tmp_path: Path, write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_secret_env(monkeypatch)
    config_path = write_config([*MINIMAL_CONFIG_LINES, *ENDPOINT_LINES])
    server = OrderServer(simulation=False)

    def fake_make_http_client(
        base_url: str, timeout: httpx.Timeout, *, transport: httpx.BaseTransport | None = None
    ) -> httpx.Client:
        return httpx.Client(base_url=base_url, timeout=timeout, transport=server.transport())

    monkeypatch.setattr(capture, "make_http_client", fake_make_http_client)
    scenarios = _TOOL_DIR / "scenarios.toml"

    exit_code = main(["orders", "--config", str(config_path), "--scenarios", str(scenarios), "--only", "harness_probe"])

    assert exit_code == EXIT_INTERLOCK_ABORT
    assert server.mutations() == []
    run_dir = next((tmp_path / "results").iterdir())
    assert _outcomes(json.loads((run_dir / ORDERS_JSON).read_text(encoding="utf-8"))) == ["interlock_abort"]


# Funds and prices


def test_exceed_funds_quantity() -> None:
    assert exceed_funds_lots(Decimal(100000), Decimal(20)) == 6


def test_exceed_funds_sized_from_largest_funds_source(tmp_path: Path, write_config: WriteConfig) -> None:
    # 250000 / (1000 * 20.7) = 12.08, so 13 lots cover it and one more exceeds it
    server = OrderServer(
        trading_limits={"trading_available": 100000},
        balance={"acc_balance": 250000.0, "date": "2026-09-29", "errmsg": ""},
    )
    case = _case("exceed", price_ref=PriceRef.LIMIT_DOWN, quantity="exceed_funds")

    exit_code, _ = _run(tmp_path, _config(write_config), server, [case])

    (_, place), _ = server.mutations()
    assert exit_code == 0
    assert place["stock_order"]["quantity"] == 14
    assert place["stock_order"]["price"] == 20.7


def test_place_bodies_carry_typed_values_and_full_account(tmp_path: Path, write_config: WriteConfig) -> None:
    cases = [
        _case("stock", custom_field="Zz9aA0", quantity=2, steps=_LIFECYCLE_STEPS),
        _case("odd", market=Market.ODD, code="0050", quantity=5),
        _case("futures", market=Market.FUTURES, code="TMFJ6"),
    ]
    server = OrderServer()

    exit_code, _ = _run(tmp_path, _config(write_config), server, cases)

    bodies = [body for path, body in server.mutations() if path != _CANCEL_PATH]
    stock, update_price, update_qty, odd, futures = bodies
    assert exit_code == 0
    assert stock == {
        "contract": {"security_type": "STK", "exchange": "TSE", "code": "2890"},
        "stock_order": {
            "action": "Buy",
            "price": 20.7,
            "quantity": 2,
            "price_type": "LMT",
            "order_type": "ROD",
            "order_lot": "Common",
            "custom_field": "Zz9aA0",
            "account": STOCK_ACCOUNT,
        },
    }
    assert update_price == {"trade_id": "000001", "price": 20.75}
    assert update_qty == {"trade_id": "000001", "quantity": 1}
    assert odd["stock_order"]["order_lot"] == "IntradayOdd"
    assert odd["stock_order"]["quantity"] == 5
    assert odd["stock_order"]["custom_field"] is None
    assert futures == {
        "contract": {"security_type": "FUT", "exchange": "TAIFEX", "code": "TMFJ6"},
        "futures_order": {
            "action": "Buy",
            "price": 45391,
            "quantity": 1,
            "price_type": "LMT",
            "order_type": "ROD",
            "octype": "Auto",
            "custom_field": None,
            "account": FUTURES_ACCOUNT,
        },
    }
    place_requests = [r for r in server.requests if r.url.path == _PLACE_PATH]
    assert b'"price":20.7,' in place_requests[0].content


def test_render_template_substitutes_typed_values() -> None:
    template = (
        '{"a": {"price": "{price}", "qty": ["{quantity}"]}, "tag": "{custom_field}", "who": "{account}", "k": "x{y}"}'
    )

    body = render_template(
        template, {"price": json_number(Decimal("20.70")), "quantity": 3, "custom_field": None, "account": {"s": 1}}
    )

    assert body == {"a": {"price": 20.7, "qty": [3]}, "tag": None, "who": {"s": 1}, "k": "x{y}"}
    assert (
        json.dumps(body, separators=(",", ":")) == '{"a":{"price":20.7,"qty":[3]},"tag":null,"who":{"s":1},"k":"x{y}"}'
    )


def test_json_number_is_exact() -> None:
    assert json_number(Decimal("45391")) == 45391
    assert isinstance(json_number(Decimal("45391.0")), int)
    assert json.dumps(json_number(Decimal("0.1"))) == "0.1"
    assert json.dumps(json_number(Decimal("0.0001"))) == "0.0001"
    for inexact_or_exponent in ("0.12345678901234567890", "0.00001", "12345678901234567.5"):
        with pytest.raises(ValueError):
            json_number(Decimal(inexact_or_exponent))


# Fills


def test_fill_then_close_sequence(tmp_path: Path, write_config: WriteConfig) -> None:
    server = OrderServer()

    exit_code, summary = _run(tmp_path, _config(write_config), server, [_fill_case("a"), _fill_case("b")])

    assert exit_code == 0
    assert _outcomes(summary) == ["closed", "closed"]

    # The second fill case is placed only after the first one's close has filled
    assert server.events == [
        ("place", "000001 Buy"),
        ("filled", "000001"),
        ("place", "000002 Sell"),
        ("filled", "000002"),
        ("place", "000003 Buy"),
        ("filled", "000003"),
        ("place", "000004 Sell"),
        ("filled", "000004"),
    ]
    close = server.mutations()[1][1]["stock_order"]
    assert (close["action"], close["price"], close["price_type"], close["order_type"]) == ("Sell", 20.7, "LMT", "ROD")
    assert summary["cases"][0]["close_trade_id"] == "000002"


def test_fill_timeout_logs_critical_and_exits_2(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = OrderServer(fills=False)
    clock = FakeClock()
    cfg = _config(write_config, fill_poll_s=2.0, fill_timeout_s=10.0)
    cases = [_fill_case("stuck"), _fill_case("next_fill"), _case("resting")]

    exit_code, summary = _run(tmp_path, cfg, server, cases, clock=clock)

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["residual_position", "skipped_after_residual", "completed"]
    places = [body for path, body in server.mutations() if path == _PLACE_PATH]
    assert [body["stock_order"]["action"] for body in places] == ["Buy", "Buy"]  # No close, no second fill
    assert places[1]["stock_order"]["price_type"] == "LMT"  # The resting case still ran
    critical = _messages(caplog, logging.CRITICAL)
    assert len(critical) == 1
    assert "stuck" in critical[0]
    assert "possible residual position, check manually" in critical[0]
    # Every 2 s until 10 s have passed, then once to confirm the resting case ended unfilled
    assert server.kinds().count("trades") == 6


def test_order_ending_unfilled_needs_no_close(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    # The exchange's rejection shows only in trades, the place having returned 200
    server = OrderServer(end_as={"2890": "Failed"})
    cases = [_fill_case("rejected_later"), _fill_case("futures_fill", market=Market.FUTURES, code="TMFJ6")]

    exit_code, summary = _run(tmp_path, _config(write_config), server, cases)

    assert exit_code == 0
    assert _outcomes(summary) == ["ended_unfilled", "closed"]
    assert _messages(caplog, logging.CRITICAL) == []


@pytest.mark.parametrize(
    ("status", "null_deal_quantity", "outcome"),
    [
        ("Failed", True, "ended_unfilled"),
        ("Cancelled", False, "ended_unfilled"),
        ("Cancelled", True, "residual_position"),
    ],
)
def test_ended_order_is_flat_only_on_evidence(
    tmp_path: Path, write_config: WriteConfig, status: str, null_deal_quantity: bool, outcome: str
) -> None:
    server = OrderServer(end_as={"2890": status}, null_deal_quantity=null_deal_quantity)

    _, summary = _run(tmp_path, _config(write_config), server, [_fill_case("fill")])

    assert _outcomes(summary) == [outcome]
    assert [body["stock_order"]["action"] for _, body in server.mutations()] == ["Buy"]


@pytest.mark.parametrize(
    ("server", "reason"),
    [
        (OrderServer(close_fills=False), "close 000002 did not fill (timed_out)"),
        (OrderServer(answer=_answer_with(422, _PLACE_PATH, action="Sell")), "close rejected"),
    ],
    ids=["close_timeout", "close_rejected"],
)
def test_close_that_does_not_fill_is_a_residual_position(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture, server: OrderServer, reason: str
) -> None:
    cfg = _config(write_config, fill_poll_s=2.0, fill_timeout_s=10.0)

    exit_code, summary = _run(tmp_path, cfg, server, [_fill_case("first"), _fill_case("next_fill")])

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["residual_position", "skipped_after_residual"]
    assert [body["stock_order"]["action"] for _, body in server.mutations()] == ["Buy", "Sell"]
    (critical,) = _messages(caplog, logging.CRITICAL)
    assert "first" in critical
    assert reason in critical
    assert "possible residual position, check manually" in critical
    close_id = "000002" if "timed_out" in reason else "none"
    assert (
        f"check trades for trade_id 000001, close_trade_id {close_id}; "
        "cancel or close it in the simulator before rerunning"
    ) in critical


def test_interlock_abort_before_the_close(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = OrderServer(modes=[True, False])
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(InterlockError):
        run_orders(
            _config(write_config), [_fill_case("fill")], run_dir, None, transport=server.transport(), clock=FakeClock()
        )

    assert [body["stock_order"]["action"] for _, body in server.mutations()] == ["Buy"]
    (case,) = json.loads((run_dir / ORDERS_JSON).read_text(encoding="utf-8"))["cases"]
    assert (case["trade_id"], case["outcome"]) == ("000001", "interlock_abort")
    (critical,) = _messages(caplog, logging.CRITICAL)
    assert "fill" in critical
    assert "possible residual position, check manually" in critical


def test_interrupt_during_fill_wait(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = OrderServer()
    cfg = _config(write_config, fill_poll_s=2.0)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(KeyboardInterrupt):
        run_orders(
            cfg,
            [_fill_case("fill"), _case("after")],
            run_dir,
            None,
            transport=server.transport(),
            clock=InterruptingClock(interrupt_on_s=cfg.orders.fill_poll_s),
        )

    summary = json.loads((run_dir / ORDERS_JSON).read_text(encoding="utf-8"))
    assert [(case["trade_id"], case["outcome"]) for case in summary["cases"]] == [
        ("000001", "interrupted"),
        (None, "not_run"),
    ]
    (critical,) = _messages(caplog, logging.CRITICAL)
    assert "fill" in critical
    assert "possible residual position, check manually" in critical


@pytest.mark.parametrize(
    ("server", "case", "state"),
    [
        (OrderServer(limit_buys_fill=True), _case("expiring", order_type="IOC", steps=("place",)), "filled"),
        (
            OrderServer(end_as={"2890": "Cancelled"}, end_deal_quantity=1),
            _case("expiring", order_type="IOC", quantity=2, steps=("place",)),
            "ended_partial",
        ),
    ],
    ids=["filled", "ended_partial"],
)
def test_order_not_meant_to_fill_that_fills_is_a_residual_position(
    tmp_path: Path,
    write_config: WriteConfig,
    caplog: pytest.LogCaptureFixture,
    server: OrderServer,
    case: Case,
    state: str,
) -> None:
    exit_code, summary = _run(tmp_path, _config(write_config), server, [case, _fill_case("next_fill")])

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["residual_position", "skipped_after_residual"]
    assert len(server.mutations()) == 1
    (critical,) = _messages(caplog, logging.CRITICAL)
    assert f"order 000001, not meant to fill, shows a fill ({state})" in critical
    assert "check trades for trade_id 000001" in critical


def test_fill_case_ending_partly_filled_is_a_residual_position(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = OrderServer(end_as={"2890": "Cancelled"}, end_deal_quantity=1)

    exit_code, summary = _run(tmp_path, _config(write_config), server, [_fill_case("partial", quantity=2)])

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["residual_position"]
    assert [body["stock_order"]["action"] for _, body in server.mutations()] == ["Buy"]
    (critical,) = _messages(caplog, logging.CRITICAL)
    assert "did not fill completely (ended_partial)" in critical


def test_accepted_fill_case_without_trade_id_is_a_residual_position(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    exit_code, summary = _run(
        tmp_path, _config(write_config), OrderServer(answer=_no_trade_id_place), [_fill_case("fill")]
    )

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["residual_position"]
    (critical,) = _messages(caplog, logging.CRITICAL)
    assert "no trade id" in critical
    assert "check trades for trade_id unknown" in critical


def test_orders_json_rewritten_after_every_case(tmp_path: Path, write_config: WriteConfig) -> None:
    path = tmp_path / "run" / ORDERS_JSON
    snapshots: list[list[str]] = []

    def snapshot() -> None:
        if path.exists():
            snapshots.append(_outcomes(json.loads(path.read_text(encoding="utf-8"))))

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    server = OrderServer()
    run_orders(
        _config(write_config),
        [_case("first"), _case("second")],
        run_dir,
        None,
        transport=server.transport(),
        clock=CallbackClock(snapshot),
    )

    # Taken while the second case runs, so only a rewrite after the first case explains it
    assert snapshots[0] == ["completed", "not_run"]


def test_session_checked_again_after_preparation(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    # A 127 s fill case fits at 13:22:50 but no longer at 13:23:00, after preparation
    server = OrderServer()
    clock = ScriptedClock(
        datetime(2026, 10, 1, 13, 22, 50, tzinfo=_TAIPEI), datetime(2026, 10, 1, 13, 23, 0, tzinfo=_TAIPEI)
    )

    exit_code, summary = _run(tmp_path, _config(write_config), server, [_fill_case("late_fill")], clock=clock)

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["refused_outside_session"]
    assert server.kinds().count("query") > 0
    assert server.mutations() == []
    assert server.kinds().count("mode") == 0


def _no_trade_id_place(path: str, _body: dict[str, Any]) -> httpx.Response | None:
    return httpx.Response(200, json={}) if path == _PLACE_PATH else None


@pytest.mark.parametrize(
    ("case", "answer", "trade_id"),
    [
        (_fill_case("interrupted"), None, "000001"),
        (_case("interrupted", quantity=2, steps=_LIFECYCLE_STEPS), None, "000001"),
        (_case("interrupted"), _no_trade_id_place, None),
    ],
    ids=["fill", "lifecycle", "accepted_without_trade_id"],
)
def test_interrupt_during_step_wait_after_accepted_place(
    tmp_path: Path,
    write_config: WriteConfig,
    caplog: pytest.LogCaptureFixture,
    case: Case,
    answer: Answer | None,
    trade_id: str | None,
) -> None:
    server = OrderServer(answer=answer)
    cfg = _config(write_config)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(KeyboardInterrupt):
        run_orders(
            cfg,
            [case],
            run_dir,
            None,
            transport=server.transport(),
            clock=InterruptingClock(interrupt_on_s=cfg.orders.step_wait_s),
        )

    assert [path for path, _ in server.mutations()] == [_PLACE_PATH]
    (recorded,) = json.loads((run_dir / ORDERS_JSON).read_text(encoding="utf-8"))["cases"]
    assert (recorded["trade_id"], recorded["outcome"]) == (trade_id, "interrupted")
    (critical,) = _messages(caplog, logging.CRITICAL)
    assert "interrupted" in critical
    assert "possible residual position, check manually" in critical


# Rejections


def test_rejection_recorded_and_continues(tmp_path: Path, write_config: WriteConfig) -> None:
    server = OrderServer(reject_place={"2890"})
    cases = [_case("refused"), _case("futures", market=Market.FUTURES, code="TMFJ6")]

    exit_code, summary = _run(tmp_path, _config(write_config), server, cases)

    assert exit_code == 0
    assert _outcomes(summary) == ["rejected", "completed"]
    rest = [json.loads(line) for line in (tmp_path / "run" / "rest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [line["status"] for line in rest if line["op"] == "place_stock"] == [422]
    assert [line["status"] for line in rest if line["op"] in ("place_futures", "cancel")] == [200, 200]


def test_rejected_place_skips_remaining_steps(tmp_path: Path, write_config: WriteConfig) -> None:
    server = OrderServer(reject_place={"2890"})

    _run(tmp_path, _config(write_config), server, [_case("refused", quantity=2, steps=_LIFECYCLE_STEPS)])

    assert [path for path, _ in server.mutations()] == [_PLACE_PATH]


def test_rejected_cancel_of_resting_order_exits_2(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = OrderServer(answer=_answer_with(422, _CANCEL_PATH))
    cfg = _config(write_config, fill_poll_s=2.0, fill_timeout_s=10.0)

    exit_code, summary = _run(tmp_path, cfg, server, [_case("resting")])

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["order_may_rest"]
    assert server.kinds().count("trades") == 5  # Waited for an end state every 2 s until 10 s had passed
    (error,) = [m for m in _messages(caplog, logging.ERROR) if "may still rest" in m]
    assert "check trades for trade_id 000001, close_trade_id none" in error


def test_rejected_cancel_of_failed_order_completes(tmp_path: Path, write_config: WriteConfig) -> None:
    server = OrderServer(end_as={"2890": "Failed"}, answer=_answer_with(422, _CANCEL_PATH))

    exit_code, summary = _run(tmp_path, _config(write_config), server, [_case("resting")])

    assert exit_code == 0
    assert _outcomes(summary) == ["completed"]
    assert summary["cases"][0]["reason"] == "cancel rejected, but the order had already ended unfilled"


def test_failed_status_on_a_2xx_place_counts_as_rejected(tmp_path: Path, write_config: WriteConfig) -> None:
    def failed_place(path: str, _body: dict[str, Any]) -> httpx.Response | None:
        body = {"order": {"id": "F00001"}, "status": {"id": "F00001", "status": "Failed", "status_code": "88"}}
        return httpx.Response(200, json=body) if path == _PLACE_PATH else None

    server = OrderServer(answer=failed_place)

    exit_code, summary = _run(
        tmp_path, _config(write_config), server, [_case("failed", quantity=2, steps=_LIFECYCLE_STEPS)]
    )

    assert exit_code == 0
    assert _outcomes(summary) == ["rejected"]
    assert summary["cases"][0]["trade_id"] is None
    assert [path for path, _ in server.mutations()] == [_PLACE_PATH]


def test_write_failure_does_not_hide_the_interlock_abort(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # A directory where the temporary summary file goes makes every write fail
    (run_dir / f"{ORDERS_JSON}.tmp").mkdir()
    server = OrderServer(modes=[False])

    with pytest.raises(InterlockError):
        run_orders(
            _config(write_config), [_case("first")], run_dir, None, transport=server.transport(), clock=FakeClock()
        )

    assert any(m.startswith("could not write") for m in _messages(caplog, logging.ERROR))


def test_rejected_cancel_of_filled_order_is_a_residual_position(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = OrderServer(limit_buys_fill=True, answer=_answer_with(422, _CANCEL_PATH))

    exit_code, summary = _run(tmp_path, _config(write_config), server, [_case("resting")])

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["residual_position"]
    (critical,) = _messages(caplog, logging.CRITICAL)
    assert "resting" in critical
    assert "cancel was rejected and order 000001, not meant to fill, shows a fill (filled)" in critical


def test_no_response_to_a_mutation_stops_the_run(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = OrderServer(refuse={_PLACE_PATH})

    exit_code, summary = _run(tmp_path, _config(write_config), server, [_case("first"), _case("second")])

    assert exit_code == EXIT_UNEXPECTED
    assert _outcomes(summary) == ["unknown_outcome", "not_run"]
    assert summary["cases"][0]["steps"] == [{"step": "place", "op": "place_stock", "status": None, "accepted": False}]
    (critical,) = _messages(caplog, logging.CRITICAL)
    assert "first" in critical
    assert "possible residual position, check manually" in critical


@pytest.mark.parametrize(
    ("answer", "cases", "steps"),
    [
        (
            _answer_with(502, _PLACE_PATH),
            [_case("first"), _case("second")],
            [("place", "place_stock", 502, False)],
        ),
        (
            _answer_with(503, _CANCEL_PATH),
            [_case("first"), _case("second")],
            [("place", "place_stock", 200, True), ("cancel", "cancel", 503, False)],
        ),
        (
            _answer_with(502, _PLACE_PATH, action="Sell"),
            [_fill_case("first"), _fill_case("second")],
            [("place", "place_stock", 200, True), ("close", "place_stock", 502, False)],
        ),
    ],
    ids=["place", "cancel", "close"],
)
def test_server_error_on_a_mutation_stops_the_run(
    tmp_path: Path,
    write_config: WriteConfig,
    caplog: pytest.LogCaptureFixture,
    answer: Answer,
    cases: list[Case],
    steps: list[tuple[str, str, int, bool]],
) -> None:
    # A 5xx may come after the server acted on the request, unlike a 4xx
    server = OrderServer(answer=answer)

    exit_code, summary = _run(tmp_path, _config(write_config), server, cases)

    assert exit_code == EXIT_UNEXPECTED
    assert _outcomes(summary) == ["unknown_outcome", "not_run"]
    assert [tuple(step.values())[:4] for step in summary["cases"][0]["steps"]] == steps
    assert len(server.mutations()) == len(steps)
    (critical,) = _messages(caplog, logging.CRITICAL)
    assert "first" in critical
    assert "possible residual position, check manually" in critical


def test_unparseable_place_body_counts_as_accepted_without_trade_id(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    # Deep enough nesting that parsing raises RecursionError, not ValueError
    nested = b"[" * 100_000 + b"]" * 100_000

    def answer(path: str, _body: dict[str, Any]) -> httpx.Response | None:
        return httpx.Response(200, content=nested) if path == _PLACE_PATH else None

    exit_code, summary = _run(tmp_path, _config(write_config), OrderServer(answer=answer), [_case("resting")])

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["order_may_rest"]
    assert any("may still rest" in message for message in _messages(caplog, logging.ERROR))


def test_unprepared_case_sends_nothing_and_exits_2(tmp_path: Path, write_config: WriteConfig) -> None:
    server = OrderServer()

    exit_code, summary = _run(tmp_path, _config(write_config), server, [_case("unknown", code="9999"), _case("known")])

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["not_prepared", "completed"]
    assert server.kinds().count("mode") == 2  # Only the known case's place and cancel


# Futures session


@pytest.mark.parametrize(
    ("session", "clock_time", "budget_s", "fits"),
    [
        (FUTURES_DAY_SESSION, "08:44:59", 1.0, False),
        (FUTURES_DAY_SESSION, "08:45:00", 1.0, True),
        (FUTURES_DAY_SESSION, "13:44:58", 2.0, True),
        (FUTURES_DAY_SESSION, "13:44:59", 2.0, False),
        (FUTURES_DAY_SESSION, "13:45:00", 0.0, False),
        (FUTURES_DAY_SESSION, "20:00:00", 1.0, False),
        (TWSE_CONTINUOUS_SESSION, "08:59:59", 1.0, False),
        (TWSE_CONTINUOUS_SESSION, "09:00:00", 1.0, True),
        (TWSE_CONTINUOUS_SESSION, "13:24:58", 2.0, True),
        (TWSE_CONTINUOUS_SESSION, "13:24:59", 2.0, False),
    ],
)
def test_session_bounds(session: TradingSession, clock_time: str, budget_s: float, fits: bool) -> None:
    start = datetime.fromisoformat(f"2026-10-01T{clock_time}+08:00")

    assert fits_session(start, budget_s, session) is fits


@pytest.mark.parametrize(("utc_time", "fits"), [("01:00:00", True), ("05:25:00", False), ("00:59:59", False)])
def test_session_bounds_take_any_timezone(utc_time: str, fits: bool) -> None:
    # 01:00 UTC is 09:00 in Taipei, and 05:25 UTC is 13:25
    start = datetime.fromisoformat(f"2026-10-01T{utc_time}+00:00")

    assert fits_session(start, 1.0, TWSE_CONTINUOUS_SESSION) is fits


def test_futures_refused_outside_day_session_sends_nothing(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = OrderServer()
    clock = FakeClock(datetime(2026, 10, 1, 14, 0, tzinfo=_TAIPEI))
    cases = [_case("futures", market=Market.FUTURES, code="TMFJ6"), _case("stock")]

    exit_code, summary = _run(tmp_path, _config(write_config), server, cases, clock=clock)

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["refused_outside_session", "completed"]
    assert not any("futures" in r.url.path for r in server.requests)
    assert all("futures_order" not in body for _, body in server.mutations())
    assert any("futures" in message for message in _messages(caplog, logging.ERROR))


def test_futures_fill_case_refused_when_it_could_outlast_the_session(tmp_path: Path, write_config: WriteConfig) -> None:
    # 1 step * 3 s + 2 * (60 s + 2 s) = 127 s from 13:43 ends after 13:45
    server = OrderServer()
    clock = FakeClock(datetime(2026, 10, 1, 13, 43, tzinfo=_TAIPEI))

    exit_code, summary = _run(
        tmp_path, _config(write_config), server, [_fill_case("late", market=Market.FUTURES, code="TMFJ6")], clock=clock
    )

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["refused_outside_session"]
    assert server.requests == []


def test_stock_fill_case_refused_when_it_could_outlast_the_twse_session(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    # 1 step * 3 s + 2 * (60 s + 2 s) = 127 s from 13:23 ends after 13:25, when continuous trading ends
    server = OrderServer()
    clock = FakeClock(datetime(2026, 10, 1, 13, 23, tzinfo=_TAIPEI))

    exit_code, summary = _run(
        tmp_path, _config(write_config), server, [_fill_case("late_fill"), _case("resting")], clock=clock
    )

    assert exit_code == EXIT_CHECK_FAILED
    assert _outcomes(summary) == ["refused_outside_session", "completed"]
    assert [body["stock_order"]["price_type"] for path, body in server.mutations() if path == _PLACE_PATH] == ["LMT"]
    assert any("late_fill" in m and "TWSE continuous trading" in m for m in _messages(caplog, logging.ERROR))


# Case selection


def test_rod_case_without_cancel_rejected_before_any_request(tmp_path: Path, write_config: WriteConfig) -> None:
    server = OrderServer()

    with pytest.raises(ConfigError, match=r"'resting'.*cancel"):
        _run(
            tmp_path,
            _config(write_config),
            server,
            [_case("expiring", order_type="IOC", steps=("place",)), _case("resting", steps=("place",))],
        )

    assert server.requests == []


@pytest.mark.parametrize(
    "case",
    [
        _case("exceed", quantity="exceed_funds"),
        _case("exceed", quantity="exceed_funds", market=Market.FUTURES, code="TMFJ6", price_ref=PriceRef.LIMIT_DOWN),
    ],
    ids=["far_below", "futures"],
)
def test_exceed_funds_needs_stock_at_limit_down(case: Case) -> None:
    with pytest.raises(ConfigError, match=r"'exceed'.*exceed_funds"):
        check_cases([case])


def test_fill_case_cannot_update_qty() -> None:
    with pytest.raises(ConfigError, match=r"'reducing'.*update_qty"):
        check_cases([_fill_case("reducing", order_type="ROD", steps=("place", "update_qty:1"), quantity=2)])


@pytest.mark.parametrize(
    "overrides",
    [
        {"action": "Sell"},
        {"price_type": "MKT"},
        {"price_ref": PriceRef.LIMIT_UP},
        {"price_ref": PriceRef.REFERENCE},
        {"steps": ("place", "update_price:limit_up", "cancel")},
    ],
    ids=["sell", "market", "limit_up", "reference", "update_to_limit_up"],
)
def test_case_not_meant_to_fill_must_rest_far_from_the_market(overrides: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match=r"'resting'.*LMT Buy"):
        check_cases([_case("resting", **overrides)])


def test_only_filter(tmp_path: Path, write_config: WriteConfig) -> None:
    server = OrderServer()
    cases = load_scenarios(_TOOL_DIR / "scenarios.toml")

    exit_code, summary = _run(tmp_path, _config(write_config), server, cases, only={"harness_probe"})

    assert exit_code == 0
    assert [case["id"] for case in summary["cases"]] == ["harness_probe"]
    assert [path for path, _ in server.mutations()] == [_PLACE_PATH, _CANCEL_PATH]


def test_unknown_only_id_rejected_before_any_request(tmp_path: Path, write_config: WriteConfig) -> None:
    server = OrderServer()

    with pytest.raises(ConfigError, match=r"no_such_case"):
        _run(tmp_path, _config(write_config), server, [_case("known")], only={"known", "no_such_case"})

    assert server.requests == []


# orders.json


def test_orders_json_content(tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    server = OrderServer(reject_place={"0050"})
    cases = [_case("lifecycle", quantity=2, steps=_LIFECYCLE_STEPS), _case("refused", market=Market.ODD, code="0050")]

    _run(tmp_path, _config(write_config), server, cases)

    path = tmp_path / "run" / ORDERS_JSON
    text = path.read_text(encoding="utf-8")
    accepted = {"status": 200, "accepted": True}
    assert json.loads(text) == {
        "base_url": "http://127.0.0.1:8080",
        "timings": {"step_wait_s": 3.0, "fill_poll_s": 2.0, "fill_timeout_s": 60.0},
        "cases": [
            {
                "id": "lifecycle",
                "trade_id": "000001",
                "close_trade_id": None,
                "steps": [
                    {"step": "place", "op": "place_stock", **accepted},
                    {"step": "update_price:far_below_plus_tick", "op": "update_price", **accepted},
                    {"step": "update_qty:1", "op": "update_qty", **accepted},
                    {"step": "cancel", "op": "cancel", **accepted},
                ],
                "outcome": "completed",
                "reason": None,
            },
            {
                "id": "refused",
                "trade_id": None,
                "close_trade_id": None,
                "steps": [{"step": "place", "op": "place_stock", "status": 422, "accepted": False}],
                "outcome": "rejected",
                "reason": "place rejected with HTTP 422",
            },
        ],
    }
    assert list(json.loads(text)["cases"][0]) == ["id", "trade_id", "close_trade_id", "steps", "outcome", "reason"]
    assert not path.with_name(f"{ORDERS_JSON}.tmp").exists()
    (summary,) = [m for m in _messages(caplog, logging.INFO) if m.startswith("orders: ")]
    assert summary == f"orders: 2 cases, 1 completed, 1 rejected; summary in {path}"
    for value in _ACCOUNT_IDENTIFIERS:
        assert value not in text
        assert value not in caplog.text


# Scenario and config files


def test_repo_scenarios_follow_the_plan() -> None:
    cases = load_scenarios(_TOOL_DIR / "scenarios.toml")
    check_cases(cases)
    by_id = {case.id: case for case in cases}

    assert len(cases) == 34
    assert {case.code for case in cases} == {"2890", "00981A", "TMFJ6"}
    market_cases = [case for case in cases if case.price_type != "LMT"]
    assert len(market_cases) == 18
    assert all(case.expect_fill and case.quantity == 1 for case in market_cases)
    assert [
        len(by_id[i].custom_field or "")
        for i in ("custom_field_base62_6", "custom_field_base62_7", "custom_field_node_hash")
    ] == [6, 7, 6]
    assert by_id["exceed_funds_stock_2890"].quantity == "exceed_funds"
    assert by_id["lifecycle_futures_tmf"].steps == _LIFECYCLE_STEPS
    assert by_id["harness_probe"].steps == ("place", "cancel")


def test_repo_p1_toml_orders_section() -> None:
    cfg = load_config(_TOOL_DIR / "p1.toml")

    assert cfg.orders.trade_id_pointer == "/order/id"
    assert set(cfg.orders.body_templates) == {"place_stock", "place_futures", "update_price", "update_qty", "cancel"}


def test_order_markets_match_scenario_markets() -> None:
    assert set(ORDER_MARKETS) == {market.value for market in Market}


def _with_orders_line(prefix: str, line: str) -> list[str]:
    lines = list(MINIMAL_CONFIG_LINES)
    (index,) = [i for i, entry in enumerate(lines) if entry.startswith(prefix)]
    lines[index] = line
    return lines


@pytest.mark.parametrize("key", ["account_id", "person_id", "username"])
def test_template_naming_account_identifier_rejected(write_config: WriteConfig, key: str) -> None:
    line = f"""update_price = '{{"trade_id": "{{trade_id}}", "price": "{{price}}", "x": [{{"{key}": "A1"}}]}}'"""

    with pytest.raises(ConfigError, match=rf"orders\.body_templates\.update_price must not name the account \({key}\)"):
        load_config(write_config(_with_orders_line("update_price = ", line)))


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("""cancel = '{"trade_id": "id-{trade_id}"}'""", "whole string value"),
        ("""cancel = '{"trade_id": "{trade_id}", "p": "{price}"}'""", "unknown placeholders: price"),
        ("""cancel = '{"id": "x"}'""", "lacks placeholders: trade_id"),
        ("""cancel = '["{trade_id}"]'""", "must be a JSON object"),
    ],
)
def test_template_placeholders_validated(write_config: WriteConfig, line: str, message: str) -> None:
    with pytest.raises(ConfigError, match=rf"orders\.body_templates\.cancel.*{message}"):
        load_config(write_config(_with_orders_line("cancel = ", line)))


@pytest.mark.parametrize(
    ("prefix", "line", "key"),
    [
        ("step_wait_s", "step_wait_s = 0", r"orders\.step_wait_s"),
        ("fill_timeout_s", "fill_timeout_s = 2", r"orders\.fill_timeout_s"),
        ("filled_pointer", 'filled_pointer = "status"', r"orders\.filled_pointer"),
        ("futures = ", "", r"orders\.exchange"),
        ("futures = ", 'futures = "CME"', r"orders\.exchange\.futures must be one of"),
        ("step_wait_s", "step_wait_s = 3\nstep_wait = 3", r"unknown config key: orders\.step_wait\b"),
        ("trade_id_pointer", 'trade_id_pointer = "/order/account/account_id"', r"orders\.trade_id_pointer.*account_id"),
        ("deal_quantity_pointer", 'deal_quantity_pointer = "/x/person_id"', r"orders\.deal_quantity_pointer"),
    ],
)
def test_orders_settings_validated(write_config: WriteConfig, prefix: str, line: str, key: str) -> None:
    with pytest.raises(ConfigError, match=key):
        load_config(write_config(_with_orders_line(prefix, line)))


def test_trades_endpoint_must_be_post(write_config: WriteConfig) -> None:
    lines = list(MINIMAL_CONFIG_LINES)
    lines[lines.index("[endpoints.trades]") + 1] = 'method = "GET"'

    with pytest.raises(ConfigError, match=r"endpoints\.trades"):
        load_config(write_config(lines))


# CLI


def test_cli_passes_only_and_base_url(
    tmp_path: Path, write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_secret_env(monkeypatch)
    config_path = write_config([*MINIMAL_CONFIG_LINES, *ENDPOINT_LINES])
    seen: list[tuple[str, set[str] | None, int]] = []

    def fake_run_orders(cfg: P1Config, cases: list[Case], _run_dir: Path, only: set[str] | None) -> int:
        seen.append((cfg.base_url, only, len(cases)))
        return 0

    monkeypatch.setattr(cli, "run_orders", fake_run_orders)

    exit_code = main(
        [
            "orders",
            "--config",
            str(config_path),
            "--scenarios",
            str(_TOOL_DIR / "scenarios.toml"),
            "--only",
            "harness_probe, lifecycle_stock_2890",
            "--base-url",
            "http://127.0.0.1:18081",
        ]
    )

    assert exit_code == 0
    assert seen == [("http://127.0.0.1:18081", {"harness_probe", "lifecycle_stock_2890"}, 34)]


@pytest.mark.parametrize("args", [["--only", "a,,b"], ["--base-url", "127.0.0.1:18081"]])
def test_cli_rejects_malformed_arguments(args: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["orders", *args])

    assert exc_info.value.code == 1


def test_cli_unknown_only_id_exits_1(
    tmp_path: Path, write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _set_secret_env(monkeypatch)
    config_path = write_config([*MINIMAL_CONFIG_LINES, *ENDPOINT_LINES])

    exit_code = main(
        ["orders", "--config", str(config_path), "--scenarios", str(_TOOL_DIR / "scenarios.toml"), "--only", "nope"]
    )

    assert exit_code == 1
    assert any("nope" in message for message in _messages(caplog, logging.ERROR))
