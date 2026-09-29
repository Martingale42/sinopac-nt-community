"""Run configured history queries and measure their daily quota cost.

Each [[history.query]] entry in p1.toml is one ticks, kbars or snapshots
POST. run_history brackets every query with a usage read, so the delta
between the two readings is an upper bound on that query's own cost in
bytes against the daily quota, not an isolated measurement: usage is
shared by every session under the same identity, so a delta measured
while another session, such as a concurrently running `stream`, is also
active runs high.
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
# outcome uses, returned by cli.py as-is with no further mapping.
EXIT_HISTORY_QUERY_FAILED = 2

# UsageOut's connections field is read from this fixed pointer, not a
# configurable one, the same way the "usage" op itself is fixed below.
_CONNECTIONS_POINTER = "/connections"


@dataclass(frozen=True)
class _UsageReading:
    """One GET usage's parsed result: bytes at usage_bytes_pointer, and connections if present."""

    bytes_used: int | None
    connections: int | None


@dataclass(frozen=True)
class _QueryResult:
    """One [[history.query]] entry's outcome, in the field order history.json writes."""

    op: str
    body: object
    status: int | None
    response_bytes: int | None
    usage_before: int | None
    usage_after: int | None
    usage_delta: int | None
    connections_before: int | None
    connections_after: int | None


def run_history(cfg: P1Config, run_dir: Path, *, transport: httpx.BaseTransport | None = None) -> int:
    """Run every configured history query and write run_dir/history.json.

    Checks simulation mode first, on the same client every read and
    query below reuses. Before the first query, two usage reads with
    nothing in between establish baseline_drift, since the daily counter
    can move on its own while this run is idle, and a non-zero
    baseline_drift is logged at WARN. Each query is then bracketed by a
    usage read the same way, and a usage_before that differs from the
    previous read's usage_after, or a negative usage_delta, is also
    logged at WARN, each as further evidence that another session under
    the same identity is active. A query that raises or returns a
    non-2xx status is logged at ERROR and does not stop the remaining
    queries; the run still exits EXIT_HISTORY_QUERY_FAILED if any query
    failed. A usage read that fails or has no value at
    cfg.history.usage_bytes_pointer only makes that reading null; it is
    logged at WARN, not treated as a failure. If a query iteration
    raises, including KeyboardInterrupt, whatever was collected so far is
    still written with "interrupted": true before the exception is
    re-raised. transport is None for real network I/O; a test passes an
    httpx.MockTransport to answer in-process instead.
    """
    with CapturingClient(
        cfg.base_url, "history", run_dir / "rest.jsonl", cfg.http_timeout_s, transport=transport
    ) as client:
        assert_simulation(client, cfg)

        results: list[_QueryResult] = []
        interrupted = False
        failed_count = 0
        baseline_before = _read_usage(client, cfg)
        baseline_after = _read_usage(client, cfg)
        baseline_drift = _delta(baseline_before.bytes_used, baseline_after.bytes_used)
        if baseline_drift is not None and baseline_drift != 0:
            logger.warning(
                "baseline usage drifted by %d bytes before the first query; "
                "another session on this identity may be active",
                baseline_drift,
            )
        try:
            previous_usage_after = baseline_after.bytes_used
            for query in cfg.history.queries:
                result = _run_one(client, cfg, query, previous_usage_after)
                results.append(result)
                previous_usage_after = result.usage_after
        except BaseException:
            interrupted = True
            raise
        finally:
            failed_count = _write_history_json(
                run_dir, baseline_before, baseline_after, baseline_drift, results, interrupted=interrupted
            )

    return EXIT_HISTORY_QUERY_FAILED if failed_count else 0


def _delta(before: int | None, after: int | None) -> int | None:
    return after - before if before is not None and after is not None else None


def _write_history_json(
    run_dir: Path,
    baseline_before: _UsageReading,
    baseline_after: _UsageReading,
    baseline_drift: int | None,
    results: list[_QueryResult],
    *,
    interrupted: bool,
) -> int:
    """Write run_dir/history.json and return the number of failed queries."""
    failed_count = sum(1 for result in results if not _query_ok(result))
    payload: dict[str, object] = {
        "baseline_usage_before": baseline_before.bytes_used,
        "baseline_usage_after": baseline_after.bytes_used,
        "baseline_drift": baseline_drift,
        "baseline_connections_before": baseline_before.connections,
        "baseline_connections_after": baseline_after.connections,
        "queries": [asdict(result) for result in results],
        "failed_count": failed_count,
    }
    if interrupted:
        payload["interrupted"] = True
    (run_dir / "history.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return failed_count


def _query_ok(result: _QueryResult) -> bool:
    return result.status is not None and 200 <= result.status < 300


def _run_one(
    client: CapturingClient, cfg: P1Config, query: HistoryQuery, previous_usage_after: int | None
) -> _QueryResult:
    usage_before = _read_usage(client, cfg)
    if (
        previous_usage_after is not None
        and usage_before.bytes_used is not None
        and usage_before.bytes_used != previous_usage_after
    ):
        logger.warning(
            "%s query's usage_before (%d) differs from the previous reading's usage_after (%d); "
            "another session on this identity may be active",
            query.op,
            usage_before.bytes_used,
            previous_usage_after,
        )

    status: int | None = None
    response_bytes: int | None = None
    body: object = None
    try:
        body = json.loads(query.body)
        response = client.request(query.op, cfg.endpoints[query.op], json_body=body)
    except Exception as exc:
        logger.error("%s query failed: %s", query.op, describe_exception(exc))
    else:
        status = response.status
        response_bytes = len(response.body)
        if not 200 <= status < 300:
            logger.error("%s query returned HTTP %d, expected 2xx", query.op, status)

    usage_after = _read_usage(client, cfg)
    usage_delta = _delta(usage_before.bytes_used, usage_after.bytes_used)
    if usage_delta is not None and usage_delta < 0:
        logger.warning(
            "%s query's usage_delta is negative (%d); the daily counter may have reset between reads",
            query.op,
            usage_delta,
        )

    logger.info("%s query: status=%s bytes=%s usage_delta=%s", query.op, status, response_bytes, usage_delta)

    return _QueryResult(
        op=query.op,
        body=body,
        status=status,
        response_bytes=response_bytes,
        usage_before=usage_before.bytes_used,
        usage_after=usage_after.bytes_used,
        usage_delta=usage_delta,
        connections_before=usage_before.connections,
        connections_after=usage_after.connections,
    )


def _read_usage(client: CapturingClient, cfg: P1Config) -> _UsageReading:
    """Return the usage endpoint's bytes and connections readings.

    bytes_used is None in every way this can fail: a request that
    raises, a non-2xx status, a body that is not valid JSON, a body with
    no value at cfg.history.usage_bytes_pointer, or a value that is not
    an integer. Each such case is logged at WARN with its specific
    reason, since a single unreadable usage figure must never abort the
    history run around it. connections is read on a best-effort basis and
    is None whenever it is missing or not an integer, without its own
    WARN, since it is diagnostic context rather than something
    usage_delta depends on.
    """
    try:
        response = client.request("usage", cfg.endpoints["usage"])
    except Exception as exc:
        logger.warning("usage request failed: %s", describe_exception(exc))
        return _UsageReading(bytes_used=None, connections=None)

    if not 200 <= response.status < 300:
        logger.warning("usage returned HTTP %d, expected 2xx", response.status)
        return _UsageReading(bytes_used=None, connections=None)

    try:
        body = response.json()
    except ValueError as exc:
        logger.warning("usage response is not valid JSON: %s", exc)
        return _UsageReading(bytes_used=None, connections=None)

    try:
        value = resolve_pointer(body, cfg.history.usage_bytes_pointer)
    except JsonPointerError as exc:
        logger.warning("usage response has no value at %r: %s", cfg.history.usage_bytes_pointer, exc)
        return _UsageReading(bytes_used=None, connections=None)

    # bool is an int subclass in Python, so an explicit isinstance(bool)
    # check comes first to reject a stray JSON true/false at the pointer.
    if isinstance(value, bool) or not isinstance(value, int):
        logger.warning("usage response at %r is %r, expected an integer", cfg.history.usage_bytes_pointer, value)
        return _UsageReading(bytes_used=None, connections=None)

    return _UsageReading(bytes_used=value, connections=_connections(body))


def _connections(body: object) -> int | None:
    """Return UsageOut's connections field, or None if missing or not an integer."""
    try:
        value = resolve_pointer(body, _CONNECTIONS_POINTER)
    except JsonPointerError:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None
