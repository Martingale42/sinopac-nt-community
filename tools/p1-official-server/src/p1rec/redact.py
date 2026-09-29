"""Secret-redacting logging setup for p1rec.

setup_logging attaches a RedactingFormatter to the root logger's handlers,
so every logger in the process, including third-party ones such as httpx
and httpcore, has its records redacted before they reach recorder.log or
stderr. No secret value or fragment (including the api_key prefix used in
the clientname) should ever reach either. Because it redacts the fully
rendered line rather than just the message, it also covers text logging
appends after the message, such as exception tracebacks and stack_info.
It also installs sys.excepthook and threading.excepthook, so an exception
that escapes every try/except, in the main thread or a worker thread, is
logged, and therefore redacted, instead of printed raw by the
interpreter's default handler.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import TracebackType

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# The official server's clientname is `PYAPI/{api_key[:10]}/...`, so this
# prefix can leak through clientname fields in responses even when the
# full key never appears in a log line.
API_KEY_PREFIX_LEN = 10

# Handlers setup_logging has installed on the root logger, so a later call
# tears down only its own handlers and leaves everything else, such as
# pytest's log-capturing handler, alone.
_installed_handlers: list[logging.Handler] = []


def redaction_values(secrets: Mapping[str, str]) -> list[str]:
    """Return every non-empty secret value, plus the api_key clientname prefix."""
    values = [value for value in secrets.values() if value]
    api_key = secrets.get("api_key")
    if api_key:
        values.append(api_key[:API_KEY_PREFIX_LEN])
    return values


class RedactingFormatter(logging.Formatter):
    """Replace secret substrings with '***' in the fully rendered log line.

    Redacting format()'s return value, rather than only record.msg/args,
    also covers text logging.Formatter appends after the message itself,
    such as an exception traceback (exc_info) or stack_info.
    """

    def __init__(self, secrets: Iterable[str], fmt: str) -> None:
        super().__init__(fmt)

        # Longest first, so a full secret is replaced before a shorter
        # substring of it (such as its prefix) could partially match and
        # leave the rest exposed, and empty values are dropped since
        # replacing an empty string would match everywhere.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        for secret in self._secrets:
            formatted = formatted.replace(secret, "***")
        return formatted


class _SafeHandlerMixin:
    """Report a formatting failure without ever printing the raw record.

    The base Handler.handleError prints the traceback and the record's
    raw, unredacted args straight to stderr, bypassing the formatter
    entirely; this override instead writes one fixed, secret-free line
    naming where the record came from, so a malformed record is visible
    rather than vanishing silently, but its content never leaks.
    """

    def handleError(self, record: logging.LogRecord) -> None:
        notice = f"logging error at {record.pathname}:{record.lineno}; record dropped\n"
        stream = getattr(self, "stream", None)
        if stream is None:
            return
        try:
            stream.write(notice)
            stream.flush()
        except Exception:
            pass


class _RedactingFileHandler(_SafeHandlerMixin, logging.FileHandler):
    pass


class _RedactingStreamHandler(_SafeHandlerMixin, logging.StreamHandler):
    pass


def _log_uncaught_exception(
    exc_type: type[BaseException],
    exc_value: BaseException | None,
    exc_traceback: TracebackType | None,
    *,
    thread_name: str | None = None,
) -> None:
    """Log an otherwise-uncaught exception through the redacting handlers.

    exc_type.__name__ is named directly in the message, not left to
    exc_info's traceback dump, so the type still shows even if exc_value
    is None.
    """
    logger = logging.getLogger(__name__)
    if thread_name is not None:
        logger.error(
            "uncaught exception in thread %s: %s",
            thread_name,
            exc_type.__name__,
            exc_info=(exc_type, exc_value, exc_traceback),
        )
    else:
        logger.error("uncaught exception: %s", exc_type.__name__, exc_info=(exc_type, exc_value, exc_traceback))


def _excepthook(
    exc_type: type[BaseException], exc_value: BaseException | None, exc_traceback: TracebackType | None
) -> None:
    _log_uncaught_exception(exc_type, exc_value, exc_traceback)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        # Matches the default threading.excepthook: SystemExit in a thread
        # is a normal way to end it, not an error worth logging.
        return
    thread_name = args.thread.name if args.thread is not None else "unknown"
    _log_uncaught_exception(args.exc_type, args.exc_value, args.exc_traceback, thread_name=thread_name)


def setup_logging(run_dir: Path, secrets: Iterable[str], level: str) -> None:
    """Configure the root logger to write redacted records to recorder.log and stderr.

    Also installs sys.excepthook and threading.excepthook so an exception
    that escapes every try/except is logged, and therefore redacted,
    rather than printed raw.
    """
    root = logging.getLogger()
    root.setLevel(level)

    for handler in _installed_handlers:
        root.removeHandler(handler)
        handler.close()
    _installed_handlers.clear()

    formatter = RedactingFormatter(secrets, _LOG_FORMAT)

    file_handler = _RedactingFileHandler(run_dir / "recorder.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    _installed_handlers.append(file_handler)

    stream_handler = _RedactingStreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    _installed_handlers.append(stream_handler)

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
