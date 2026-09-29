"""Tests for p1rec.capture: verbatim REST recording."""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

import httpx
import pytest

from p1rec import capture
from p1rec.capture import CapturedResponse, CapturingClient, make_http_client
from p1rec.config import Endpoint

BASE_URL = "http://127.0.0.1:8080"
PLACE = Endpoint(method="POST", path="/api/v1/order/place_order")
CONTRACT_INFO = Endpoint(method="GET", path="/api/v1/data/contracts/{code}/info")
MODE = Endpoint(method="GET", path="/api/v1/info")
USAGE = Endpoint(method="GET", path="/api/v1/auth/usage")

Handler = Callable[[httpx.Request], httpx.Response]


def _use_transport(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    def _make(base_url: str, timeout: httpx.Timeout) -> httpx.Client:
        return httpx.Client(base_url=base_url, timeout=timeout, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(capture, "make_http_client", _make)


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_rest_error_status_recorded_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.content)
        return httpx.Response(500, json={"detail": "價格超過漲跌幅範圍"})

    _use_transport(monkeypatch, handler)
    rest_log = tmp_path / "rest.jsonl"
    client = CapturingClient(BASE_URL, "a", rest_log, timeout_s=35.0)
    body = {"code": "2890", "price": "18.5", "custom_field": "ab12cd"}

    response = client.request("place_stock", PLACE, json_body=body)

    assert response.status == 500
    assert response.json() == {"detail": "價格超過漲跌幅範圍"}
    [line] = _lines(rest_log)
    assert line["client"] == "a"
    assert line["op"] == "place_stock"
    assert line["method"] == "POST"
    assert line["url"] == f"{BASE_URL}/api/v1/order/place_order"

    # req_body is the exact text that went over the wire, not a re-serialization
    assert line["req_body"] == received[0].decode("utf-8")
    assert json.loads(str(line["req_body"])) == body
    assert line["status"] == 500
    assert line["resp_body"] == response.body.decode("utf-8")
    assert ["content-type", "application/json"] in line["resp_headers"]
    assert isinstance(line["sent_ts_ns"], int)
    assert line["sent_ts_ns"] <= line["recv_ts_ns"] == response.recv_ts_ns
    assert "resp_body_b64" not in line
    assert "error" not in line


def test_non_utf8_body_base64(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b"\xff\xfe\x00\x80not utf-8"
    _use_transport(monkeypatch, lambda request: httpx.Response(200, content=raw))
    rest_log = tmp_path / "rest.jsonl"

    response = CapturingClient(BASE_URL, "a", rest_log, timeout_s=35.0).request("usage", USAGE)

    assert response.body == raw
    [line] = _lines(rest_log)
    assert base64.b64decode(str(line["resp_body_b64"])) == raw
    assert "resp_body" not in line


def test_transport_error_recorded_then_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused")

    _use_transport(monkeypatch, handler)
    rest_log = tmp_path / "rest.jsonl"

    with pytest.raises(httpx.ConnectError):
        CapturingClient(BASE_URL, "a", rest_log, timeout_s=35.0).request("mode", MODE)

    [line] = _lines(rest_log)
    assert line["op"] == "mode"
    assert line["error"] == "ConnectError: [Errno 111] Connection refused"
    assert line["sent_ts_ns"] <= line["error_ts_ns"]
    assert "status" not in line
    assert "recv_ts_ns" not in line


def test_interrupt_recorded_then_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    _use_transport(monkeypatch, handler)
    rest_log = tmp_path / "rest.jsonl"

    with pytest.raises(KeyboardInterrupt):
        CapturingClient(BASE_URL, "a", rest_log, timeout_s=35.0).request("place_stock", PLACE, json_body={})

    [line] = _lines(rest_log)
    assert line["op"] == "place_stock"
    assert line["error"] == "KeyboardInterrupt"
    assert line["sent_ts_ns"] <= line["error_ts_ns"]
    assert "status" not in line


def test_context_manager_closes_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_transport(monkeypatch, lambda request: httpx.Response(200, content=b"ok"))

    with CapturingClient(BASE_URL, "a", tmp_path / "rest.jsonl", timeout_s=35.0) as client:
        assert client.request("mode", MODE).status == 200

    with pytest.raises(RuntimeError, match="closed"):
        client.request("mode", MODE)


def test_make_http_client_asks_for_identity_encoding() -> None:
    with make_http_client(BASE_URL, httpx.Timeout(1.0)) as client:
        assert client.headers["accept-encoding"] == "identity"
        assert client.trust_env is False


def test_path_vars_and_params_build_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_transport(monkeypatch, lambda request: httpx.Response(200, json={}))
    rest_log = tmp_path / "rest.jsonl"
    client = CapturingClient(BASE_URL, "a", rest_log, timeout_s=35.0)

    client.request("contracts_stock", CONTRACT_INFO, path_vars={"code": "00981A"}, params={"root": "TMF"})
    client.request("contracts_stock", CONTRACT_INFO, path_vars={"code": "a/b"})

    first, second = _lines(rest_log)
    assert first["url"] == f"{BASE_URL}/api/v1/data/contracts/00981A/info?root=TMF"
    assert first["method"] == "GET"
    assert first["req_body"] is None

    # A path variable can never add a path segment
    assert second["url"] == f"{BASE_URL}/api/v1/data/contracts/a%2Fb/info"


def test_missing_path_var_raises_before_sending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    _use_transport(monkeypatch, handler)
    rest_log = tmp_path / "rest.jsonl"

    with pytest.raises(ValueError, match="code"):
        CapturingClient(BASE_URL, "a", rest_log, timeout_s=35.0).request("contracts_stock", CONTRACT_INFO)

    assert calls == []
    assert not rest_log.exists()


def test_response_headers_keep_duplicates_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = [("x-trace", "1"), ("set-cookie", "a=1"), ("set-cookie", "b=2")]
    _use_transport(monkeypatch, lambda request: httpx.Response(200, headers=headers, content=b"ok"))
    rest_log = tmp_path / "rest.jsonl"

    CapturingClient(BASE_URL, "a", rest_log, timeout_s=35.0).request("mode", MODE)

    [line] = _lines(rest_log)
    recorded = [pair for pair in line["resp_headers"] if pair[0] in {"x-trace", "set-cookie"}]
    assert recorded == [list(pair) for pair in headers]


def test_json_raises_on_invalid_body() -> None:
    with pytest.raises(ValueError):
        CapturedResponse(status=200, body=b"not json", recv_ts_ns=0).json()


class _SplitWriteFile:
    """A binary file whose every write lands as two flushed halves with a thread switch between.

    A single append of a long line can reach the disk in several system
    calls, and this makes that happen on every write, so lines from
    unsynchronized writers would interleave.
    """

    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh

    def __enter__(self) -> _SplitWriteFile:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._fh.close()

    def write(self, data: bytes) -> int:
        half = len(data) // 2
        self._fh.write(data[:half])
        self._fh.flush()
        time.sleep(0.001)
        self._fh.write(data[half:])
        return len(data)

    def flush(self) -> None:
        self._fh.flush()


def test_concurrent_clients_append_whole_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=(request.url.params["n"] * 100).encode("ascii"))

    _use_transport(monkeypatch, handler)
    (tmp_path / "sub").mkdir()

    # Two spellings of one file must still share a lock
    spellings = [tmp_path / "rest.jsonl", tmp_path / "sub" / ".." / "rest.jsonl"]
    threads_n, requests_n = 8, 10
    barrier = threading.Barrier(threads_n)
    real_open = Path.open

    def split_open(self: Path, *args: Any, **kwargs: Any) -> _SplitWriteFile:
        return _SplitWriteFile(real_open(self, *args, **kwargs))

    def worker(index: int) -> None:
        client = CapturingClient(BASE_URL, f"c{index}", spellings[index % 2], timeout_s=35.0)
        barrier.wait()
        for _ in range(requests_n):
            client.request("trades", PLACE, params={"n": str(index)})
        client.close()

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", split_open)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    lines = _lines(tmp_path / "rest.jsonl")
    assert len(lines) == threads_n * requests_n
    for line in lines:
        index = str(line["client"]).removeprefix("c")
        assert line["resp_body"] == index * 100
