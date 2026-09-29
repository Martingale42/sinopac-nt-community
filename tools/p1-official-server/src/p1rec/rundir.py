"""Create per-run result directories under results_root."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def new_run(results_root: Path, subcommand: str, now: datetime, tag: str | None) -> Path:
    """Create and return results_root/<UTC timestamp>-<subcommand>[-<tag>].

    now is converted to UTC and formatted as YYYYMMDDTHHMMSSZ; it must be
    timezone-aware, since a naive datetime's offset is ambiguous. Raises
    FileExistsError if the target directory already exists, so a run never
    overwrites a previous one.
    """
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")

    utc_now = now.astimezone(timezone.utc)
    name = f"{utc_now.strftime('%Y%m%dT%H%M%SZ')}-{subcommand}"
    if tag:
        name = f"{name}-{tag}"
    run_dir = results_root / name
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir
