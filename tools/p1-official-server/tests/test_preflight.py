"""Tests for p1rec.preflight: step 0 against an in-process fake official server."""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest
from helpers import MINIMAL_CONFIG_LINES, SECRET_ENV_VARS, WriteConfig
from mock_official import (
    ENDPOINT_LINES,
    FUTURES_ACCOUNT,
    FUTURES_REQUIRED_TWD,
    SCENARIOS,
    STOCK_ACCOUNT,
    STOCK_ONLY_SCENARIOS,
    STOCK_REQUIRED_TWD,
    FakeServer,
    preflight_config,
    write_scenarios,
)

from p1rec import cli
from p1rec.cli import main
from p1rec.config import ConfigError, P1Config
from p1rec.interlock import InterlockError
from p1rec.preflight import EXIT_CHECK_FAILED, run_preflight
from p1rec.redact import setup_logging


def _run(tmp_path: Path, cfg: P1Config, server: FakeServer, scenarios: str = SCENARIOS) -> tuple[int, dict]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    exit_code = run_preflight(cfg, write_scenarios(tmp_path, scenarios), run_dir, transport=server.transport())
    return exit_code, json.loads((run_dir / "preflight.json").read_text(encoding="utf-8"))


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def test_sufficient_funds_exits_0_and_writes_json(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer(
        trading_limits={"trading_available": int(STOCK_REQUIRED_TWD)}, margin={"available_margin": 12000.0}
    )

    exit_code, report = _run(tmp_path, preflight_config(write_config), server)

    assert exit_code == 0
    assert list(report) == ["funds_ok", "required", "available", "connections", "accounts", "limits"]
    assert report == {
        "funds_ok": True,
        "required": {"stock_twd": STOCK_REQUIRED_TWD, "futures_margin_twd": FUTURES_REQUIRED_TWD},
        "available": {
            "stock_twd": STOCK_REQUIRED_TWD,
            "futures_margin_twd": "12000.0",
            "stock_sources": {"trading_limits": STOCK_REQUIRED_TWD, "balance": "1000000.0"},
        },
        "connections": 2,
        "accounts": {"F": True, "H": False, "S": True},
        "limits": {
            "0050": {"limit_up": "60.5", "limit_down": "49.5", "reference": "55.0"},
            "2890": {"limit_up": "25.3", "limit_down": "20.7", "reference": "23.0"},
            "TMFJ6": {"limit_up": "52559.0", "limit_down": "43003.0", "reference": "47781.0"},
        },
    }


def test_insufficient_exits_2_and_writes_json(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer(trading_limits={"trading_available": int(STOCK_REQUIRED_TWD) - 1})

    exit_code, report = _run(tmp_path, preflight_config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["funds_ok"] is False
    assert any("insufficient stock funds" in message for message in _messages(caplog, logging.ERROR))


def test_stock_compared_against_configured_source_only(tmp_path: Path, write_config: WriteConfig) -> None:
    # A large settlement balance must not hide a trading limit that is too small
    server = FakeServer(
        trading_limits={"trading_available": 100},
        balance={"acc_balance": 99999999.0, "date": "2026-09-29", "errmsg": ""},
    )

    exit_code, report = _run(tmp_path, preflight_config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["available"]["stock_sources"] == {"trading_limits": "100", "balance": "99999999.0"}


def test_missing_available_field_exits_2(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer(trading_limits={"trading_limit": 500000})

    exit_code, report = _run(tmp_path, preflight_config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["funds_ok"] is False
    assert report["available"]["stock_twd"] is None
    assert any("/trading_available" in message for message in _messages(caplog, logging.ERROR))


def test_non_numeric_available_field_exits_2(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer(margin={"available_margin": "lots"})

    exit_code, report = _run(tmp_path, preflight_config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["available"]["futures_margin_twd"] is None


def test_errmsg_on_compared_balance_is_failed_query_not_shortfall(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = preflight_config(write_config, report_only={})
    funds = dataclasses.replace(cfg.funds, stock_available_op="balance", stock_available_pointer="/acc_balance")
    cfg = dataclasses.replace(cfg, funds=funds)
    server = FakeServer(balance={"acc_balance": 0.0, "date": "2026-09-29", "errmsg": "query failed"})

    exit_code, report = _run(tmp_path, cfg, server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["available"]["stock_twd"] is None
    errors = _messages(caplog, logging.ERROR)
    assert any("reports an error" in message for message in errors)
    assert not any("insufficient" in message for message in errors)


def test_report_only_failure_warns_without_failing(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer(balance={"acc_balance": 0.0, "date": "2026-09-29", "errmsg": "query failed"})

    exit_code, report = _run(tmp_path, preflight_config(write_config), server)

    assert exit_code == 0
    assert report["available"]["stock_sources"] == {"trading_limits": "500000", "balance": None}
    assert any("report-only query failed" in message for message in _messages(caplog, logging.WARNING))


def test_unsigned_futures_account_warns(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer()
    server.accounts[1]["signed"] = False

    exit_code, report = _run(tmp_path, preflight_config(write_config), server)

    assert exit_code == 0
    assert report["accounts"]["F"] is False
    warnings = _messages(caplog, logging.WARNING)
    assert any("account type F is not signed" in message for message in warnings)
    # The unsigned H account is traded by no case, so it gets no warning
    assert not any("account type H" in message for message in warnings)


def test_stock_only_scenarios_need_no_futures_account(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer()
    del server.accounts[1]

    exit_code, report = _run(tmp_path, preflight_config(write_config, margins={}), server, STOCK_ONLY_SCENARIOS)

    assert exit_code == 0
    assert "/api/v1/portfolio/margin" not in server.paths()
    assert report["available"]["futures_margin_twd"] is None
    assert _messages(caplog, logging.ERROR) == []
    assert not any("account type" in message for message in _messages(caplog, logging.WARNING))


def test_missing_futures_margin_exits_2(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    exit_code, report = _run(tmp_path, preflight_config(write_config, margins={}), FakeServer())

    assert exit_code == EXIT_CHECK_FAILED
    assert report["funds_ok"] is False
    assert report["required"] == {"stock_twd": None, "futures_margin_twd": None}
    assert any("'TMFJ6'" in message for message in _messages(caplog, logging.ERROR))


def test_missing_futures_account_exits_2(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer()
    del server.accounts[1]

    exit_code, report = _run(tmp_path, preflight_config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["available"]["futures_margin_twd"] is None


def test_bad_contract_limits_exit_2(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer()
    server.stock_contracts["2890"]["limit_up"] = 0.0

    exit_code, report = _run(tmp_path, preflight_config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["limits"] == {}


def test_account_requests_copy_live_account_fields(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer()

    _run(tmp_path, preflight_config(write_config), server)

    request_keys = ("account_type", "broker_id", "account_id", "person_id")
    stock_request = {k: STOCK_ACCOUNT[k] for k in request_keys}
    assert server.bodies("/api/v1/portfolio/trading_limits") == [stock_request]
    assert server.bodies("/api/v1/portfolio/account_balance") == [stock_request]
    assert server.bodies("/api/v1/portfolio/margin") == [{k: FUTURES_ACCOUNT[k] for k in request_keys}]


def test_every_query_recorded_in_rest_jsonl(tmp_path: Path, write_config: WriteConfig) -> None:
    _run(tmp_path, preflight_config(write_config), FakeServer())

    lines = (tmp_path / "run" / "rest.jsonl").read_text(encoding="utf-8").splitlines()
    ops = [json.loads(line)["op"] for line in lines]
    assert ops == [
        "mode",
        "accounts",
        "trading_limits",
        "balance",
        "margin",
        "usage",
        "ca_status",
        "contracts_stock",
        "contracts_stock",
        "contracts_futures",
    ]


def test_interlock_abort_writes_json_and_raises(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer(simulation=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(InterlockError):
        run_preflight(preflight_config(write_config), write_scenarios(tmp_path), run_dir, transport=server.transport())

    assert server.paths() == ["/api/v1/info"]
    assert json.loads((run_dir / "preflight.json").read_text(encoding="utf-8"))["funds_ok"] is False


def test_malformed_scenarios_writes_json_and_raises(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scenarios = write_scenarios(tmp_path, "[[csae]]\nid = 'typo'\n")

    with pytest.raises(ConfigError, match=r"csae"):
        run_preflight(preflight_config(write_config), scenarios, run_dir, transport=server.transport())

    assert server.requests == []
    assert json.loads((run_dir / "preflight.json").read_text(encoding="utf-8"))["funds_ok"] is False


def test_refused_connection_mid_preflight_exits_2(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer(refuse={"/api/v1/portfolio/margin"})

    exit_code, report = _run(tmp_path, preflight_config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["funds_ok"] is False
    assert report["available"]["futures_margin_twd"] is None
    assert report["available"]["stock_twd"] == "500000"
    # Every query after the refused one still ran
    assert set(report["limits"]) == {"0050", "2890", "TMFJ6"}
    assert report["connections"] == 2
    assert any("ConnectError" in message for message in _messages(caplog, logging.ERROR))


def test_refused_usage_query_exits_2_with_funds_in_hand(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer(refuse={"/api/v1/auth/usage"})

    exit_code, report = _run(tmp_path, preflight_config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["funds_ok"] is False
    assert report["connections"] is None
    assert report["available"]["stock_twd"] == "500000"


def test_failed_accounts_query_skips_ca_status_quietly(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer(refuse={"/api/v1/auth/accounts"})

    exit_code, _report = _run(tmp_path, preflight_config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert "/api/v1/auth/ca_expiretime" not in server.paths()
    assert not any("person_id" in message for message in _messages(caplog, logging.WARNING))


def test_account_identifiers_redacted_from_log(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = preflight_config(write_config)
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    setup_logging(log_dir, secrets=[], level="INFO")

    _run(tmp_path, cfg, FakeServer())
    logging.getLogger("p1rec.test").error(
        "saw %s %s %s %s",
        STOCK_ACCOUNT["account_id"],
        FUTURES_ACCOUNT["account_id"],
        STOCK_ACCOUNT["person_id"],
        STOCK_ACCOUNT["username"],
    )
    for handler in logging.getLogger().handlers:
        handler.flush()

    log_text = (log_dir / "recorder.log").read_text(encoding="utf-8")
    assert "saw *** *** *** ***" in log_text
    for value in ("0000001", "0000002", "FAKEPID001", "fake-user"):
        assert value not in log_text


def test_cli_preflight_passes_scenarios_path(
    tmp_path: Path, write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")
    config_path = write_config([*MINIMAL_CONFIG_LINES, *ENDPOINT_LINES])
    seen: list[Path] = []

    def _fake_run_preflight(_cfg: P1Config, scenarios: Path, _run_dir: Path) -> int:
        seen.append(scenarios)
        return EXIT_CHECK_FAILED

    monkeypatch.setattr(cli, "run_preflight", _fake_run_preflight)

    exit_code = main(["preflight", "--config", str(config_path), "--scenarios", "custom.toml"])

    assert exit_code == EXIT_CHECK_FAILED
    assert seen == [Path("custom.toml")]


def test_cli_malformed_scenarios_exits_1(
    tmp_path: Path, write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")
    config_path = write_config([*MINIMAL_CONFIG_LINES, *ENDPOINT_LINES])
    scenarios = write_scenarios(tmp_path, "[[case]]\nid = 'only_an_id'\n")

    exit_code = main(["preflight", "--config", str(config_path), "--scenarios", str(scenarios)])

    assert exit_code == 1
    assert any("missing key" in message for message in _messages(caplog, logging.ERROR))
