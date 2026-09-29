"""Command-line entry point for p1rec.

Wires the argparse skeleton and the shared setup every subcommand needs:
config loading, secret reading, a run directory and redacted logging. Each
subcommand is registered once, in _SUBCOMMANDS, with its own argument
hook and handler, so adding a subcommand-specific flag never touches this
module's dispatch logic. A handler takes (args, cfg, run_dir) and returns
its exit code; until implemented, a handler logs that it is not
implemented yet and returns 1.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

from p1rec.config import ConfigError, P1Config, load_config, read_secrets
from p1rec.history import run_history
from p1rec.interlock import EXIT_INTERLOCK_ABORT, InterlockError, self_test
from p1rec.orders import run_orders
from p1rec.preflight import run_preflight
from p1rec.redact import redaction_values, setup_logging
from p1rec.rundir import new_run
from p1rec.scenarios import load_scenarios

Handler = Callable[[argparse.Namespace, P1Config, Path], int]
ArgumentHook = Callable[[argparse.ArgumentParser], None]

_TAG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_tag(value: str) -> str:
    """Reject a --tag value that could corrupt the run directory name."""
    if not _TAG_RE.fullmatch(value) or ".." in value:
        raise argparse.ArgumentTypeError(
            f"invalid --tag {value!r}: must match {_TAG_RE.pattern!r} and not contain '..'"
        )
    return value


class _ArgumentParser(argparse.ArgumentParser):
    """An ArgumentParser whose usage errors exit 1.

    Exit code 2 is reserved for p1rec's own "check failed" outcome, so a
    malformed command line must not collide with it. add_subparsers()
    passes parser_class=type(self) by default, so every subparser
    inherits this too.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def _no_extra_arguments(_sub: argparse.ArgumentParser) -> None:
    pass


def _add_scenarios_argument(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--scenarios",
        default="scenarios.toml",
        help="path to scenarios.toml (default: scenarios.toml)",
    )


def _parse_only(value: str) -> set[str]:
    """Parse --only's comma-separated case ids, rejecting an empty id."""
    ids = [part.strip() for part in value.split(",")]
    if not all(ids):
        raise argparse.ArgumentTypeError(f"invalid --only {value!r}: expected comma-separated case ids")
    return set(ids)


def _parse_base_url(value: str) -> str:
    """Accept an http or https URL with a host, such as http://127.0.0.1:18081."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise argparse.ArgumentTypeError(f"invalid --base-url {value!r}: expected http://host:port")
    return value


def _add_orders_arguments(sub: argparse.ArgumentParser) -> None:
    _add_scenarios_argument(sub)
    sub.add_argument(
        "--only",
        type=_parse_only,
        default=None,
        help="comma-separated case ids to run, still in scenarios.toml order (default: every case)",
    )
    sub.add_argument(
        "--base-url",
        type=_parse_base_url,
        default=None,
        help="official server to send this run to, overriding base_url in p1.toml",
    )


def _not_implemented(args: argparse.Namespace, _cfg: P1Config, _run_dir: Path) -> int:
    logging.getLogger("p1rec.cli").error("%s: not implemented yet", args.command)
    return 1


def _self_test_interlock(_args: argparse.Namespace, cfg: P1Config, _run_dir: Path) -> int:
    return self_test(cfg.interlock)


def _history(_args: argparse.Namespace, cfg: P1Config, run_dir: Path) -> int:
    return run_history(cfg, run_dir)


def _preflight(args: argparse.Namespace, cfg: P1Config, run_dir: Path) -> int:
    return run_preflight(cfg, Path(args.scenarios), run_dir)


def _orders(args: argparse.Namespace, cfg: P1Config, run_dir: Path) -> int:
    if args.base_url is not None:
        logging.getLogger("p1rec.cli").info("orders: base_url %s from --base-url", args.base_url)
        cfg = dataclasses.replace(cfg, base_url=args.base_url)
    return run_orders(cfg, load_scenarios(Path(args.scenarios)), run_dir, args.only)


@dataclass(frozen=True)
class Subcommand:
    add_arguments: ArgumentHook
    handler: Handler
    needs_secrets: bool = True


_SUBCOMMANDS: dict[str, Subcommand] = {
    "self-test-interlock": Subcommand(
        add_arguments=_no_extra_arguments,
        handler=_self_test_interlock,
        # self-test-interlock never touches the network and is run
        # without the env file loaded, so it must work with no secrets
        # available.
        needs_secrets=False,
    ),
    "preflight": Subcommand(add_arguments=_add_scenarios_argument, handler=_preflight),
    "stream": Subcommand(add_arguments=_no_extra_arguments, handler=_not_implemented),
    "orders": Subcommand(add_arguments=_add_orders_arguments, handler=_orders),
    "reconcile": Subcommand(add_arguments=_no_extra_arguments, handler=_not_implemented),
    "stats": Subcommand(add_arguments=_no_extra_arguments, handler=_not_implemented),
    "history": Subcommand(add_arguments=_no_extra_arguments, handler=_history),
    "scrub": Subcommand(add_arguments=_no_extra_arguments, handler=_not_implemented),
}


def _add_common_arguments(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--config", default="p1.toml", help="path to p1.toml (default: p1.toml)")
    sub.add_argument(
        "--tag",
        default=None,
        type=_validate_tag,
        help="optional run tag appended to the run directory name",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="p1rec",
        description="Black-box recorder for the official shioaji server 1.7.6.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, spec in _SUBCOMMANDS.items():
        sub = subparsers.add_parser(name)
        _add_common_arguments(sub)
        spec.add_arguments(sub)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    spec = _SUBCOMMANDS[args.command]

    try:
        cfg = load_config(Path(args.config))
        secrets = read_secrets(cfg) if spec.needs_secrets else {}
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    logging_ready = False
    try:
        run_dir = new_run(cfg.results_root, args.command, datetime.now(timezone.utc), args.tag)
        setup_logging(run_dir, redaction_values(secrets), "INFO")
        logging_ready = True
        return spec.handler(args, cfg, run_dir)
    except InterlockError:
        # assert_simulation already logged the reason at ERROR before
        # raising; logging again here would print the same abort twice.
        return EXIT_INTERLOCK_ABORT
    except ConfigError as exc:
        # A handler that loads its own input, such as scenarios.toml,
        # reports a malformed file the way a malformed p1.toml is reported.
        logging.getLogger("p1rec.cli").error("%s: %s", args.command, exc)
        return 1
    except Exception as exc:
        # A future subcommand-specific error will get its own except
        # clause here ahead of this catch-all, so this stays the one place
        # that decides how a failure is reported.
        if logging_ready:
            logging.getLogger("p1rec.cli").exception("%s failed", args.command)
        else:
            print(f"error: failed to start {args.command}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
