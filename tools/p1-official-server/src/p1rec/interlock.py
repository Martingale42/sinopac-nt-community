"""Abort unless the official server confirms it is in simulation mode.

Every code path that could reach a live order (place, update price, update
quantity, cancel, or close a filled position with a reverse order) must
call assert_simulation immediately before sending; every other subcommand
that talks to the server checks once at startup. That wiring belongs to
those subcommands; this module only provides the check itself, its
exception, and a network-free self-test.
"""

from __future__ import annotations

import contextvars
import json
import logging
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

import httpx

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

# cli.py's mapping from an InterlockError that escapes a subcommand to a
# process exit code, and self_test()'s own exit code when a case behaves
# unexpectedly, name the same value from this one place.
EXIT_INTERLOCK_ABORT = 3


class InterlockError(RuntimeError):
    """Raised when the official server is not confirmed to be in simulation mode."""


# Set for the duration of a self-test case's assert_simulation call, so
# _abort logs it as a self-test case instead of the "interlock check
# failed" template real callers get; self_test() reports each case's
# outcome itself, so the two must never both log the same abort.
_self_test_active: contextvars.ContextVar[bool] = contextvars.ContextVar("self_test_active", default=False)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """object_pairs_hook that raises ValueError if a JSON object repeats a key.

    A repeated key is ambiguous about which value the server meant, and
    json.loads otherwise silently keeps the last one, which would make the
    interlock trust whichever value happened to be written last.
    """
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON object")
        seen[key] = value
    return seen


def assert_simulation(client: CapturingClient, cfg: P1Config) -> None:
    """Raise InterlockError unless the official server reports simulation mode.

    Queries cfg.interlock.endpoint_op through client and requires a 2xx,
    JSON response whose value at cfg.interlock.pointer is exactly the JSON
    boolean true. Every other outcome aborts: a non-2xx status, a request
    that raises (a transport error or anything else), a body that is not
    valid JSON (including one with a duplicate key, or one whose nesting
    triggers a RecursionError), a missing pointer, false, or a non-boolean
    value. This function never lets simulation mode be simply unproven; an
    unproven check is always treated the same as a confirmed production
    server, since both must abort.
    """
    op = cfg.interlock.endpoint_op
    endpoint = cfg.endpoints[op]

    try:
        response = client.request(op, endpoint)
    except Exception as exc:
        _abort(f"{op} request failed: {exc}", cause=exc)

    if not 200 <= response.status < 300:
        _abort(f"{op} returned HTTP {response.status}, expected 2xx")

    try:
        body = json.loads(response.body, object_pairs_hook=_reject_duplicate_keys)
    except Exception as exc:
        _abort(f"{op} response is not valid JSON: {exc}", cause=exc)

    try:
        value = resolve_pointer(body, cfg.interlock.pointer)
    except JsonPointerError as exc:
        _abort(f"{op} response has no value at {cfg.interlock.pointer!r}: {exc}", cause=exc)

    # Python bools are also ints (1 == True), so only an identity check
    # rejects a non-boolean truthy value such as 1 or the string "true".
    if value is not True:
        _abort(f"{op} response at {cfg.interlock.pointer!r} is {value!r}, expected JSON true")

    logger.info("interlock check passed: %s%s is true", op, cfg.interlock.pointer)


def _abort(reason: str, cause: BaseException | None = None) -> NoReturn:
    if _self_test_active.get():
        # self_test() logs its own per-case outcome at the end of its
        # loop; logging the real template here too would print the same
        # abort twice, once generic and once case-labelled.
        logger.info("self-test: %s", reason)
    else:
        logger.error("interlock check failed: %s", reason)
    raise InterlockError(reason) from cause


_SELF_TEST_BASE_URL = "http://p1rec.self-test.invalid"

# self_test()'s synthetic client answers in-process from a fixed body and
# never waits, so this value is never actually exercised; it only
# satisfies CapturingClient's required timeout_s parameter.
_SELF_TEST_TIMEOUT_S = 1.0

# assert_simulation reads only the interlock and endpoints sections, so
# the self-test's event fields are never consulted and hold empty values.
_UNUSED_EVENT_FIELDS = EventFields(
    order_event_name="",
    order_states=(),
    deal_states=(),
    event_id="",
    order_key="",
    op_type="",
    op_code="",
    order_qty="",
    cancel_qty="",
    deal_key="",
    deal_qty="",
    op_code_success="",
    order_exchange_ts="",
    deal_exchange_ts="",
    rest_list="",
    rest_key="",
    rest_status="",
    rest_filled_qty="",
    rest_order_ts="",
    exchange_ts_pointers=(),
    exchange_ts_format=None,
    heartbeat_event="",
    gap_threshold_s=float("inf"),
)


def _self_test_cfg(interlock: InterlockConfig) -> P1Config:
    """Build the P1Config self_test() checks interlock against.

    The mode endpoint's method and path are fixed facts of the official
    server's contract (GET /api/v1/info), not something p1.toml
    configures, so they are hardcoded here rather than read from a
    p1.toml this function never opens.
    """
    return P1Config(
        base_url=_SELF_TEST_BASE_URL,
        http_timeout_s=_SELF_TEST_TIMEOUT_S,
        results_root=Path("."),
        fixtures_root=Path("."),
        secret_env={},
        endpoints={interlock.endpoint_op: Endpoint(method="GET", path="/api/v1/info")},
        interlock=interlock,
        contracts=ContractFields(),
        funds=FundsConfig(),
        stream=StreamConfig(),
        orders=OrdersConfig(),
        events=_UNUSED_EVENT_FIELDS,
        history=HistoryConfig(),
        scrub=ScrubConfig(),
    )


def _api_info_dict(*, simulation: bool, uds_plaintext: bool) -> dict[str, object]:
    """Return a dict shaped like the official server's ApiInfoResponse.

    Every field the schema requires is present (test_data/official/1.7.6/
    openapi.json, ApiInfoResponse), so a pointer misconfigured to some
    other real field, such as uds_plaintext, still resolves to a value
    instead of aborting on a missing pointer, letting self_test() notice
    the misconfiguration by its effect rather than by luck.
    """
    return {
        "name": "shioaji",
        "version": "1.7.6",
        "description": "Shioaji API server",
        "protocols": ["http"],
        "scheme": "http",
        "uds_plaintext": uds_plaintext,
        "simulation": simulation,
        "agent_harness": {
            "enabled": False,
            "mode": "off",
            "capability_version": 1,
            "capability_header": "X-Shioaji-Agent-Capability",
            "digest_scheme": "compact_keyed_blake3",
            "max_ttl_seconds": 60,
            "audience": None,
            "bootstrap": None,
        },
    }


def _api_info_body(*, simulation: bool, uds_plaintext: bool) -> bytes:
    return json.dumps(_api_info_dict(simulation=simulation, uds_plaintext=uds_plaintext)).encode("utf-8")


def _api_info_body_missing_simulation() -> bytes:
    body = _api_info_dict(simulation=True, uds_plaintext=False)
    del body["simulation"]
    return json.dumps(body).encode("utf-8")


class _Outcome(StrEnum):
    PASSED = "passed"
    INTERLOCK_ABORT = "aborted"
    UNEXPECTED = "raised an unexpected exception"


@dataclass(frozen=True)
class _CannedCase:
    name: str
    status: int
    body: bytes
    expected: _Outcome  # always PASSED or INTERLOCK_ABORT; never UNEXPECTED


# The five built-in fakes self_test() checks: a Production-shaped
# response (uds_plaintext true, unlike the Simulation response below, so
# a pointer misconfigured to "/uds_plaintext" instead of "/simulation"
# makes this case wrongly pass and self_test() catch it), two malformed
# responses, a server error, and a Simulation-shaped response.
_CANNED_CASES = (
    _CannedCase(
        "production", 200, _api_info_body(simulation=False, uds_plaintext=True), expected=_Outcome.INTERLOCK_ABORT
    ),
    _CannedCase(
        "missing_simulation_field", 200, _api_info_body_missing_simulation(), expected=_Outcome.INTERLOCK_ABORT
    ),
    _CannedCase(
        "http_500", 500, _api_info_body(simulation=True, uds_plaintext=False), expected=_Outcome.INTERLOCK_ABORT
    ),
    _CannedCase("non_json", 200, b"not json", expected=_Outcome.INTERLOCK_ABORT),
    _CannedCase(
        "simulation", 200, _api_info_body(simulation=True, uds_plaintext=False), expected=_Outcome.PASSED
    ),
)


def _canned_transport(status: int, body: bytes) -> httpx.MockTransport:
    """Return a transport that only answers GET /api/v1/info, 404 otherwise.

    Pinning the request shape means a bug that sent the wrong method or
    path would show up as an unexpected 404, rather than silently getting
    back the canned body meant for the real mode endpoint.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "GET" or request.url.path != "/api/v1/info":
            return httpx.Response(404)
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


def self_test(interlock: InterlockConfig) -> int:
    """Run assert_simulation against five canned mode responses, with no network I/O.

    interlock is the live p1.toml's [interlock] section, so a pointer
    misconfigured to some other real ApiInfoResponse field is exercised
    the same way a live check would be, not against a fixed default this
    function invents itself. Each case's outcome is one of three kinds:
    passed, aborted (InterlockError), or raised an unexpected exception;
    a case must match the kind _CANNED_CASES expects it to, not merely
    "did or did not pass", so a bug that aborts for the wrong reason, or
    an abort that turns into some other exception, is still caught.
    Returns 0 only if every case matches; returns EXIT_INTERLOCK_ABORT if
    any case does not, including one that raises unexpectedly, which is
    logged rather than left to propagate out of the self-test subcommand.
    """
    cfg = _self_test_cfg(interlock)
    all_ok = True

    with tempfile.TemporaryDirectory(prefix="p1rec-self-test-") as tmp_dir:
        rest_log = Path(tmp_dir) / "rest.jsonl"
        for case in _CANNED_CASES:
            detail = ""
            try:
                client = CapturingClient(
                    _SELF_TEST_BASE_URL,
                    "self-test",
                    rest_log,
                    timeout_s=_SELF_TEST_TIMEOUT_S,
                    transport=_canned_transport(case.status, case.body),
                )
                token = _self_test_active.set(True)
                try:
                    assert_simulation(client, cfg)
                    outcome = _Outcome.PASSED
                except InterlockError:
                    outcome = _Outcome.INTERLOCK_ABORT
                finally:
                    _self_test_active.reset(token)
                    client.close()
            except Exception as exc:
                # A case must never crash self_test() itself, since that
                # would propagate past the "no network" subcommand and be
                # reported as an ordinary failure instead of an interlock
                # self-check result.
                outcome = _Outcome.UNEXPECTED
                detail = f": {exc}"

            ok = outcome == case.expected
            all_ok = all_ok and ok
            if ok:
                logger.info("self-test case %s: %s as expected", case.name, outcome.value)
            else:
                logger.error(
                    "self-test case %s: expected to be %s but %s%s",
                    case.name,
                    case.expected.value,
                    outcome.value,
                    detail,
                )

    return 0 if all_ok else EXIT_INTERLOCK_ABORT
