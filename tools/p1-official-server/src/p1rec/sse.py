"""Split, parse and record Server-Sent Events streams verbatim.

SseRecorder writes one JSON line per event to its own file: a "frame" line
for each complete frame exactly as received, plus "connected",
"disconnected" and "stopped" lines for the connection's lifecycle.
split_frames and parse_frame follow the WHATWG Server-Sent Events rules for
line endings and fields. parse_frame is for analysis only and never
changes what was recorded.
"""

from __future__ import annotations

import base64
import codecs
import logging
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from p1rec import capture
from p1rec.config import Endpoint

logger = logging.getLogger(__name__)

_LINE_END_RE = re.compile(r"\r\n|\n|\r")


@dataclass(frozen=True)
class SseFrame:
    """One recorded frame and the fields parse_frame found in it."""

    raw: str
    event: str | None
    id: str | None
    data: str | None
    is_comment: bool


def split_frames(buffer: str) -> tuple[list[str], str]:
    """Split buffer into complete frames and the unterminated remainder.

    A frame ends at a blank line, and lines may end in CRLF, LF or a bare
    CR, mixed freely. Each frame keeps its own line endings and its
    terminating blank line, so the frames followed by the remainder
    concatenate back to buffer unchanged.
    """
    frames: list[str] = []
    frame_start = 0
    line_start = 0
    for match in _LINE_END_RE.finditer(buffer):
        # A CR at the very end may be the first half of a CRLF still in
        # flight, and cutting there would leave its LF to start the next
        # frame as a spurious blank line.
        if match.group() == "\r" and match.end() == len(buffer):
            break
        if match.start() == line_start:
            frames.append(buffer[frame_start : match.end()])
            frame_start = match.end()
        line_start = match.end()
    return frames, buffer[frame_start:]


def parse_frame(raw: str) -> SseFrame:
    """Parse one recorded frame into its event name, id and data.

    A line starting with ":" is a comment. Any other line is a field whose
    name runs up to the first ":" (the whole line if there is none), with
    exactly one leading space dropped from the value. data lines are
    joined with "\\n"; an id containing NUL is ignored, as are retry and
    unknown fields. is_comment is True only for a frame made entirely of
    comment lines.
    """
    event: str | None = None
    event_id: str | None = None
    data_lines: list[str] = []
    has_comment = False
    has_field = False

    for line in _LINE_END_RE.split(raw):
        if not line:
            continue
        if line.startswith(":"):
            has_comment = True
            continue
        has_field = True
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if name == "event":
            event = value
        elif name == "data":
            data_lines.append(value)
        elif name == "id" and "\0" not in value:
            event_id = value

    return SseFrame(
        raw=raw,
        event=event,
        id=event_id,
        data="\n".join(data_lines) if data_lines else None,
        is_comment=has_comment and not has_field,
    )


def _text_fields(key: str, text: str) -> dict[str, str]:
    """Return text under key, adding key_b64 with the exact bytes if any were not UTF-8.

    Received bytes are decoded with surrogateescape, so a stream that is
    not valid UTF-8 still splits into frames without losing a byte. Such
    text cannot be written as JSON, so key then holds it with U+FFFD in
    place of each bad sequence and key_b64 holds the bytes as received.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        data = text.encode("utf-8", "surrogateescape")
        return {key: data.decode("utf-8", "replace"), f"{key}_b64": base64.b64encode(data).decode("ascii")}
    return {key: text}


class SseRecorder(threading.Thread):
    """Record one SSE endpoint to its own JSONL file until stop is set.

    Each connection attempt that gets an HTTP response writes a "connected"
    line with its status, url, the Last-Event-ID sent and the response
    headers. Every complete frame is then written as a "frame" line with raw
    and recv_ts_ns, whatever the status, so an error body is kept too;
    recv_ts_ns is when the chunk completing the frame arrived. When the
    stream ends or fails, a "disconnected" line with the error is written
    and the recorder reconnects after retry_s, sending Last-Event-ID once
    the server has sent a non-empty id. Text received after the last
    complete frame is kept in a "partial" field of the disconnected or
    stopped line.

    on_connected runs after every 200 response, before its first frame is
    read; if it raises, the error is logged and recording continues.
    Setting stop ends the retry wait at once and shuts down the open
    connection's socket, so a read blocked on a silent stream returns
    without waiting for the next heartbeat. A connection attempt still
    waiting for response headers, or a running on_connected, is allowed to
    finish first. A "stopped" line is always written last.
    """

    last_event_id: str | None

    def __init__(
        self,
        base_url: str,
        client_name: str,
        stream_name: str,
        connect: Endpoint,
        out: Path,
        retry_s: float,
        stop: threading.Event,
        on_connected: Callable[[], None],
    ) -> None:
        super().__init__(name=f"sse-{client_name}-{stream_name}", daemon=True)
        self._base_url = base_url
        self._client_name = client_name
        self._stream_name = stream_name
        self._endpoint = connect
        self._out = out
        self._retry_s = retry_s

        # Named _stop_event because Python 3.12's Thread uses _stop internally
        self._stop_event = stop
        self._on_connected = on_connected
        self.last_event_id = None
        self._socket_lock = threading.Lock()
        self._socket: socket.socket | None = None

    def run(self) -> None:
        threading.Thread(target=self._shutdown_on_stop, name=f"{self.name}-stop", daemon=True).start()
        extra: dict[str, str] = {}
        try:
            tail = self._record()
            if tail:
                extra.update(_text_fields("partial", tail))
        except Exception as exc:
            # Not re-raised: threading.excepthook would print the traceback
            # straight to stderr, bypassing the redacting log formatter.
            logger.exception("SSE %s/%s recorder failed", self._client_name, self._stream_name)
            extra["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            self._write("stopped", **extra)
            logger.info("SSE %s/%s stopped", self._client_name, self._stream_name)

    def _record(self) -> str:
        """Connect and reconnect until stop is set, returning any unterminated text left at stop."""
        with capture.make_http_client(self._base_url, httpx.Timeout(None)) as client:
            while not self._stop_event.is_set():
                tail = self._stream_once(client)
                if tail is not None:
                    return tail
                if self._stop_event.wait(self._retry_s):
                    break
        return ""

    def _stream_once(self, client: httpx.Client) -> str | None:
        """Hold one connection open and record its frames.

        Return the unterminated text left over if stop was set by the time
        the connection ended, or None once the stream has ended or failed on
        its own and its "disconnected" line has been written.
        """
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
        if self.last_event_id:
            headers["Last-Event-ID"] = self.last_event_id
        decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
        buffer = ""
        error = "stream ended by server"

        try:
            with client.stream(self._endpoint.method, self._endpoint.path, headers=headers) as response:
                self._write(
                    "connected",
                    status=response.status_code,
                    url=str(response.url),
                    last_event_id=headers.get("Last-Event-ID"),
                    resp_headers=[[name, value] for name, value in response.headers.multi_items()],
                )
                logger.info(
                    "SSE %s/%s connected: HTTP %d", self._client_name, self._stream_name, response.status_code
                )
                self._track_socket(response)
                try:
                    if response.status_code == 200:
                        self._notify_connected()

                    # Checked only after the socket is tracked, so a stop set
                    # from here on is seen either by this check or by the
                    # shutdown watcher.
                    if not self._stop_event.is_set():
                        for chunk in response.iter_bytes():
                            recv_ts_ns = time.time_ns()
                            frames, buffer = split_frames(buffer + decoder.decode(chunk))
                            for frame in frames:
                                self._write_frame(frame, recv_ts_ns)
                            if self._stop_event.is_set():
                                break
                finally:
                    self._track_socket(None)
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"

        tail = buffer + decoder.decode(b"", final=True)
        if self._stop_event.is_set():
            return tail

        fields: dict[str, str] = {"error": error}
        if tail:
            fields.update(_text_fields("partial", tail))
        self._write("disconnected", **fields)
        logger.warning(
            "SSE %s/%s disconnected: %s; reconnecting in %ss",
            self._client_name,
            self._stream_name,
            error,
            self._retry_s,
        )
        return None

    def _track_socket(self, response: httpx.Response | None) -> None:
        """Remember the socket under response for the shutdown watcher, or forget it if None."""
        sock: socket.socket | None = None
        if response is not None:
            network_stream = response.extensions.get("network_stream")
            if network_stream is not None:
                sock = network_stream.get_extra_info("socket")
        with self._socket_lock:
            self._socket = sock

    def _shutdown_on_stop(self) -> None:
        """Wait for stop, then shut down the tracked socket to end a blocked read."""
        self._stop_event.wait()
        with self._socket_lock:
            if self._socket is not None:
                try:
                    self._socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    # The peer or httpx closed it first, which ends the read just the same
                    pass

    def _notify_connected(self) -> None:
        try:
            self._on_connected()
        except Exception:
            # A failing callback, such as a subscription request that could
            # not be sent, must not end a recording meant to run for days.
            logger.exception("SSE %s/%s on_connected failed", self._client_name, self._stream_name)

    def _write_frame(self, frame: str, recv_ts_ns: int) -> None:
        self._write("frame", recv_ts_ns=recv_ts_ns, **_text_fields("raw", frame))
        frame_id = parse_frame(frame).id
        if frame_id is not None:
            # An empty id resets the last event ID, as in the SSE spec
            self.last_event_id = frame_id or None

    def _write(self, kind: str, recv_ts_ns: int | None = None, **fields: object) -> None:
        record: dict[str, object] = {
            "client": self._client_name,
            "stream": self._stream_name,
            "kind": kind,
            "recv_ts_ns": time.time_ns() if recv_ts_ns is None else recv_ts_ns,
        }
        record.update(fields)
        capture.append_jsonl(self._out, record)
