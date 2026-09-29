"""Tests for p1rec.cli: shared setup wiring around subcommand handlers."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from p1rec.cli import main

_SECRET_ENV_VARS = ("SJ_API_KEY", "SJ_SEC_KEY", "SJ_CA_PASSWD", "P1_PERSON_ID", "P1_EGRESS_IP")

# A minimal but complete p1.toml, independent of the real p1.toml and of
# tests/test_config.py's fixture.
_MINIMAL_CONFIG_LINES = [
    'base_url = "http://127.0.0.1:8080"',
    "http_timeout_s = 35",
    'results_root = "results"',
    'fixtures_root = "fixtures"',
    "",
    "[secrets]",
    'api_key = "SJ_API_KEY"',
    'sec_key = "SJ_SEC_KEY"',
    'ca_passwd = "SJ_CA_PASSWD"',
    'person_id = "P1_PERSON_ID"',
    'egress_ip = "P1_EGRESS_IP"',
    "",
    "[endpoints.mode]",
    'method = "GET"',
    'path = "/api/v1/info"',
]


def _write_config(tmp_path: Path, lines: list[str] | None = None) -> Path:
    config_path = tmp_path / "p1.toml"
    config_path.write_text("\n".join(lines if lines is not None else _MINIMAL_CONFIG_LINES) + "\n", encoding="utf-8")
    return config_path


def _reset_p1rec_logger() -> None:
    logger = logging.getLogger("p1rec")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.filters.clear()


def test_subcommand_creates_single_run_dir_with_recorder_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    for var in _SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")

    try:
        exit_code = main(["stream", "--config", str(config_path)])

        assert exit_code == 1  # stub handler: not implemented yet
        run_dirs = list((tmp_path / "results").iterdir())
        assert len(run_dirs) == 1
        run_dir = run_dirs[0]
        assert re.fullmatch(r"\d{8}T\d{6}Z-stream", run_dir.name)
        assert (run_dir / "recorder.log").exists()
    finally:
        _reset_p1rec_logger()


def test_broken_config_exits_1_with_full_key_name_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = [line for line in _MINIMAL_CONFIG_LINES if line != "http_timeout_s = 35"]
    config_path = _write_config(tmp_path, lines)

    exit_code = main(["stream", "--config", str(config_path)])

    assert exit_code == 1
    assert "http_timeout_s" in capsys.readouterr().err


def test_self_test_interlock_runs_without_secret_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write_config(tmp_path)
    for var in _SECRET_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    try:
        exit_code = main(["self-test-interlock", "--config", str(config_path)])

        stderr = capsys.readouterr().err
        assert "environment variable" not in stderr
        for var in _SECRET_ENV_VARS:
            assert var not in stderr
        assert "self-test-interlock: not implemented yet" in stderr
        assert exit_code == 1  # stub handler, reached without a secrets ConfigError
    finally:
        _reset_p1rec_logger()
