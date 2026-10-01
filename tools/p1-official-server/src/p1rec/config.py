"""Load and validate p1.toml into a P1Config.

Each of InterlockConfig, ContractFields, FundsConfig, StreamConfig,
OrdersConfig, TradeFields, EventFields, HistoryConfig and ScrubConfig is
filled in by its own `_parse_<section>` function below, which receives
the whole parsed document plus the config file's directory, so a full
dotted key (for validation errors) or a relative path can be resolved
without touching load_config itself.
"""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, cast

from p1rec.jsonpointer import is_valid_pointer_syntax


class ConfigError(ValueError):
    """Raised when p1.toml is missing a required key or has the wrong type."""


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str  # May contain {name}, substituted from path_vars at request time


@dataclass(frozen=True)
class InterlockConfig:
    endpoint_op: str  # Logical operation name in [endpoints]; defaults to "mode"
    pointer: str  # JSON pointer into that operation's response, e.g. "/simulation"


@dataclass(frozen=True)
class ContractFields:
    """JSON pointers into one contract record, and the codes that use ETF ticks.

    The same pointers serve contracts_stock's single record and each
    element of contracts_futures' array, since both are ContractInfoRecord.
    """

    code: str
    limit_up: str
    limit_down: str
    reference: str
    futures_tick: str  # Checked against p1rec's own futures tick when present
    etf_codes: tuple[str, ...]


@dataclass(frozen=True)
class FundsConfig:
    # The stock funds query compared against the requirement: a POST
    # operation taking an AccountRequest, and a pointer into its response.
    stock_available_op: str
    stock_available_pointer: str

    # Further stock funds queries, operation -> pointer, run and reported
    # alongside the compared one but never compared.
    stock_report_only: dict[str, str]

    futures_available_pointer: str  # Into the margin response

    # Contract code -> initial margin per contract in TWD
    futures_margin_twd: dict[str, Decimal]


@dataclass(frozen=True)
class StartupRequest:
    """A request sent before the subscriptions: a literal JSON body, or the AccountRequest of an account type."""

    op: str  # Logical operation in [endpoints]
    body: str | None  # JSON text, set exactly when account_type is not
    account_type: str | None  # S | F | H, whose account the accounts response supplies

    def __post_init__(self) -> None:
        if (self.body is None) == (self.account_type is None):
            raise ValueError(f"startup request {self.op!r} needs exactly one of body and account_type")

    def describe(self) -> str:
        return f"{self.op} for account type {self.account_type}" if self.account_type else self.op


@dataclass(frozen=True)
class Instrument:
    code: str
    exchange: str  # TSE | OTC | TAIFEX
    security_type: str  # STK | FUT | OPT | IND
    quote_types: tuple[str, ...]  # Tick | BidAsk | Quote
    intraday_odd: bool


@dataclass(frozen=True)
class StreamAction:
    """A subscription change at a set time, applied to every instrument with this code."""

    at: datetime  # Timezone-aware
    op: Literal["subscribe", "unsubscribe"]
    code: str
    quote_type: str


@dataclass(frozen=True)
class StreamConfig:
    """The [stream] section: the SSE endpoints to record and the subscriptions to hold.

    The defaults stand for a p1.toml without a [stream] section, which
    every subcommand other than stream accepts. When the section is
    present, streams is non-empty and subscribe and unsubscribe are set.
    """

    # Stream name -> SSE endpoint, in file order, so the first is the leader stream
    streams: dict[str, Endpoint] = field(default_factory=dict)
    subscribe: Endpoint | None = None
    unsubscribe: Endpoint | None = None

    # JSON object templates, in which a string value that is exactly
    # "{name}" stands for that field of the subscription.
    subscribe_body: str = ""
    unsubscribe_body: str = ""

    startup_requests: tuple[StartupRequest, ...] = ()
    retry_s: float = 0.0

    # Seconds between retry rounds for requests that failed after the first send
    request_retry_s: float = 0.0

    # Asia/Taipei wall-clock time of the daily full resend
    daily_resend_at: time | None = None

    instruments: tuple[Instrument, ...] = ()
    actions: tuple[StreamAction, ...] = ()


@dataclass(frozen=True)
class OrdersConfig:
    # Logical operation (place_stock, place_futures, update_price,
    # update_qty, cancel) -> JSON body template; a string value that is
    # exactly "{name}" is replaced by a typed value when sent.
    body_templates: dict[str, str]

    # Market (stock, odd, futures) -> ContractRequest.exchange
    exchange: dict[str, str]

    step_wait_s: float
    fill_poll_s: float
    fill_timeout_s: float


@dataclass(frozen=True)
class TradeFields:
    """JSON pointers into the trades response and into one Trade, and the accounts reconcile lists trades for.

    The place, update and cancel responses are each one Trade too, so
    orders and reconcile read every Trade through these same pointers.
    """

    # The Trade array within the trades response, "" for a bare array
    list: str

    # The order id, which update_price, update_qty and cancel take as trade_id
    id: str

    # The order status ("Filled" once filled, "Failed" when rejected) and
    # the quantity filled so far.
    status: str
    deal_quantity: str

    # The order time in epoch seconds, which dates a trades snapshot when
    # no --trading-day is given and windows the REST side by calendar date.
    order_ts: str

    # Account types whose trades reconcile queries one account at a time,
    # since trades without an account lists the stock account's orders alone.
    account_types: tuple[str, ...]


@dataclass(frozen=True)
class EventFields:
    """Where order reports and stream timing live in recorded JSON."""

    # SSE event name carrying OrderEvent frames, and the OrderEvent state
    # values that mark an order report or a deal report.
    order_event_name: str
    order_states: tuple[str, ...]
    deal_states: tuple[str, ...]

    # JSON pointers into the report an OrderEvent holds at data/<state>
    event_id: str
    order_key: str
    op_type: str
    op_code: str
    order_qty: str
    cancel_qty: str
    deal_key: str
    deal_qty: str

    # op_code of an accepted operation
    op_code_success: str

    # Pointers to the exchange time, in epoch seconds, of an order report
    # and of a deal report, which date an order for the calendar-date window.
    order_exchange_ts: str
    deal_exchange_ts: str

    # Pointers into a frame's data giving the exchange time, empty to skip
    # latency: one numeric epoch-seconds value when exchange_ts_format is
    # None, otherwise strings joined by a space and parsed with it as
    # Taipei local time.
    exchange_ts_pointers: tuple[str, ...]
    exchange_ts_format: str | None

    heartbeat_event: str

    # Silence between consecutive frames of one file that stats reports
    gap_threshold_s: float


@dataclass(frozen=True)
class HistoryQuery:
    op: Literal["ticks", "kbars", "snapshots"]

    # Parsed and re-serialized by httpx when sent, so rest.jsonl records
    # what actually went on the wire, not this string byte-for-byte.
    body: str


@dataclass(frozen=True)
class HistoryConfig:
    queries: tuple[HistoryQuery, ...]
    usage_bytes_pointer: str  # JSON pointer into UsageOut, e.g. "/bytes"


@dataclass(frozen=True)
class ScrubConfig:
    # JSON pointers applied to every element of each recorded accounts
    # response, and to each ca_status response, to find the identifiers to
    # scrub, with the pointer's last token upper-cased naming the category.
    account_id_pointers: tuple[str, ...]
    ca_pointers: tuple[str, ...]

    # Key names whose non-empty values, at any depth of recorded JSON, are
    # account identifiers too, with the key upper-cased naming the category.
    account_id_keys: tuple[str, ...]

    # Holds the original values themselves, so it must stay under results_root
    map_path: Path

    # fixtures.toml; each entry has name, run_id, source, lines, checklist, instrument and optionally recorded_at
    selection: Path

    # scrub --scan-only always scans fixtures_root's parent and this
    # document, and extra_scan_paths can only add to them.
    contract_doc: Path
    extra_scan_paths: tuple[Path, ...]


@dataclass(frozen=True)
class P1Config:
    base_url: str
    http_timeout_s: float
    results_root: Path
    fixtures_root: Path

    # Role -> env var name: api_key, sec_key, ca_passwd, person_id, egress_ip
    secret_env: dict[str, str]

    # Keyed by the logical operation name (mode, place_stock, ...)
    endpoints: dict[str, Endpoint]

    interlock: InterlockConfig
    contracts: ContractFields
    funds: FundsConfig
    stream: StreamConfig
    orders: OrdersConfig
    trades: TradeFields
    events: EventFields
    history: HistoryConfig
    scrub: ScrubConfig


# Roles read via read_secrets(); see P1Config.secret_env above
_SECRET_ROLES = ("api_key", "sec_key", "ca_passwd", "person_id", "egress_ip")

# Keys that must never carry a literal secret value anywhere in p1.toml,
# since inside [secrets] these same names are role -> env-var-name mappings
# instead, not secret values, which is why that table is exempt in
# _reject_inline_secrets.
_INLINE_SECRET_KEYS = frozenset(_SECRET_ROLES)

# Shape every [secrets] value must have, restricted to the SJ_/P1_ prefixes
# this project actually uses rather than any uppercase identifier, so a
# value shaped like a real secret (a Taiwanese national ID, for example) is
# rejected here instead of being accepted and later echoed by read_secrets'
# missing-variable error message.
_ENV_VAR_NAME_RE = re.compile(r"^(SJ|P1)_[A-Z0-9_]+$")


def load_config(path: Path) -> P1Config:
    """Load and validate a p1.toml file into a P1Config.

    Relative paths inside the file (results_root, fixtures_root) resolve
    against the directory that holds path.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file: {path} ({exc})") from exc

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    _reject_inline_secrets(data)

    config_dir = path.parent

    return P1Config(
        base_url=_get_str(data, "base_url"),
        http_timeout_s=_get_number(data, "http_timeout_s"),
        results_root=_get_fs_path(data, "results_root", config_dir),
        fixtures_root=_get_fs_path(data, "fixtures_root", config_dir),
        secret_env=_get_secret_env(data),
        endpoints=_get_endpoints(data),
        interlock=_parse_interlock(data, config_dir),
        contracts=_parse_contracts(data, config_dir),
        funds=_parse_funds(data, config_dir),
        stream=_parse_stream(data, config_dir),
        orders=_parse_orders(data, config_dir),
        trades=_parse_trades(data, config_dir),
        events=_parse_events(data, config_dir),
        history=_parse_history(data, config_dir),
        scrub=_parse_scrub(data, config_dir),
    )


def read_secrets(cfg: P1Config) -> dict[str, str]:
    """Read secret values from the environment variables named in cfg.secret_env.

    An empty value is treated the same as a missing one, so an accidentally
    blank environment variable can never silently disable redaction or
    scrubbing downstream.
    """
    secrets: dict[str, str] = {}
    for role, env_var in cfg.secret_env.items():
        value = os.environ.get(env_var)
        if not value:
            raise ConfigError(f"missing environment variable {env_var} for secrets.{role}")
        secrets[role] = value
    return secrets


def _reject_inline_secrets(data: Mapping[str, Any], path: str = "") -> None:
    """Raise ConfigError if api_key/sec_key/ca_passwd appear outside [secrets]."""
    for key, value in data.items():
        if path == "" and key == "secrets":
            continue
        current = f"{path}.{key}" if path else key
        if key in _INLINE_SECRET_KEYS:
            raise ConfigError(
                f"inline secret value not allowed at {current}; "
                "map it in [secrets] to an environment variable name instead"
            )
        _reject_inline_secrets_in_value(value, current)


def _reject_inline_secrets_in_value(value: Any, path: str) -> None:
    """Recurse into nested tables and arrays of tables looking for inline secrets."""
    if isinstance(value, Mapping):
        _reject_inline_secrets(value, path)
    elif isinstance(value, list):
        for item in value:
            _reject_inline_secrets_in_value(item, path)


def _lookup(data: Mapping[str, Any], dotted_key: str) -> Any:
    """Walk a dotted key path through nested TOML tables.

    Raises ConfigError naming the full dotted_key if any segment is
    missing or not itself a table.
    """
    node: Any = data
    for part in dotted_key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise ConfigError(f"missing config key: {dotted_key}")
        node = node[part]
    return node


def _get_str(data: Mapping[str, Any], dotted_key: str) -> str:
    value = _lookup(data, dotted_key)
    if not isinstance(value, str):
        raise ConfigError(f"config key {dotted_key} must be a string, got {type(value).__name__}")
    return value


def _get_number(data: Mapping[str, Any], dotted_key: str) -> float:
    value = _lookup(data, dotted_key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"config key {dotted_key} must be a number, got {type(value).__name__}")
    return float(value)


def _get_fs_path(data: Mapping[str, Any], dotted_key: str, base_dir: Path) -> Path:
    raw = _get_str(data, dotted_key)
    return (base_dir / raw).resolve()


def _get_pointer(data: Mapping[str, Any], dotted_key: str, *, allow_root: bool = False) -> str:
    """Read dotted_key as a string and validate it is a syntactically valid JSON pointer.

    Rejects the empty (whole-document) pointer unless allow_root is set:
    that pointer resolves to the whole response body, so any endpoint
    whose body happened to be the bare JSON value true would satisfy a
    caller checking "is this value true", regardless of what the body
    actually means.
    """
    pointer = _get_str(data, dotted_key)
    if not is_valid_pointer_syntax(pointer):
        raise ConfigError(f"config key {dotted_key} is not a valid JSON pointer: {pointer!r}")
    if pointer == "" and not allow_root:
        raise ConfigError(f"config key {dotted_key} must not be the empty pointer")
    return pointer


def _get_secret_env(data: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for role in _SECRET_ROLES:
        dotted_key = f"secrets.{role}"
        value = _get_str(data, dotted_key)
        if not _ENV_VAR_NAME_RE.fullmatch(value):
            # Never echo value: it may itself be a stray secret typed in by mistake
            raise ConfigError(f"config key {dotted_key} must be an environment variable name")
        result[role] = value
    return result


def _get_endpoints(data: Mapping[str, Any]) -> dict[str, Endpoint]:
    raw_endpoints = _lookup(data, "endpoints")
    if not isinstance(raw_endpoints, Mapping):
        raise ConfigError("config key endpoints must be a table")
    return {
        name: Endpoint(
            method=_get_str(data, f"endpoints.{name}.method"),
            path=_get_str(data, f"endpoints.{name}.path"),
        )
        for name in raw_endpoints
    }


def _optional_table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return data[key] validated as a table, or {} if the optional section is absent."""
    if key not in data:
        return {}
    value = data[key]
    if not isinstance(value, Mapping):
        raise ConfigError(f"config key {key} must be a table")
    return value


def _require_endpoint_method(known_endpoints: Mapping[str, Any], op: str, method: str, key: str) -> None:
    """Raise ConfigError, naming key, unless known_endpoints[op] exists with the given HTTP method."""
    if op not in known_endpoints:
        raise ConfigError(f"config key {key} names unknown endpoint: {op!r}")
    endpoint_table = known_endpoints[op]
    endpoint_method = endpoint_table.get("method") if isinstance(endpoint_table, Mapping) else None
    if endpoint_method != method:
        raise ConfigError(f"config key {key} must name a {method} endpoint, but {op!r} is {endpoint_method!r}")


_DEFAULT_INTERLOCK_ENDPOINT_OP = "mode"


def _parse_interlock(data: Mapping[str, Any], config_dir: Path) -> InterlockConfig:
    table = _optional_table(data, "interlock")
    if "endpoint_op" in table:
        endpoint_op = _get_str(data, "interlock.endpoint_op")
    else:
        endpoint_op = _DEFAULT_INTERLOCK_ENDPOINT_OP
    pointer = _get_pointer(data, "interlock.pointer")

    known_endpoints = _lookup(data, "endpoints")
    if not isinstance(known_endpoints, Mapping):
        raise ConfigError(f"config key interlock.endpoint_op names unknown endpoint: {endpoint_op!r}")

    # A POST that reports simulation mode as a side effect would still be
    # a mutation the interlock must not itself trigger, so only a GET may
    # back the check.
    _require_endpoint_method(known_endpoints, endpoint_op, "GET", "interlock.endpoint_op")

    return InterlockConfig(endpoint_op=endpoint_op, pointer=pointer)


def _parse_contracts(data: Mapping[str, Any], config_dir: Path) -> ContractFields:
    _optional_table(data, "contracts")
    etf_codes = _lookup(data, "contracts.etf_codes")
    if not isinstance(etf_codes, list) or not all(isinstance(code, str) and code for code in etf_codes):
        raise ConfigError("config key contracts.etf_codes must be an array of non-empty strings")
    return ContractFields(
        code=_get_pointer(data, "contracts.code"),
        limit_up=_get_pointer(data, "contracts.limit_up"),
        limit_down=_get_pointer(data, "contracts.limit_down"),
        reference=_get_pointer(data, "contracts.reference"),
        futures_tick=_get_pointer(data, "contracts.futures_tick"),
        etf_codes=tuple(etf_codes),
    )


def _parse_funds(data: Mapping[str, Any], config_dir: Path) -> FundsConfig:
    funds = _optional_table(data, "funds")
    endpoints = _lookup(data, "endpoints")

    def post_op(dotted_key: str, op: str) -> str:
        # Funds queries send the account in a JSON body, so only a POST fits
        endpoint = endpoints.get(op) if isinstance(endpoints, Mapping) else None
        method = endpoint.get("method") if isinstance(endpoint, Mapping) else None
        if method != "POST":
            raise ConfigError(f"config key {dotted_key} must name a POST endpoint, but {op!r} is {method!r}")
        return op

    stock_available_op = post_op("funds.stock_available_op", _get_str(data, "funds.stock_available_op"))

    report_only: dict[str, str] = {}
    raw_report_only = funds.get("stock_report_only", {})
    if not isinstance(raw_report_only, Mapping):
        raise ConfigError("config key funds.stock_report_only must be a table")
    for op in raw_report_only:
        dotted_key = f"funds.stock_report_only.{op}"
        if op == stock_available_op:
            raise ConfigError(f"config key {dotted_key} repeats funds.stock_available_op")
        report_only[post_op(dotted_key, op)] = _get_pointer(data, dotted_key)

    raw_margins = _lookup(data, "funds.futures_margin_twd")
    if not isinstance(raw_margins, Mapping):
        raise ConfigError("config key funds.futures_margin_twd must be a table")

    margins: dict[str, Decimal] = {}
    for code, value in raw_margins.items():
        dotted_key = f"funds.futures_margin_twd.{code}"

        # A TOML float would carry binary rounding into the funds check,
        # so only an integer or a decimal string is accepted.
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ConfigError(f"config key {dotted_key} must be an integer or a decimal string")
        try:
            margin = Decimal(value)
        except InvalidOperation:
            raise ConfigError(f"config key {dotted_key} is not a decimal number: {value!r}") from None
        if not margin.is_finite() or margin <= 0:
            raise ConfigError(f"config key {dotted_key} must be a positive number")
        margins[code] = margin

    return FundsConfig(
        stock_available_op=stock_available_op,
        stock_available_pointer=_get_pointer(data, "funds.stock_available_pointer"),
        stock_report_only=report_only,
        futures_available_pointer=_get_pointer(data, "funds.futures_available_pointer"),
        futures_margin_twd=margins,
    )


# Logical operations the stream subcommand calls by name (docs/official-server-contract.md section 1)
ACCOUNTS_OP = "accounts"
STREAM_SUBSCRIBE_OP = "stream_subscribe"
STREAM_UNSUBSCRIBE_OP = "stream_unsubscribe"

# One placeholder per BaseSubscriptionRequest field (openapi.json)
SUBSCRIPTION_PLACEHOLDERS = frozenset({"code", "exchange", "security_type", "quote_type", "intraday_odd"})
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# A startup request body standing for the AccountRequest of the account of
# this type, one of the AccountType values in openapi.json, so that no
# account identifier has to be written into the public p1.toml.
_ACCOUNT_BODY_RE = re.compile(r"@account:(S|F|H)")

# Account fields that identify the holder (openapi.json Account), refused
# anywhere in a configured request body for the same reason, and redacted
# from logs once the live accounts response names their values.
ACCOUNT_IDENTIFIER_KEYS = frozenset({"account_id", "person_id", "username"})

# Every key [stream] and its tables may hold, so a misspelt one, such as
# [[stream.actions]], fails at load instead of being silently ignored.
_STREAM_KEYS = frozenset(
    {
        "retry_s",
        "request_retry_s",
        "daily_resend_at",
        "subscribe_body",
        "unsubscribe_body",
        "startup_requests",
        "streams",
        "instrument",
        "action",
    }
)
_STREAM_ENDPOINT_KEYS = frozenset({"method", "path"})
_INSTRUMENT_KEYS = frozenset({"code", "exchange", "security_type", "quote_types", "intraday_odd"})
_ACTION_KEYS = frozenset({"at", "op", "code", "quote_type"})

# Hours and minutes, both zero-padded, such as "08:35"
_WALL_TIME_RE = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")

# Stream names become part of file names, so they are kept to a safe set
_STREAM_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

# Values named in BaseSubscriptionRequest's field descriptions (openapi.json)
_EXCHANGES = ("TSE", "OTC", "TAIFEX")
_SECURITY_TYPES = ("STK", "FUT", "OPT", "IND")
_QUOTE_TYPES = ("Tick", "BidAsk", "Quote")

_ACTION_OPS = ("subscribe", "unsubscribe")


def _parse_stream(data: Mapping[str, Any], config_dir: Path) -> StreamConfig:
    if "stream" not in data:
        return StreamConfig()
    table = _optional_table(data, "stream")
    _reject_unknown_keys(table, _STREAM_KEYS, "stream")

    instruments = _parse_instruments(table)
    return StreamConfig(
        streams=_parse_stream_endpoints(data),
        subscribe=_named_endpoint(data, STREAM_SUBSCRIBE_OP),
        unsubscribe=_named_endpoint(data, STREAM_UNSUBSCRIBE_OP),
        subscribe_body=_get_body_template(data, "stream.subscribe_body"),
        unsubscribe_body=_get_body_template(data, "stream.unsubscribe_body"),
        startup_requests=_parse_startup_requests(data),
        retry_s=_get_interval(data, "stream.retry_s"),
        request_retry_s=_get_interval(data, "stream.request_retry_s"),
        daily_resend_at=_get_wall_time(data, "stream.daily_resend_at"),
        instruments=instruments,
        actions=_parse_actions(table, instruments),
    )


def _get_interval(data: Mapping[str, Any], dotted_key: str) -> float:
    """Read dotted_key as a positive, finite number of seconds."""
    value = _get_number(data, dotted_key)
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"config key {dotted_key} must be a positive, finite number of seconds, got {value!r}")
    return value


def _get_wall_time(data: Mapping[str, Any], dotted_key: str) -> time:
    text = _get_str(data, dotted_key)
    if not _WALL_TIME_RE.fullmatch(text):
        raise ConfigError(f"config key {dotted_key} must be an HH:MM time, such as 08:35, got {text!r}")
    return time.fromisoformat(text)


def _reject_unknown_keys(table: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown config key: {', '.join(f'{where}.{key}' for key in unknown)}")


def _identifier_keys(value: object) -> set[str]:
    """Return every account identifier key found at any depth of a parsed JSON value."""
    if isinstance(value, dict):
        found = set(ACCOUNT_IDENTIFIER_KEYS & value.keys())
        for item in value.values():
            found |= _identifier_keys(item)
        return found
    if isinstance(value, list):
        return set().union(*(_identifier_keys(item) for item in value))
    return set()


def _named_endpoint(data: Mapping[str, Any], op: str) -> Endpoint:
    return Endpoint(method=_get_str(data, f"endpoints.{op}.method"), path=_get_str(data, f"endpoints.{op}.path"))


def _parse_stream_endpoints(data: Mapping[str, Any]) -> dict[str, Endpoint]:
    raw = _lookup(data, "stream.streams")
    if not isinstance(raw, Mapping) or not raw:
        raise ConfigError("config key stream.streams must be a non-empty table of SSE endpoints")

    streams: dict[str, Endpoint] = {}
    for name in raw:
        if not _STREAM_NAME_RE.fullmatch(name):
            raise ConfigError(f"config key stream.streams.{name}: a stream name may only use letters, digits, - and _")
        if not isinstance(raw[name], Mapping):
            raise ConfigError(f"config key stream.streams.{name} must be a table")
        _reject_unknown_keys(raw[name], _STREAM_ENDPOINT_KEYS, f"stream.streams.{name}")
        endpoint = Endpoint(
            method=_get_str(data, f"stream.streams.{name}.method"),
            path=_get_str(data, f"stream.streams.{name}.path"),
        )
        if endpoint.method != "GET":
            raise ConfigError(f"config key stream.streams.{name}.method must be GET, got {endpoint.method!r}")
        streams[name] = endpoint
    return streams


def _get_body_template(data: Mapping[str, Any], dotted_key: str) -> str:
    """Read dotted_key as the text of a JSON object whose placeholders are all known, whole string values."""
    text = _get_str(data, dotted_key)
    try:
        template = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"config key {dotted_key} is not valid JSON: {exc}") from exc
    if not isinstance(template, dict):
        raise ConfigError(f"config key {dotted_key} must be a JSON object")
    _check_placeholders(template, dotted_key)
    _reject_identifiers(template, dotted_key)
    return text


def _reject_identifiers(body: object, where: str) -> None:
    found = _identifier_keys(body)
    if found:
        raise ConfigError(
            f"config key {where} must not name the account ({', '.join(sorted(found))}); "
            'a startup request takes it from "@account:<S|F|H>"'
        )


def _check_placeholders(value: object, dotted_key: str) -> None:
    """Raise ConfigError for a placeholder that substitution would not replace.

    Substitution replaces only a string value that is exactly "{name}", so
    a placeholder inside a longer string or in an object key would
    otherwise be sent to the server verbatim.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if PLACEHOLDER_RE.search(key):
                raise ConfigError(f"config key {dotted_key}: a placeholder cannot be an object key: {key!r}")
            _check_placeholders(item, dotted_key)
    elif isinstance(value, list):
        for item in value:
            _check_placeholders(item, dotted_key)
    elif isinstance(value, str):
        whole = PLACEHOLDER_RE.fullmatch(value)
        if whole is None and PLACEHOLDER_RE.search(value):
            raise ConfigError(f"config key {dotted_key}: a placeholder must be the whole string value: {value!r}")
        if whole is not None and whole.group(1) not in SUBSCRIPTION_PLACEHOLDERS:
            raise ConfigError(
                f"config key {dotted_key}: unknown placeholder {value!r}; "
                f"known: {', '.join(sorted(SUBSCRIPTION_PLACEHOLDERS))}"
            )


def _parse_startup_requests(data: Mapping[str, Any]) -> tuple[StartupRequest, ...]:
    dotted_key = "stream.startup_requests"
    raw = _lookup(data, dotted_key)
    if not isinstance(raw, list):
        raise ConfigError(f"config key {dotted_key} must be an array of [operation, body] pairs")
    endpoints = _lookup(data, "endpoints")

    requests: list[StartupRequest] = []
    for index, item in enumerate(raw):
        where = f"{dotted_key}[{index}]"
        if not (isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item)):
            raise ConfigError(f"config key {where} must be an [operation, body] pair of strings")
        op, body = item
        if not isinstance(endpoints, Mapping) or op not in endpoints:
            raise ConfigError(f"config key {where} names unknown endpoint: {op!r}")
        account = _ACCOUNT_BODY_RE.fullmatch(body)
        if account is not None:
            if ACCOUNTS_OP not in endpoints:
                raise ConfigError(f"config key {where} uses {body!r}, which needs endpoints.{ACCOUNTS_OP}")
            requests.append(StartupRequest(op=op, body=None, account_type=account.group(1)))
            continue
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise ConfigError(f'config key {where} body must be JSON or "@account:<S|F|H>"') from exc
        _reject_identifiers(parsed, where)
        requests.append(StartupRequest(op=op, body=body, account_type=None))
    return tuple(requests)


def _item(table: Mapping[str, Any], key: str, where: str, kind: type) -> Any:
    """Return table[key] checked to be a kind, naming where.key in any error."""
    if key not in table:
        raise ConfigError(f"missing config key: {where}.{key}")
    value = table[key]
    if not isinstance(value, kind):
        raise ConfigError(f"config key {where}.{key} must be a {kind.__name__}, got {type(value).__name__}")
    return value


def _item_choice(table: Mapping[str, Any], key: str, where: str, choices: tuple[str, ...]) -> str:
    value = cast(str, _item(table, key, where, str))
    if value not in choices:
        raise ConfigError(f"config key {where}.{key} must be one of {', '.join(choices)}, got {value!r}")
    return value


def _array_of_tables(table: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    raw = table[key]
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise ConfigError(f"config key stream.{key} must be an array of tables ([[stream.{key}]])")
    return raw


def _parse_instruments(table: Mapping[str, Any]) -> tuple[Instrument, ...]:
    if "instrument" not in table:
        raise ConfigError("missing config key: stream.instrument")
    raw = _array_of_tables(table, "instrument")
    if not raw:
        raise ConfigError("config key stream.instrument must list at least one instrument")

    instruments: list[Instrument] = []
    seen: set[tuple[str, str, str, str, bool]] = set()
    for index, item in enumerate(raw):
        where = f"stream.instrument[{index}]"
        _reject_unknown_keys(item, _INSTRUMENT_KEYS, where)
        code = cast(str, _item(item, "code", where, str))
        if not code:
            raise ConfigError(f"config key {where}.code must not be empty")
        quote_types = _item(item, "quote_types", where, list)
        if not quote_types or any(quote_type not in _QUOTE_TYPES for quote_type in quote_types):
            raise ConfigError(f"config key {where}.quote_types must list one or more of {', '.join(_QUOTE_TYPES)}")
        instrument = Instrument(
            code=code,
            exchange=_item_choice(item, "exchange", where, _EXCHANGES),
            security_type=_item_choice(item, "security_type", where, _SECURITY_TYPES),
            quote_types=tuple(quote_types),
            intraday_odd=cast(bool, _item(item, "intraday_odd", where, bool)),
        )
        for quote_type in instrument.quote_types:
            key = (code, instrument.exchange, instrument.security_type, quote_type, instrument.intraday_odd)
            if key in seen:
                raise ConfigError(f"config key {where} repeats the {code} {quote_type} subscription")
            seen.add(key)
        instruments.append(instrument)
    return tuple(instruments)


def _parse_actions(table: Mapping[str, Any], instruments: tuple[Instrument, ...]) -> tuple[StreamAction, ...]:
    if "action" not in table:
        return ()
    codes = {instrument.code for instrument in instruments}

    actions: list[StreamAction] = []
    for index, item in enumerate(_array_of_tables(table, "action")):
        where = f"stream.action[{index}]"
        _reject_unknown_keys(item, _ACTION_KEYS, where)
        at = cast(datetime, _item(item, "at", where, datetime))
        if at.utcoffset() is None:
            raise ConfigError(f"config key {where}.at must carry a UTC offset, such as 2026-10-02T10:30:00+08:00")
        code = cast(str, _item(item, "code", where, str))
        if code not in codes:
            raise ConfigError(f"config key {where}.code names no [[stream.instrument]]: {code!r}")
        actions.append(
            StreamAction(
                at=at,
                op=cast(Literal["subscribe", "unsubscribe"], _item_choice(item, "op", where, _ACTION_OPS)),
                code=code,
                quote_type=_item_choice(item, "quote_type", where, _QUOTE_TYPES),
            )
        )
    return tuple(actions)


# Placeholders each order template must use, and those it may use besides.
# A place template must carry every field a case varies, so a value typed
# into the template can never silently override the scenario, and the
# account placeholder, since the server takes an account given by number
# alone to be a stock account.
_PLACE_REQUIRED = frozenset(
    {"code", "action", "price", "quantity", "price_type", "order_type", "custom_field", "account"}
)
_PLACE_OPTIONAL = frozenset({"exchange", "security_type"})
_ORDER_TEMPLATE_PLACEHOLDERS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "place_stock": (_PLACE_REQUIRED | {"order_lot"}, _PLACE_OPTIONAL),
    "place_futures": (_PLACE_REQUIRED, _PLACE_OPTIONAL | {"octype"}),
    "update_price": (frozenset({"trade_id", "price"}), frozenset()),
    "update_qty": (frozenset({"trade_id", "quantity"}), frozenset()),
    "cancel": (frozenset({"trade_id"}), frozenset()),
}

# scenarios.Market's values; config.py cannot import scenarios, which imports it
ORDER_MARKETS = ("stock", "odd", "futures")

# Every key [orders] may hold, so a misspelt one fails at load
_ORDERS_KEYS = frozenset({"body_templates", "exchange", "step_wait_s", "fill_poll_s", "fill_timeout_s"})

# ContractRequest.exchange values (openapi.json Exchange)
_ORDER_EXCHANGES = ("TSE", "OTC", "OES", "TAIFEX", "TIM")


def _parse_orders(data: Mapping[str, Any], config_dir: Path) -> OrdersConfig:
    _reject_unknown_keys(_optional_table(data, "orders"), _ORDERS_KEYS, "orders")
    known_endpoints = _lookup(data, "endpoints")
    if not isinstance(known_endpoints, Mapping):
        raise ConfigError("config key endpoints must be a table")

    raw_templates = _lookup(data, "orders.body_templates")
    if not isinstance(raw_templates, Mapping):
        raise ConfigError("config key orders.body_templates must be a table")
    unknown = sorted(set(raw_templates) - set(_ORDER_TEMPLATE_PLACEHOLDERS))
    if unknown:
        raise ConfigError(f"config key orders.body_templates has unknown operations: {', '.join(unknown)}")
    templates: dict[str, str] = {}
    for op, (required, optional) in _ORDER_TEMPLATE_PLACEHOLDERS.items():
        key = f"orders.body_templates.{op}"
        _require_endpoint_method(known_endpoints, op, "POST", key)
        templates[op] = _get_order_template(data, key, required, optional)

    # The fill poll reads trades with the account in a JSON body
    _require_endpoint_method(known_endpoints, "trades", "POST", "endpoints.trades")

    raw_exchange = _lookup(data, "orders.exchange")
    if not isinstance(raw_exchange, Mapping) or sorted(raw_exchange) != sorted(ORDER_MARKETS):
        raise ConfigError(
            f"config key orders.exchange must be a table with exactly the keys {', '.join(ORDER_MARKETS)}"
        )
    exchange = {
        market: _item_choice(raw_exchange, market, "orders.exchange", _ORDER_EXCHANGES) for market in ORDER_MARKETS
    }

    step_wait_s = _get_interval(data, "orders.step_wait_s")
    fill_poll_s = _get_interval(data, "orders.fill_poll_s")
    fill_timeout_s = _get_interval(data, "orders.fill_timeout_s")
    if fill_timeout_s <= fill_poll_s:
        raise ConfigError("config key orders.fill_timeout_s must be greater than orders.fill_poll_s")

    return OrdersConfig(
        body_templates=templates,
        exchange=exchange,
        step_wait_s=step_wait_s,
        fill_poll_s=fill_poll_s,
        fill_timeout_s=fill_timeout_s,
    )


# Every key [trades] may hold, so a misspelt one fails at load
_TRADES_KEYS = frozenset({"list", "id", "status", "deal_quantity", "order_ts", "account_types"})


def _parse_trades(data: Mapping[str, Any], config_dir: Path) -> TradeFields:
    _reject_unknown_keys(_optional_table(data, "trades"), _TRADES_KEYS, "trades")
    return TradeFields(
        list=_get_trade_pointer(data, "trades.list", allow_root=True),
        id=_get_trade_pointer(data, "trades.id"),
        status=_get_trade_pointer(data, "trades.status"),
        deal_quantity=_get_trade_pointer(data, "trades.deal_quantity"),
        order_ts=_get_trade_pointer(data, "trades.order_ts"),
        account_types=_get_account_types(data, "trades.account_types"),
    )


def _get_account_types(data: Mapping[str, Any], dotted_key: str) -> tuple[str, ...]:
    """Read dotted_key as a non-empty array of distinct, non-empty account type strings."""
    value = _lookup(data, dotted_key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"config key {dotted_key} must be a non-empty array of non-empty strings")
    repeated = sorted({item for item in value if value.count(item) > 1})
    if repeated:
        raise ConfigError(f"config key {dotted_key} repeats {', '.join(repeated)}")
    return tuple(value)


def _get_trade_pointer(data: Mapping[str, Any], dotted_key: str, *, allow_root: bool = False) -> str:
    """Read a [trades] pointer, refusing one that ends at an account identifier.

    The order id, status and deal quantity these pointers find are logged
    or written to orders.json and reconcile.json, so none of them may be
    an account's identifier; the list and order time pointers have no
    reason to end at one, so the same rule holds for them.
    """
    pointer = _get_pointer(data, dotted_key, allow_root=allow_root)
    last = pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
    if last in ACCOUNT_IDENTIFIER_KEYS:
        raise ConfigError(f"config key {dotted_key} must not point at an account identifier ({last})")
    return pointer


def _get_order_template(
    data: Mapping[str, Any], dotted_key: str, required: frozenset[str], optional: frozenset[str]
) -> str:
    """Read one order body template and check its placeholders and keys.

    Raises ConfigError naming dotted_key unless the template is a JSON
    object whose keys, at any depth, never name an account identifier, and
    whose placeholders are all whole string values that include every name
    in required and nothing outside required and optional.
    """
    template = _get_str(data, dotted_key)
    try:
        doc = json.loads(template)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config key {dotted_key} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ConfigError(f"config key {dotted_key} must be a JSON object")

    identifiers = _identifier_keys(doc)
    if identifiers:
        raise ConfigError(
            f"config key {dotted_key} must not name the account ({', '.join(sorted(identifiers))}); it comes "
            'from the live accounts response through "{account}"'
        )
    used = _template_placeholders(doc, dotted_key)

    if unknown := sorted(used - required - optional):
        raise ConfigError(f"config key {dotted_key} uses unknown placeholders: {', '.join(unknown)}")
    if missing := sorted(required - used):
        raise ConfigError(f"config key {dotted_key} lacks placeholders: {', '.join(missing)}")
    return template


def _template_placeholders(value: object, dotted_key: str) -> set[str]:
    """Return every placeholder name in a parsed order template.

    Raises ConfigError for a placeholder substitution would not replace,
    one inside a longer string or in an object key, since it would
    otherwise reach the server verbatim.
    """
    if isinstance(value, dict):
        found: set[str] = set()
        for key, item in value.items():
            if PLACEHOLDER_RE.search(key):
                raise ConfigError(f"config key {dotted_key}: a placeholder cannot be an object key: {key!r}")
            found |= _template_placeholders(item, dotted_key)
        return found
    if isinstance(value, list):
        return set().union(*(_template_placeholders(item, dotted_key) for item in value))
    if isinstance(value, str) and PLACEHOLDER_RE.search(value):
        whole = PLACEHOLDER_RE.fullmatch(value)
        if whole is None:
            raise ConfigError(f"config key {dotted_key}: a placeholder must be the whole string value: {value!r}")
        return {whole.group(1)}
    return set()


# Every key [events] may hold, so a misspelt key, or one that belongs in
# another table, fails at load instead of being silently ignored.
_EVENTS_KEYS = frozenset(
    {
        "order_event_name",
        "order_states",
        "deal_states",
        "event_id",
        "order_key",
        "op_type",
        "op_code",
        "order_qty",
        "cancel_qty",
        "deal_key",
        "deal_qty",
        "op_code_success",
        "order_exchange_ts",
        "deal_exchange_ts",
        "exchange_ts_pointers",
        "exchange_ts_format",
        "heartbeat_event",
        "gap_threshold_s",
    }
)


def _parse_events(data: Mapping[str, Any], config_dir: Path) -> EventFields:
    table = _optional_table(data, "events")
    _reject_unknown_keys(table, _EVENTS_KEYS, "events")

    def name(key: str) -> str:
        value = _get_str(data, f"events.{key}")
        if not value:
            raise ConfigError(f"config key events.{key} must not be empty")
        return value

    def names(key: str) -> tuple[str, ...]:
        value = _lookup(data, f"events.{key}")
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            raise ConfigError(f"config key events.{key} must be a non-empty array of non-empty strings")
        return tuple(value)

    def pointer(key: str) -> str:
        return _get_pointer(data, f"events.{key}")

    order_states = names("order_states")
    deal_states = names("deal_states")
    if set(order_states) & set(deal_states):
        raise ConfigError("config keys events.order_states and events.deal_states must not share a state")

    exchange_ts_pointers: tuple[str, ...] = ()
    if "exchange_ts_pointers" in table:
        exchange_ts_pointers = names("exchange_ts_pointers")
        for index, item in enumerate(exchange_ts_pointers):
            if not is_valid_pointer_syntax(item):
                raise ConfigError(f"config key events.exchange_ts_pointers[{index}] is not a valid JSON pointer")
    exchange_ts_format = name("exchange_ts_format") if "exchange_ts_format" in table else None
    if exchange_ts_format is None and len(exchange_ts_pointers) > 1:
        raise ConfigError("config key events.exchange_ts_format is required with more than one exchange_ts_pointers")
    if exchange_ts_format is not None and not exchange_ts_pointers:
        raise ConfigError("config key events.exchange_ts_format needs events.exchange_ts_pointers")

    gap_threshold_s = _get_number(data, "events.gap_threshold_s")
    if not 0 < gap_threshold_s < float("inf"):
        raise ConfigError("config key events.gap_threshold_s must be a positive finite number")

    return EventFields(
        order_event_name=name("order_event_name"),
        order_states=order_states,
        deal_states=deal_states,
        event_id=pointer("event_id"),
        order_key=pointer("order_key"),
        op_type=pointer("op_type"),
        op_code=pointer("op_code"),
        order_qty=pointer("order_qty"),
        cancel_qty=pointer("cancel_qty"),
        deal_key=pointer("deal_key"),
        deal_qty=pointer("deal_qty"),
        op_code_success=name("op_code_success"),
        order_exchange_ts=pointer("order_exchange_ts"),
        deal_exchange_ts=pointer("deal_exchange_ts"),
        exchange_ts_pointers=exchange_ts_pointers,
        exchange_ts_format=exchange_ts_format,
        heartbeat_event=name("heartbeat_event"),
        gap_threshold_s=gap_threshold_s,
    )


_HISTORY_OPS = ("ticks", "kbars", "snapshots")


def _parse_history(data: Mapping[str, Any], config_dir: Path) -> HistoryConfig:
    # usage_bytes_pointer is looked up through _lookup (via _get_pointer)
    # rather than read from the table below, so a p1.toml missing the
    # whole [history] section fails here with that dotted key, the same
    # way a missing [interlock] section does.
    usage_bytes_pointer = _get_pointer(data, "history.usage_bytes_pointer")

    known_endpoints = _lookup(data, "endpoints")
    if not isinstance(known_endpoints, Mapping):
        raise ConfigError("config key endpoints must be a table")

    # run_history always reads usage itself, not through a configurable
    # op name, so this checks the fixed "usage" entry directly rather
    # than a dotted key someone could point elsewhere.
    _require_endpoint_method(known_endpoints, "usage", "GET", "endpoints.usage")

    table = _optional_table(data, "history")
    raw_queries = table.get("query", [])
    if not isinstance(raw_queries, list):
        raise ConfigError("config key history.query must be an array of tables")
    if not raw_queries:
        raise ConfigError("config key history.query must not be empty")

    queries = tuple(
        _parse_history_query(item, index, known_endpoints) for index, item in enumerate(raw_queries)
    )
    return HistoryConfig(queries=queries, usage_bytes_pointer=usage_bytes_pointer)


def _parse_history_query(item: Any, index: int, known_endpoints: Mapping[str, Any]) -> HistoryQuery:
    key = f"history.query[{index}]"
    if not isinstance(item, Mapping):
        raise ConfigError(f"config key {key} must be a table")

    op = item.get("op")
    if op not in _HISTORY_OPS:
        raise ConfigError(f"config key {key}.op must be one of {_HISTORY_OPS}, got {op!r}")
    _require_endpoint_method(known_endpoints, op, "POST", f"{key}.op")

    body = item.get("body")
    if not isinstance(body, str):
        raise ConfigError(f"config key {key}.body must be a string, got {type(body).__name__}")
    try:
        parsed_body = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config key {key}.body is not valid JSON: {exc}") from exc
    if not isinstance(parsed_body, dict):
        raise ConfigError(f"config key {key}.body must be a JSON object")

    return HistoryQuery(op=op, body=body)


def _get_str_tuple(data: Mapping[str, Any], dotted_key: str) -> tuple[str, ...]:
    value = _lookup(data, dotted_key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"config key {dotted_key} must be an array of strings")
    return tuple(value)


# A plain field name not ending in _<digits>, since it becomes a
# placeholder category and <CATEGORY>_<n> must parse back unambiguously.
_SCRUB_FIELD_NAME = r"(?![A-Za-z0-9_]*_[0-9]+$)[A-Za-z][A-Za-z0-9_]*"

# The last token names the placeholder category of every value the pointer finds
_SCRUB_POINTER_RE = re.compile(rf"^(/[^/]*)*/{_SCRUB_FIELD_NAME}$")


def _get_scrub_pointers(data: Mapping[str, Any], dotted_key: str) -> tuple[str, ...]:
    pointers = _get_str_tuple(data, dotted_key)
    if not pointers:
        raise ConfigError(f"config key {dotted_key} must list at least one JSON pointer")
    for index, pointer in enumerate(pointers):
        if not _SCRUB_POINTER_RE.fullmatch(pointer):
            raise ConfigError(
                f"config key {dotted_key}[{index}] must be a JSON pointer ending in a field name "
                "that does not end in _<digits>"
            )
    return pointers


_SCRUB_KEY_RE = re.compile(rf"^{_SCRUB_FIELD_NAME}$")


def _get_scrub_keys(data: Mapping[str, Any], dotted_key: str) -> tuple[str, ...]:
    keys = _get_str_tuple(data, dotted_key)
    for index, key in enumerate(keys):
        if not _SCRUB_KEY_RE.fullmatch(key):
            raise ConfigError(f"config key {dotted_key}[{index}] must be a plain field name not ending in _<digits>")
    return keys


def _parse_scrub(data: Mapping[str, Any], config_dir: Path) -> ScrubConfig:
    _optional_table(data, "scrub")
    results_root = _get_fs_path(data, "results_root", config_dir)
    map_path = _get_fs_path(data, "scrub.map_path", config_dir)
    if not map_path.is_relative_to(results_root):
        raise ConfigError("config key scrub.map_path must be inside results_root, which stays out of version control")
    return ScrubConfig(
        account_id_pointers=_get_scrub_pointers(data, "scrub.account_id_pointers"),
        ca_pointers=_get_scrub_pointers(data, "scrub.ca_pointers"),
        account_id_keys=_get_scrub_keys(data, "scrub.account_id_keys"),
        map_path=map_path,
        selection=_get_fs_path(data, "scrub.selection", config_dir),
        contract_doc=_get_fs_path(data, "scrub.contract_doc", config_dir),
        extra_scan_paths=tuple(
            (config_dir / raw).resolve() for raw in _get_str_tuple(data, "scrub.extra_scan_paths")
        ),
    )
