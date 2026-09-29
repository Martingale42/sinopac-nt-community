"""Tests for p1rec.sse: frame splitting, parsing and the recording thread."""

from __future__ import annotations

import base64
import json
import logging
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import httpx
import pytest

from p1rec import capture, sse
from p1rec.config import Endpoint
from p1rec.sse import SseRecorder, parse_frame, split_frames

BASE_URL = "http://127.0.0.1:8080"
STREAM = Endpoint(method="GET", path="/api/v1/stream/data")
SSE_HEADERS = {"content-type": "text/event-stream"}

# Verbatim frame shape observed on the official server 1.7.6
HEARTBEAT = (
    'event:heartbeat\ndata:{"type":"heartbeat","timestamp":"2026-09-29T10:06:17.319594218+00:00",'
    '"connection_id":"1"}\n\n'
)

Handler = Callable[[httpx.Request], httpx.Response]


class _Chunks(httpx.SyncByteStream):
    """A response body delivered as the given chunks, like successive network reads.

    after runs once the consumer asks for more data past the last chunk,
    and error, if set, is raised at that point to simulate a dropped
    connection.
    """

    def __init__(
        self,
        chunks: list[bytes],
        after: Callable[[], None] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._chunks = chunks
        self._after = after
        self._error = error

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks
        if self._after is not None:
            self._after()
        if self._error is not None:
            raise self._error


def _recorder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handler: Handler,
    stop: threading.Event,
    on_connected: Callable[[], None] = lambda: None,
    retry_s: float = 0.001,
) -> tuple[SseRecorder, Path]:
    def _make(base_url: str, timeout: httpx.Timeout) -> httpx.Client:
        return httpx.Client(base_url=base_url, timeout=timeout, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(capture, "make_http_client", _make)
    out = tmp_path / "sse_a_data.jsonl"
    recorder = SseRecorder(BASE_URL, "a", "data", STREAM, out, retry_s, stop, on_connected)
    return recorder, out


def _run(recorder: SseRecorder) -> None:
    recorder.start()
    recorder.join(timeout=5)
    assert not recorder.is_alive()


class _SilentSseHandler(BaseHTTPRequestHandler):
    """Answer an SSE request with headers, one heartbeat on the first connection only, then silence."""

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        server = cast(_SilentSseServer, self.server)
        server.accept_encodings.append(self.headers.get("Accept-Encoding"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if len(server.accept_encodings) == 1:
            self.wfile.write(HEARTBEAT.encode("utf-8"))
        self.wfile.flush()
        server.connections.release()
        server.done.wait()


class _SilentSseServer(ThreadingHTTPServer):
    """A real HTTP server on a free loopback port whose SSE streams go silent."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _SilentSseHandler)
        self.accept_encodings: list[str | None] = []
        self.connections = threading.Semaphore(0)
        self.done = threading.Event()

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host!s}:{port}"

    def handle_error(self, request: object, client_address: object) -> None:
        # The recorder hanging up on a silent stream is expected in these tests
        pass


@pytest.fixture
def silent_sse_server() -> Iterator[_SilentSseServer]:
    server = _SilentSseServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.done.set()
        server.shutdown()
        server.server_close()


def _serve_once(stop: threading.Event, response: httpx.Response) -> Handler:
    """Return a handler that serves response once, then sets stop and refuses the reconnect.

    Stop is set only after the server side has closed the first stream, so
    the recorder sees a real disconnect before it is told to stop.
    """
    served: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if served:
            stop.set()
            raise httpx.ConnectError("[Errno 111] Connection refused")
        served.append(request)
        return response

    return handler


def _record_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, chunks: list[bytes]) -> list[dict[str, object]]:
    """Serve chunks on one connection, then stop, and return the recorded lines."""
    stop = threading.Event()
    handler = _serve_once(stop, httpx.Response(200, headers=SSE_HEADERS, stream=_Chunks(chunks)))

    recorder, out = _recorder(monkeypatch, tmp_path, handler, stop)
    _run(recorder)
    return _lines(out)


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _frames(lines: list[dict[str, object]]) -> list[str]:
    return [str(line["raw"]) for line in lines if line["kind"] == "frame"]


def test_split_frames_lf_and_crlf() -> None:
    buffer = "event:a\ndata:1\n\nevent:b\r\ndata:2\r\n\r\ndata:3\r\n\ndata:4"

    frames, rest = split_frames(buffer)

    assert frames == ["event:a\ndata:1\n\n", "event:b\r\ndata:2\r\n\r\n", "data:3\r\n\n"]
    assert rest == "data:4"
    assert "".join(frames) + rest == buffer


def test_split_frames_partial_tail() -> None:
    assert split_frames("data:1\n\ndata:2\n") == (["data:1\n\n"], "data:2\n")
    assert split_frames("data:2") == ([], "data:2")

    # A trailing CR may be half of a CRLF, so the frame waits for the next byte
    assert split_frames("data:1\r\n\r") == ([], "data:1\r\n\r")
    assert split_frames("data:1\r\n\r\n") == (["data:1\r\n\r\n"], "")


def test_frame_splitter_matches_split_frames_in_any_pieces() -> None:
    stream = "event:a\r\ndata:1\r\n\r\n: c\n\ndata:2\r\rdata:3\r\n\ndata:4"
    expected = (["event:a\r\ndata:1\r\n\r\n", ": c\n\n", "data:2\r\r", "data:3\r\n\n"], "data:4")
    assert split_frames(stream) == expected

    for size in (1, 2, 3, 7):
        splitter = sse._FrameSplitter()
        frames: list[str] = []
        for start in range(0, len(stream), size):
            frames += splitter.feed(stream[start : start + size])
        assert (frames, splitter.pending) == expected


def test_frame_splitter_resumes_where_it_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    scan_starts: list[int] = []
    line_end_re = sse._LINE_END_RE

    class _Spy:
        def finditer(self, string: str, pos: int = 0) -> Iterator[re.Match[str]]:
            scan_starts.append(pos)
            return line_end_re.finditer(string, pos)

    monkeypatch.setattr(sse, "_LINE_END_RE", _Spy())
    splitter = sse._FrameSplitter()

    assert splitter.feed("data:aaa") == []
    assert splitter.feed("bbb\r") == []
    assert splitter.feed("\n\n") == ["data:aaabbb\r\n\n"]

    # Each feed starts where the last one stopped, except that a trailing CR
    # is looked at again in case an LF follows it.
    assert scan_starts == [0, 8, 11]


def test_split_frames_bare_cr() -> None:
    assert split_frames("data:1\r\rdata:2") == (["data:1\r\r"], "data:2")


def test_parse_multiline_data() -> None:
    frame = parse_frame("event:tick\ndata:first\ndata: second\r\ndata:\n\n")

    assert frame.event == "tick"
    assert frame.data == "first\nsecond\n"
    assert frame.id is None
    assert not frame.is_comment


def test_parse_strips_exactly_one_space() -> None:
    frame = parse_frame("event: tick\ndata:  indented\n\n")

    assert frame.event == "tick"
    assert frame.data == " indented"


def test_parse_id_and_field_without_colon() -> None:
    assert parse_frame("id:42\ndata\n\n").id == "42"
    assert parse_frame("id:42\ndata\n\n").data == ""

    # An id containing NUL is ignored under the SSE rules
    assert parse_frame("id:4\x002\ndata:x\n\n").id is None


def test_parse_leaves_raw_untouched() -> None:
    raw = "event: tick\r\ndata: x\r\n\r\n"

    assert parse_frame(raw).raw == raw


def test_comment_frame_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = _record_once(monkeypatch, tmp_path, [b": ping\n\n"])

    assert _frames(lines) == [": ping\n\n"]
    assert parse_frame(": ping\n\n").is_comment

    # A comment mixed with fields is an ordinary frame
    assert not parse_frame(": note\ndata:x\n\n").is_comment


def test_heartbeat_event_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = _record_once(monkeypatch, tmp_path, [HEARTBEAT.encode("utf-8")])

    assert _frames(lines) == [HEARTBEAT]
    frame = parse_frame(HEARTBEAT)
    assert frame.event == "heartbeat"
    assert json.loads(str(frame.data))["connection_id"] == "1"
    assert not frame.is_comment


def test_multibyte_char_split_across_chunks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # 價 is E5 83 B9 in UTF-8; the chunks split it and the blank line
    chunks = [b'event:order\ndata:{"msg":"\xe5\x83', b'\xb9"}\n', b"\n"]

    lines = _record_once(monkeypatch, tmp_path, chunks)

    [frame] = [line for line in lines if line["kind"] == "frame"]
    assert frame["raw"] == 'event:order\ndata:{"msg":"價"}\n\n'

    # Decoding each chunk on its own would flag this valid text as undecodable
    assert "raw_b64" not in frame


def test_invalid_utf8_kept_as_base64(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = _record_once(monkeypatch, tmp_path, [b"data:\xff\n\n"])

    [frame] = [line for line in lines if line["kind"] == "frame"]
    assert frame["raw"] == "data:�\n\n"
    assert base64.b64decode(str(frame["raw_b64"])) == b"data:\xff\n\n"


def test_unterminated_tail_not_written_as_frame(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lines = _record_once(monkeypatch, tmp_path, [HEARTBEAT.encode("utf-8") + b"event:tick\ndata:{"])

    assert [line["kind"] for line in lines] == ["connected", "frame", "disconnected", "stopped"]
    assert _frames(lines) == [HEARTBEAT]
    assert lines[2]["error"] == "stream ended by server"
    assert lines[2]["partial"] == "event:tick\ndata:{"


def test_reconnect_sends_last_event_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stop = threading.Event()
    requests: list[httpx.Request] = []
    connected_calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            # The comment frame carries no id, so the last id seen must survive it
            first = _Chunks(
                [b"id:7\nevent:tick_fop\ndata:a\n\n", b": keepalive\n\n"],
                error=httpx.ReadError("connection reset by peer"),
            )
            return httpx.Response(200, headers=SSE_HEADERS, stream=first)
        return httpx.Response(200, headers=SSE_HEADERS, stream=_Chunks([HEARTBEAT.encode("utf-8")]))

    def on_connected() -> None:
        connected_calls.append(len(requests))
        if len(connected_calls) == 2:
            stop.set()

    recorder, out = _recorder(monkeypatch, tmp_path, handler, stop, on_connected)
    _run(recorder)

    assert "last-event-id" not in requests[0].headers
    assert requests[1].headers["last-event-id"] == "7"
    assert requests[1].headers["accept"] == "text/event-stream"
    assert connected_calls == [1, 2]
    assert recorder.last_event_id == "7"

    lines = _lines(out)
    assert [line["kind"] for line in lines] == ["connected", "frame", "frame", "disconnected", "connected", "stopped"]
    assert all(line["client"] == "a" and line["stream"] == "data" for line in lines)
    assert all(isinstance(line["recv_ts_ns"], int) for line in lines)
    assert lines[0]["status"] == 200
    assert lines[0]["last_event_id"] is None
    assert lines[3]["error"] == "ReadError: connection reset by peer"
    assert lines[4]["last_event_id"] == "7"


def test_empty_id_clears_last_event_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stop = threading.Event()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            first = _Chunks([b"id:7\ndata:a\n\n", b"id:\ndata:b\n\n"], error=httpx.ReadError("reset"))
            return httpx.Response(200, headers=SSE_HEADERS, stream=first)
        stop.set()
        return httpx.Response(200, headers=SSE_HEADERS, stream=_Chunks([]))

    recorder, _ = _recorder(monkeypatch, tmp_path, handler, stop)
    _run(recorder)

    assert "last-event-id" not in requests[1].headers
    assert recorder.last_event_id is None


def test_stop_between_chunks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stop = threading.Event()
    read_past_stop: list[bool] = []

    def body() -> Iterator[bytes]:
        yield HEARTBEAT.encode("utf-8")
        stop.set()
        yield HEARTBEAT.encode("utf-8") + b"event:tick"
        read_past_stop.append(True)
        yield HEARTBEAT.encode("utf-8")

    class _Body(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            return body()

    recorder, out = _recorder(
        monkeypatch, tmp_path, lambda request: httpx.Response(200, headers=SSE_HEADERS, stream=_Body()), stop
    )
    _run(recorder)

    assert read_past_stop == []
    lines = _lines(out)
    assert [line["kind"] for line in lines] == ["connected", "frame", "frame", "stopped"]
    assert lines[-1]["partial"] == "event:tick"


def test_stop_interrupts_silent_stream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stop = threading.Event()
    server_end, client_end = socket.socketpair()
    reads: list[None] = []
    blocked = threading.Event()

    class _SocketBody(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            while True:
                reads.append(None)
                if len(reads) == 2:
                    blocked.set()
                data = client_end.recv(65536)
                if not data:
                    return
                yield data

    class _NetworkStream:
        def get_extra_info(self, info: str) -> object:
            return client_end if info == "socket" else None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers=SSE_HEADERS, stream=_SocketBody(), extensions={"network_stream": _NetworkStream()}
        )

    recorder, out = _recorder(monkeypatch, tmp_path, handler, stop)
    server_end.sendall(HEARTBEAT.encode("utf-8"))
    try:
        recorder.start()

        # The recorder has written the heartbeat and is now blocked on a
        # stream that will never send again.
        assert blocked.wait(timeout=5)
        stop.set()
        recorder.join(timeout=5)
        assert not recorder.is_alive()
    finally:
        stop.set()
        server_end.close()
        client_end.close()

    assert [line["kind"] for line in _lines(out)] == ["connected", "frame", "stopped"]


def test_real_socket_stop_ends_silent_stream(
    silent_sse_server: _SilentSseServer,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = threading.Event()
    frame_written = threading.Event()
    append_jsonl = capture.append_jsonl

    def spy_append(path: Path, record: Mapping[str, object]) -> None:
        append_jsonl(path, record)
        if record["kind"] == "frame":
            frame_written.set()

    monkeypatch.setattr(capture, "append_jsonl", spy_append)
    out = tmp_path / "sse_a_data.jsonl"
    recorder = SseRecorder(silent_sse_server.base_url, "a", "data", STREAM, out, 0.001, stop, lambda: None)

    with caplog.at_level(logging.WARNING, logger="p1rec.sse"):
        recorder.start()
        try:
            assert frame_written.wait(timeout=5)

            # Give the recorder time to block on the silent socket again, where
            # only the shutdown watcher can reach it before the read timeout.
            time.sleep(0.1)
        finally:
            stop.set()
        recorder.join(timeout=5)

    assert not recorder.is_alive()
    assert [line["kind"] for line in _lines(out)] == ["connected", "frame", "stopped"]
    assert silent_sse_server.accept_encodings == ["identity"]
    assert not any("exposes no socket" in record.getMessage() for record in caplog.records)


def test_silent_stream_times_out_then_reconnects(
    silent_sse_server: _SilentSseServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sse, "SSE_READ_TIMEOUT_S", 0.3)
    stop = threading.Event()
    out = tmp_path / "sse_a_data.jsonl"
    recorder = SseRecorder(silent_sse_server.base_url, "a", "data", STREAM, out, 0.001, stop, lambda: None)

    recorder.start()
    try:
        assert silent_sse_server.connections.acquire(timeout=5)
        assert silent_sse_server.connections.acquire(timeout=5)
    finally:
        stop.set()
    recorder.join(timeout=5)

    assert not recorder.is_alive()
    lines = _lines(out)
    assert [line["kind"] for line in lines] == ["connected", "frame", "disconnected", "connected", "stopped"]
    assert str(lines[2]["error"]).startswith("ReadTimeout")


def test_no_on_connected_when_stop_set_before_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stop = threading.Event()
    connected_calls: list[None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stop.set()
        return httpx.Response(200, headers=SSE_HEADERS, stream=_Chunks([HEARTBEAT.encode("utf-8")]))

    recorder, out = _recorder(
        monkeypatch, tmp_path, handler, stop, on_connected=lambda: connected_calls.append(None)
    )
    _run(recorder)

    assert connected_calls == []
    assert [line["kind"] for line in _lines(out)] == ["connected", "stopped"]


def test_outage_logs_warning_then_debug_then_recovery_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stop = threading.Event()
    attempts: list[httpx.Request] = []

    def dropped() -> httpx.Response:
        body = _Chunks([HEARTBEAT.encode("utf-8")], error=httpx.ReadError("connection reset by peer"))
        return httpx.Response(200, headers=SSE_HEADERS, stream=body)

    # Drop, two refusals, recovery that drops again, then a final recovery
    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) in (1, 4):
            return dropped()
        if len(attempts) in (2, 3):
            raise httpx.ConnectError("[Errno 111] Connection refused")
        return httpx.Response(200, headers=SSE_HEADERS, stream=_Chunks([], after=stop.set))

    recorder, _ = _recorder(monkeypatch, tmp_path, handler, stop)
    with caplog.at_level(logging.DEBUG, logger="p1rec.sse"):
        _run(recorder)

    messages = [(record.levelno, record.getMessage()) for record in caplog.records if record.name == "p1rec.sse"]
    failures = [(level, text) for level, text in messages if "disconnected" in text or "failed:" in text]
    assert [level for level, _ in failures] == [logging.WARNING, logging.DEBUG, logging.DEBUG, logging.WARNING]
    assert "ReadError: connection reset by peer" in failures[0][1]
    recoveries = [text for level, text in messages if level == logging.INFO and "reconnected" in text]
    assert len(recoveries) == 2
    assert re.search(r"after 3 failed attempt\(s\) over \d+\.\ds$", recoveries[0])
    assert re.search(r"after 1 failed attempt\(s\) over \d+\.\ds$", recoveries[1])

    # MockTransport responses carry no socket, which is flagged once per 200 connection
    assert sum("exposes no socket" in text for _, text in messages) == 3


def test_stop_during_retry_wait(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stop = threading.Event()
    attempted = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        attempted.set()
        raise httpx.ConnectError("[Errno 111] Connection refused")

    # A retry wait far longer than the join timeout below, so only an
    # interruptible wait lets the recorder finish in time.
    recorder, out = _recorder(monkeypatch, tmp_path, handler, stop, retry_s=3600.0)
    recorder.start()
    try:
        assert attempted.wait(timeout=5)
    finally:
        stop.set()
    recorder.join(timeout=5)

    assert not recorder.is_alive()
    lines = _lines(out)
    assert [line["kind"] for line in lines] == ["disconnected", "stopped"]
    assert lines[0]["error"] == "ConnectError: [Errno 111] Connection refused"


def test_non_200_recorded_without_on_connected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stop = threading.Event()
    connected_calls: list[None] = []

    handler = _serve_once(stop, httpx.Response(503, stream=_Chunks([b'{"detail":"starting"}'])))
    recorder, out = _recorder(
        monkeypatch, tmp_path, handler, stop, on_connected=lambda: connected_calls.append(None)
    )
    _run(recorder)

    lines = _lines(out)
    assert [line["kind"] for line in lines] == ["connected", "disconnected", "stopped"]
    assert lines[0]["status"] == 503
    assert lines[1]["partial"] == '{"detail":"starting"}'
    assert connected_calls == []


def test_on_connected_failure_keeps_recording(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stop = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=SSE_HEADERS, stream=_Chunks([HEARTBEAT.encode("utf-8")], after=stop.set))

    def on_connected() -> None:
        raise httpx.ConnectError("subscription request failed")

    recorder, out = _recorder(monkeypatch, tmp_path, handler, stop, on_connected)
    with caplog.at_level(logging.ERROR, logger="p1rec.sse"):
        _run(recorder)

    assert _frames(_lines(out)) == [HEARTBEAT]
    assert any("on_connected failed" in record.getMessage() for record in caplog.records)
