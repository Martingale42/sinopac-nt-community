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

Identifiers only learned at run time, such as account ids in a live
response, are added to the active formatter through add_redaction_values.
httpx and httpcore are held at WARNING, since their INFO lines carry full
request URLs, query strings included, and rest.jsonl already records every
request.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterable, Mapping, Sequence
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

# The formatter the latest setup_logging installed, which
# add_redaction_values extends.
_active_formatter: RedactingFormatter | None = None

_QUIET_THIRD_PARTY_LOGGERS = ("httpx", "httpcore")


def redaction_values(secrets: Mapping[str, Sequence[str]]) -> list[str]:
    """Return every non-empty value of every role, plus each api_key value's clientname prefix."""
    values = [value for role_values in secrets.values() for value in role_values if value]
    values += [api_key[:API_KEY_PREFIX_LEN] for api_key in secrets.get("api_key", ()) if api_key]
    return values


class RedactingFormatter(logging.Formatter):
    """Replace secret substrings with '***' in the fully rendered log line.

    Redacting format()'s return value, rather than only record.msg/args,
    also covers text logging.Formatter appends after the message itself,
    such as an exception traceback (exc_info) or stack_info.
    """

    def __init__(self, secrets: Iterable[str], fmt: str) -> None:
        super().__init__(fmt)
        self._lock = threading.Lock()
        self._secrets = _longest_first(secrets)

    def add_secrets(self, values: Iterable[str]) -> None:
        """Redact values too in every line formatted from now on.

        Safe to call while other threads are logging: format() reads the
        whole tuple of secrets once per line, and the tuple is replaced,
        never mutated.
        """
        with self._lock:
            self._secrets = _longest_first((*self._secrets, *values))

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        for secret in self._secrets:
            formatted = formatted.replace(secret, "***")
        return formatted


def _longest_first(values: Iterable[str]) -> tuple[str, ...]:
    """Return the distinct non-empty values, longest first.

    Longest first, so a full secret is replaced before a shorter substring
    of it (such as its prefix) could partially match and leave the rest
    exposed, and empty values are dropped since replacing an empty string
    would match everywhere.
    """
    return tuple(sorted({value for value in values if value}, key=len, reverse=True))


def add_redaction_values(values: Iterable[str]) -> None:
    """Redact values in every line the handlers from setup_logging write from now on.

    Does nothing before setup_logging has run, since no p1rec handler
    exists yet to write a line; a later setup_logging starts again from
    its own secrets.
    """
    formatter = _active_formatter
    if formatter is not None:
        formatter.add_secrets(values)


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
    rather than printed raw, and holds httpx and httpcore at WARNING so
    request URLs stay out of the log.
    """
    global _active_formatter

    root = logging.getLogger()
    root.setLevel(level)
    for name in _QUIET_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    for handler in _installed_handlers:
        root.removeHandler(handler)
        handler.close()
    _installed_handlers.clear()

    formatter = RedactingFormatter(secrets, _LOG_FORMAT)
    _active_formatter = formatter

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
