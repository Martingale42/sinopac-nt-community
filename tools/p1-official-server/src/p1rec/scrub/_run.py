"""Run `p1rec scrub`: guard results_root, build the map, then export fixtures or scan committed files."""

from __future__ import annotations

import subprocess
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

from p1rec.config import ConfigError, P1Config, read_secrets
from p1rec.scrub._common import ScrubError, category_of, logger
from p1rec.scrub._discovery import discover, key_categories, pointer_category
from p1rec.scrub._export import LeakError, export_fixtures
from p1rec.scrub._map import build_map, load_map, save_map
from p1rec.scrub._matching import MIN_SUBSTRING_MATCH_LEN, is_short, scan, scrub_text

# The server's OpenAPI document, committed beside the fixtures rather than exported from a recording
_KEPT_BESIDE_FIXTURES = frozenset({"openapi.json"})


def _missing_categories(cfg: P1Config, mapping: Mapping[str, str]) -> list[str]:
    """Return the pointer categories no known original belongs to."""
    wanted = {pointer_category(pointer) for pointer in (*cfg.scrub.account_id_pointers, *cfg.scrub.ca_pointers)}
    known = {category_of(placeholder) for placeholder in mapping.values()}
    return sorted(wanted - known)


def _scan_targets(cfg: P1Config) -> list[Path]:
    """Return what scrub --scan-only checks: fixtures_root's parent, contract_doc and extra_scan_paths.

    Raises ConfigError if any of them is missing, since a mistyped path
    would otherwise pass unscanned, or if one overlaps results_root, whose
    recordings hold the originals.
    """
    named = [
        ("fixtures_root's parent", cfg.fixtures_root.parent),
        ("scrub.contract_doc", cfg.scrub.contract_doc),
        *(("scrub.extra_scan_paths", extra) for extra in cfg.scrub.extra_scan_paths),
    ]
    targets: list[Path] = []
    for label, path in named:
        if not path.exists():
            raise ConfigError(f"{label} names a path that does not exist: {path}")
        targets.append(path)

    results_root = cfg.results_root.resolve()
    for target in targets:
        resolved = target.resolve()
        if results_root.is_relative_to(resolved) or resolved.is_relative_to(results_root):
            raise ConfigError(f"scan target {target} overlaps results_root {results_root}")
    return targets


def _git(args: list[str], cwd: Path) -> int:
    """Run a git command quietly in cwd and return its exit status."""
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False).returncode
    except OSError as exc:
        raise ConfigError(f"cannot run git in {cwd} ({exc})") from exc


def _require_git_ignored(results_root: Path, map_path: Path) -> None:
    """Raise ConfigError unless git ignores results_root and does not track the scrub map.

    Ignoring results_root keeps new recordings and the map out of commits,
    but a file already added stays tracked regardless.
    """
    if _git(["ls-files", "--error-unmatch", str(map_path)], results_root) == 0:
        raise ConfigError(f"scrub map {map_path} is tracked by git; remove it from the index before scrubbing")
    if _git(["check-ignore", "-q", str(results_root)], results_root) != 0:
        raise ConfigError(f"results_root {results_root} is not ignored by git, so the scrub map could be committed")


def _unselected_entries(fixtures_root: Path, written: Iterable[Path]) -> list[Path]:
    """Return what fixtures_root holds besides the files just written and those kept beside the fixtures."""
    produced = {path.name for path in written} | _KEPT_BESIDE_FIXTURES
    return sorted(entry for entry in fixtures_root.iterdir() if entry.name not in produced)


def _loggable_path(path: Path, mapping: Mapping[str, str]) -> str:
    """Return path scrubbed for a log line, NFKC-normalized first so a fullwidth original is replaced too."""
    return scrub_text(unicodedata.normalize("NFKC", str(path)), mapping)


def _log_short_originals(mapping: Mapping[str, str]) -> None:
    short = Counter(category_of(placeholder) for original, placeholder in mapping.items() if is_short(original))
    for category, count in sorted(short.items()):
        logger.warning(
            "%s: %d values shorter than %d UTF-8 bytes match only as whole tokens",
            category,
            count,
            MIN_SUBSTRING_MATCH_LEN,
        )


def run_scrub(cfg: P1Config, *, scan_only: bool) -> int:
    """Export the selected fixtures, or with scan_only scan the committed ones, and return the exit code.

    Both first check that git ignores results_root, then extend and save the
    scrub map with the current secrets and newly discovered identifiers.
    Returns 0 when no original was found, 2 when one was (each reported as
    path:line:category, the path scrubbed, never the value) or when the
    fixture directory holds files the selection did not produce, and 1 on
    a configuration or scrubbing error, including when a category the
    scrub pointers name has no known value, since its identifiers could
    then pass unscrubbed.
    """
    try:
        _require_git_ignored(cfg.results_root, cfg.scrub.map_path)
        mapping = build_map(read_secrets(cfg), discover(cfg), load_map(cfg.scrub.map_path))
        save_map(cfg.scrub.map_path, mapping)
        _log_short_originals(mapping)
        missing = _missing_categories(cfg, mapping)
        if missing:
            logger.error(
                "no %s value is known, so they cannot be scrubbed; record an accounts and a ca_status "
                "response first",
                ", ".join(missing),
            )
            return 1

        unselected: list[Path] = []
        if scan_only:
            leaks = scan(_scan_targets(cfg), mapping, key_categories(cfg))
        else:
            try:
                written = export_fixtures(cfg, mapping)
            except LeakError as exc:
                leaks = list(exc.leaks)
                logger.error(
                    "scrubbed output still holds original values; %s is untouched and the output is in %s",
                    cfg.fixtures_root,
                    exc.staging_dir,
                )
            else:
                leaks = []
                logger.info("exported %d files to %s", len(written), cfg.fixtures_root)
                unselected = _unselected_entries(cfg.fixtures_root, written)
    except (ConfigError, ScrubError) as exc:
        logger.error("scrub failed: %s", exc)
        return 1

    for leak in leaks:
        logger.error("%s:%d:%s", _loggable_path(leak.path, mapping), leak.line, leak.kind)
    for entry in unselected:
        logger.error(
            "%s was not produced by the current selection; nothing was removed", _loggable_path(entry, mapping)
        )
    if leaks or unselected:
        logger.error("found %d leaks and %d files the selection did not produce", len(leaks), len(unselected))
        return 2
    logger.info("no original value found")
    return 0
