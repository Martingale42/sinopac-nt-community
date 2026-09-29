"""Tests for p1rec.redact: secret-redacting logging setup."""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

import httpx
import pytest

from p1rec.redact import (
    API_KEY_PREFIX_LEN,
    RedactingFormatter,
    add_redaction_values,
    redaction_values,
    setup_logging,
)


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


def test_redacting_formatter_replaces_longest_match_first() -> None:
    full = "abcdefghij1234"
    prefix = full[:10]
    record = _make_record("key=%s prefix=%s", (full, prefix))

    result = RedactingFormatter([full, prefix], "%(message)s").format(record)

    assert result == "key=*** prefix=***"


def test_redacting_formatter_ignores_empty_secret() -> None:
    record = _make_record("hello world")

    result = RedactingFormatter(["", "world"], "%(message)s").format(record)

    assert result == "hello ***"


def test_redaction_values_includes_api_key_prefix() -> None:
    secrets = {
        "api_key": "abcdefghijklmnopqrstuvwx",
        "sec_key": "othersecretvalue",
        "ca_passwd": "",  # empty values must never be redaction targets
    }

    values = redaction_values(secrets)

    assert secrets["api_key"] in values
    assert secrets["api_key"][:API_KEY_PREFIX_LEN] in values
    assert secrets["sec_key"] in values
    assert "" not in values


def test_redaction_values_without_api_key() -> None:
    # self-test-interlock runs with no secrets at all
    assert redaction_values({}) == []


def test_log_redacts_secret_and_prefix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secrets = {"api_key": "abcdefghijklmnopqrstuvwx", "sec_key": "othersecretvalue"}
    full_key = secrets["api_key"]
    prefix = full_key[:API_KEY_PREFIX_LEN]

    setup_logging(tmp_path, secrets=redaction_values(secrets), level="INFO")
    logger = logging.getLogger("p1rec.orders")
    logger.info("clientname=PYAPI/%s/node1 full=%s", prefix, full_key)

    for handler in logging.getLogger().handlers:
        handler.flush()

    log_text = (tmp_path / "recorder.log").read_text(encoding="utf-8")
    stderr_text = capsys.readouterr().err

    for output in (log_text, stderr_text):
        assert full_key not in output
        assert prefix not in output
        assert "***" in output


def test_log_exception_redacts_secret_in_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    full_key = "abcdefghijklmnopqrstuvwx"
    prefix = full_key[:API_KEY_PREFIX_LEN]

    setup_logging(tmp_path, secrets=[full_key, prefix], level="INFO")
    logger = logging.getLogger("p1rec.orders")
    try:
        raise ValueError(f"rejected api_key={full_key} prefix={prefix}")
    except ValueError:
        logger.exception("order placement failed")

    for handler in logging.getLogger().handlers:
        handler.flush()

    log_text = (tmp_path / "recorder.log").read_text(encoding="utf-8")
    stderr_text = capsys.readouterr().err

    for output in (log_text, stderr_text):
        assert full_key not in output
        assert prefix not in output
        assert "***" in output
        # The traceback itself must survive, just with the secret gone
        assert "ValueError" in output


def test_malformed_record_leaves_notice_not_silence(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret_value = "abcdefghijklmnopqrstuvwx"

    setup_logging(tmp_path, secrets=[secret_value], level="INFO")

    # Outside of a test run, p1rec's two handlers are the only ones on the
    # root logger; pytest's own LogCaptureHandler deliberately re-raises a
    # formatting error to fail the test (by design, unrelated to this
    # behaviour), so it is detached here to check what p1rec's own
    # handlers do on their own, matching how the CLI actually runs.
    root = logging.getLogger()
    foreign_handlers = [h for h in root.handlers if not isinstance(h.formatter, RedactingFormatter)]
    for handler in foreign_handlers:
        root.removeHandler(handler)
    try:
        logger = logging.getLogger("p1rec.orders")
        # Two %-placeholders but one arg, so record.getMessage() raises
        # inside Handler.emit(); the base Handler.handleError would print
        # the traceback and the record's raw, unredacted args straight to
        # stderr, so this must leave a notice, not vanish silently either.
        logger.critical("residual position %s %s", secret_value)

        for handler in root.handlers:
            handler.flush()
    finally:
        for handler in foreign_handlers:
            root.addHandler(handler)

    log_text = (tmp_path / "recorder.log").read_text(encoding="utf-8")
    stderr_text = capsys.readouterr().err

    for output in (log_text, stderr_text):
        assert secret_value not in output
        assert "logging error at" in output
        assert "record dropped" in output


def test_sys_excepthook_redacts_secret(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret_value = "abcdefghijklmnopqrstuvwx"

    setup_logging(tmp_path, secrets=[secret_value], level="INFO")
    try:
        raise RuntimeError("boom " + secret_value)
    except RuntimeError:
        exc_type, exc_value, exc_tb = sys.exc_info()
        assert exc_type is not None
        sys.excepthook(exc_type, exc_value, exc_tb)

    for handler in logging.getLogger().handlers:
        handler.flush()

    log_text = (tmp_path / "recorder.log").read_text(encoding="utf-8")
    stderr_text = capsys.readouterr().err

    for output in (log_text, stderr_text):
        assert secret_value not in output
        assert "***" in output
        assert "RuntimeError" in output


def test_thread_exception_is_redacted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret_value = "abcdefghijklmnopqrstuvwx"

    setup_logging(tmp_path, secrets=[secret_value], level="INFO")

    def _raise() -> None:
        raise RuntimeError("sse failed " + secret_value)

    thread = threading.Thread(target=_raise, name="sse-worker")
    thread.start()
    thread.join()

    for handler in logging.getLogger().handlers:
        handler.flush()

    log_text = (tmp_path / "recorder.log").read_text(encoding="utf-8")
    stderr_text = capsys.readouterr().err

    for output in (log_text, stderr_text):
        assert secret_value not in output
        assert "***" in output
        assert "RuntimeError" in output
        assert "sse-worker" in output


def test_thread_system_exit_is_ignored(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(tmp_path, secrets=[], level="INFO")

    def _quit() -> None:
        raise SystemExit("quit")

    thread = threading.Thread(target=_quit, name="quitting-worker")
    thread.start()
    thread.join()

    for handler in logging.getLogger().handlers:
        handler.flush()

    log_text = (tmp_path / "recorder.log").read_text(encoding="utf-8")
    stderr_text = capsys.readouterr().err

    # Matches the default threading.excepthook: SystemExit is not logged
    assert log_text == ""
    assert "quitting-worker" not in stderr_text


def test_setup_logging_creates_recorder_log(tmp_path: Path) -> None:
    setup_logging(tmp_path, secrets=[], level="DEBUG")
    logging.getLogger("p1rec.preflight").debug("starting preflight")

    for handler in logging.getLogger().handlers:
        handler.flush()

    assert (tmp_path / "recorder.log").exists()
    assert "starting preflight" in (tmp_path / "recorder.log").read_text(encoding="utf-8")


def test_setup_logging_redacts_third_party_logger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret_value = "abcdefghijklmnopqrstuvwx"

    setup_logging(tmp_path, secrets=[secret_value], level="INFO")
    # httpx/httpcore log under their own names, not under "p1rec", so the
    # handlers must live on the root logger to catch them too.
    logging.getLogger("httpx").warning("request sent with token %s", secret_value)

    for handler in logging.getLogger().handlers:
        handler.flush()

    log_text = (tmp_path / "recorder.log").read_text(encoding="utf-8")
    stderr_text = capsys.readouterr().err

    assert secret_value not in log_text
    assert secret_value not in stderr_text
    assert "***" in log_text


def test_setup_logging_removes_only_its_own_handlers(tmp_path: Path) -> None:
    root = logging.getLogger()
    marker_handler = logging.NullHandler()
    root.addHandler(marker_handler)
    try:
        setup_logging(tmp_path, secrets=[], level="INFO")

        assert marker_handler in root.handlers
    finally:
        root.removeHandler(marker_handler)


def test_caplog_still_works_after_setup_logging(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    setup_logging(tmp_path, secrets=[], level="INFO")
    logging.getLogger("p1rec.orders").info("hello from caplog")

    assert "hello from caplog" in caplog.text


def test_httpx_request_url_not_logged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(tmp_path, secrets=[], level="INFO")
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))

    with httpx.Client(transport=transport) as client:
        client.get("http://127.0.0.1:8080/api/v1/auth/ca_expiretime", params={"person_id": "FAKEPID001"})

    for handler in logging.getLogger().handlers:
        handler.flush()

    log_text = (tmp_path / "recorder.log").read_text(encoding="utf-8")
    stderr_text = capsys.readouterr().err

    assert "FAKEPID001" not in log_text
    assert "HTTP Request" not in log_text
    assert "FAKEPID001" not in stderr_text


def test_added_redaction_value_is_redacted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(tmp_path, secrets=["fake-secret-0123456789"], level="INFO")
    logger = logging.getLogger("p1rec.test")
    logger.info("before: account 0000001")

    add_redaction_values(["0000001", "", "fake-user"])
    logger.info("after: account 0000001 held by fake-user, secret fake-secret-0123456789")

    for handler in logging.getLogger().handlers:
        handler.flush()

    log_lines = (tmp_path / "recorder.log").read_text(encoding="utf-8").splitlines()
    stderr_text = capsys.readouterr().err

    # Only lines formatted after the call are affected
    assert "before: account 0000001" in log_lines[0]
    assert log_lines[1].endswith("after: account *** held by ***, secret ***")
    assert "after: account *** held by ***, secret ***" in stderr_text
    assert "0000001 held" not in stderr_text


def test_added_value_keeps_longest_first_order() -> None:
    formatter = RedactingFormatter(["abcd"], "%(message)s")

    formatter.add_secrets(["abcdefgh"])

    assert formatter.format(_make_record("key=%s", ("abcdefgh",))) == "key=***"


def test_add_redaction_values_is_thread_safe() -> None:
    formatter = RedactingFormatter([], "%(message)s")
    values = [f"value-{index:04d}" for index in range(400)]

    threads = [
        threading.Thread(target=formatter.add_secrets, args=(values[start::4],)) for start in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rendered = formatter.format(_make_record(" ".join(values)))
    assert rendered == " ".join(["***"] * len(values))
