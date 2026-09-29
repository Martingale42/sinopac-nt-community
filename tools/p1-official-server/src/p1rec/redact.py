"""Secret-redacting logging setup for p1rec.

setup_logging attaches a RedactingFormatter to the root logger's handlers,
so every logger in the process, including third-party ones such as httpx
and httpcore, has its records redacted before they reach recorder.log or
stderr. No secret value or fragment (including the api_key prefix used in
the clientname) should ever reach either. Because it redacts the fully
rendered line rather than just the message, it also covers text logging
appends after the message, such as exception tracebacks and stack_info.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# The official server's clientname is `PYAPI/{api_key[:10]}/...`, so this
# prefix can leak through clientname fields in responses even when the
# full key never appears in a log line.
API_KEY_PREFIX_LEN = 10


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


def setup_logging(run_dir: Path, secrets: Iterable[str], level: str) -> None:
    """Configure the root logger to write redacted records to recorder.log and stderr."""
    # A %-format mismatch makes Handler.emit() fall back to handleError,
    # which by default prints the traceback and the record's raw,
    # unredacted args straight to stderr; disabling it drops that one
    # record instead of leaking through a path the formatter never sees.
    logging.raiseExceptions = False

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = RedactingFormatter(secrets, _LOG_FORMAT)

    file_handler = logging.FileHandler(run_dir / "recorder.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
