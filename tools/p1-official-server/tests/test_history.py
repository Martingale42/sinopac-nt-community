"""Tests for p1rec.history: history queries and their quota cost."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypedDict, cast

import httpx
import pytest

from p1rec.config import (
    ContractFields,
    Endpoint,
    EventFields,
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
        contracts=ContractFields(),
        funds=FundsConfig(),
        stream=StreamConfig(),
        orders=OrdersConfig(),
        events=EventFields(),
        history=HistoryConfig(queries=queries, usage_bytes_pointer="/bytes"),
        scrub=ScrubConfig(),
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
    order, so a test can hand the recorder-known "before" and "after"
    readings for the single query under test. query_responses maps a
    request path to the fixed response returned for every call to it.
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
    body: str
    status: int | None
    response_bytes: int | None
    usage_before: int | None
    usage_after: int | None
    usage_delta: int | None


class _HistoryJson(TypedDict):
    queries: list[_QueryEntry]
    failed_count: int


def _history_json(run_dir: Path) -> _HistoryJson:
    raw = json.loads((run_dir / "history.json").read_text(encoding="utf-8"))
    return cast(_HistoryJson, raw)


def test_usage_delta_recorded(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path, (_TICKS_QUERY,))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    handler = _queue_handler(
        iter([100, 250]), {_TICKS_PATH: httpx.Response(200, content=b'{"ticks": []}')}
    )

    exit_code = run_history(cfg, run_dir, transport=httpx.MockTransport(handler))

    assert exit_code == 0
    entry = _history_json(run_dir)["queries"][0]
    assert entry["op"] == "ticks"
    assert entry["status"] == 200
    assert entry["response_bytes"] == len(b'{"ticks": []}')
    assert entry["usage_before"] == 100
    assert entry["usage_after"] == 250
    assert entry["usage_delta"] == 150


def test_missing_usage_field_null_and_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _build_cfg(tmp_path, (_TICKS_QUERY,))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    # None means _usage_body omits the "bytes" field entirely, so the
    # configured pointer has nothing to resolve.
    handler = _queue_handler(iter([None, None]), {_TICKS_PATH: httpx.Response(200, content=b'{"ticks": []}')})

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

    # Four usage calls total: before/after each of the two queries.
    handler = _queue_handler(
        iter([100, 150, 150, 200]),
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
    queries = _history_json(run_dir)["queries"]
    assert len(queries) == 2  # the second query still ran after the first failed
    assert queries[0]["op"] == "ticks"
    assert queries[0]["status"] == 500
    assert queries[1]["op"] == "kbars"
    assert queries[1]["status"] == 200
    assert queries[1]["usage_delta"] == 50
