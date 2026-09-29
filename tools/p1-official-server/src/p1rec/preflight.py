"""Step 0 of a live run: confirm simulation mode, then inventory what orders need.

run_preflight queries the accounts, the stock balance, the futures
margin, usage, CA status and every contract the scenarios trade, all
through one CapturingClient so each raw response lands in rest.jsonl as
fixture material. It then checks that each account can fund the largest
single position the scenarios open. Once simulation mode is confirmed, a
failed query, including one that never gets a response, is logged at
ERROR and the remaining queries still run, so one run records as much as
it can; preflight.json is written whatever the outcome. Account
identifiers from the live accounts response are added to log redaction
as soon as they are known.

fetch_limits, fetch_accounts, account_for_market, account_request and
fetch_available_twd are also what the order runner uses, so an order is
priced and funded from the same responses preflight checked.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import httpx

from p1rec.capture import CapturedResponse, CapturingClient, describe_exception
from p1rec.config import ConfigError, P1Config
from p1rec.interlock import assert_simulation
from p1rec.jsonpointer import JsonPointerError, resolve_pointer
from p1rec.prices import ContractLimits, FundsRequirement, required_funds
from p1rec.redact import add_redaction_values
from p1rec.scenarios import Case, Market, load_scenarios

logger = logging.getLogger(__name__)

EXIT_CHECK_FAILED = 2

PREFLIGHT_JSON = "preflight.json"

_CLIENT_NAME = "preflight"

# The stock account also holds odd-lot positions
ACCOUNT_TYPE_FOR_MARKET: Mapping[Market, str] = {Market.STOCK: "S", Market.ODD: "S", Market.FUTURES: "F"}

# AccountRequest's fields (openapi.json), copied from the live account
# object so no account identifier ever has to live in p1.toml.
_ACCOUNT_REQUEST_KEYS = ("account_type", "broker_id", "account_id", "person_id")

# Account fields that identify the holder (openapi.json Account)
_ACCOUNT_IDENTIFIER_KEYS = ("account_id", "person_id", "username")

# UsageOut.connections (openapi.json)
_USAGE_CONNECTIONS_POINTER = "/connections"

# A TAIFEX futures code is its product root followed by a delivery code
# of one month letter and one year digit (TMFJ6), or R1/R2 for the rolling
# near and next months, so removing the delivery code leaves the root.
_FUTURES_DELIVERY_CODE_LEN = 2

_REQUIRED_OPS = (
    "accounts",
    "balance",
    "margin",
    "usage",
    "ca_status",
    "contracts_stock",
    "contracts_futures",
)


class PreflightError(RuntimeError):
    """Raised when a query's response lacks what preflight or the order runner needs."""


def parse_json_exact(op: str, response: CapturedResponse) -> object:
    """Return a 2xx response's JSON body with every non-integer number as a Decimal.

    Raises PreflightError for a non-2xx status or a body that is not JSON.
    """
    if not 200 <= response.status < 300:
        raise PreflightError(f"{op} returned HTTP {response.status}")
    try:
        return json.loads(response.body, parse_float=Decimal)
    except ValueError as exc:
        raise PreflightError(f"{op} response is not valid JSON: {exc}") from exc


def fetch_accounts(client: CapturingClient, cfg: P1Config) -> list[dict[str, object]]:
    """Query accounts and return every account object as the server sent it.

    Raises PreflightError unless the body is an array of objects that each
    carry a string account_type.
    """
    body = parse_json_exact("accounts", client.request("accounts", cfg.endpoints["accounts"]))
    if not isinstance(body, list) or not all(
        isinstance(account, dict) and isinstance(account.get("account_type"), str) for account in body
    ):
        raise PreflightError("accounts response is not an array of objects with a string account_type")
    return body


def account_for_market(accounts: Sequence[Mapping[str, object]], market: Market) -> Mapping[str, object]:
    """Return the first account whose type trades market, raising PreflightError if none does."""
    account_type = ACCOUNT_TYPE_FOR_MARKET[market]
    for account in accounts:
        if account.get("account_type") == account_type:
            return account
    raise PreflightError(f"accounts response has no account of type {account_type!r}")


def account_request(account: Mapping[str, object]) -> dict[str, object]:
    """Build an AccountRequest body from a live account object."""
    return {key: account.get(key) for key in _ACCOUNT_REQUEST_KEYS}


def fetch_available_twd(
    client: CapturingClient, cfg: P1Config, market: Market, account: Mapping[str, object]
) -> Decimal:
    """Query the funds market's account can spend, in TWD.

    Stock and odd lots read funds.stock_available_pointer from balance;
    futures read funds.futures_available_pointer from margin. Raises
    PreflightError if the query fails or the pointer does not name a number.
    """
    if market is Market.FUTURES:
        op, pointer = "margin", cfg.funds.futures_available_pointer
    else:
        op, pointer = "balance", cfg.funds.stock_available_pointer
    response = client.request(op, cfg.endpoints[op], json_body=account_request(account))
    return _decimal_at(parse_json_exact(op, response), pointer, f"{op} response")


def fetch_limits(client: CapturingClient, cfg: P1Config, cases: Sequence[Case]) -> dict[str, ContractLimits]:
    """Query the contract of every code the cases trade and return its price limits.

    Stock and odd-lot codes are queried one at a time through
    contracts_stock. Futures codes are grouped by product root and each
    root is queried once through contracts_futures, which lists every
    contract of a root; the element whose code matches is used. Raises
    PreflightError naming the code if a query fails, the contract is
    missing or ambiguous, or a limit is not a positive number.
    """
    fields = cfg.contracts
    limits: dict[str, ContractLimits] = {}

    stock_codes = sorted({case.code for case in cases if case.market is not Market.FUTURES})
    for code in stock_codes:
        response = client.request("contracts_stock", cfg.endpoints["contracts_stock"], path_vars={"code": code})
        limits[code] = _limits_from_record(parse_json_exact("contracts_stock", response), code, cfg)

    codes_by_root: dict[str, list[str]] = {}
    for code in sorted({case.code for case in cases if case.market is Market.FUTURES}):
        codes_by_root.setdefault(_futures_root(code), []).append(code)
    for root, codes in codes_by_root.items():
        response = client.request("contracts_futures", cfg.endpoints["contracts_futures"], params={"root": root})
        records = parse_json_exact("contracts_futures", response)
        if not isinstance(records, list):
            raise PreflightError(f"contracts_futures response for root {root!r} is not an array")
        for code in codes:
            matches = [record for record in records if _value_or_none(record, fields.code) == code]
            if len(matches) != 1:
                raise PreflightError(
                    f"contracts_futures for root {root!r} lists {len(matches)} contracts with code {code!r}, "
                    "expected exactly one"
                )
            limits[code] = _limits_from_record(matches[0], code, cfg)

    return limits


def run_preflight(
    cfg: P1Config, scenarios: Path, run_dir: Path, *, transport: httpx.BaseTransport | None = None
) -> int:
    """Run step 0 and return 0 if every account can fund the scenarios, else 2.

    Raises InterlockError, after writing preflight.json, if the server is
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
    connections: int | None = None
    signed: dict[str, bool | None] = field(default_factory=dict)  # Account type -> signed
    limits: dict[str, ContractLimits] = field(default_factory=dict)

    # Set when a query got no response, which fails preflight even if
    # every funds value it needed arrived.
    unanswered_query: bool = False


def _inventory(client: CapturingClient, cfg: P1Config, cases: Sequence[Case], report: _Report) -> bool:
    """Run every preflight query, fill report, and return whether funds suffice."""
    accounts = _attempt(report, lambda: fetch_accounts(client, cfg))
    stock_account = futures_account = None
    if accounts is not None:
        add_redaction_values(_account_identifiers(accounts))
        report.signed = _signed_by_type(accounts)
        _warn_unsigned(report.signed, cases)
        stock_account = _attempt(report, lambda: account_for_market(accounts, Market.STOCK))
        futures_account = _attempt(report, lambda: account_for_market(accounts, Market.FUTURES))

    if stock_account is not None:
        report.available_stock_twd = _attempt(
            report, lambda: fetch_available_twd(client, cfg, Market.STOCK, stock_account)
        )
    if futures_account is not None:
        report.available_futures_margin_twd = _attempt(
            report, lambda: fetch_available_twd(client, cfg, Market.FUTURES, futures_account)
        )

    report.connections = _attempt(report, lambda: _fetch_connections(client, cfg))
    _attempt(report, lambda: _record_ca_status(client, cfg, accounts or []))

    limits = _attempt(report, lambda: fetch_limits(client, cfg, cases))
    if limits is not None:
        report.limits = limits
        report.required = _attempt(
            report,
            lambda: required_funds(cases, limits, cfg.funds.futures_margin_twd, etf_codes=cfg.contracts.etf_codes),
        )

    funds_ok = _funds_suffice(report) and not report.unanswered_query
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


def _attempt[T](report: _Report, query: Callable[[], T]) -> T | None:
    """Run query, returning None after logging a failed check or an unanswered request at ERROR.

    A request that got no response, such as a refused connection or a
    timeout, also marks report so preflight fails even if the value it
    was after is not a funds figure.
    """
    try:
        return query()
    except (PreflightError, ConfigError) as exc:
        logger.error("preflight check failed: %s", exc)
    except httpx.RequestError as exc:
        logger.error("preflight query got no response: %s", describe_exception(exc))
        report.unanswered_query = True
    return None


def _funds_suffice(report: _Report) -> bool:
    """Return whether both accounts are known to cover the requirement, logging each shortfall."""
    required = report.required
    if required is None or report.available_stock_twd is None or report.available_futures_margin_twd is None:
        # Whatever left a value unknown was already logged at ERROR
        return False
    ok = True
    if report.available_stock_twd < required.stock_twd:
        logger.error(
            "insufficient stock funds: required %s TWD, available %s TWD",
            _fmt(required.stock_twd),
            _fmt(report.available_stock_twd),
        )
        ok = False
    if report.available_futures_margin_twd < required.futures_margin_twd:
        logger.error(
            "insufficient futures margin: required %s TWD, available %s TWD",
            _fmt(required.futures_margin_twd),
            _fmt(report.available_futures_margin_twd),
        )
        ok = False
    return ok


def _account_identifiers(accounts: Sequence[Mapping[str, object]]) -> list[str]:
    """Return every non-empty identifying string across all accounts."""
    return [
        value
        for account in accounts
        for key in _ACCOUNT_IDENTIFIER_KEYS
        if isinstance(value := account.get(key), str) and value
    ]


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
    except (PreflightError, JsonPointerError) as exc:
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


def _futures_root(code: str) -> str:
    if len(code) <= _FUTURES_DELIVERY_CODE_LEN:
        raise PreflightError(f"futures code {code!r} is too short to carry a delivery code")
    return code[:-_FUTURES_DELIVERY_CODE_LEN]


def _value_or_none(doc: object, pointer: str) -> object:
    try:
        return resolve_pointer(doc, pointer)
    except JsonPointerError:
        return None


def _decimal_at(doc: object, pointer: str, what: str) -> Decimal:
    """Return the number at pointer in doc as a Decimal, raising PreflightError otherwise."""
    try:
        value = resolve_pointer(doc, pointer)
    except JsonPointerError as exc:
        raise PreflightError(f"{what} has no value at {pointer!r}: {exc}") from exc

    # bool is an int subclass, and a JSON float was already parsed as Decimal
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise PreflightError(f"{what} at {pointer!r} is {type(value).__name__}, not a number")
    return Decimal(value)


def _limits_from_record(record: object, code: str, cfg: P1Config) -> ContractLimits:
    fields = cfg.contracts
    what = f"contract {code!r}"
    record_code = _value_or_none(record, fields.code)
    if record_code != code:
        raise PreflightError(f"{what}: response has code {record_code!r} at {fields.code!r}")

    values: dict[str, Decimal] = {}
    for name, pointer in (
        ("limit_up", fields.limit_up),
        ("limit_down", fields.limit_down),
        ("reference", fields.reference),
    ):
        value = _decimal_at(record, pointer, what)

        # A zero limit_up would make the stock requirement zero and let an
        # unfunded account pass, so a non-positive limit fails closed.
        if value <= 0:
            raise PreflightError(f"{what}: {name} at {pointer!r} is not positive: {value}")
        values[name] = value

    return ContractLimits(
        code=code, limit_up=values["limit_up"], limit_down=values["limit_down"], reference=values["reference"]
    )


def _fmt(value: Decimal | None) -> str | None:
    """Render a Decimal in plain notation, never exponent notation, or pass None through."""
    return None if value is None else format(value, "f")


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
