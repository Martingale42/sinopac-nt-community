"""Command-line entry point for p1rec.

Wires the argparse skeleton for every subcommand. Each subcommand's real
behaviour is implemented separately; until then it logs that it is not
implemented yet and exits 1.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("p1rec.cli").error("%s: not implemented yet", args.command)
    return 1


if __name__ == "__main__":
    sys.exit(main())
