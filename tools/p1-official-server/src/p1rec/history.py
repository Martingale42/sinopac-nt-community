"""Run configured history queries and measure their daily quota cost.

Each [[history.query]] entry in p1.toml is one ticks, kbars or snapshots
POST. run_history brackets every query with a usage read, so the delta
between the two readings is the query's measured cost in bytes against
the official server's daily quota, independent of whatever the query's
own response body happens to report about itself.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from p1rec.capture import CapturingClient, describe_exception
from p1rec.config import HistoryQuery, P1Config
from p1rec.interlock import assert_simulation
from p1rec.jsonpointer import JsonPointerError, resolve_pointer

logger = logging.getLogger(__name__)

# This module's own exit code for "checked, and at least one query
# failed", the same value every other subcommand that reports that
# outcome uses; cli.py returns it as-is, without any further mapping.
EXIT_HISTORY_QUERY_FAILED = 2


@dataclass(frozen=True)
class _QueryResult:
    """One [[history.query]] entry's outcome, in the field order history.json writes."""

    op: str
    body: str
    status: int | None
    response_bytes: int | None
    usage_before: int | None
    usage_after: int | None
    usage_delta: int | None


def run_history(cfg: P1Config, run_dir: Path, *, transport: httpx.BaseTransport | None = None) -> int:
    """Run every configured history query and write run_dir/history.json.

    Checks simulation mode first, on the same client every query below
    reuses. A query that raises or returns a non-2xx status is logged at
    ERROR and does not stop the remaining queries; the run still exits
    EXIT_HISTORY_QUERY_FAILED if any query failed. A usage read that fails
    or has no value at cfg.history.usage_bytes_pointer only makes that
    query's usage_delta null; it is logged at WARN, not treated as the
    query itself failing. transport is None for real network I/O; a test
    passes an httpx.MockTransport to answer in-process instead.
    """
    with CapturingClient(
        cfg.base_url, "history", run_dir / "rest.jsonl", cfg.http_timeout_s, transport=transport
    ) as client:
        assert_simulation(client, cfg)
        results = [_run_one(client, cfg, query) for query in cfg.history.queries]

    failed_count = sum(1 for result in results if not _query_ok(result))
    payload = {"queries": [asdict(result) for result in results], "failed_count": failed_count}
    (run_dir / "history.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return EXIT_HISTORY_QUERY_FAILED if failed_count else 0


def _query_ok(result: _QueryResult) -> bool:
    return result.status is not None and 200 <= result.status < 300


def _run_one(client: CapturingClient, cfg: P1Config, query: HistoryQuery) -> _QueryResult:
    usage_before = _read_usage(client, cfg)

    status: int | None = None
    response_bytes: int | None = None
    try:
        response = client.request(query.op, cfg.endpoints[query.op], json_body=json.loads(query.body))
    except Exception as exc:
        logger.error("%s query failed: %s", query.op, describe_exception(exc))
    else:
        status = response.status
        response_bytes = len(response.body)
        if not 200 <= status < 300:
            logger.error("%s query returned HTTP %d, expected 2xx", query.op, status)

    usage_after = _read_usage(client, cfg)
    usage_delta = usage_after - usage_before if usage_before is not None and usage_after is not None else None

    logger.info("%s query: status=%s bytes=%s usage_delta=%s", query.op, status, response_bytes, usage_delta)

    return _QueryResult(
        op=query.op,
        body=query.body,
        status=status,
        response_bytes=response_bytes,
        usage_before=usage_before,
        usage_after=usage_after,
        usage_delta=usage_delta,
    )


def _read_usage(client: CapturingClient, cfg: P1Config) -> int | None:
    """Return the usage endpoint's value at cfg.history.usage_bytes_pointer, or None.

    None covers every way this can fail: a request that raises, a non-2xx
    status, a body that is not valid JSON, a body with no value at the
    pointer, or a value that is not an integer. Each case is logged at
    WARN with its specific reason, since a single unreadable usage figure
    must never abort the history run around it.
    """
    try:
        response = client.request("usage", cfg.endpoints["usage"])
    except Exception as exc:
        logger.warning("usage request failed: %s", describe_exception(exc))
        return None

    if not 200 <= response.status < 300:
        logger.warning("usage returned HTTP %d, expected 2xx", response.status)
        return None

    try:
        body = response.json()
    except ValueError as exc:
        logger.warning("usage response is not valid JSON: %s", exc)
        return None

    try:
        value = resolve_pointer(body, cfg.history.usage_bytes_pointer)
    except JsonPointerError as exc:
        logger.warning("usage response has no value at %r: %s", cfg.history.usage_bytes_pointer, exc)
        return None

    # bool is an int subclass in Python, so an explicit isinstance(bool)
    # check comes first to reject a stray JSON true/false at the pointer.
    if isinstance(value, bool) or not isinstance(value, int):
        logger.warning("usage response at %r is %r, expected an integer", cfg.history.usage_bytes_pointer, value)
        return None

    return value
