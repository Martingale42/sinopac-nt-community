"""Tests for p1rec.history: history queries and their quota cost."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import httpx
import pytest
from helpers import SECRET_ENV_VARS, UNUSED_EVENT_FIELDS, WriteConfig

from p1rec import capture
from p1rec.cli import main
from p1rec.config import (
    ContractFields,
    Endpoint,
    FundsConfig,
    HistoryConfig,
    HistoryQuery,
    InterlockConfig,
    OrdersConfig,
    P1Config,
    ScrubConfig,
    StreamConfig,
)
from p1rec.history import EXIT_HISTORY_QUERY_FAILED, run_history
from p1rec.interlock import EXIT_INTERLOCK_ABORT

_BASE_URL = "http://127.0.0.1:8080"
_INFO_PATH = "/api/v1/info"
_USAGE_PATH = "/api/v1/auth/usage"
_TICKS_PATH = "/api/v1/data/ticks"
_KBARS_PATH = "/api/v1/data/kbars"

_TICKS_QUERY = HistoryQuery(op="ticks", body='{"contract": {"code": "2330"}, "date": "2026-09-29"}')
_KBARS_QUERY = HistoryQuery(
    op="kbars", body='{"contract": {"code": "TMFJ6"}, "start": "2026-09-29", "end": "2026-09-29"}'
)


def _build_cfg(tmp_path: Path, queries: tuple[HistoryQuery, ...]) -> P1Config:
    return P1Config(
        base_url=_BASE_URL,
        http_timeout_s=5.0,
        results_root=tmp_path / "results",
        fixtures_root=tmp_path / "fixtures",
        secret_env={},
        endpoints={
            "mode": Endpoint(method="GET", path=_INFO_PATH),
            "usage": Endpoint(method="GET", path=_USAGE_PATH),
            "ticks": Endpoint(method="POST", path=_TICKS_PATH),
            "kbars": Endpoint(method="POST", path=_KBARS_PATH),
            "snapshots": Endpoint(method="POST", path="/api/v1/data/snapshots"),
        },
        interlock=InterlockConfig(endpoint_op="mode", pointer="/simulation"),
        contracts=ContractFields(
            code="", limit_up="", limit_down="", reference="", futures_tick="", etf_codes=()
        ),
        funds=FundsConfig(
            stock_available_op="",
            stock_available_pointer="",
            stock_report_only={},
            futures_available_pointer="",
            futures_margin_twd={},
        ),
        stream=StreamConfig(),
        orders=OrdersConfig(),
        events=UNUSED_EVENT_FIELDS,
        history=HistoryConfig(queries=queries, usage_bytes_pointer="/bytes"),
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


def _usage_body(*, bytes_used: int | None) -> bytes:
    payload: dict[str, object] = {"connections": 1, "limit_bytes": 2_000_000_000, "remaining_bytes": 999}
    if bytes_used is not None:
        payload["bytes"] = bytes_used
    return json.dumps(payload).encode("utf-8")


def _mode_response(request: httpx.Request) -> httpx.Response | None:
    if request.method == "GET" and request.url.path == _INFO_PATH:
        return httpx.Response(200, content=b'{"simulation": true}')
    return None


def _queue_handler(
    usage_values: Iterator[int | None], query_responses: dict[str, httpx.Response]
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a MockTransport handler answering mode, usage and the given query paths.

    usage_values is consumed one value per GET /api/v1/auth/usage call, in
    order: two baseline reads before the first query, then one before and
    one after each query, so a test hands as many values as that implies.
    query_responses maps a request path to the fixed response returned
    for every call to it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        mode = _mode_response(request)
        if mode is not None:
            return mode
        if request.method == "GET" and request.url.path == _USAGE_PATH:
            return httpx.Response(200, content=_usage_body(bytes_used=next(usage_values)))
        if request.url.path in query_responses:
            return query_responses[request.url.path]
        return httpx.Response(404)

    return handler


class _QueryEntry(TypedDict):
    op: str
    body: object
    status: int | None
    response_bytes: int | None
    usage_before: int | None
    usage_after: int | None
    usage_delta: int | None
    connections_before: int | None
    connections_after: int | None


class _HistoryJson(TypedDict):
    baseline_usage_before: int | None
    baseline_usage_after: int | None
    baseline_drift: int | None
    baseline_connections_before: int | None
    baseline_connections_after: int | None
    queries: list[_QueryEntry]
    failed_count: int
    interrupted: NotRequired[bool]


def _history_json(run_dir: Path) -> _HistoryJson:
    raw = json.loads((run_dir / "history.json").read_text(encoding="utf-8"))
    return cast(_HistoryJson, raw)


def test_usage_delta_recorded(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path, (_TICKS_QUERY,))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # Two neutral baseline reads (100/100), then 100/250 for the query
    handler = _queue_handler(
        iter([100, 100, 100, 250]), {_TICKS_PATH: httpx.Response(200, content=b'{"ticks": []}')}
    )

    exit_code = run_history(cfg, run_dir, transport=httpx.MockTransport(handler))

    assert exit_code == 0
    payload = _history_json(run_dir)
    assert payload["baseline_usage_before"] == 100
    assert payload["baseline_usage_after"] == 100
    assert payload["baseline_drift"] == 0
    assert payload["baseline_connections_before"] == 1
    assert payload["baseline_connections_after"] == 1
    entry = payload["queries"][0]
    assert entry["op"] == "ticks"
    assert entry["body"] == json.loads(_TICKS_QUERY.body)  # a JSON object, not the source string
    assert entry["status"] == 200
    assert entry["response_bytes"] == len(b'{"ticks": []}')
    assert entry["usage_before"] == 100
    assert entry["usage_after"] == 250
    assert entry["usage_delta"] == 150
    assert entry["connections_before"] == 1
    assert entry["connections_after"] == 1


def test_missing_usage_field_null_and_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _build_cfg(tmp_path, (_TICKS_QUERY,))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # None means _usage_body omits the "bytes" field entirely, so the
    # configured pointer has nothing to resolve, and four reads cover the
    # two baseline reads plus this query's own before/after.
    handler = _queue_handler(
        iter([None, None, None, None]), {_TICKS_PATH: httpx.Response(200, content=b'{"ticks": []}')}
    )

    with caplog.at_level(logging.WARNING, logger="p1rec.history"):
        exit_code = run_history(cfg, run_dir, transport=httpx.MockTransport(handler))

    assert exit_code == 0  # a missing usage field never fails the query itself
    entry = _history_json(run_dir)["queries"][0]
    assert entry["usage_before"] is None
    assert entry["usage_after"] is None
    assert entry["usage_delta"] is None
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_failed_query_continues_exit_2(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _build_cfg(tmp_path, (_TICKS_QUERY, _KBARS_QUERY))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # Six usage calls total: two baseline reads, then before/after each query
    handler = _queue_handler(
        iter([100, 100, 100, 150, 150, 200]),
        {
            _TICKS_PATH: httpx.Response(500, content=b'{"error": "boom"}'),
            _KBARS_PATH: httpx.Response(200, content=b'{"kbars": []}'),
        },
    )

    with caplog.at_level(logging.ERROR, logger="p1rec.history"):
        exit_code = run_history(cfg, run_dir, transport=httpx.MockTransport(handler))

    # A caller reading this exit code (a shell script, say) sees the
    # literal 2, not the name EXIT_HISTORY_QUERY_FAILED, so the constant
    # must actually equal it rather than only itself.
    assert exit_code == 2 == EXIT_HISTORY_QUERY_FAILED
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    payload = _history_json(run_dir)
    assert payload["failed_count"] == 1
    queries = payload["queries"]
    assert len(queries) == 2  # the second query still ran after the first failed
    assert queries[0]["op"] == "ticks"
    assert queries[0]["status"] == 500
    assert queries[1]["op"] == "kbars"
    assert queries[1]["status"] == 200
    assert queries[1]["usage_delta"] == 50


def test_transport_exception_counts_as_failed(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path, (_TICKS_QUERY,))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        mode = _mode_response(request)
        if mode is not None:
            return mode
        if request.method == "GET" and request.url.path == _USAGE_PATH:
            return httpx.Response(200, content=_usage_body(bytes_used=100))
        if request.url.path == _TICKS_PATH:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(404)

    exit_code = run_history(cfg, run_dir, transport=httpx.MockTransport(handler))

    assert exit_code == EXIT_HISTORY_QUERY_FAILED
    payload = _history_json(run_dir)
    assert payload["failed_count"] == 1
    entry = payload["queries"][0]
    assert entry["status"] is None
    assert entry["response_bytes"] is None


def test_interlock_abort_via_cli_writes_no_history_json(
    tmp_path: Path, write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MINIMAL_CONFIG_LINES already carries a full, valid [history] section
    # (helpers.py), so this exercises the real cli.py -> run_history wiring,
    # not a stand-in handler.
    config_path = write_config()
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == _INFO_PATH:
            return httpx.Response(200, content=b'{"simulation": false}')
        return httpx.Response(404)

    def fake_make_http_client(
        base_url: str, timeout: httpx.Timeout, *, transport: httpx.BaseTransport | None = None
    ) -> httpx.Client:
        # Ignores the real transport argument so every CapturingClient
        # built anywhere in this call, including the one run_history
        # builds itself with no override, answers from this mock instead
        # of touching the network.
        return httpx.Client(base_url=base_url, timeout=timeout, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(capture, "make_http_client", fake_make_http_client)

    exit_code = main(["history", "--config", str(config_path)])

    assert exit_code == EXIT_INTERLOCK_ABORT
    run_dirs = list((tmp_path / "results").iterdir())
    assert len(run_dirs) == 1
    assert not (run_dirs[0] / "history.json").exists()


def test_interrupted_query_writes_partial_history(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path, (_TICKS_QUERY, _KBARS_QUERY))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        mode = _mode_response(request)
        if mode is not None:
            return mode
        if request.method == "GET" and request.url.path == _USAGE_PATH:
            return httpx.Response(200, content=_usage_body(bytes_used=100))
        if request.url.path == _TICKS_PATH:
            raise KeyboardInterrupt
        return httpx.Response(404)

    with pytest.raises(KeyboardInterrupt):
        run_history(cfg, run_dir, transport=httpx.MockTransport(handler))

    payload = _history_json(run_dir)
    assert payload["interrupted"] is True
    assert payload["queries"] == []  # the first query never finished appending a result


def test_interrupted_second_query_keeps_first_result(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path, (_TICKS_QUERY, _KBARS_QUERY))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        mode = _mode_response(request)
        if mode is not None:
            return mode
        if request.method == "GET" and request.url.path == _USAGE_PATH:
            return httpx.Response(200, content=_usage_body(bytes_used=100))
        if request.url.path == _TICKS_PATH:
            return httpx.Response(200, content=b'{"ticks": []}')
        if request.url.path == _KBARS_PATH:
            raise KeyboardInterrupt
        return httpx.Response(404)

    with pytest.raises(KeyboardInterrupt):
        run_history(cfg, run_dir, transport=httpx.MockTransport(handler))

    payload = _history_json(run_dir)
    assert payload["interrupted"] is True
    queries = payload["queries"]
    assert len(queries) == 1  # the first (ticks) query finished before kbars interrupted
    assert queries[0]["op"] == "ticks"
    assert queries[0]["status"] == 200


def test_usage_drift_between_queries_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _build_cfg(tmp_path, (_TICKS_QUERY, _KBARS_QUERY))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # Baseline 100/100 (no baseline drift), then 100/150 for the first
    # query and 900/950 for the second: the second query's usage_before
    # (900) jumps far past the first query's usage_after (150), as if
    # another session on this identity used quota in between.
    handler = _queue_handler(
        iter([100, 100, 100, 150, 900, 950]),
        {
            _TICKS_PATH: httpx.Response(200, content=b'{"ticks": []}'),
            _KBARS_PATH: httpx.Response(200, content=b'{"kbars": []}'),
        },
    )

    with caplog.at_level(logging.WARNING, logger="p1rec.history"):
        exit_code = run_history(cfg, run_dir, transport=httpx.MockTransport(handler))

    assert exit_code == 0
    assert any(
        record.levelno == logging.WARNING
        and "usage_before" in record.getMessage()
        and "differs" in record.getMessage()
        for record in caplog.records
    )


def test_negative_usage_delta_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _build_cfg(tmp_path, (_TICKS_QUERY,))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # Baseline 100/100 (no baseline drift), then the query's usage_after
    # (80) is less than its usage_before (100), as if the daily counter
    # reset between the two reads.
    handler = _queue_handler(
        iter([100, 100, 100, 80]), {_TICKS_PATH: httpx.Response(200, content=b'{"ticks": []}')}
    )

    with caplog.at_level(logging.WARNING, logger="p1rec.history"):
        exit_code = run_history(cfg, run_dir, transport=httpx.MockTransport(handler))

    assert exit_code == 0
    entry = _history_json(run_dir)["queries"][0]
    assert entry["usage_delta"] == -20
    assert any(
        record.levelno == logging.WARNING and "negative" in record.getMessage() for record in caplog.records
    )


def test_baseline_drift_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _build_cfg(tmp_path, (_TICKS_QUERY,))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # The two baseline reads (100, 130) differ with no query run yet, as
    # if another session on this identity used quota in between.
    handler = _queue_handler(
        iter([100, 130, 130, 180]), {_TICKS_PATH: httpx.Response(200, content=b'{"ticks": []}')}
    )

    with caplog.at_level(logging.WARNING, logger="p1rec.history"):
        exit_code = run_history(cfg, run_dir, transport=httpx.MockTransport(handler))

    assert exit_code == 0
    payload = _history_json(run_dir)
    assert payload["baseline_drift"] == 30
    assert any(
        record.levelno == logging.WARNING and "baseline" in record.getMessage() for record in caplog.records
    )
