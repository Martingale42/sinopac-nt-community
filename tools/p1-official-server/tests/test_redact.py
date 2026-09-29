"""Tests for p1rec.redact: secret-redacting logging setup."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from p1rec.redact import RedactingFilter, setup_logging


def _make_record(msg: str, args: tuple[object, ...] = ()) -> logging.LogRecord:
    return logging.LogRecord(
        name="p1rec.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def _reset_p1rec_logger() -> None:
    logger = logging.getLogger("p1rec")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.filters.clear()


def test_redacting_filter_replaces_longest_match_first() -> None:
    full = "abcdefghij1234"
    prefix = full[:10]
    record = _make_record("key=%s prefix=%s", (full, prefix))

    result = RedactingFilter([full, prefix]).filter(record)

    assert result is True
    assert record.getMessage() == "key=*** prefix=***"


def test_redacting_filter_ignores_empty_secret() -> None:
    record = _make_record("hello world")

    RedactingFilter(["", "world"]).filter(record)

    assert record.getMessage() == "hello ***"


def test_log_redacts_secret_and_prefix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    full_key = "abcdefghijklmnopqrstuvwx"
    prefix = full_key[:10]

    try:
        setup_logging(tmp_path, secrets=[full_key, prefix], level="INFO")
        logger = logging.getLogger("p1rec.orders")
        logger.info("clientname=PYAPI/%s/node1 full=%s", prefix, full_key)

        for handler in logging.getLogger("p1rec").handlers:
            handler.flush()

        log_text = (tmp_path / "recorder.log").read_text(encoding="utf-8")
        stderr_text = capsys.readouterr().err

        for output in (log_text, stderr_text):
            assert full_key not in output
            assert prefix not in output
            assert "***" in output
    finally:
        _reset_p1rec_logger()


def test_setup_logging_creates_recorder_log(tmp_path: Path) -> None:
    try:
        setup_logging(tmp_path, secrets=[], level="DEBUG")
        logging.getLogger("p1rec.preflight").debug("starting preflight")

        for handler in logging.getLogger("p1rec").handlers:
            handler.flush()

        assert (tmp_path / "recorder.log").exists()
        assert "starting preflight" in (tmp_path / "recorder.log").read_text(encoding="utf-8")
    finally:
        _reset_p1rec_logger()
