"""Tests for p1rec.interlock: the simulation-mode safety interlock."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from helpers import SECRET_ENV_VARS, WriteConfig

from p1rec import capture, cli, interlock
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
from p1rec.interlock import InterlockError, assert_simulation, self_test

_BASE_URL = "http://127.0.0.1:8080"
_OP = "mode"


def _build_cfg(tmp_path: Path) -> P1Config:
    return P1Config(
        base_url=_BASE_URL,
        http_timeout_s=5.0,
        results_root=tmp_path / "results",
        fixtures_root=tmp_path / "fixtures",
        secret_env={},
        endpoints={_OP: Endpoint(method="GET", path="/api/v1/info")},
        interlock=InterlockConfig(endpoint_op=_OP, pointer="/simulation"),
        contracts=ContractFields(),
        funds=FundsConfig(),
        stream=StreamConfig(),
        orders=OrdersConfig(),
        events=EventFields(),
        history=HistoryConfig(),
        scrub=ScrubConfig(),
    )


def _mock_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, body: bytes
) -> CapturingClient:
    def _make(base_url: str, timeout: httpx.Timeout) -> httpx.Client:
        transport = httpx.MockTransport(lambda _request: httpx.Response(status, content=body))
        return httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    monkeypatch.setattr(capture, "make_http_client", _make)
    return CapturingClient(_BASE_URL, "test", tmp_path / "rest.jsonl", timeout_s=5.0)


def _mock_client_with_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> CapturingClient:
    def _make(base_url: str, timeout: httpx.Timeout) -> httpx.Client:
        return httpx.Client(base_url=base_url, timeout=timeout, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(capture, "make_http_client", _make)
    return CapturingClient(_BASE_URL, "test", tmp_path / "rest.jsonl", timeout_s=5.0)


def test_true_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_cfg(tmp_path)
    client = _mock_client(tmp_path, monkeypatch, 200, b'{"simulation": true}')

    assert_simulation(client, cfg)  # must not raise


def test_false_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_cfg(tmp_path)
    client = _mock_client(tmp_path, monkeypatch, 200, b'{"simulation": false}')

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)


def test_missing_pointer_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_cfg(tmp_path)
    client = _mock_client(tmp_path, monkeypatch, 200, b"{}")

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)


def test_non_bool_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_cfg(tmp_path)
    # 1 == True in Python, so this only aborts if the check is an identity
    # check rather than an equality check.
    client = _mock_client(tmp_path, monkeypatch, 200, b'{"simulation": 1}')

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)


def test_http_500_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_cfg(tmp_path)
    client = _mock_client(tmp_path, monkeypatch, 500, b'{"simulation": true}')

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)


def test_non_json_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_cfg(tmp_path)
    client = _mock_client(tmp_path, monkeypatch, 200, b"not json")

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)


def test_transport_error_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_cfg(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused")

    client = _mock_client_with_handler(tmp_path, monkeypatch, handler)

    with pytest.raises(InterlockError) as exc_info:
        assert_simulation(client, cfg)

    # The original transport error must survive as the cause, not be
    # swallowed into a bare InterlockError.
    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)


def test_duplicate_keys_in_body_abort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _build_cfg(tmp_path)
    body = b'{"simulation": false, "simulation": true}'
    client = _mock_client(tmp_path, monkeypatch, 200, body)

    with pytest.raises(InterlockError):
        assert_simulation(client, cfg)


def test_self_test_returns_zero() -> None:
    assert self_test() == 0


def test_self_test_returns_3_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _raise(_client: object, _cfg: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(interlock, "assert_simulation", _raise)

    with caplog.at_level(logging.ERROR, logger="p1rec.interlock"):
        exit_code = self_test()

    assert exit_code == 3
    assert "unexpected exception" in caplog.text


def test_cli_exit_code_3(write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config()
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")

    def _raising_handler(_args: object, _cfg: object, _run_dir: Path) -> int:
        raise InterlockError("official server did not report simulation mode")

    original = cli._SUBCOMMANDS["stream"]
    monkeypatch.setitem(cli._SUBCOMMANDS, "stream", dataclasses.replace(original, handler=_raising_handler))

    exit_code = main(["stream", "--config", str(config_path)])

    assert exit_code == 3


def test_cli_exit_code_3_on_transport_error(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_config()
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused")

    def _make(base_url: str, timeout: httpx.Timeout) -> httpx.Client:
        return httpx.Client(base_url=base_url, timeout=timeout, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(capture, "make_http_client", _make)

    def _handler(_args: object, cfg: P1Config, run_dir: Path) -> int:
        client = CapturingClient(cfg.base_url, "test", run_dir / "rest.jsonl", timeout_s=cfg.http_timeout_s)
        assert_simulation(client, cfg)
        return 0

    original = cli._SUBCOMMANDS["stream"]
    monkeypatch.setitem(cli._SUBCOMMANDS, "stream", dataclasses.replace(original, handler=_handler))

    exit_code = main(["stream", "--config", str(config_path)])

    assert exit_code == 3
