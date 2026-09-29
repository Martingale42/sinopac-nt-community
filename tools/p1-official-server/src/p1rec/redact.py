"""Secret-redacting logging setup for p1rec.

setup_logging attaches a RedactingFilter to every handler so that no
secret value or fragment (including the api_key prefix used in the
clientname) ever reaches recorder.log or stderr.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RedactingFilter(logging.Filter):
    """Replace configured secret substrings with '***' in every log record."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()

        # Longest first, so a full secret is replaced before a shorter
        # substring of it (such as its prefix) could partially match and
        # leave the rest exposed, and empty values are dropped since
        # replacing an empty string would match everywhere.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "***")
        record.msg = message
        record.args = ()
        return True


def setup_logging(run_dir: Path, secrets: Iterable[str], level: str) -> None:
    """Configure the p1rec logger to write redacted records to recorder.log and stderr."""
    logger = logging.getLogger("p1rec")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT)
    redacting_filter = RedactingFilter(secrets)

    file_handler = logging.FileHandler(run_dir / "recorder.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redacting_filter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redacting_filter)
    logger.addHandler(stream_handler)
