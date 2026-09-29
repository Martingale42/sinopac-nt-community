"""Create per-run result directories under results_root."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def new_run(results_root: Path, subcommand: str, now: datetime, tag: str | None) -> Path:
    """Create and return results_root/<UTC timestamp>-<subcommand>[-<tag>].

    now must be a UTC datetime; it is formatted as YYYYMMDDTHHMMSSZ. Raises
    FileExistsError if the target directory already exists, so a run never
    overwrites a previous one.
    """
    name = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{subcommand}"
    if tag:
        name = f"{name}-{tag}"
    run_dir = results_root / name
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir
