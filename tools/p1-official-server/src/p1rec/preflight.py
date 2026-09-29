"""Step 0 of a live run: confirm simulation mode, then inventory what orders need.

run_preflight queries the accounts, the stock funds sources, the futures
margin, usage, CA status and every contract the scenarios trade, all
through one CapturingClient so each raw response lands in rest.jsonl as
fixture material. It then checks that each account the scenarios trade
through can fund the largest single case they run. Once simulation mode
is confirmed, a failed query, including one that never gets a response,
is logged and the remaining queries still run, so one run records as much
as it can; preflight.json is written whatever the outcome.

The queries themselves live in p1rec.broker, which the order runner uses
too; this module only sequences them and judges and reports the result.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import httpx

from p1rec.broker import (
    ACCOUNT_TYPE_FOR_MARKET,
    BrokerError,
    account_for_market,
    fetch_accounts,
    fetch_available_twd,
    fetch_funds_value,
    fetch_limits,
    parse_json_exact,
)
from p1rec.capture import CapturingClient, describe_exception
from p1rec.config import ConfigError, P1Config
from p1rec.interlock import assert_simulation
from p1rec.jsonpointer import JsonPointerError, resolve_pointer
from p1rec.prices import ContractLimits, FundsRequirement, format_decimal, required_funds
from p1rec.scenarios import Case, Market, load_scenarios

logger = logging.getLogger(__name__)

EXIT_CHECK_FAILED = 2

PREFLIGHT_JSON = "preflight.json"

_CLIENT_NAME = "preflight"

# UsageOut.connections (openapi.json)
_USAGE_CONNECTIONS_POINTER = "/connections"

_REQUIRED_OPS = (
    "accounts",
    "margin",
    "usage",
    "ca_status",
    "contracts_stock",
    "contracts_futures",
)


def run_preflight(
    cfg: P1Config, scenarios: Path, run_dir: Path, *, transport: httpx.BaseTransport | None = None
) -> int:
    """Run step 0 and return 0 if every traded account can fund the scenarios, else 2.

    Raises, after writing preflight.json, InterlockError if the server is
    not confirmed to be in simulation mode, and ConfigError if scenarios is
    malformed or p1.toml lacks an endpoint preflight queries. transport is
    None for real network I/O; tests pass an httpx.MockTransport.
    """
    report = _Report()
    try:
        cases = load_scenarios(scenarios)
        missing = [op for op in (cfg.interlock.endpoint_op, *_REQUIRED_OPS) if op not in cfg.endpoints]
        if missing:
            raise ConfigError(f"preflight needs endpoints missing from p1.toml: {', '.join(missing)}")

        with CapturingClient(
            cfg.base_url, _CLIENT_NAME, run_dir / "rest.jsonl", cfg.http_timeout_s, transport=transport
        ) as client:
            assert_simulation(client, cfg)
            report.funds_ok = _inventory(client, cfg, cases, report)
    finally:
        _write_report(run_dir / PREFLIGHT_JSON, report)

    return 0 if report.funds_ok else EXIT_CHECK_FAILED


@dataclass
class _Report:
    funds_ok: bool = False
    required: FundsRequirement | None = None
    available_stock_twd: Decimal | None = None
    available_futures_margin_twd: Decimal | None = None

    # Stock funds operation -> amount, the compared one first
    stock_sources: dict[str, Decimal | None] = field(default_factory=dict)

    connections: int | None = None
    signed: dict[str, bool | None] = field(default_factory=dict)  # Account type -> signed
    limits: dict[str, ContractLimits] = field(default_factory=dict)

    # Set when a query got no response, which fails preflight even if
    # every funds value it needed arrived.
    unanswered_query: bool = False


def _inventory(client: CapturingClient, cfg: P1Config, cases: Sequence[Case], report: _Report) -> bool:
    """Run every preflight query, fill report, and return whether funds suffice."""
    trades_stock = any(case.market is not Market.FUTURES for case in cases)
    trades_futures = any(case.market is Market.FUTURES for case in cases)

    accounts = _attempt(report, lambda: fetch_accounts(client, cfg))
    stock_account = futures_account = None
    if accounts is not None:
        report.signed = _signed_by_type(accounts)
        _warn_unsigned(report.signed, cases)
        if trades_stock:
            stock_account = _attempt(report, lambda: account_for_market(accounts, Market.STOCK))
        if trades_futures:
            futures_account = _attempt(report, lambda: account_for_market(accounts, Market.FUTURES))

    if stock_account is not None:
        _query_stock_funds(client, cfg, stock_account, report)
    if futures_account is not None:
        report.available_futures_margin_twd = _attempt(
            report, lambda: fetch_available_twd(client, cfg, Market.FUTURES, futures_account)
        )

    report.connections = _attempt(report, lambda: _fetch_connections(client, cfg))
    if accounts is not None:
        _attempt(report, lambda: _record_ca_status(client, cfg, accounts))

    limits = _attempt(report, lambda: fetch_limits(client, cfg, cases))
    if limits is not None:
        report.limits = limits
        report.required = _attempt(
            report,
            lambda: required_funds(cases, limits, cfg.funds.futures_margin_twd, etf_codes=cfg.contracts.etf_codes),
        )

    funds_ok = (
        _funds_suffice(report, trades_stock=trades_stock, trades_futures=trades_futures) and not report.unanswered_query
    )
    logger.info(
        "preflight: stock required %s TWD, available %s TWD; futures margin required %s TWD, "
        "available %s TWD; connections %s; funds_ok %s",
        _fmt(report.required.stock_twd if report.required else None),
        _fmt(report.available_stock_twd),
        _fmt(report.required.futures_margin_twd if report.required else None),
        _fmt(report.available_futures_margin_twd),
        report.connections,
        funds_ok,
    )
    return funds_ok


def _query_stock_funds(client: CapturingClient, cfg: P1Config, account: Mapping[str, object], report: _Report) -> None:
    """Query the compared stock funds source, then every report-only one, into report."""
    report.available_stock_twd = _attempt(report, lambda: fetch_available_twd(client, cfg, Market.STOCK, account))
    report.stock_sources[cfg.funds.stock_available_op] = report.available_stock_twd
    for op, pointer in cfg.funds.stock_report_only.items():
        query = functools.partial(fetch_funds_value, client, cfg, op, pointer, account)
        report.stock_sources[op] = _attempt(report, query, report_only=True)


def _attempt[T](report: _Report, query: Callable[[], T], *, report_only: bool = False) -> T | None:
    """Run query, returning None after logging a failed check or an unanswered request.

    A failed check is an ERROR, or only a WARN for a report_only query,
    whose value never decides funds_ok. A request that got no response,
    such as a refused connection or a timeout, is always an ERROR and also
    marks report so preflight fails.
    """
    try:
        return query()
    except (BrokerError, ConfigError) as exc:
        if report_only:
            logger.warning("preflight report-only query failed: %s", exc)
        else:
            logger.error("preflight check failed: %s", exc)
    except httpx.RequestError as exc:
        logger.error("preflight query got no response: %s", describe_exception(exc))
        report.unanswered_query = True
    return None


def _funds_suffice(report: _Report, *, trades_stock: bool, trades_futures: bool) -> bool:
    """Return whether every traded account is known to cover the requirement, logging each shortfall."""
    required = report.required
    if required is None:
        # Whatever left it unknown was already logged at ERROR
        return False
    checks = []
    if trades_stock:
        checks.append(("stock funds", required.stock_twd, report.available_stock_twd))
    if trades_futures:
        checks.append(("futures margin", required.futures_margin_twd, report.available_futures_margin_twd))

    ok = True
    for what, needed, available in checks:
        if available is None:
            ok = False
        elif available < needed:
            logger.error("insufficient %s: required %s TWD, available %s TWD", what, _fmt(needed), _fmt(available))
            ok = False
    return ok


def _signed_by_type(accounts: Sequence[Mapping[str, object]]) -> dict[str, bool | None]:
    """Map each account type to its first account's signed flag, None if not a boolean."""
    signed: dict[str, bool | None] = {}
    for account in accounts:
        account_type = str(account["account_type"])
        if account_type not in signed:
            value = account.get("signed")
            signed[account_type] = value if isinstance(value, bool) else None
    return signed


def _warn_unsigned(signed: Mapping[str, bool | None], cases: Sequence[Case]) -> None:
    """Warn for each account type the scenarios trade whose signed flag is not true."""
    markets_by_type: dict[str, list[str]] = {}
    for market in sorted({case.market for case in cases}):
        markets_by_type.setdefault(ACCOUNT_TYPE_FOR_MARKET[market], []).append(market.value)
    for account_type, markets in markets_by_type.items():
        if account_type in signed and signed[account_type] is not True:
            logger.warning(
                "account type %s is not signed (signed=%s) but scenarios include %s cases; "
                "continuing, and any rejections will be recorded",
                account_type,
                signed[account_type],
                "/".join(markets),
            )


def _fetch_connections(client: CapturingClient, cfg: P1Config) -> int | None:
    """Query usage and return its connection count, or None with a WARN."""
    try:
        body = parse_json_exact("usage", client.request("usage", cfg.endpoints["usage"]))
        value = resolve_pointer(body, _USAGE_CONNECTIONS_POINTER)
    except (BrokerError, JsonPointerError) as exc:
        logger.warning("connection count unavailable: %s", exc)
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        logger.warning(
            "connection count unavailable: usage %s is %s, not an integer",
            _USAGE_CONNECTIONS_POINTER,
            type(value).__name__,
        )
        return None
    return value


def _record_ca_status(client: CapturingClient, cfg: P1Config, accounts: Sequence[Mapping[str, object]]) -> None:
    """Query ca_status for the record, warning instead of failing when it cannot be queried."""
    person_id = next(
        (value for account in accounts if isinstance(value := account.get("person_id"), str) and value), None
    )
    if person_id is None:
        logger.warning("ca_status not queried: no account carries a person_id")
        return
    response = client.request("ca_status", cfg.endpoints["ca_status"], params={"person_id": person_id})
    if not 200 <= response.status < 300:
        logger.warning("ca_status returned HTTP %d", response.status)


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else format_decimal(value)


def _write_report(path: Path, report: _Report) -> None:
    required = report.required
    doc = {
        "funds_ok": report.funds_ok,
        "required": {
            "stock_twd": _fmt(required.stock_twd if required else None),
            "futures_margin_twd": _fmt(required.futures_margin_twd if required else None),
        },
        "available": {
            "stock_twd": _fmt(report.available_stock_twd),
            "futures_margin_twd": _fmt(report.available_futures_margin_twd),
            "stock_sources": {op: _fmt(value) for op, value in report.stock_sources.items()},
        },
        "connections": report.connections,
        "accounts": {account_type: report.signed[account_type] for account_type in sorted(report.signed)},
        "limits": {
            code: {
                "limit_up": _fmt(limits.limit_up),
                "limit_down": _fmt(limits.limit_down),
                "reference": _fmt(limits.reference),
            }
            for code, limits in sorted(report.limits.items())
        },
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
