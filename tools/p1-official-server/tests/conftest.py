"""Shared pytest fixtures for p1rec tests.

Shared constants and type aliases live in helpers.py; this module only
holds fixtures, which need pytest's machinery to be usable.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import MINIMAL_CONFIG_LINES, WriteConfig

# Third-party loggers setup_logging raises to WARNING
_QUIETED_LOGGERS = ("httpx", "httpcore")


@pytest.fixture
def write_config(tmp_path: Path) -> WriteConfig:
    """Write a p1.toml under tmp_path and return its path.

    Defaults to MINIMAL_CONFIG_LINES; pass a different list of lines to
    test a variation, such as a missing key or a bad value.
    """

    def _write(lines: list[str] | None = None) -> Path:
        config_path = tmp_path / "p1.toml"
        content = lines if lines is not None else MINIMAL_CONFIG_LINES
        config_path.write_text("\n".join(content) + "\n", encoding="utf-8")
        return config_path

    return _write


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Restore the root logger's state, the excepthooks and the quieted loggers' levels.

    setup_logging attaches handlers to the root logger and installs
    sys.excepthook / threading.excepthook, so a test that calls it must
    not leak any of that into unrelated tests; in particular, pytest's own
    log-capturing handler on the root logger must survive untouched.
    """
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_propagate = root.propagate
    original_sys_excepthook = sys.excepthook
    original_thread_excepthook = threading.excepthook
    original_quiet_levels = {name: logging.getLogger(name).level for name in _QUIETED_LOGGERS}

    yield

    for handler in list(root.handlers):
        if handler not in original_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(original_level)
    root.propagate = original_propagate
    sys.excepthook = original_sys_excepthook
    threading.excepthook = original_thread_excepthook
    for name, level in original_quiet_levels.items():
        logging.getLogger(name).setLevel(level)
