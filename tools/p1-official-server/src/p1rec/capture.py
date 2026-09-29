"""Record REST exchanges with the official server verbatim.

Every request made through CapturingClient appends one line to its
rest.jsonl before the caller sees the outcome, so the file stays the
complete record of what was sent and received even when a later step
fails. append_jsonl and make_http_client are shared with the SSE recorder,
so every JSONL file is written the same way and tests have one place to
inject a mock transport.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from p1rec.config import Endpoint

_PATH_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_file_locks: dict[Path, threading.Lock] = {}
_file_locks_guard = threading.Lock()


def _file_lock(path: Path) -> threading.Lock:
    """Return the process-wide lock that serializes appends to path."""
    key = path.resolve()
    with _file_locks_guard:
        return _file_locks.setdefault(key, threading.Lock())


def append_jsonl(path: Path, record: Mapping[str, object]) -> None:
    """Append record to path as one JSON line.

    The line is written with a single write and flush while holding a lock
    shared by every writer of the same file in this process, so lines from
    concurrent writers never interleave.
    """
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    data = line.encode("utf-8")
    with _file_lock(path), path.open("ab") as fh:
        fh.write(data)
        fh.flush()


def make_http_client(base_url: str, timeout: httpx.Timeout) -> httpx.Client:
    """Build the httpx client used for every request to the official server."""

    # The official server is always reached directly, and a proxy picked up
    # from the environment would sit in the middle and distort timings.
    return httpx.Client(base_url=base_url, timeout=timeout, trust_env=False)


def _expand_path(template: str, path_vars: Mapping[str, str]) -> str:
    """Replace each {name} in template with its percent-encoded value from path_vars."""

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in path_vars:
            raise ValueError(f"missing path variable {name!r} for path {template!r}")
        return quote(path_vars[name], safe="")

    return _PATH_VAR_RE.sub(_substitute, template)


def _body_fields(body: bytes) -> dict[str, str]:
    """Return the body as resp_body if it is UTF-8, otherwise as resp_body_b64."""
    try:
        return {"resp_body": body.decode("utf-8")}
    except UnicodeDecodeError:
        return {"resp_body_b64": base64.b64encode(body).decode("ascii")}


@dataclass(frozen=True)
class CapturedResponse:
    """A response exactly as received, returned once its exchange is recorded."""

    status: int
    body: bytes
    recv_ts_ns: int

    def json(self) -> object:
        """Parse the body as JSON, raising ValueError if it is not valid JSON."""
        return json.loads(self.body)


class CapturingClient:
    """An HTTP client that records every exchange in rest.jsonl.

    An error status is recorded and returned, never raised. A transport
    failure, such as a refused connection or a timeout, is recorded as a
    line with an "error" field and no "status", then re-raised. Several
    instances, on any threads, may share one rest_log.
    """

    def __init__(self, base_url: str, client_name: str, rest_log: Path, timeout_s: float) -> None:
        self._client_name = client_name
        self._rest_log = rest_log
        self._http = make_http_client(base_url, httpx.Timeout(timeout_s))

    def request(
        self,
        op: str,
        endpoint: Endpoint,
        *,
        json_body: object | None = None,
        params: dict[str, str] | None = None,
        path_vars: dict[str, str] | None = None,
    ) -> CapturedResponse:
        """Send one request for the logical operation op and record the exchange.

        The line holds client, op, method, url, req_body (the exact text sent,
        or null), sent_ts_ns and then either recv_ts_ns, status, resp_headers
        ([name, value] pairs in received order) and resp_body or resp_body_b64,
        or error_ts_ns and error. Raises ValueError, before sending anything,
        if endpoint.path names a variable missing from path_vars.
        """
        path = _expand_path(endpoint.path, path_vars or {})
        request = self._http.build_request(endpoint.method, path, json=json_body, params=params)
        record: dict[str, object] = {
            "client": self._client_name,
            "op": op,
            "method": request.method,
            "url": str(request.url),
            "req_body": request.content.decode("utf-8") if request.content else None,
        }

        record["sent_ts_ns"] = time.time_ns()
        try:
            response = self._http.send(request)
        except httpx.HTTPError as exc:
            record["error_ts_ns"] = time.time_ns()
            record["error"] = f"{type(exc).__name__}: {exc}"
            append_jsonl(self._rest_log, record)
            raise
        recv_ts_ns = time.time_ns()

        body = response.content
        record["recv_ts_ns"] = recv_ts_ns
        record["status"] = response.status_code
        record["resp_headers"] = [[name, value] for name, value in response.headers.multi_items()]
        record.update(_body_fields(body))
        append_jsonl(self._rest_log, record)
        return CapturedResponse(status=response.status_code, body=body, recv_ts_ns=recv_ts_ns)

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._http.close()
