"""Tests for p1rec.cli: shared setup wiring around subcommand handlers."""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from helpers import MINIMAL_CONFIG_LINES, SECRET_ENV_VARS, WriteConfig

from p1rec import cli
from p1rec.cli import main


def _set_all_secret_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")


def _delete_all_secret_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in SECRET_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_subcommand_creates_single_run_dir_with_recorder_log(
    tmp_path: Path, write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, stub_subcommand: str
) -> None:
    config_path = write_config()
    _set_all_secret_env_vars(monkeypatch)

    exit_code = main([stub_subcommand, "--config", str(config_path)])

    assert exit_code == 0  # stub handler
    run_dirs = list((tmp_path / "results").iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert re.fullmatch(r"\d{8}T\d{6}Z-stub", run_dir.name)
    assert (run_dir / "recorder.log").exists()


def test_broken_config_exits_1_with_full_key_name_on_stderr(
    write_config: WriteConfig, capsys: pytest.CaptureFixture[str], stub_subcommand: str
) -> None:
    lines = [line for line in MINIMAL_CONFIG_LINES if line != "http_timeout_s = 35"]
    config_path = write_config(lines)

    exit_code = main([stub_subcommand, "--config", str(config_path)])

    assert exit_code == 1
    assert "http_timeout_s" in capsys.readouterr().err


def test_self_test_interlock_runs_without_secret_env_vars(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_config()
    _delete_all_secret_env_vars(monkeypatch)

    exit_code = main(["self-test-interlock", "--config", str(config_path)])

    stderr = capsys.readouterr().err
    assert "environment variable" not in stderr
    for var in SECRET_ENV_VARS:
        assert var not in stderr
    assert exit_code == 0  # all five canned responses behaved as expected


def test_handler_exception_is_redacted(
    tmp_path: Path,
    write_config: WriteConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_subcommand: str,
) -> None:
    config_path = write_config()
    secret_value = "abcdefghijklmnopqrstuvwx"
    monkeypatch.setenv("SJ_API_KEY", secret_value)
    for var in SECRET_ENV_VARS[1:]:
        monkeypatch.setenv(var, "fake-value-0123456789")

    def _raising_handler(_args: object, _cfg: object, _run_dir: Path) -> int:
        raise KeyError(secret_value)

    original = cli._SUBCOMMANDS[stub_subcommand]
    monkeypatch.setitem(cli._SUBCOMMANDS, stub_subcommand, dataclasses.replace(original, handler=_raising_handler))

    exit_code = main([stub_subcommand, "--config", str(config_path)])

    assert exit_code == 1
    stderr = capsys.readouterr().err
    assert secret_value not in stderr
    assert "***" in stderr

    run_dirs = list((tmp_path / "results").iterdir())
    log_text = (run_dirs[0] / "recorder.log").read_text(encoding="utf-8")
    assert secret_value not in log_text
    assert "***" in log_text


def test_new_run_collision_gives_clean_error(
    tmp_path: Path,
    write_config: WriteConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_subcommand: str,
) -> None:
    config_path = write_config()
    _set_all_secret_env_vars(monkeypatch)

    frozen_now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        # Same signature as datetime.now(tz=...), which p1rec.cli calls
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return frozen_now

    monkeypatch.setattr(cli, "datetime", _FrozenDatetime)

    first_exit_code = main([stub_subcommand, "--config", str(config_path)])
    second_exit_code = main([stub_subcommand, "--config", str(config_path)])

    assert first_exit_code == 0  # stub handler
    assert second_exit_code == 1  # collision, reported before logging exists
    stderr = capsys.readouterr().err
    assert "Traceback" not in stderr
    assert "error:" in stderr


def test_unknown_flag_exits_1(capsys: pytest.CaptureFixture[str], stub_subcommand: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([stub_subcommand, "--nonexistent"])

    assert exc_info.value.code == 1


def test_tag_rejects_path_traversal(write_config: WriteConfig, stub_subcommand: str) -> None:
    config_path = write_config()

    with pytest.raises(SystemExit) as exc_info:
        main([stub_subcommand, "--config", str(config_path), "--tag", "../escape"])

    assert exc_info.value.code == 1


def test_tag_accepts_ordinary_value(
    tmp_path: Path, write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, stub_subcommand: str
) -> None:
    config_path = write_config()
    _set_all_secret_env_vars(monkeypatch)

    exit_code = main([stub_subcommand, "--config", str(config_path), "--tag", "soak-1"])

    assert exit_code == 0  # stub handler
    run_dirs = list((tmp_path / "results").iterdir())
    assert run_dirs[0].name.endswith("-stub-soak-1")
