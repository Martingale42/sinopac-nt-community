"""Summarise a stream run's SSE files.

Event counts per client, and per recorder file the busiest seconds,
heartbeat spacing, silences, outages and receive latency.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from array import array
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

import httpx

from p1rec.config import EventFields, P1Config
from p1rec.jsonpointer import JsonPointerError, resolve_pointer
from p1rec.records import (
    EXIT_ANALYSIS_ERROR,
    EXIT_OK,
    NS_PER_S,
    NS_PER_US,
    UNIX_EPOCH,
    AnalysisError,
    discover_sse_files,
    epoch_s_to_ns,
    exchange_tz,
    frame_of,
    iso_utc,
    read_jsonl,
    recv_ts_of,
    resolve_run_dir,
    write_json,
)
from p1rec.sse import SseFrame

logger = logging.getLogger(__name__)

# How many of the busiest seconds stats lists, enough to show the shape
# of the opening spike without flooding the report.
PEAK_SECONDS = 10

# Nearest-rank percentiles reported for receive latency
LATENCY_PERCENTILES = (50, 99)

# SSE's default event type for a frame without an event field, and the
# name counted for a frame made only of comment lines.
_DEFAULT_EVENT_NAME = "message"
_COMMENT_EVENT_NAME = ":comment"


def percentile_nearest_rank(sorted_values: Sequence[float], p: float) -> float:
    """Return the p-th percentile of sorted_values by the nearest-rank method.

    Definition: The smallest recorded value such that at least p percent
                of the values are less than or equal to it.
    Formula:    P_p = x_(k), k = max(1, ceil(p / 100 * n)), 1-based over x
                sorted ascending; computed with exact fractions of p's
                decimal text, so 99.9 is 999/10 rather than its binary
                approximation.
    Domain:     sorted_values is sorted ascending and non-empty; 0 <= p <=
                100. No interpolation, so small samples round up to an
                observed value.
    Returns:    An element of sorted_values, in its unit.
    """
    if not sorted_values:
        raise ValueError("percentile of an empty sequence")
    if not 0 <= p <= 100:
        raise ValueError(f"percentile must be within [0, 100], got {p}")
    rank = max(1, math.ceil(Fraction(str(p)) * len(sorted_values) / 100))
    return sorted_values[rank - 1]


def interval_summary(intervals: Sequence[float]) -> dict[str, object] | None:
    """Summarise heartbeat intervals by their count, minimum, median and maximum.

    Definition: The spread of the time between consecutive heartbeats.
    Formula:    n = len(x); min(x); median(x) (statistics.median, the mean
                of the two middle values when n is even); max(x).
    Domain:     Intervals in seconds, each measured within one connection;
                empty when a file had fewer than two heartbeats on one
                connection.
    Returns:    {"n", "min", "median", "max"} in seconds, or None when
                there are no intervals.
    """
    if not intervals:
        return None
    return {"n": len(intervals), "min": min(intervals), "median": statistics.median(intervals), "max": max(intervals)}


def latency_summary(latencies: Sequence[float], unparsable: int) -> dict[str, object]:
    """Summarise receive latency by nearest-rank p50 and p99 and the maximum.

    Definition: How long after the exchange time a frame was recorded.
    Formula:    latency = recv_ts - exchange_ts per frame; p50 and p99 by
                percentile_nearest_rank; max(latency).
    Domain:     Seconds; negative when the local and exchange clocks
                disagree, so the local NTP state belongs next to it.
                unparsable counts frames whose exchange time fields exist
                but do not parse.
    Returns:    {"n", "p50", "p99", "max", "unparsable"} in seconds; the
                statistics are None when n is 0.
    """
    ordered = sorted(latencies)
    summary: dict[str, object] = {"n": len(ordered)}
    for p in LATENCY_PERCENTILES:
        summary[f"p{p}"] = percentile_nearest_rank(ordered, p) if ordered else None
    summary["max"] = ordered[-1] if ordered else None
    summary["unparsable"] = unparsable
    return summary


def exchange_ts_ns(doc: object, fields: EventFields) -> int | None:
    """Return the exchange time in a frame's data as epoch nanoseconds.

    Definition: The instant the exchange stamped on a frame, on the same
                scale as recv_ts_ns.
    Formula:    Without exchange_ts_format: ns = epoch_s_to_ns(v), v the
                single pointer's value in epoch seconds. With it: t = strptime(" ".join(values),
                exchange_ts_format) as Asia/Taipei local time, ns =
                (t - 1970-01-01T00:00Z) in whole microseconds * NS_PER_US.
    Domain:     doc is a frame's parsed data. Frames where a pointer does
                not resolve, such as heartbeats, have no exchange time.
                The format must not parse a zone. Raises ValueError when
                the values resolve but are not a number or do not parse.
    Returns:    Epoch nanoseconds (UTC), microsecond resolution for parsed
                strings, or None when a pointer does not resolve.
    """
    try:
        values = [resolve_pointer(doc, pointer) for pointer in fields.exchange_ts_pointers]
    except JsonPointerError:
        return None

    if fields.exchange_ts_format is None:
        (value,) = values
        return epoch_s_to_ns(value)

    texts = [value for value in values if isinstance(value, str)]
    if len(texts) != len(values):
        raise ValueError("exchange time fields are not all strings")
    parsed = datetime.strptime(" ".join(texts), fields.exchange_ts_format)
    if parsed.tzinfo is not None:
        raise ValueError("exchange_ts_format must not parse a zone, since exchange time is Taipei local")
    return (parsed.replace(tzinfo=exchange_tz()) - UNIX_EPOCH) // timedelta(microseconds=1) * NS_PER_US


def _event_name(frame: SseFrame) -> str:
    if frame.event:
        return frame.event
    return _COMMENT_EVENT_NAME if frame.is_comment else _DEFAULT_EVENT_NAME


@dataclass
class _Outage:
    """A run of failed connection attempts, ended by the next 200 connect or by the file's end."""

    start_ns: int
    first_error: object
    attempts: int = 1
    end_ns: int | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "disconnected_at": iso_utc(self.start_ns),
            "reconnected_at": iso_utc(self.end_ns) if self.end_ns is not None else None,
            "outage_s": (self.end_ns - self.start_ns) / NS_PER_S if self.end_ns is not None else None,
            "attempts": self.attempts,
            "first_error": self.first_error,
        }


class _FileStats:
    """One SSE file's statistics, accumulated while its lines are read once."""

    def __init__(self, path: Path, fields: EventFields) -> None:
        self._path = path
        self._fields = fields
        self.stream: str | None = None
        self.frames = 0
        self.event_counts: Counter[str] = Counter()
        self._per_second: Counter[int] = Counter()
        self._prev_frame_ns: int | None = None
        self._gaps: list[dict[str, object]] = []
        self._prev_heartbeat_ns: int | None = None
        self._heartbeat_intervals: list[float] = []
        self._outages: list[_Outage] = []
        self._outage: _Outage | None = None
        self._latencies: array[float] = array("d")
        self._unparsable = 0

    def read(self) -> None:
        for number, record in read_jsonl(self._path):
            if self.stream is None and isinstance(record.get("stream"), str):
                self.stream = str(record["stream"])
            recv_ts_ns = recv_ts_of(record, self._path, number)
            kind = record.get("kind")
            if kind == "frame":
                self._frame(recv_ts_ns, frame_of(record, self._path, number))
            elif kind == "connected":
                self._prev_heartbeat_ns = None
                if record.get("status") == httpx.codes.OK:
                    self._end_outage(recv_ts_ns)
            elif kind == "disconnected":
                self._prev_heartbeat_ns = None
                self._start_or_extend_outage(recv_ts_ns, record.get("error"))
            elif kind == "stopped":
                self._prev_heartbeat_ns = None
                self._end_outage(None)
        self._end_outage(None)

    def _frame(self, recv_ts_ns: int, frame: SseFrame) -> None:
        self.frames += 1
        self.event_counts[_event_name(frame)] += 1
        self._per_second[recv_ts_ns // NS_PER_S] += 1

        if self._prev_frame_ns is not None:
            gap_s = (recv_ts_ns - self._prev_frame_ns) / NS_PER_S
            if gap_s > self._fields.gap_threshold_s:
                self._gaps.append({"from": iso_utc(self._prev_frame_ns), "to": iso_utc(recv_ts_ns), "gap_s": gap_s})
        self._prev_frame_ns = recv_ts_ns

        if frame.event == self._fields.heartbeat_event:
            if self._prev_heartbeat_ns is not None:
                self._heartbeat_intervals.append((recv_ts_ns - self._prev_heartbeat_ns) / NS_PER_S)
            self._prev_heartbeat_ns = recv_ts_ns

        if self._fields.exchange_ts_pointers and frame.data is not None:
            self._latency(recv_ts_ns, frame.data)

    def _latency(self, recv_ts_ns: int, data: str) -> None:
        try:
            doc = json.loads(data)
        except ValueError:
            return
        try:
            exchange_ns = exchange_ts_ns(doc, self._fields)
        except ValueError:
            self._unparsable += 1
            return
        if exchange_ns is not None:
            self._latencies.append((recv_ts_ns - exchange_ns) / NS_PER_S)

    def _start_or_extend_outage(self, recv_ts_ns: int, error: object) -> None:
        if self._outage is None:
            self._outage = _Outage(start_ns=recv_ts_ns, first_error=error)
        else:
            self._outage.attempts += 1

    def _end_outage(self, recv_ts_ns: int | None) -> None:
        if self._outage is None:
            return
        self._outage.end_ns = recv_ts_ns
        self._outages.append(self._outage)
        self._outage = None

    def to_json(self, client: str) -> dict[str, object]:
        busiest = sorted(self._per_second.items(), key=lambda item: (-item[1], item[0]))[:PEAK_SECONDS]
        return {
            "client": client,
            "stream": self.stream,
            "frames": self.frames,
            "event_counts": dict(sorted(self.event_counts.items())),
            "peak_seconds": [
                {"second": iso_utc(second * NS_PER_S, "seconds"), "frames": count} for second, count in busiest
            ],
            "heartbeat_interval_s": interval_summary(self._heartbeat_intervals),
            "gaps": self._gaps,
            "outages": [outage.to_json() for outage in self._outages],
            "latency_s": self._latency_json(),
        }

    def _latency_json(self) -> dict[str, object] | None:
        if not self._fields.exchange_ts_pointers:
            return None
        return latency_summary(self._latencies, self._unparsable)


def stream_stats(paths: Mapping[str, Sequence[Path]], fields: EventFields) -> dict[str, object]:
    """Return the stats.json document for a stream run's SSE files.

    Definition: Per-client event counts, and per-file frame rate peaks,
                heartbeat spacing, silences, outages and receive latency.
    Formula:    event_counts[name] = frames with that event name;
                peak_seconds = the PEAK_SECONDS one-second buckets
                floor(recv_ts_ns / 1e9) with the most frames, ties broken
                by the earlier second; a gap is recv_ts[i] - recv_ts[i-1]
                > gap_threshold_s between consecutive frames of a file; a
                heartbeat interval is the same difference between
                consecutive heartbeat_event frames on one connection; an
                outage runs from a disconnect to the next 200 connect;
                latency as in latency_summary.
    Domain:     paths maps a client to all of its files; each file is one
                recorder, so intervals and gaps never span two files.
                Client event_counts sum that client's files, so an event
                recorded on both the combined and the order_event stream
                counts twice there; per-file counts are exact.
                recv_ts is the recorder's wall clock, which trails socket
                arrival whenever the recorder falls behind.
    Returns:    {"gap_threshold_s", "heartbeat_event", "clients": {client:
                {"event_counts"}}, "files": {file name: {"client",
                "stream", "frames", "event_counts", "peak_seconds",
                "heartbeat_interval_s", "gaps", "outages", "latency_s"}}},
                times as ISO 8601 UTC and durations in seconds.
    """
    clients: dict[str, dict[str, object]] = {}
    files: dict[str, dict[str, object]] = {}
    for client in sorted(paths):
        client_counts: Counter[str] = Counter()
        for path in paths[client]:
            stats = _FileStats(path, fields)
            stats.read()
            client_counts.update(stats.event_counts)
            files[path.name] = stats.to_json(client)
        clients[client] = {"event_counts": dict(sorted(client_counts.items()))}
    return {
        "gap_threshold_s": fields.gap_threshold_s,
        "heartbeat_event": fields.heartbeat_event,
        "clients": clients,
        "files": files,
    }


def run_stats(cfg: P1Config, sse_run: str, run_dir: Path) -> int:
    """Write run_dir/stats.json for a stream run and return 0, or log the failure and return 1."""
    try:
        sse_files = discover_sse_files(resolve_run_dir(cfg.results_root, sse_run))
        document = {"sse_run": sse_run, **stream_stats(sse_files, cfg.events)}
    except (AnalysisError, OSError) as exc:
        logger.error("stats failed: %s", exc)
        return EXIT_ANALYSIS_ERROR
    write_json(run_dir / "stats.json", document)
    file_count = sum(len(client_paths) for client_paths in sse_files.values())
    logger.info("stats: %d file(s) from %d client(s)", file_count, len(sse_files))
    return EXIT_OK
