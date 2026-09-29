"""Command-line entry point for p1rec.

Wires the argparse skeleton and the shared setup every subcommand needs:
config loading, secret reading, a run directory and redacted logging. Each
subcommand's real behaviour is implemented separately, as a handler taking
(args, cfg, run_dir) and returning its exit code; until implemented, a
handler logs that it is not implemented yet and returns 1.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from p1rec.config import ConfigError, P1Config, load_config, read_secrets
from p1rec.redact import redaction_values, setup_logging
from p1rec.rundir import new_run

_SUBCOMMANDS = (
    "self-test-interlock",
    "preflight",
    "stream",
    "orders",
    "reconcile",
    "stats",
    "history",
    "scrub",
)

# These subcommands additionally take --scenarios
_SCENARIOS_SUBCOMMANDS = frozenset({"preflight", "orders"})

# self-test-interlock is the check every other subcommand relies on having
# already passed, so it must be runnable before any secret is available
_NO_SECRETS_SUBCOMMANDS = frozenset({"self-test-interlock"})

Handler = Callable[[argparse.Namespace, P1Config, Path], int]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="p1rec",
        description="Black-box recorder for the official shioaji server 1.7.6.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in _SUBCOMMANDS:
        sub = subparsers.add_parser(name)
        sub.add_argument("--config", default="p1.toml", help="path to p1.toml (default: p1.toml)")
        sub.add_argument("--tag", default=None, help="optional run tag appended to the run directory name")
        if name in _SCENARIOS_SUBCOMMANDS:
            sub.add_argument(
                "--scenarios",
                default="scenarios.toml",
                help="path to scenarios.toml (default: scenarios.toml)",
            )

    return parser


def _not_implemented(args: argparse.Namespace, _cfg: P1Config, _run_dir: Path) -> int:
    logging.getLogger("p1rec.cli").error("%s: not implemented yet", args.command)
    return 1


_HANDLERS: dict[str, Handler] = dict.fromkeys(_SUBCOMMANDS, _not_implemented)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = load_config(Path(args.config))
        secrets = {} if args.command in _NO_SECRETS_SUBCOMMANDS else read_secrets(cfg)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    run_dir = new_run(cfg.results_root, args.command, datetime.now(timezone.utc), args.tag)
    setup_logging(run_dir, redaction_values(secrets), "INFO")

    handler = _HANDLERS[args.command]
    return handler(args, cfg, run_dir)


if __name__ == "__main__":
    sys.exit(main())
