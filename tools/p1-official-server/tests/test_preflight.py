"""Tests for p1rec.preflight: step 0 against an in-process fake official server."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from helpers import MINIMAL_CONFIG_LINES, SECRET_ENV_VARS, WriteConfig

from p1rec import cli
from p1rec.cli import main
from p1rec.config import P1Config, load_config
from p1rec.interlock import InterlockError
from p1rec.preflight import EXIT_CHECK_FAILED, run_preflight
from p1rec.redact import setup_logging

_ENDPOINT_LINES = [
    "",
    "[endpoints.accounts]",
    'method = "GET"',
    'path = "/api/v1/auth/accounts"',
    "",
    "[endpoints.balance]",
    'method = "POST"',
    'path = "/api/v1/portfolio/account_balance"',
    "",
    "[endpoints.margin]",
    'method = "POST"',
    'path = "/api/v1/portfolio/margin"',
    "",
    "[endpoints.usage]",
    'method = "GET"',
    'path = "/api/v1/auth/usage"',
    "",
    "[endpoints.ca_status]",
    'method = "GET"',
    'path = "/api/v1/auth/ca_expiretime"',
    "",
    "[endpoints.contracts_stock]",
    'method = "GET"',
    'path = "/api/v1/data/contracts/{code}/info"',
    "",
    "[endpoints.contracts_futures]",
    'method = "GET"',
    'path = "/api/v1/data/contracts/futures"',
]

_MARGIN_TABLE = "[funds.futures_margin_twd]"

# One filling and one resting case per market; the filling stock case needs
# 1 * 1000 * 25.3 = 25300 TWD and the filling futures case 1 * 12000 TWD.
_SCENARIOS = """
[[case]]
id = "stock_mkt_ioc"
market = "stock"
code = "2890"
action = "Buy"
price_ref = "limit_up"
price_type = "MKT"
order_type = "IOC"
quantity = 1
expect_fill = true
steps = ["place"]

[[case]]
id = "odd_far_rod"
market = "odd"
code = "0050"
action = "Buy"
price_ref = "far_below"
price_type = "LMT"
order_type = "ROD"
quantity = 1
expect_fill = false
steps = ["place", "cancel"]

[[case]]
id = "futures_mkt_ioc"
market = "futures"
code = "TMFJ6"
action = "Buy"
price_ref = "limit_up"
price_type = "MKT"
order_type = "IOC"
quantity = 1
expect_fill = true
steps = ["place"]
"""

_STOCK_ONLY_SCENARIOS = _SCENARIOS.split('[[case]]\nid = "futures_mkt_ioc"')[0]

_STOCK_ACCOUNT = {
    "account_type": "S",
    "broker_id": "0000",
    "account_id": "0000001",
    "person_id": "FAKEPID001",
    "username": "fake-user",
    "signed": True,
}
_FUTURES_ACCOUNT = {**_STOCK_ACCOUNT, "account_type": "F", "broker_id": "F000000", "account_id": "0000002"}
_HEDGE_ACCOUNT = {**_STOCK_ACCOUNT, "account_type": "H", "account_id": "0000003", "signed": False}

_STOCK_INFO_RE = re.compile(r"^/api/v1/data/contracts/([^/]+)/info$")


def _stock_record(code: str, limit_up: float, limit_down: float, reference: float) -> dict[str, object]:
    return {
        "security_type": "STK",
        "region": "TW",
        "exchange": "TSE",
        "code": code,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "reference": reference,
    }


def _futures_record(code: str, limit_up: float, limit_down: float, reference: float) -> dict[str, object]:
    return {
        "security_type": "FUT",
        "region": "TW",
        "exchange": "TAIFEX",
        "code": code,
        "root": "TMF",
        "limit_up": limit_up,
        "limit_down": limit_down,
        "reference": reference,
    }


@dataclass
class FakeServer:
    """Answers every endpoint preflight queries, with fields named as in openapi.json."""

    simulation: bool = True
    accounts: list[dict[str, object]] = field(
        default_factory=lambda: [dict(_STOCK_ACCOUNT), dict(_FUTURES_ACCOUNT), dict(_HEDGE_ACCOUNT)]
    )
    balance: dict[str, object] = field(
        default_factory=lambda: {"acc_balance": 1000000.0, "date": "2026-09-29", "errmsg": ""}
    )
    margin: dict[str, object] = field(default_factory=lambda: {"available_margin": 500000.0, "equity": 500000.0})
    stock_contracts: dict[str, dict[str, object]] = field(
        default_factory=lambda: {
            "2890": _stock_record("2890", 25.3, 20.7, 23.0),
            "0050": _stock_record("0050", 60.5, 49.5, 55.0),
        }
    )
    futures_contracts: dict[str, list[dict[str, object]]] = field(
        default_factory=lambda: {
            "TMF": [
                _futures_record("TMFK6", 53000.0, 43400.0, 48200.0),
                _futures_record("TMFJ6", 52559.0, 43003.0, 47781.0),
            ]
        }
    )
    refuse: set[str] = field(default_factory=set)  # Paths whose connection is refused
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path in self.refuse:
            raise httpx.ConnectError("[Errno 111] Connection refused", request=request)
        route = (request.method, request.url.path)
        if route == ("GET", "/api/v1/info"):
            return httpx.Response(200, json={"simulation": self.simulation})
        if route == ("GET", "/api/v1/auth/accounts"):
            return httpx.Response(200, json=self.accounts)
        if route == ("POST", "/api/v1/portfolio/account_balance"):
            return httpx.Response(200, json=self.balance)
        if route == ("POST", "/api/v1/portfolio/margin"):
            return httpx.Response(200, json=self.margin)
        if route == ("GET", "/api/v1/auth/usage"):
            return httpx.Response(
                200, json={"connections": 2, "bytes": 0, "limit_bytes": 2147483648, "remaining_bytes": 2147483648}
            )
        if route == ("GET", "/api/v1/auth/ca_expiretime"):
            return httpx.Response(
                200, json={"person_id": request.url.params["person_id"], "expire_time": "2027-01-01 00:00:00"}
            )
        if route == ("GET", "/api/v1/data/contracts/futures"):
            return httpx.Response(200, json=self.futures_contracts.get(request.url.params["root"], []))
        match = _STOCK_INFO_RE.match(request.url.path)
        if request.method == "GET" and match and match.group(1) in self.stock_contracts:
            return httpx.Response(200, json=self.stock_contracts[match.group(1)])
        return httpx.Response(404, json={"code": 404, "message": "not found", "details": {}})

    def bodies(self, path: str) -> list[object]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


def _config(write_config: WriteConfig, *, margins: dict[str, int] | None = None) -> P1Config:
    lines = [*MINIMAL_CONFIG_LINES, *_ENDPOINT_LINES]
    margins = {"TMFJ6": 12000} if margins is None else margins
    at = lines.index(_MARGIN_TABLE) + 1
    lines[at:at] = [f"{code} = {value}" for code, value in margins.items()]
    return load_config(write_config(lines))


def _scenarios(tmp_path: Path, text: str = _SCENARIOS) -> Path:
    path = tmp_path / "scenarios.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _run(tmp_path: Path, cfg: P1Config, server: FakeServer, scenarios: str = _SCENARIOS) -> tuple[int, dict]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    exit_code = run_preflight(
        cfg, _scenarios(tmp_path, scenarios), run_dir, transport=httpx.MockTransport(server.handler)
    )
    return exit_code, json.loads((run_dir / "preflight.json").read_text(encoding="utf-8"))


def test_sufficient_funds_exits_0_and_writes_json(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer(
        balance={"acc_balance": 25300.0, "date": "2026-09-29", "errmsg": ""},
        margin={"available_margin": 12000.0},
    )

    exit_code, report = _run(tmp_path, _config(write_config), server)

    assert exit_code == 0
    assert list(report) == ["funds_ok", "required", "available", "connections", "accounts", "limits"]
    assert report == {
        "funds_ok": True,
        "required": {"stock_twd": "25300.0", "futures_margin_twd": "12000"},
        "available": {"stock_twd": "25300.0", "futures_margin_twd": "12000.0"},
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
    server = FakeServer(balance={"acc_balance": 25299.0, "date": "2026-09-29", "errmsg": ""})

    exit_code, report = _run(tmp_path, _config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["funds_ok"] is False
    assert report["available"]["stock_twd"] == "25299.0"
    assert any(r.levelno == logging.ERROR and "insufficient stock funds" in r.getMessage() for r in caplog.records)


def test_missing_available_field_exits_2(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer(balance={"date": "2026-09-29", "errmsg": ""})

    exit_code, report = _run(tmp_path, _config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["funds_ok"] is False
    assert report["available"]["stock_twd"] is None
    assert any(r.levelno == logging.ERROR and "/acc_balance" in r.getMessage() for r in caplog.records)


def test_non_numeric_available_field_exits_2(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer(margin={"available_margin": "lots"})

    exit_code, report = _run(tmp_path, _config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["available"]["futures_margin_twd"] is None


def test_unsigned_futures_account_warns(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer()
    server.accounts[1]["signed"] = False

    exit_code, report = _run(tmp_path, _config(write_config), server)

    assert exit_code == 0
    assert report["accounts"]["F"] is False
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("account type F is not signed" in message for message in warnings)
    # The unsigned H account is traded by no case, so it gets no warning
    assert not any("account type H" in message for message in warnings)


def test_unsigned_futures_account_silent_without_futures_cases(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer()
    server.accounts[1]["signed"] = False

    exit_code, _report = _run(tmp_path, _config(write_config), server, _STOCK_ONLY_SCENARIOS)

    assert exit_code == 0
    assert not any(r.levelno == logging.WARNING and "account type" in r.getMessage() for r in caplog.records)


def test_missing_futures_margin_exits_2(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    exit_code, report = _run(tmp_path, _config(write_config, margins={}), FakeServer())

    assert exit_code == EXIT_CHECK_FAILED
    assert report["funds_ok"] is False
    assert report["required"] == {"stock_twd": None, "futures_margin_twd": None}
    assert any(r.levelno == logging.ERROR and "'TMFJ6'" in r.getMessage() for r in caplog.records)


def test_missing_futures_account_exits_2(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer()
    del server.accounts[1]

    exit_code, report = _run(tmp_path, _config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["available"]["futures_margin_twd"] is None


def test_non_positive_limit_fails_closed(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer()
    server.stock_contracts["2890"]["limit_up"] = 0.0

    exit_code, report = _run(tmp_path, _config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["limits"] == {}


def test_account_requests_copy_live_account_fields(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer()

    _run(tmp_path, _config(write_config), server)

    request_keys = ("account_type", "broker_id", "account_id", "person_id")
    assert server.bodies("/api/v1/portfolio/account_balance") == [{k: _STOCK_ACCOUNT[k] for k in request_keys}]
    assert server.bodies("/api/v1/portfolio/margin") == [{k: _FUTURES_ACCOUNT[k] for k in request_keys}]


def test_futures_limits_come_from_matching_code_in_root(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer()

    _exit_code, report = _run(tmp_path, _config(write_config), server)

    futures_queries = [r for r in server.requests if r.url.path == "/api/v1/data/contracts/futures"]
    assert [dict(r.url.params) for r in futures_queries] == [{"root": "TMF"}]
    assert report["limits"]["TMFJ6"]["reference"] == "47781.0"


def test_futures_code_absent_from_root_exits_2(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer(futures_contracts={"TMF": [_futures_record("TMFK6", 53000.0, 43400.0, 48200.0)]})

    exit_code, report = _run(tmp_path, _config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["limits"] == {}


def test_ambiguous_futures_code_exits_2(tmp_path: Path, write_config: WriteConfig) -> None:
    duplicate = _futures_record("TMFJ6", 52559.0, 43003.0, 47781.0)
    server = FakeServer(futures_contracts={"TMF": [duplicate, dict(duplicate)]})

    exit_code, report = _run(tmp_path, _config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["limits"] == {}


def test_every_query_recorded_in_rest_jsonl(tmp_path: Path, write_config: WriteConfig) -> None:
    _run(tmp_path, _config(write_config), FakeServer())

    lines = (tmp_path / "run" / "rest.jsonl").read_text(encoding="utf-8").splitlines()
    ops = [json.loads(line)["op"] for line in lines]
    assert ops == [
        "mode",
        "accounts",
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
        run_preflight(
            _config(write_config), _scenarios(tmp_path), run_dir, transport=httpx.MockTransport(server.handler)
        )

    assert [r.url.path for r in server.requests] == ["/api/v1/info"]
    assert json.loads((run_dir / "preflight.json").read_text(encoding="utf-8"))["funds_ok"] is False


def test_cli_preflight_passes_scenarios_path(
    tmp_path: Path, write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, "fake-value-0123456789")
    config_path = write_config([*MINIMAL_CONFIG_LINES, *_ENDPOINT_LINES])
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
    config_path = write_config([*MINIMAL_CONFIG_LINES, *_ENDPOINT_LINES])
    scenarios = _scenarios(tmp_path, "[[case]]\nid = 'only_an_id'\n")

    exit_code = main(["preflight", "--config", str(config_path), "--scenarios", str(scenarios)])

    assert exit_code == 1
    assert any(r.levelno == logging.ERROR and "missing key" in r.getMessage() for r in caplog.records)


def test_refused_connection_mid_preflight_exits_2(
    tmp_path: Path, write_config: WriteConfig, caplog: pytest.LogCaptureFixture
) -> None:
    server = FakeServer(refuse={"/api/v1/portfolio/margin"})

    exit_code, report = _run(tmp_path, _config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["funds_ok"] is False
    assert report["available"] == {"stock_twd": "1000000.0", "futures_margin_twd": None}
    # Every query after the refused one still ran
    assert set(report["limits"]) == {"0050", "2890", "TMFJ6"}
    assert report["connections"] == 2
    assert any(r.levelno == logging.ERROR and "ConnectError" in r.getMessage() for r in caplog.records)


def test_refused_usage_query_exits_2_with_funds_in_hand(tmp_path: Path, write_config: WriteConfig) -> None:
    server = FakeServer(refuse={"/api/v1/auth/usage"})

    exit_code, report = _run(tmp_path, _config(write_config), server)

    assert exit_code == EXIT_CHECK_FAILED
    assert report["funds_ok"] is False
    assert report["connections"] is None
    assert report["available"]["stock_twd"] == "1000000.0"


def test_account_identifiers_redacted_from_log(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = _config(write_config)
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    setup_logging(log_dir, secrets=[], level="INFO")

    _run(tmp_path, cfg, FakeServer())
    logging.getLogger("p1rec.test").error(
        "saw %s %s %s %s",
        _STOCK_ACCOUNT["account_id"],
        _FUTURES_ACCOUNT["account_id"],
        _STOCK_ACCOUNT["person_id"],
        _STOCK_ACCOUNT["username"],
    )
    for handler in logging.getLogger().handlers:
        handler.flush()

    log_text = (log_dir / "recorder.log").read_text(encoding="utf-8")
    assert "saw *** *** *** ***" in log_text
    for value in ("0000001", "0000002", "FAKEPID001", "fake-user"):
        assert value not in log_text
