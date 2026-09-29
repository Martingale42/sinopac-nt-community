"""Tests for p1rec.rundir: per-run result directory creation."""

from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from p1rec.rundir import new_run

_NOW = datetime(2026, 9, 29, 1, 2, 3, tzinfo=timezone.utc)


def test_new_run_creates_timestamped_dir(tmp_path: Path) -> None:
    run_dir = new_run(tmp_path, "preflight", _NOW, tag=None)

    assert run_dir == tmp_path / "20260929T010203Z-preflight"
    assert run_dir.is_dir()


def test_new_run_appends_tag(tmp_path: Path) -> None:
    run_dir = new_run(tmp_path, "preflight", _NOW, tag="dryrun")

    assert run_dir == tmp_path / "20260929T010203Z-preflight-dryrun"
    assert run_dir.is_dir()


def test_new_run_creates_missing_results_root(tmp_path: Path) -> None:
    results_root = tmp_path / "results" / "p1"
    assert not results_root.exists()

    run_dir = new_run(results_root, "stream", _NOW, tag=None)

    assert run_dir.is_dir()


def test_new_run_converts_non_utc_to_utc(tmp_path: Path) -> None:
    taipei = timezone(timedelta(hours=8))
    taipei_now = datetime(2026, 9, 29, 9, 0, 0, tzinfo=taipei)

    run_dir = new_run(tmp_path, "preflight", taipei_now, tag=None)

    assert run_dir.name == "20260929T010000Z-preflight"


def test_new_run_rejects_naive_datetime(tmp_path: Path) -> None:
    naive_now = datetime(2026, 9, 29, 9, 0, 0)

    with pytest.raises(ValueError):
        new_run(tmp_path, "preflight", naive_now, tag=None)


def test_new_run_refuses_existing_dir(tmp_path: Path) -> None:
    new_run(tmp_path, "orders", _NOW, tag="soak")

    with pytest.raises(FileExistsError):
        new_run(tmp_path, "orders", _NOW, tag="soak")

    # A different tag is a different directory name, so it can coexist
    other = new_run(tmp_path, "orders", _NOW, tag="dryrun")
    assert other.is_dir()
    assert other != tmp_path / "20260929T010203Z-orders-soak"


def test_new_run_keeps_results_private(tmp_path: Path) -> None:
    results_root = tmp_path / "results" / "p1"

    run_dir = new_run(results_root, "stream", _NOW, tag=None)

    assert stat.S_IMODE(results_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700


def test_new_run_narrows_existing_results_root(tmp_path: Path) -> None:
    results_root = tmp_path / "results"
    results_root.mkdir(mode=0o755)
    results_root.chmod(0o755)

    new_run(results_root, "stream", _NOW, tag=None)

    assert stat.S_IMODE(results_root.stat().st_mode) == 0o700
