"""Tests for p1rec.interlock: the simulation-mode safety interlock."""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from helpers import SECRET_ENV_VARS, WriteConfig

from p1rec import cli, interlock
from p1rec.capture import CapturingClient
from p1rec.cli import main
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
from p1rec.interlock import EXIT_INTERLOCK_ABORT, InterlockError, assert_simulation, self_test

_BASE_URL = "http://127.0.0.1:8080"
_OP = "mode"
_INFO_PATH = "/api/v1/info"


def _build_cfg(tmp_path: Path) -> P1Config:
    return P1Config(
        base_url=_BASE_URL,
        http_timeout_s=5.0,
        results_root=tmp_path / "results",
        fixtures_root=tmp_path / "fixtures",
        secret_env={},
        endpoints={_OP: Endpoint(method="GET", path=_INFO_PATH)},
        interlock=InterlockConfig(endpoint_op=_OP, pointer="/simulation"),
        contracts=ContractFields(code="", limit_up="", limit_down="", reference="", etf_codes=()),
        funds=FundsConfig(stock_available_pointer="", futures_available_pointer="", futures_margin_twd={}),
        stream=StreamConfig(),
        orders=OrdersConfig(),
        events=EventFields(),
        history=HistoryConfig(),
        scrub=ScrubConfig(),
    )


def _pinned_transport(status: int, body: bytes) -> httpx.MockTransport:
    """Return a transport that only answers GET /api/v1/info, 404 otherwise.

    Pinning the request means a bug that sent the wrong method or path
    shows up as an unexpected 404 instead of silently getting back the
    canned body meant for the real mode endpoint.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "GET" or request.url.path != _INFO_PATH:
            return httpx.Response(404)
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


def _client(tmp_path: Path, status: int, body: bytes) -> CapturingClient:
    return CapturingClient(
        _BASE_URL, "test", tmp_path / "rest.jsonl", timeout_s=5.0, transport=_pinned_transport(status, body)
    )


def _client_with_handler(tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]) -> CapturingClient:
    return CapturingClient(
        _BASE_URL, "test", tmp_path / "rest.jsonl", timeout_s=5.0, transport=httpx.MockTransport(handler)
    )


def _assert_single_mode_request(rest_log: Path) -> None:
    lines = rest_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["op"] == "mode"


def test_true_passes(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path)
    client = _client(tmp_path, 200, b'{"simulation": true}')

    assert_simulation(client, cfg)  # must not raise

    _assert_single_mode_request(tmp_path / "rest.jsonl")


def test_false_aborts(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path)
    client = _client(tmp_path, 200, b'{"simulation": false}')

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)

    _assert_single_mode_request(tmp_path / "rest.jsonl")


def test_missing_pointer_aborts(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path)
    client = _client(tmp_path, 200, b"{}")

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)

    _assert_single_mode_request(tmp_path / "rest.jsonl")


def test_non_bool_aborts(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path)
    # 1 == True in Python, so this only aborts if the check is an identity
    # check rather than an equality check.
    client = _client(tmp_path, 200, b'{"simulation": 1}')

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)

    _assert_single_mode_request(tmp_path / "rest.jsonl")


def test_string_true_aborts(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path)
    # The JSON string "true" is not the JSON boolean true.
    client = _client(tmp_path, 200, b'{"simulation": "true"}')

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)

    _assert_single_mode_request(tmp_path / "rest.jsonl")


def test_http_500_aborts(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path)
    client = _client(tmp_path, 500, b'{"simulation": true}')

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)

    _assert_single_mode_request(tmp_path / "rest.jsonl")


def test_http_204_aborts(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path)
    # 204 is 2xx, but its empty body is not valid JSON.
    client = _client(tmp_path, 204, b"")

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)

    _assert_single_mode_request(tmp_path / "rest.jsonl")


def test_http_3xx_aborts(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path)
    client = _client(tmp_path, 302, b"")

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)

    _assert_single_mode_request(tmp_path / "rest.jsonl")


def test_non_json_aborts(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path)
    client = _client(tmp_path, 200, b"not json")

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)

    _assert_single_mode_request(tmp_path / "rest.jsonl")


def test_duplicate_keys_in_body_abort(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path)
    body = b'{"simulation": false, "simulation": true}'
    client = _client(tmp_path, 200, body)

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)

    _assert_single_mode_request(tmp_path / "rest.jsonl")


def test_transport_error_aborts(tmp_path: Path) -> None:
    cfg = _build_cfg(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused")

    client = _client_with_handler(tmp_path, handler)

    with pytest.raises(InterlockError) as exc_info:
        assert_simulation(client, cfg)

    # The original transport error must survive as the cause, not be
    # swallowed into a bare InterlockError.
    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)
    _assert_single_mode_request(tmp_path / "rest.jsonl")


def test_abort_logs_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _build_cfg(tmp_path)
    client = _client(tmp_path, 200, b'{"simulation": false}')

    with caplog.at_level(logging.ERROR, logger="p1rec.interlock"), pytest.raises(InterlockError):
        assert_simulation(client, cfg)

    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_self_test_returns_zero() -> None:
    assert self_test(InterlockConfig(endpoint_op="mode", pointer="/simulation")) == 0


def test_self_test_fails_on_misconfigured_pointer() -> None:
    # /uds_plaintext is true in the canned Production body and false in
    # the canned Simulation body: the opposite of what /simulation says,
    # so a pointer pointed at the wrong field must make the self-test
    # fail rather than quietly pass.
    misconfigured = InterlockConfig(endpoint_op="mode", pointer="/uds_plaintext")

    assert self_test(misconfigured) == EXIT_INTERLOCK_ABORT


def test_self_test_returns_3_when_abort_case_raises_unexpectedly(monkeypatch: pytest.MonkeyPatch) -> None:
    real_assert_simulation = interlock.assert_simulation

    def _wrapped(client: CapturingClient, cfg: P1Config) -> None:
        try:
            real_assert_simulation(client, cfg)
        except InterlockError as exc:
            # A genuine (non-simulation) abort that raises the wrong
            # exception type is exactly what the three-valued outcome
            # must catch: self_test() expects "aborted" for these four
            # cases, not "raised an unexpected exception".
            raise RuntimeError(str(exc)) from exc

    monkeypatch.setattr(interlock, "assert_simulation", _wrapped)

    result = self_test(InterlockConfig(endpoint_op="mode", pointer="/simulation"))

    assert result == EXIT_INTERLOCK_ABORT


def test_cli_exit_code_3(write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config()
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")

    def _raising_handler(_args: object, _cfg: object, _run_dir: Path) -> int:
        raise InterlockError("official server did not report simulation mode")

    original = cli._SUBCOMMANDS["stream"]
    monkeypatch.setitem(cli._SUBCOMMANDS, "stream", dataclasses.replace(original, handler=_raising_handler))

    exit_code = main(["stream", "--config", str(config_path)])

    assert exit_code == EXIT_INTERLOCK_ABORT


def test_cli_exit_code_3_on_transport_error(write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config()
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused")

    def _handler(_args: object, cfg: P1Config, run_dir: Path) -> int:
        client = CapturingClient(
            cfg.base_url,
            "test",
            run_dir / "rest.jsonl",
            timeout_s=cfg.http_timeout_s,
            transport=httpx.MockTransport(handler),
        )
        assert_simulation(client, cfg)
        return 0

    original = cli._SUBCOMMANDS["stream"]
    monkeypatch.setitem(cli._SUBCOMMANDS, "stream", dataclasses.replace(original, handler=_handler))

    exit_code = main(["stream", "--config", str(config_path)])

    assert exit_code == EXIT_INTERLOCK_ABORT
