"""Shared pytest fixtures for p1rec tests."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest

# Shape of the write_config fixture below: an optional list of TOML lines in,
# the written file's path out.
WriteConfig = Callable[..., Path]

# A minimal but complete p1.toml, shared by tests that need a valid config
# to load and mutate, independent of the real p1.toml.
MINIMAL_CONFIG_LINES = [
    'base_url = "http://127.0.0.1:8080"',
    "http_timeout_s = 35",
    'results_root = "results"',
    'fixtures_root = "fixtures"',
    "",
    "[secrets]",
    'api_key = "SJ_API_KEY"',
    'sec_key = "SJ_SEC_KEY"',
    'ca_passwd = "SJ_CA_PASSWD"',
    'person_id = "P1_PERSON_ID"',
    'egress_ip = "P1_EGRESS_IP"',
    "",
    "[endpoints.mode]",
    'method = "GET"',
    'path = "/api/v1/info"',
    "",
    "[endpoints.place_stock]",
    'method = "POST"',
    'path = "/api/v1/order/place_order"',
]

SECRET_ENV_VARS = ("SJ_API_KEY", "SJ_SEC_KEY", "SJ_CA_PASSWD", "P1_PERSON_ID", "P1_EGRESS_IP")


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
def _reset_logging() -> None:
    """Restore the root logger's handlers, level and raiseExceptions flag.

    setup_logging attaches handlers to the root logger so third-party
    loggers are redacted too, so a test that calls it must not leak a
    handler, a changed level or a disabled raiseExceptions flag into
    unrelated tests.
    """
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_propagate = root.propagate
    original_raise_exceptions = logging.raiseExceptions

    yield

    for handler in list(root.handlers):
        if handler not in original_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(original_level)
    root.propagate = original_propagate
    logging.raiseExceptions = original_raise_exceptions
