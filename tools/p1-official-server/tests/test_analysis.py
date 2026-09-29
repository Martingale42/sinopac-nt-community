"""Tests for p1rec.records, p1rec.reconcile and p1rec.stats: order reconciliation and SSE stream statistics."""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from helpers import MINIMAL_CONFIG_LINES, SECRET_ENV_VARS, WriteConfig

from p1rec import reconcile as reconciliation
from p1rec import stats
from p1rec.cli import main
from p1rec.config import ConfigError, EventFields, load_config
from p1rec.interlock import InterlockError
from p1rec.reconcile import (
    Mismatch,
    OrderView,
    derive_status,
    orders_from_rest,
    orders_from_sse,
    reconcile,
    run_reconcile,
)
from p1rec.records import NS_PER_S, AnalysisError, read_jsonl
from p1rec.stats import percentile_nearest_rank

T0_NS = int(datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc).timestamp()) * NS_PER_S

# 2026-10-01 09:00 Taipei in epoch seconds, the exchange and order time of the fixtures
T0_S = float(T0_NS // NS_PER_S)
SECONDS_PER_DAY = 86_400
SSE_RUN = "20261001T010000Z-stream"
REST_RUN = "20261001T011000Z-orders"


@pytest.fixture
def fields(write_config: WriteConfig) -> EventFields:
    return load_config(write_config()).events


@pytest.fixture
def results_root(tmp_path: Path) -> Path:
    # MINIMAL_CONFIG_LINES sets results_root = "results" next to p1.toml
    return tmp_path / "results"


def _order(
    key: str,
    op_type: str = "New",
    op_code: str = "00",
    *,
    event_id: str,
    quantity: int = 2,
    cancel_qty: int = 0,
    exchange_ts: float = T0_S,
    state: str = "FuturesOrder",
) -> dict[str, object]:
    """Return an OrderEvent with the keys of the official server's order reports, values fake.

    The server reports the placed size in order.quantity, while a Cancel's
    status.order_quantity reads 0, so here only an accepted New carries
    the size in status.
    """
    status_order_qty = quantity if op_type == "New" and op_code == "00" else 0
    return {
        "state": state,
        "data": {
            state: {
                "event_id": event_id,
                "operation": {"op_type": op_type, "op_code": op_code, "op_msg": ""},
                "order": {
                    "id": key,
                    "seqno": "000001",
                    "ordno": "X0001",
                    "account": {"account_type": "F", "broker_id": "F000000", "account_id": "0000000", "signed": True},
                    "action": "Buy",
                    "price": 100.0,
                    "quantity": quantity,
                    "order_type": "ROD",
                    "price_type": "LMT",
                    "market_type": "Day",
                    "oc_type": "Auto",
                    "subaccount": "",
                    "combo": False,
                },
                "status": {
                    "id": key,
                    "exchange_ts": exchange_ts,
                    "modified_price": 0.0,
                    "cancel_quantity": cancel_qty,
                    "order_quantity": status_order_qty,
                    "web_id": "Z",
                },
                "contract": {"code": "TMFJ6"},
            }
        },
    }


def _deal(key: str, quantity: int, *, event_id: str, ts: float = T0_S, state: str = "FuturesDeal") -> dict[str, object]:
    """Return an OrderEvent shaped like the OpenAPI FuturesDealEvent."""
    return {
        "state": state,
        "data": {
            state: {
                "event_id": event_id,
                "trade_id": key,
                "seqno": "000001",
                "ordno": "X0001",
                "exchange_seq": "1",
                "action": "Buy",
                "code": "TMFJ6",
                "price": 100.0,
                "quantity": quantity,
                "security_type": "FUT",
                "ts": ts,
            }
        },
    }


def _frame(client: str, stream: str, ts_ns: int, event: str, data: object) -> dict[str, object]:
    text = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    return {
        "client": client,
        "stream": stream,
        "kind": "frame",
        "recv_ts_ns": ts_ns,
        "raw": f"event:{event}\ndata:{text}\n\n",
    }


def _lifecycle(client: str, stream: str, ts_ns: int, *, kind: str, **extra: object) -> dict[str, object]:
    return {"client": client, "stream": stream, "kind": kind, "recv_ts_ns": ts_ns, **extra}


def _order_frames(client: str, events: list[dict[str, object]], stream: str = "order_event") -> list[dict[str, object]]:
    return [_frame(client, stream, T0_NS + i * NS_PER_S, "order_event", event) for i, event in enumerate(events)]


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _write_sse(run_dir: Path, client: str, stream: str, records: list[dict[str, object]]) -> Path:
    return _write_jsonl(run_dir / f"sse_{client}_{stream}.jsonl", records)


def _trade(
    key: str, status: str, deal_quantity: int | None, order_quantity: int = 2, order_ts: float | None = T0_S
) -> dict[str, object]:
    """Return a Trade shaped like the official server's trades list items."""
    return {
        "contract": {"code": "TMFJ6"},
        "order": {"id": key, "seqno": "000001", "ordno": "X0001", "action": "Buy", "quantity": order_quantity},
        "status": {
            "id": key,
            "status": status,
            "status_code": "00",
            "deal_quantity": deal_quantity,
            "cancel_quantity": 0,
            "order_quantity": order_quantity,
            "order_ts": order_ts,
        },
    }


def _trades_record(trades: object, status: int = 200) -> dict[str, object]:
    return {
        "client": "orders",
        "op": "trades",
        "method": "POST",
        "url": "http://127.0.0.1:8080/api/v1/order/trades",
        "req_body": '{"refresh":true}',
        "sent_ts_ns": T0_NS,
        "recv_ts_ns": T0_NS,
        "status": status,
        "resp_headers": [],
        "resp_body": json.dumps(trades),
    }


# Three orders both clients see: one working, one cancelled and one
# filled by two deals.
_EVENTS = [
    _order("t1", event_id="e1"),
    _order("t2", event_id="e2"),
    _order("t2", "Cancel", event_id="e3", cancel_qty=2),
    _order("t3", event_id="e4"),
    _deal("t3", 1, event_id="e5"),
    _deal("t3", 1, event_id="e6"),
]
_MATCHING_TRADES = [_trade("t1", "Submitted", 0), _trade("t2", "Cancelled", 0), _trade("t3", "Filled", 2)]


def _set_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")


def _only_new_run(results_root: Path, subcommand: str) -> Path:
    (run_dir,) = [path for path in results_root.iterdir() if path.name.endswith(f"-{subcommand}")]
    return run_dir


def _run_cli_reconcile(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, results_root: Path, *extra_args: str
) -> tuple[int, dict[str, object]]:
    _set_secret_env(monkeypatch)
    args = ["reconcile", "--config", str(write_config()), "--sse-run", SSE_RUN, "--rest-run", REST_RUN, *extra_args]
    exit_code = main(args)
    report = json.loads((_only_new_run(results_root, "reconcile") / "reconcile.json").read_text(encoding="utf-8"))
    return exit_code, report


def test_all_match_exit_0(write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, results_root: Path) -> None:
    for client in ("a", "b"):
        _write_sse(results_root / SSE_RUN, client, "order_event", _order_frames(client, _EVENTS))
    _write_jsonl(results_root / REST_RUN / "rest.jsonl", [_trades_record(_MATCHING_TRADES)])

    exit_code, report = _run_cli_reconcile(write_config, monkeypatch, results_root)

    assert exit_code == 0
    assert report["only_in_rest"] == []
    assert report["only_in_sse"] == []
    assert report["mismatched"] == []
    assert report["missing_by_client"] == {"a": [], "b": []}
    assert report["per_client_keys"] == {"a": ["t1", "t2", "t3"], "b": ["t1", "t2", "t3"]}


def test_missing_in_sse_reported(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, results_root: Path
) -> None:
    for client in ("a", "b"):
        _write_sse(results_root / SSE_RUN, client, "order_event", _order_frames(client, _EVENTS))
    trades = [*_MATCHING_TRADES, _trade("t4", "Submitted", 0)]
    _write_jsonl(results_root / REST_RUN / "rest.jsonl", [_trades_record(trades)])

    exit_code, report = _run_cli_reconcile(write_config, monkeypatch, results_root)

    assert exit_code == 2
    assert report["only_in_rest"] == ["t4"]
    assert report["only_in_sse"] == []
    assert report["mismatched"] == []
    assert report["missing_by_client"] == {"a": ["t4"], "b": ["t4"]}


def test_status_mismatch_reported(tmp_path: Path, fields: EventFields) -> None:
    events = [_order("t1", event_id="e1"), _deal("t1", 1, event_id="e2")]
    path = _write_sse(tmp_path, "a", "order_event", _order_frames("a", events))

    sse = orders_from_sse({"a": [path]}, fields)
    rest = orders_from_rest([_trade("t1", "Filled", 2)], fields)
    report = reconcile(sse, rest)

    assert report.mismatched == [
        Mismatch(
            client="a",
            sse=OrderView(key="t1", status="PartFilled", filled_qty=Decimal(1)),
            rest=OrderView(key="t1", status="Filled", filled_qty=Decimal(2)),
        )
    ]
    assert not report.clean


def test_client_coverage_difference(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, results_root: Path
) -> None:
    without_t2 = [event for event in _EVENTS if "t2" not in json.dumps(event)]
    _write_sse(results_root / SSE_RUN, "a", "order_event", _order_frames("a", _EVENTS))
    _write_sse(results_root / SSE_RUN, "b", "order_event", _order_frames("b", without_t2))
    _write_jsonl(results_root / REST_RUN / "rest.jsonl", [_trades_record(_MATCHING_TRADES)])

    exit_code, report = _run_cli_reconcile(write_config, monkeypatch, results_root)

    # A report one client missed fails even though another client saw it
    assert exit_code == 2
    assert report["only_in_rest"] == ["t2"]
    assert report["missing_by_client"] == {"a": [], "b": ["t2"]}
    keys = report["per_client_keys"]
    assert isinstance(keys, dict)
    assert set(keys["a"]) - set(keys["b"]) == {"t2"}


def test_no_client_misses_every_rest_order(fields: EventFields) -> None:
    report = reconcile({}, orders_from_rest(_MATCHING_TRADES, fields))

    assert report.only_in_rest == ["t1", "t2", "t3"]
    assert report.missing_by_client == {}


def test_deals_accumulate_qty(tmp_path: Path, fields: EventFields) -> None:
    events = [
        _order("t1", event_id="e1", quantity=3),
        _deal("t1", 1, event_id="e2"),
        _deal("t1", 2, event_id="e3"),
    ]
    path = _write_sse(tmp_path, "a", "order_event", _order_frames("a", events))

    views = orders_from_sse({"a": [path]}, fields)

    assert views["a"].orders["t1"] == OrderView(key="t1", status="Filled", filled_qty=Decimal(3))


def test_stats_peak_second_and_gaps(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, results_root: Path
) -> None:
    heartbeat = {"type": "heartbeat", "connection_id": "1"}
    tick = {"code": "TMFJ6", "close": "100"}
    burst_second_ns = T0_NS + 61 * NS_PER_S
    records = [_frame("a", "data", T0_NS + i * 30 * NS_PER_S, "heartbeat", heartbeat) for i in range(3)]
    records += [_frame("a", "data", burst_second_ns + i * 1_000_000, "tick_fop", tick) for i in range(50)]
    last_burst_ns = burst_second_ns + 49 * 1_000_000
    records.append(_frame("a", "data", last_burst_ns + 90 * NS_PER_S, "heartbeat", heartbeat))
    _write_sse(results_root / SSE_RUN, "a", "data", records)
    _set_secret_env(monkeypatch)

    exit_code = main(["stats", "--config", str(write_config()), "--sse-run", SSE_RUN])

    assert exit_code == 0
    stats = json.loads((_only_new_run(results_root, "stats") / "stats.json").read_text(encoding="utf-8"))
    file_stats = stats["files"]["sse_a_data.jsonl"]
    assert file_stats["peak_seconds"][0] == {"second": "2026-10-01T01:01:01Z", "frames": 50}
    assert len(file_stats["peak_seconds"]) == 5  # the burst second plus four one-heartbeat seconds
    assert file_stats["gaps"] == [
        {"from": "2026-10-01T01:01:01.049000Z", "to": "2026-10-01T01:02:31.049000Z", "gap_s": 90.0}
    ]
    assert stats["clients"]["a"]["event_counts"] == {"heartbeat": 4, "tick_fop": 50}
    assert file_stats["heartbeat_interval_s"] == {"n": 3, "min": 30.0, "median": 30.0, "max": pytest.approx(91.049)}


@pytest.mark.parametrize(
    ("new_ok", "cancel_ok", "order_qty", "cancel_qty", "filled_qty", "expected"),
    [
        (False, False, 2, 0, 0, "Failed"),
        (True, False, 2, 0, 0, "Submitted"),
        (True, False, 2, 0, 1, "PartFilled"),
        (True, False, 2, 0, 2, "Filled"),
        (True, True, 2, 2, 0, "Cancelled"),
        (True, True, 3, 2, 1, "Cancelled"),  # partly filled, remainder cancelled
        (True, False, 3, 1, 1, "PartFilled"),  # quantity reduced without a Cancel
        (True, True, 3, 1, 0, "Submitted"),  # a Cancel that left quantity working
        (None, False, None, 0, 1, "Unknown"),  # deals seen, order events missed
        (None, False, 2, 0, 0, "Unknown"),  # quantity known but no New seen
        (True, False, 0, 0, 0, "Submitted"),  # a zero quantity alone never reads Filled
        (None, True, 2, 2, 0, "Unknown"),  # a Cancel seen without its New
        (None, False, 2, 0, 2, "Unknown"),  # deals seen without the New
    ],
)
def test_derive_status_rules(
    new_ok: bool | None, cancel_ok: bool, order_qty: int | None, cancel_qty: int, filled_qty: int, expected: str
) -> None:
    status = derive_status(
        new_ok=new_ok,
        cancel_ok=cancel_ok,
        order_qty=None if order_qty is None else Decimal(order_qty),
        cancel_qty=Decimal(cancel_qty),
        filled_qty=Decimal(filled_qty),
    )

    assert status == expected


def test_partial_fill_then_cancel_is_cancelled(tmp_path: Path, fields: EventFields) -> None:
    events = [
        _order("t1", event_id="e1", quantity=3),
        _deal("t1", 1, event_id="e2"),
        _order("t1", "Cancel", event_id="e3", quantity=3, cancel_qty=2),
    ]
    path = _write_sse(tmp_path, "a", "order_event", _order_frames("a", events))

    views = orders_from_sse({"a": [path]}, fields)

    assert views["a"].orders["t1"] == OrderView(key="t1", status="Cancelled", filled_qty=Decimal(1))


def test_rejected_update_qty_does_not_change_size(tmp_path: Path, fields: EventFields) -> None:
    events = [
        _order("t1", event_id="e1", quantity=3),
        _deal("t1", 1, event_id="e2"),
        _order("t1", "UpdateQty", "88", event_id="e3", quantity=1),
    ]
    path = _write_sse(tmp_path, "a", "order_event", _order_frames("a", events))

    views = orders_from_sse({"a": [path]}, fields)

    assert views["a"].orders["t1"] == OrderView(key="t1", status="PartFilled", filled_qty=Decimal(1))


def test_two_day_sse_against_one_day_rest_snapshot(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, results_root: Path
) -> None:
    yesterday = T0_S - SECONDS_PER_DAY
    earlier = [_order("t0", event_id="e0", exchange_ts=yesterday), _deal("t0", 2, event_id="d0", ts=yesterday)]
    for client in ("a", "b"):
        _write_sse(results_root / SSE_RUN, client, "order_event", _order_frames(client, [*earlier, *_EVENTS]))
    _write_jsonl(results_root / REST_RUN / "rest.jsonl", [_trades_record(_MATCHING_TRADES)])

    exit_code, report = _run_cli_reconcile(write_config, monkeypatch, results_root)

    assert exit_code == 0
    assert report["trading_day"] == "2026-10-01"
    assert report["excluded_other_days"] == {"a": 1, "b": 1}
    assert report["per_client_keys"] == {"a": ["t1", "t2", "t3"], "b": ["t1", "t2", "t3"]}
    assert report["missing_events_by_client"] == {"a": [], "b": []}


def test_trading_day_required_without_rest_order_times(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, results_root: Path
) -> None:
    for client in ("a", "b"):
        _write_sse(results_root / SSE_RUN, client, "order_event", _order_frames(client, _EVENTS))
    undated = [_trade("t1", "Submitted", 0, order_ts=None), _trade("t2", "Cancelled", 0, order_ts=None)]
    undated.append(_trade("t3", "Filled", 2, order_ts=None))
    _write_jsonl(results_root / REST_RUN / "rest.jsonl", [_trades_record(undated)])
    _set_secret_env(monkeypatch)
    config = str(write_config())
    base_args = ["reconcile", "--config", config, "--sse-run", SSE_RUN, "--rest-run", REST_RUN]

    assert main(base_args) == 1
    assert main([*base_args, "--trading-day", "2026-10-01", "--tag", "dated"]) == 0


def test_rest_snapshot_spanning_two_days_needs_trading_day(fields: EventFields) -> None:
    trades = [_trade("t1", "Submitted", 0), _trade("t0", "Filled", 2, order_ts=T0_S - SECONDS_PER_DAY)]

    with pytest.raises(AnalysisError, match=r"2026-09-30, 2026-10-01"):
        reconciliation.trading_day_of(trades, fields)


@pytest.mark.parametrize("value", ["2026-1-01", "20261001", "2026-02-30"])
def test_trading_day_flag_rejects_bad_dates(write_config: WriteConfig, value: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["reconcile", "--config", str(write_config()), "--sse-run", SSE_RUN, "--trading-day", value])

    assert exc_info.value.code == 1


def test_client_missing_reports_fails(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, results_root: Path
) -> None:
    new = _order("t1", event_id="e1")
    update_price = _order("t1", "UpdatePrice", event_id="e2")
    cancel = _order("t1", "Cancel", event_id="e3", cancel_qty=2)
    _write_sse(results_root / SSE_RUN, "a", "order_event", _order_frames("a", [new, update_price, cancel]))
    _write_sse(results_root / SSE_RUN, "b", "order_event", _order_frames("b", [cancel]))
    _write_jsonl(results_root / REST_RUN / "rest.jsonl", [_trades_record([_trade("t1", "Cancelled", 0)])])

    exit_code, report = _run_cli_reconcile(write_config, monkeypatch, results_root)

    assert exit_code == 2
    assert report["missing_events_by_client"] == {"a": [], "b": ["e1", "e2"]}
    assert report["mismatched"] == [
        {
            "client": "b",
            "key": "t1",
            "sse": {"status": "Unknown", "filled_qty": "0"},
            "rest": {"status": "Cancelled", "filled_qty": "0"},
        }
    ]
    assert report["only_in_rest"] == []


# Candidate shapes of New(2) -> UpdatePrice -> UpdateQty to 1 -> Cancel,
# as (UpdateQty's order.quantity, its cancel_quantity, Cancel's
# order.quantity, its cancel_quantity).
@pytest.mark.parametrize(
    ("update_qty_size", "update_qty_cancel", "cancel_size", "cancel_cancel", "expected"),
    [
        (1, 1, 1, 2, "Cancelled"),  # the assumed shape: reduced size, cumulative cancel_quantity
        (2, 1, 2, 2, "Cancelled"),  # placed size kept, cumulative cancel_quantity
        (2, 1, 2, 1, "Submitted"),  # placed size kept, per-operation cancel_quantity: a known gap
    ],
)
def test_lifecycle_case_under_candidate_shapes(
    tmp_path: Path,
    fields: EventFields,
    update_qty_size: int,
    update_qty_cancel: int,
    cancel_size: int,
    cancel_cancel: int,
    expected: str,
) -> None:
    events = [
        _order("t1", event_id="e1", quantity=2),
        _order("t1", "UpdatePrice", event_id="e2", quantity=2),
        _order("t1", "UpdateQty", event_id="e3", quantity=update_qty_size, cancel_qty=update_qty_cancel),
        _order("t1", "Cancel", event_id="e4", quantity=cancel_size, cancel_qty=cancel_cancel),
    ]
    path = _write_sse(tmp_path, "a", "order_event", _order_frames("a", events))

    views = orders_from_sse({"a": [path]}, fields)

    assert views["a"].orders["t1"] == OrderView(key="t1", status=expected, filled_qty=Decimal(0))


def test_reduced_order_filling_its_remainder_is_filled(tmp_path: Path, fields: EventFields) -> None:
    events = [
        _order("t1", event_id="e1", quantity=2),
        _order("t1", "UpdateQty", event_id="e2", quantity=1, cancel_qty=1),
        _deal("t1", 1, event_id="e3"),
    ]
    path = _write_sse(tmp_path, "a", "order_event", _order_frames("a", events))

    views = orders_from_sse({"a": [path]}, fields)

    assert views["a"].orders["t1"] == OrderView(key="t1", status="Filled", filled_qty=Decimal(1))


def test_rejected_new_matches_prefixed_rest_status(tmp_path: Path, fields: EventFields) -> None:
    path = _write_sse(tmp_path, "a", "order_event", _order_frames("a", [_order("t1", op_code="88", event_id="e1")]))
    rest = orders_from_rest([_trade("t1", "Status.Failed", None)], fields)

    report = reconcile(orders_from_sse({"a": [path]}, fields), rest)

    assert report.clean


def test_event_on_two_streams_counted_once(tmp_path: Path, fields: EventFields) -> None:
    events = [_order("t1", event_id="e1"), _deal("t1", 1, event_id="e2")]
    paths = [
        _write_sse(tmp_path, "a", "data", _order_frames("a", events, stream="data")),
        _write_sse(tmp_path, "a", "order_event", _order_frames("a", events)),
    ]

    views = orders_from_sse({"a": paths}, fields)

    assert views["a"].orders["t1"] == OrderView(key="t1", status="PartFilled", filled_qty=Decimal(1))


def test_duplicate_across_streams_keeps_earliest_arrival(tmp_path: Path, fields: EventFields) -> None:
    update = _order("t1", "UpdateQty", event_id="e2", quantity=1, cancel_qty=1)
    order_stream = [
        _frame("a", "order_event", T0_NS, "order_event", _order("t1", event_id="e1")),
        _frame("a", "order_event", T0_NS + 30 * NS_PER_S, "order_event", update),
    ]
    data_stream = [_frame("a", "data", T0_NS + NS_PER_S, "order_event", update)]
    paths = [_write_sse(tmp_path, "a", "order_event", order_stream), _write_sse(tmp_path, "a", "data", data_stream)]

    reports = reconciliation._reports(paths, fields)

    assert [(report.identity, report.recv_ts_ns) for report in reports] == [
        ("e1", T0_NS),
        ("e2", T0_NS + NS_PER_S),
    ]
    assert reports[1].where.startswith(str(paths[1]))


def test_repeated_event_id_with_other_content_warns(
    tmp_path: Path, fields: EventFields, caplog: pytest.LogCaptureFixture
) -> None:
    events = [_order("t1", event_id="e1", quantity=2), _order("t1", event_id="e1", quantity=3)]
    path = _write_sse(tmp_path, "a", "order_event", _order_frames("a", events))

    with caplog.at_level(logging.WARNING, logger="p1rec.reconcile"):
        views = orders_from_sse({"a": [path]}, fields)

    assert "event_id e1 repeats" in caplog.text
    assert views["a"].orders["t1"] == OrderView(key="t1", status="Submitted", filled_qty=Decimal(0))
    assert views["a"].event_ids == frozenset({"e1"})


def test_update_price_keeps_status_and_size(
    tmp_path: Path, fields: EventFields, caplog: pytest.LogCaptureFixture
) -> None:
    events = [_order("t1", event_id="e1", quantity=2), _order("t1", "UpdatePrice", event_id="e2", quantity=2)]
    path = _write_sse(tmp_path, "a", "order_event", _order_frames("a", events))

    with caplog.at_level(logging.WARNING, logger="p1rec.reconcile"):
        views = orders_from_sse({"a": [path]}, fields)

    assert views["a"].orders["t1"] == OrderView(key="t1", status="Submitted", filled_qty=Decimal(0))
    assert views["a"].event_ids == frozenset({"e1", "e2"})
    assert caplog.text == ""


def test_mismatch_in_one_client_only(tmp_path: Path, fields: EventFields) -> None:
    new = _order("t1", event_id="e1")
    deals = [_deal("t1", 1, event_id="e2"), _deal("t1", 1, event_id="e3")]
    paths = {
        "a": [_write_sse(tmp_path, "a", "order_event", _order_frames("a", [new, deals[0]]))],
        "b": [_write_sse(tmp_path, "b", "order_event", _order_frames("b", [new, *deals]))],
    }

    report = reconcile(orders_from_sse(paths, fields), orders_from_rest([_trade("t1", "Filled", 2)], fields))

    assert report.mismatched == [
        Mismatch(
            client="a",
            sse=OrderView(key="t1", status="PartFilled", filled_qty=Decimal(1)),
            rest=OrderView(key="t1", status="Filled", filled_qty=Decimal(2)),
        )
    ]
    assert report.missing_events_by_client == {"a": ["e3"], "b": []}
    assert report.only_in_rest == []


def test_trades_item_without_deal_quantity_counts_zero(fields: EventFields) -> None:
    item = _trade("t1", "Submitted", 0)
    status = item["status"]
    assert isinstance(status, dict)
    del status["deal_quantity"]

    assert orders_from_rest([item], fields) == {"t1": OrderView(key="t1", status="Submitted", filled_qty=Decimal(0))}


def test_truncated_last_line_skipped_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = _write_jsonl(tmp_path / "x.jsonl", [{"n": 1}, {"n": 2}])
    with path.open("ab") as fh:
        fh.write(b'{"n": 3, "raw": "ev' + b"\0" * 8)

    with caplog.at_level(logging.WARNING, logger="p1rec.records"):
        records = [record for _, record in read_jsonl(path)]

    assert records == [{"n": 1}, {"n": 2}]
    assert "x.jsonl:3: skipping unreadable last line" in caplog.text


def test_unreadable_middle_line_raises(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    path.write_text('{"n": 1}\n{"n": \n{"n": 3}\n', encoding="utf-8")

    with pytest.raises(AnalysisError, match=r"x\.jsonl:2"):
        list(read_jsonl(path))


def test_rest_run_uses_last_trades_response(tmp_path: Path, fields: EventFields) -> None:
    rest_log = _write_jsonl(
        tmp_path / "rest.jsonl",
        [
            _trades_record([_trade("old", "Submitted", 0)]),
            {"client": "orders", "op": "trades", "sent_ts_ns": T0_NS, "error_ts_ns": T0_NS, "error": "ReadTimeout"},
            _trades_record([_trade("new", "Submitted", 0)]),
            {"client": "orders", "op": "cancel", "status": 200, "resp_body": "{}"},
        ],
    )

    payload = reconciliation.last_trades_payload(rest_log)

    assert set(orders_from_rest(payload, fields)) == {"new"}


def _live_transport(simulation: bool, trades: object, seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and request.url.path == "/api/v1/info":
            return httpx.Response(200, json={"simulation": simulation})
        if request.method == "POST" and request.url.path == "/api/v1/order/trades":
            return httpx.Response(200, json=trades)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _live_cfg_lines() -> list[str]:
    return [*MINIMAL_CONFIG_LINES, "", "[endpoints.trades]", 'method = "POST"', 'path = "/api/v1/order/trades"']


def test_last_trades_payload_rejects_non_2xx(tmp_path: Path) -> None:
    rest_log = _write_jsonl(
        tmp_path / "rest.jsonl",
        [_trades_record([_trade("old", "Submitted", 0)]), _trades_record({"error": "busy"}, status=503)],
    )

    with pytest.raises(AnalysisError, match=r"HTTP 503"):
        reconciliation.last_trades_payload(rest_log)


def test_last_trades_payload_decodes_base64_body(tmp_path: Path, fields: EventFields) -> None:
    record = _trades_record([])
    del record["resp_body"]
    record["resp_body_b64"] = base64.b64encode(json.dumps([_trade("t1", "Submitted", 0)]).encode()).decode()
    rest_log = _write_jsonl(tmp_path / "rest.jsonl", [record])

    payload = reconciliation.last_trades_payload(rest_log)

    assert set(orders_from_rest(payload, fields)) == {"t1"}


def test_live_reconcile_checks_interlock_then_records_trades(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = load_config(write_config(_live_cfg_lines()))
    for client in ("a", "b"):
        _write_sse(cfg.results_root / SSE_RUN, client, "order_event", _order_frames(client, _EVENTS))
    run_dir = tmp_path / "reconcile-run"
    run_dir.mkdir()
    seen: list[httpx.Request] = []

    exit_code = run_reconcile(cfg, SSE_RUN, None, run_dir, transport=_live_transport(True, _MATCHING_TRADES, seen))

    assert exit_code == 0
    assert [request.url.path for request in seen] == ["/api/v1/info", "/api/v1/order/trades"]
    assert json.loads(seen[1].content) == {"refresh": True}
    recorded = [record for _, record in read_jsonl(run_dir / "rest.jsonl")]
    assert [(record["client"], record["op"]) for record in recorded] == [("reconcile", "mode"), ("reconcile", "trades")]
    assert json.loads((run_dir / "reconcile.json").read_text(encoding="utf-8"))["rest_run"] is None


def test_live_reconcile_aborts_before_trades_when_not_simulation(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = load_config(write_config(_live_cfg_lines()))
    _write_sse(cfg.results_root / SSE_RUN, "a", "order_event", _order_frames("a", _EVENTS))
    run_dir = tmp_path / "reconcile-run"
    run_dir.mkdir()
    seen: list[httpx.Request] = []

    with pytest.raises(InterlockError):
        run_reconcile(cfg, SSE_RUN, None, run_dir, transport=_live_transport(False, _MATCHING_TRADES, seen))

    assert [request.url.path for request in seen] == ["/api/v1/info"]
    assert not (run_dir / "reconcile.json").exists()


def _file_stats(path: Path, fields: EventFields) -> dict[str, object]:
    """Return the stats of the single file path, recorded by client a."""
    files = stats.stream_stats({"a": [path]}, fields)["files"]
    assert isinstance(files, dict)
    file_stats = files[path.name]
    assert isinstance(file_stats, dict)
    return file_stats


def test_heartbeat_intervals_reset_on_reconnect_and_outage_listed(tmp_path: Path, fields: EventFields) -> None:
    heartbeat = {"type": "heartbeat"}
    s = NS_PER_S
    records = [
        _lifecycle("a", "data", T0_NS, kind="connected", status=200),
        _frame("a", "data", T0_NS + 30 * s, "heartbeat", heartbeat),
        _frame("a", "data", T0_NS + 60 * s, "heartbeat", heartbeat),
        _lifecycle("a", "data", T0_NS + 70 * s, kind="disconnected", error="RemoteProtocolError"),
        _lifecycle("a", "data", T0_NS + 71 * s, kind="disconnected", error="ConnectError"),
        _lifecycle("a", "data", T0_NS + 72 * s, kind="connected", status=503),
        _lifecycle("a", "data", T0_NS + 72 * s, kind="disconnected", error="HTTP 503 response ended"),
        _lifecycle("a", "data", T0_NS + 80 * s, kind="connected", status=200),
        _frame("a", "data", T0_NS + 110 * s, "heartbeat", heartbeat),
        _frame("a", "data", T0_NS + 140 * s, "heartbeat", heartbeat),
        _lifecycle("a", "data", T0_NS + 150 * s, kind="stopped"),
    ]
    path = _write_sse(tmp_path, "a", "data", records)

    file_stats = _file_stats(path, fields)

    assert file_stats["heartbeat_interval_s"] == {"n": 2, "min": 30.0, "median": 30.0, "max": 30.0}
    assert file_stats["outages"] == [
        {
            "disconnected_at": "2026-10-01T01:01:10.000000Z",
            "reconnected_at": "2026-10-01T01:01:20.000000Z",
            "outage_s": 10.0,
            "attempts": 3,
            "first_error": "RemoteProtocolError",
        }
    ]
    assert file_stats["gaps"] == []  # 50 s across the outage stays under 60 s


def test_outage_ended_by_stop(tmp_path: Path, fields: EventFields) -> None:
    records = [
        _lifecycle("a", "data", T0_NS, kind="connected", status=200),
        _lifecycle("a", "data", T0_NS + 5 * NS_PER_S, kind="disconnected", error="ReadTimeout"),
        _lifecycle("a", "data", T0_NS + 6 * NS_PER_S, kind="stopped"),
    ]
    path = _write_sse(tmp_path, "a", "data", records)

    outages = _file_stats(path, fields)["outages"]

    assert outages == [
        {
            "disconnected_at": "2026-10-01T01:00:05.000000Z",
            "reconnected_at": None,
            "outage_s": None,
            "attempts": 1,
            "first_error": "ReadTimeout",
        }
    ]


def test_latency_from_taipei_date_and_time(tmp_path: Path, fields: EventFields) -> None:
    # 09:00:00.000 Taipei is T0 (01:00:00Z), and tick i arrives (i + 1) * 5 ms after it
    records = [
        _frame(
            "a",
            "tick_fop",
            T0_NS + (i + 1) * 5_000_000,
            "tick_fop",
            {"code": "TMFJ6", "date": "2026-10-01", "time": "09:00:00.000000"},
        )
        for i in range(100)
    ]
    records.append(_frame("a", "tick_fop", T0_NS, "tick_fop", {"code": "TMFJ6", "date": "2026-10-01", "time": "bad"}))
    records.append(_frame("a", "tick_fop", T0_NS, "heartbeat", {"type": "heartbeat"}))
    path = _write_sse(tmp_path, "a", "tick_fop", records)

    latency = _file_stats(path, fields)["latency_s"]

    assert latency == {
        "n": 100,
        "p50": pytest.approx(0.25),
        "p99": pytest.approx(0.495),
        "max": pytest.approx(0.5),
        "unparsable": 1,
    }


def test_exchange_ts_numeric_epoch_seconds(fields: EventFields) -> None:
    numeric = dataclasses.replace(fields, exchange_ts_pointers=("/status/exchange_ts",), exchange_ts_format=None)

    assert stats.exchange_ts_ns({"status": {"exchange_ts": 1790000000.25}}, numeric) == 1790000000250000000
    assert stats.exchange_ts_ns({"status": {}}, numeric) is None


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 11)]

    assert percentile_nearest_rank(values, 50) == 5.0
    assert percentile_nearest_rank(values, 99) == 10.0
    assert percentile_nearest_rank(values, 0) == 1.0
    assert percentile_nearest_rank([7.0], 99) == 7.0
    # 99.9 as a binary float is slightly above 999/10, which would round the rank up to 1000
    assert percentile_nearest_rank([float(v) for v in range(1, 1001)], 99.9) == 999.0
    with pytest.raises(ValueError):
        percentile_nearest_rank([], 50)


def _replace_event_line(key: str, new_line: str | None) -> list[str]:
    prefix = f"{key} = "
    lines = [line for line in MINIMAL_CONFIG_LINES if not line.startswith(prefix)]
    return lines if new_line is None else [*lines, new_line]


@pytest.mark.parametrize(
    ("key", "new_line", "message"),
    [
        ("exchange_ts_format", None, r"events\.exchange_ts_format is required"),
        ("order_key", 'order_key = ""', r"events\.order_key"),
        ("gap_threshold_s", "gap_threshold_s = 0", r"events\.gap_threshold_s"),
        ("deal_states", 'deal_states = ["FuturesOrder"]', r"must not share a state"),
        ("order_event_name", None, r"events\.order_event_name"),
    ],
)
def test_events_config_rejected(write_config: WriteConfig, key: str, new_line: str | None, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(_replace_event_line(key, new_line)))


def test_events_config_optional_exchange_ts(write_config: WriteConfig) -> None:
    lines = [line for line in MINIMAL_CONFIG_LINES if not line.startswith("exchange_ts_")]

    events = load_config(write_config(lines)).events

    assert events.exchange_ts_pointers == ()
    assert events.exchange_ts_format is None
    assert events.rest_list == ""


def test_run_id_with_separator_rejected(write_config: WriteConfig) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["stats", "--config", str(write_config()), "--sse-run", "runs/elsewhere"])

    assert exc_info.value.code == 1


def test_missing_sse_run_exits_1(write_config: WriteConfig) -> None:
    assert main(["stats", "--config", str(write_config()), "--sse-run", SSE_RUN]) == 1


def test_stats_runs_without_secret_env(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, results_root: Path
) -> None:
    for var in SECRET_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    _write_sse(results_root / SSE_RUN, "a", "data", [_frame("a", "data", T0_NS, "heartbeat", {"type": "heartbeat"})])

    exit_code = main(["stats", "--config", str(write_config()), "--sse-run", SSE_RUN])

    assert exit_code == 0
    assert (_only_new_run(results_root, "stats") / "stats.json").exists()
