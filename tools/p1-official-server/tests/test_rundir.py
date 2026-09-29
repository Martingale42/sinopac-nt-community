"""Tests for p1rec.rundir: per-run result directory creation."""

from __future__ import annotations

from datetime import datetime, timezone
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


def test_new_run_refuses_existing_dir(tmp_path: Path) -> None:
    new_run(tmp_path, "orders", _NOW, tag=None)

    with pytest.raises(FileExistsError):
        new_run(tmp_path, "orders", _NOW, tag=None)


def test_new_run_refuses_existing_dir_with_same_tag(tmp_path: Path) -> None:
    new_run(tmp_path, "orders", _NOW, tag="soak")

    with pytest.raises(FileExistsError):
        new_run(tmp_path, "orders", _NOW, tag="soak")


def test_new_run_allows_different_tags_to_coexist(tmp_path: Path) -> None:
    first = new_run(tmp_path, "orders", _NOW, tag="a")
    second = new_run(tmp_path, "orders", _NOW, tag="b")

    assert first != second
    assert first.is_dir()
    assert second.is_dir()
