"""Abort unless the official server confirms it is in simulation mode.

Every code path that could reach a live order (place, update price, update
quantity, cancel) must call assert_simulation immediately before sending;
every other subcommand that talks to the server checks once at startup.
That wiring belongs to those subcommands; this module only provides the
check itself, its exception, and a network-free self-test.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import httpx

from p1rec import capture
from p1rec.capture import CapturingClient
from p1rec.config import (
    ContractFields,
    Endpoint,
    EventFields,
    FundsConfig,
    HistoryConfig,
    InterlockConfig,
    OrdersConfig,
    P1Config,
    ScrubConfig,
    StreamConfig,
)
from p1rec.jsonpointer import JsonPointerError, resolve_pointer

logger = logging.getLogger(__name__)


class InterlockError(RuntimeError):
    """Raised when the official server is not confirmed to be in simulation mode."""


def assert_simulation(client: CapturingClient, cfg: P1Config) -> None:
    """Raise InterlockError unless the official server reports simulation mode.

    Queries cfg.interlock.endpoint_op through client and requires a 2xx,
    JSON response whose value at cfg.interlock.pointer is exactly the JSON
    boolean true. Every other outcome, including a non-2xx status, a
    non-JSON body, a missing pointer, false, or a non-boolean value, is
    treated as not-simulation and aborts.
    """
    op = cfg.interlock.endpoint_op
    endpoint = cfg.endpoints[op]

    response = client.request(op, endpoint)
    if not 200 <= response.status < 300:
        _abort(f"{op} returned HTTP {response.status}, expected 2xx")

    try:
        body = response.json()
    except ValueError as exc:
        _abort(f"{op} response is not valid JSON: {exc}")

    try:
        value = resolve_pointer(body, cfg.interlock.pointer)
    except JsonPointerError as exc:
        _abort(f"{op} response has no value at {cfg.interlock.pointer!r}: {exc}")

    # Python bools are also ints (1 == True), so only an identity check
    # rejects a non-boolean truthy value such as 1 or "true".
    if value is not True:
        _abort(f"{op} response at {cfg.interlock.pointer!r} is {value!r}, expected JSON true")

    logger.info("interlock check passed: %s%s is true", op, cfg.interlock.pointer)


def _abort(reason: str) -> NoReturn:
    logger.error("interlock check failed: %s", reason)
    raise InterlockError(reason)


_SELF_TEST_BASE_URL = "http://p1rec.self-test.invalid"
_SELF_TEST_OP = "mode"


def _self_test_config() -> P1Config:
    """Build the P1Config self_test() checks against, independent of any p1.toml."""
    return P1Config(
        base_url=_SELF_TEST_BASE_URL,
        http_timeout_s=1.0,
        results_root=Path("."),
        fixtures_root=Path("."),
        secret_env={},
        endpoints={_SELF_TEST_OP: Endpoint(method="GET", path="/api/v1/info")},
        interlock=InterlockConfig(endpoint_op=_SELF_TEST_OP, pointer="/simulation"),
        contracts=ContractFields(),
        funds=FundsConfig(),
        stream=StreamConfig(),
        orders=OrdersConfig(),
        events=EventFields(),
        history=HistoryConfig(),
        scrub=ScrubConfig(),
    )


@dataclass(frozen=True)
class _CannedCase:
    name: str
    status: int
    body: bytes
    should_pass: bool


# The five built-in fakes self_test() checks: a real Production server, two
# malformed responses, a server error, and a real Simulation server.
_CANNED_CASES = (
    _CannedCase("production", 200, b'{"simulation": false}', should_pass=False),
    _CannedCase("missing_field", 200, b"{}", should_pass=False),
    _CannedCase("http_500", 500, b'{"simulation": true}', should_pass=False),
    _CannedCase("non_json", 200, b"not json", should_pass=False),
    _CannedCase("simulation", 200, b'{"simulation": true}', should_pass=True),
)


def _canned_client(rest_log: Path, status: int, body: bytes) -> CapturingClient:
    """Build a CapturingClient whose traffic is served from memory, never a socket.

    CapturingClient always builds its httpx.Client through the module-level
    capture.make_http_client, so swapping that factory for the single call
    inside CapturingClient.__init__, then restoring it immediately, injects
    an httpx.MockTransport without ever opening a connection.
    """

    def _make(base_url: str, timeout: httpx.Timeout) -> httpx.Client:
        transport = httpx.MockTransport(lambda _request: httpx.Response(status, content=body))
        return httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    original = capture.make_http_client
    capture.make_http_client = _make
    try:
        return CapturingClient(_SELF_TEST_BASE_URL, "self-test", rest_log, timeout_s=1.0)
    finally:
        capture.make_http_client = original


def self_test() -> int:
    """Run assert_simulation against five canned mode responses, with no network I/O.

    Returns 0 only if the four non-simulation responses (production, a
    response missing the simulation field, an HTTP 500, and a non-JSON
    body) each raise InterlockError and the simulation response passes;
    returns 3 if any case behaves unexpectedly.
    """
    cfg = _self_test_config()
    all_ok = True

    with tempfile.TemporaryDirectory(prefix="p1rec-self-test-") as tmp_dir:
        rest_log = Path(tmp_dir) / "rest.jsonl"
        for case in _CANNED_CASES:
            client = _canned_client(rest_log, case.status, case.body)
            try:
                assert_simulation(client, cfg)
                passed = True
            except InterlockError:
                passed = False
            finally:
                client.close()

            ok = passed == case.should_pass
            all_ok = all_ok and ok
            logger.info(
                "self-test case %s: %s (expected to %s)",
                case.name,
                "passed" if passed else "aborted",
                "pass" if case.should_pass else "abort",
            )

    return 0 if all_ok else 3
