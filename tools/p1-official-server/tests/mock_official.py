"""An in-process fake of the official server for broker and preflight tests.

FakeServer.handler plugs into httpx.MockTransport and answers every
endpoint preflight queries, with fields named as in openapi.json and
entirely made-up values. preflight_config builds a loadable P1Config
around it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from helpers import MINIMAL_CONFIG_LINES, WriteConfig

from p1rec.config import P1Config, load_config

# Endpoints preflight needs beyond MINIMAL_CONFIG_LINES, which already
# holds mode, usage, ticks and trading_limits.
ENDPOINT_LINES = [
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

# One filling and one resting case per market. The filling stock case needs
# 1 * 1000 * 25.3 = 25300 TWD, 25337 once the buy commission is added, and
# outweighs the resting odd lot; the filling futures case needs 1 * 12000.
SCENARIOS = """
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

STOCK_ONLY_SCENARIOS = SCENARIOS.split('[[case]]\nid = "futures_mkt_ioc"')[0]

STOCK_REQUIRED_TWD = "25337"
FUTURES_REQUIRED_TWD = "12000"

STOCK_ACCOUNT = {
    "account_type": "S",
    "broker_id": "0000",
    "account_id": "0000001",
    "person_id": "FAKEPID001",
    "username": "fake-user",
    "signed": True,
}
FUTURES_ACCOUNT = {**STOCK_ACCOUNT, "account_type": "F", "broker_id": "F000000", "account_id": "0000002"}
HEDGE_ACCOUNT = {**STOCK_ACCOUNT, "account_type": "H", "account_id": "0000003", "signed": False}

_STOCK_INFO_RE = re.compile(r"^/api/v1/data/contracts/([^/]+)/info$")


def stock_record(code: str, limit_up: float, limit_down: float, reference: float) -> dict[str, object]:
    return {
        "security_type": "STK",
        "region": "TW",
        "exchange": "TSE",
        "code": code,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "reference": reference,
    }


def futures_record(
    code: str, limit_up: float, limit_down: float, reference: float, *, tick: float | None = 1.0
) -> dict[str, object]:
    return {
        "security_type": "FUT",
        "region": "TW",
        "exchange": "TAIFEX",
        "code": code,
        "root": code[:3],
        "tick": tick,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "reference": reference,
    }


@dataclass
class FakeServer:
    """Answers every endpoint preflight queries; a test mutates fields to vary a response."""

    simulation: bool = True
    accounts: list[dict[str, object]] = field(
        default_factory=lambda: [dict(STOCK_ACCOUNT), dict(FUTURES_ACCOUNT), dict(HEDGE_ACCOUNT)]
    )
    balance: dict[str, object] = field(
        default_factory=lambda: {"acc_balance": 1000000.0, "date": "2026-09-29", "errmsg": ""}
    )
    trading_limits: dict[str, object] = field(
        default_factory=lambda: {"trading_limit": 500000, "trading_used": 0, "trading_available": 500000}
    )
    margin: dict[str, object] = field(default_factory=lambda: {"available_margin": 500000.0, "equity": 500000.0})
    stock_contracts: dict[str, dict[str, object]] = field(
        default_factory=lambda: {
            "2890": stock_record("2890", 25.3, 20.7, 23.0),
            "0050": stock_record("0050", 60.5, 49.5, 55.0),
        }
    )
    futures_contracts: dict[str, list[dict[str, object]]] = field(
        default_factory=lambda: {
            "TMF": [
                futures_record("TMFK6", 53000.0, 43400.0, 48200.0),
                futures_record("TMFJ6", 52559.0, 43003.0, 47781.0),
                futures_record("TMFR1", 52559.0, 43003.0, 47781.0),
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
        if route == ("POST", "/api/v1/portfolio/trading_limits"):
            return httpx.Response(200, json=self.trading_limits)
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

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def bodies(self, path: str) -> list[object]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def preflight_config(
    write_config: WriteConfig,
    *,
    margins: dict[str, int] | None = None,
    report_only: dict[str, str] | None = None,
) -> P1Config:
    """Load a P1Config for FakeServer: TMFJ6 margin 12000 and balance reported only, unless overridden."""
    lines = [*MINIMAL_CONFIG_LINES, *ENDPOINT_LINES]
    margins = {"TMFJ6": 12000, "TMFR1": 12000} if margins is None else margins
    report_only = {"balance": "/acc_balance"} if report_only is None else report_only
    _insert_after(lines, "[funds.futures_margin_twd]", [f"{code} = {value}" for code, value in margins.items()])
    _insert_after(lines, "[funds.stock_report_only]", [f'{op} = "{ptr}"' for op, ptr in report_only.items()])
    return load_config(write_config(lines))


def write_scenarios(directory: Path, text: str = SCENARIOS) -> Path:
    path = directory / "scenarios.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _insert_after(lines: list[str], header: str, entries: list[str]) -> None:
    at = lines.index(header) + 1
    lines[at:at] = entries
