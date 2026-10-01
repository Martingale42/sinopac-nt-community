"""Queries against the official server that preflight, the order runner and reconcile share.

Every function sends through a CapturingClient, so each raw response is
recorded in rest.jsonl, and reads numbers as Decimal, so no price or
amount ever passes through a binary float. A response that lacks what the
caller needs raises BrokerError; a request that gets no response at all
raises its httpx error unchanged, for the caller to decide what that means.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal

from p1rec.capture import CapturedResponse, CapturingClient
from p1rec.config import ACCOUNT_IDENTIFIER_KEYS, P1Config
from p1rec.jsonpointer import JsonPointerError, resolve_pointer
from p1rec.prices import ContractLimits, check_limits, format_decimal, tick_size
from p1rec.redact import add_redaction_values
from p1rec.scenarios import Case, Market

# The stock account also holds odd-lot positions
ACCOUNT_TYPE_FOR_MARKET: Mapping[Market, str] = {Market.STOCK: "S", Market.ODD: "S", Market.FUTURES: "F"}

# AccountRequest's fields (openapi.json), copied from the live account
# object so no account identifier ever has to live in p1.toml.
_ACCOUNT_REQUEST_KEYS = ("account_type", "broker_id", "account_id", "person_id")

# AccountBalance.errmsg (openapi.json); Margin and TradingLimits carry no
# error field, so a 2xx body from them is taken at face value.
_ERROR_MESSAGE_POINTERS: Mapping[str, str] = {"balance": "/errmsg"}

# A TAIFEX futures code is its product root followed by a delivery code
# of one month letter and one year digit (TMFJ6), or R1/R2 for the rolling
# near and next months, so removing the delivery code leaves the root.
_FUTURES_DELIVERY_CODE_LEN = 2


class BrokerError(RuntimeError):
    """Raised when a response lacks what the caller needs."""


def parse_json_exact(op: str, response: CapturedResponse) -> object:
    """Return a 2xx response's JSON body with every non-integer number as a Decimal.

    Raises BrokerError for a non-2xx status or a body that is not JSON.
    """
    if not 200 <= response.status < 300:
        raise BrokerError(f"{op} returned HTTP {response.status}")
    try:
        return json.loads(response.body, parse_float=Decimal)
    except ValueError as exc:
        raise BrokerError(f"{op} response is not valid JSON: {exc}") from exc


def fetch_accounts(client: CapturingClient, cfg: P1Config) -> list[dict[str, object]]:
    """Query accounts and return every account object as the server sent it.

    Each account's account_id, person_id and username is added to log
    redaction before the response is used for anything else, so no caller
    can log one by accident. Raises BrokerError unless the body is an array
    of objects that each carry a string account_type.
    """
    body = parse_json_exact("accounts", client.request("accounts", cfg.endpoints["accounts"]))
    if isinstance(body, list):
        add_redaction_values(_account_identifiers(account for account in body if isinstance(account, Mapping)))
    if not isinstance(body, list) or not all(
        isinstance(account, dict) and isinstance(account.get("account_type"), str) for account in body
    ):
        raise BrokerError("accounts response is not an array of objects with a string account_type")
    return body


def account_for_market(accounts: Sequence[Mapping[str, object]], market: Market) -> Mapping[str, object]:
    """Return the first account whose type trades market, raising BrokerError if none does."""
    account_type = ACCOUNT_TYPE_FOR_MARKET[market]
    for account in accounts:
        if account.get("account_type") == account_type:
            return account
    raise BrokerError(f"accounts response has no account of type {account_type!r}")


def account_request(account: Mapping[str, object]) -> dict[str, object]:
    """Build an AccountRequest body from a live account object."""
    return {key: account.get(key) for key in _ACCOUNT_REQUEST_KEYS}


def fetch_funds_value(
    client: CapturingClient, cfg: P1Config, op: str, pointer: str, account: Mapping[str, object]
) -> Decimal:
    """Query the funds operation op for account and return the amount at pointer, in TWD.

    Raises BrokerError for a non-2xx or non-JSON response, a 2xx response
    whose body reports an error (AccountBalance's errmsg), or a pointer that
    does not name a number.
    """
    body = parse_json_exact(op, client.request(op, cfg.endpoints[op], json_body=account_request(account)))
    error_pointer = _ERROR_MESSAGE_POINTERS.get(op)
    if error_pointer is not None:
        message = _value_or_none(body, error_pointer)
        if isinstance(message, str) and message:
            # The message may quote the account, so it stays in rest.jsonl
            raise BrokerError(f"{op} response reports an error at {error_pointer!r}; see rest.jsonl")
    return _decimal_at(body, pointer, f"{op} response")


def fetch_available_twd(
    client: CapturingClient, cfg: P1Config, market: Market, account: Mapping[str, object]
) -> Decimal:
    """Query the funds market's account can commit, in TWD.

    Stock and odd lots read funds.stock_available_pointer from
    funds.stock_available_op; futures read funds.futures_available_pointer
    from margin. Raises BrokerError as fetch_funds_value does.
    """
    if market is Market.FUTURES:
        return fetch_funds_value(client, cfg, "margin", cfg.funds.futures_available_pointer, account)
    return fetch_funds_value(client, cfg, cfg.funds.stock_available_op, cfg.funds.stock_available_pointer, account)


def fetch_limits(client: CapturingClient, cfg: P1Config, cases: Sequence[Case]) -> dict[str, ContractLimits]:
    """Query the contract of every code the cases trade and return its price limits.

    Stock and odd-lot codes are queried one at a time through
    contracts_stock. Futures codes are grouped by product root and each
    root is queried once through contracts_futures, which lists every
    contract of a root; the element whose code matches is used. Raises
    BrokerError naming the code if a query fails, the contract is missing
    or ambiguous, a limit is not a number, the limits fail check_limits, or
    a futures record's tick differs from p1rec's futures tick.
    """
    limits: dict[str, ContractLimits] = {}

    for code in sorted({case.code for case in cases if case.market is not Market.FUTURES}):
        response = client.request("contracts_stock", cfg.endpoints["contracts_stock"], path_vars={"code": code})
        limits[code] = _limits_from_record(parse_json_exact("contracts_stock", response), code, cfg, Market.STOCK)

    codes_by_root: dict[str, list[str]] = {}
    for code in sorted({case.code for case in cases if case.market is Market.FUTURES}):
        codes_by_root.setdefault(_futures_root(code), []).append(code)
    for root, codes in codes_by_root.items():
        response = client.request("contracts_futures", cfg.endpoints["contracts_futures"], params={"root": root})
        records = parse_json_exact("contracts_futures", response)
        if not isinstance(records, list):
            raise BrokerError(f"contracts_futures response for root {root!r} is not an array")
        for code in codes:
            matches = [record for record in records if _value_or_none(record, cfg.contracts.code) == code]
            if len(matches) != 1:
                raise BrokerError(
                    f"contracts_futures for root {root!r} lists {len(matches)} contracts with code {code!r}, "
                    "expected exactly one"
                )
            limits[code] = _limits_from_record(matches[0], code, cfg, Market.FUTURES)

    return limits


def _account_identifiers(accounts: Iterable[Mapping[str, object]]) -> list[str]:
    """Return every non-empty identifying string across all accounts."""
    return [
        value
        for account in accounts
        for key in sorted(ACCOUNT_IDENTIFIER_KEYS)
        if isinstance(value := account.get(key), str) and value
    ]


def _futures_root(code: str) -> str:
    if len(code) <= _FUTURES_DELIVERY_CODE_LEN:
        raise BrokerError(f"futures code {code!r} is too short to carry a delivery code")
    return code[:-_FUTURES_DELIVERY_CODE_LEN]


def _value_or_none(doc: object, pointer: str) -> object:
    try:
        return resolve_pointer(doc, pointer)
    except JsonPointerError:
        return None


def _decimal_at(doc: object, pointer: str, what: str) -> Decimal:
    """Return the number at pointer in doc as a Decimal, raising BrokerError otherwise."""
    try:
        value = resolve_pointer(doc, pointer)
    except JsonPointerError as exc:
        raise BrokerError(f"{what} has no value at {pointer!r}: {exc}") from exc

    # bool is an int subclass, and a JSON float was already parsed as Decimal
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise BrokerError(f"{what} at {pointer!r} is {type(value).__name__}, not a number")
    return Decimal(value)


def _limits_from_record(record: object, code: str, cfg: P1Config, market: Market) -> ContractLimits:
    fields = cfg.contracts
    what = f"contract {code!r}"
    record_code = _value_or_none(record, fields.code)
    if record_code != code:
        raise BrokerError(f"{what}: response has code {record_code!r} at {fields.code!r}")

    limits = ContractLimits(
        code=code,
        limit_up=_decimal_at(record, fields.limit_up, what),
        limit_down=_decimal_at(record, fields.limit_down, what),
        reference=_decimal_at(record, fields.reference, what),
    )

    # A zero or inverted band would make every derived price and the funds
    # requirement meaningless, so it fails closed rather than being priced.
    try:
        check_limits(limits, market, is_etf=code in fields.etf_codes)
    except ValueError as exc:
        raise BrokerError(f"{what}: {exc}") from exc

    if market is Market.FUTURES and _value_or_none(record, fields.futures_tick) is not None:
        record_tick = _decimal_at(record, fields.futures_tick, what)
        own_tick = tick_size(market, limits.reference, is_etf=False)
        if record_tick != own_tick:
            raise BrokerError(
                f"{what}: tick {format_decimal(record_tick)} at {fields.futures_tick!r} differs from "
                f"p1rec's futures tick {format_decimal(own_tick)}"
            )

    return limits
