"""Create per-run result directories under results_root."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

# Recordings hold account identifiers and the scrub map holds every
# original, and this host is shared, so only the owner may enter them.
_PRIVATE_DIR_MODE = 0o700


def new_run(results_root: Path, subcommand: str, now: datetime, tag: str | None) -> Path:
    """Create and return results_root/<UTC timestamp>-<subcommand>[-<tag>].

    now is converted to UTC and formatted as YYYYMMDDTHHMMSSZ; it must be
    timezone-aware, since a naive datetime's offset is ambiguous. Raises
    FileExistsError if the target directory already exists, so a run never
    overwrites a previous one. results_root and the run directory are made
    accessible to their owner only, including a results_root that already
    existed with wider permissions.
    """
    if now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")

    utc_now = now.astimezone(timezone.utc)
    name = f"{utc_now.strftime('%Y%m%dT%H%M%SZ')}-{subcommand}"
    if tag:
        name = f"{name}-{tag}"

    # chmod as well as mkdir's mode, which a umask can narrow and which an existing directory ignores
    results_root.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    results_root.chmod(_PRIVATE_DIR_MODE)
    run_dir = results_root / name
    run_dir.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=False)
    run_dir.chmod(_PRIVATE_DIR_MODE)
    return run_dir
