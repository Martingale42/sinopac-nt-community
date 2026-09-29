"""Load and validate p1.toml into a P1Config.

Each of InterlockConfig, ContractFields, FundsConfig, StreamConfig,
OrdersConfig, EventFields, HistoryConfig and ScrubConfig is filled in by
its own `_parse_<section>` function below, which receives the whole
parsed document plus the config file's directory, so a full dotted key
(for validation errors) or a relative path can be resolved without
touching load_config itself.
"""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

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
class StreamConfig:
    """Placeholder, filled in once SSE client fields are defined."""


@dataclass(frozen=True)
class OrdersConfig:
    # Logical operation (place_stock, place_futures, update_price,
    # update_qty, cancel) -> JSON body template; a string value that is
    # exactly "{name}" is replaced by a typed value when sent.
    body_templates: dict[str, str]

    # Market (stock, odd, futures) -> ContractRequest.exchange
    exchange: dict[str, str]

    # JSON pointers into one Trade, the place response and each element of
    # the trades response alike: the order id that update_price, update_qty
    # and cancel take as trade_id, the order status ("Filled" once filled,
    # "Failed" when rejected), and the quantity filled so far.
    trade_id_pointer: str
    filled_pointer: str
    deal_quantity_pointer: str

    step_wait_s: float
    fill_poll_s: float
    fill_timeout_s: float


@dataclass(frozen=True)
class EventFields:
    """Placeholder, filled in once order/trade event field names are defined."""


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
    """Placeholder, filled in once de-identification fields are defined."""


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


def _parse_stream(data: Mapping[str, Any], config_dir: Path) -> StreamConfig:
    _optional_table(data, "stream")
    return StreamConfig()


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

# Account fields that identify the holder, which only ever come from the
# live accounts response, never from p1.toml.
_ACCOUNT_IDENTIFIER_KEYS = frozenset({"account_id", "person_id", "username"})

# scenarios.Market's values; config.py cannot import scenarios, which imports it
ORDER_MARKETS = ("stock", "odd", "futures")

# A "{name}" placeholder in an order body template
TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _parse_orders(data: Mapping[str, Any], config_dir: Path) -> OrdersConfig:
    _optional_table(data, "orders")
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
    exchange = {market: _get_str(data, f"orders.exchange.{market}") for market in ORDER_MARKETS}

    step_wait_s = _get_positive_number(data, "orders.step_wait_s")
    fill_poll_s = _get_positive_number(data, "orders.fill_poll_s")
    fill_timeout_s = _get_positive_number(data, "orders.fill_timeout_s")
    if fill_timeout_s <= fill_poll_s:
        raise ConfigError("config key orders.fill_timeout_s must be greater than orders.fill_poll_s")

    return OrdersConfig(
        body_templates=templates,
        exchange=exchange,
        trade_id_pointer=_get_pointer(data, "orders.trade_id_pointer"),
        filled_pointer=_get_pointer(data, "orders.filled_pointer"),
        deal_quantity_pointer=_get_pointer(data, "orders.deal_quantity_pointer"),
        step_wait_s=step_wait_s,
        fill_poll_s=fill_poll_s,
        fill_timeout_s=fill_timeout_s,
    )


def _get_positive_number(data: Mapping[str, Any], dotted_key: str) -> float:
    value = _get_number(data, dotted_key)
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"config key {dotted_key} must be a positive number")
    return value


def _get_order_template(
    data: Mapping[str, Any], dotted_key: str, required: frozenset[str], optional: frozenset[str]
) -> str:
    """Read one order body template and check its placeholders and keys.

    Raises ConfigError naming dotted_key unless the template is a JSON
    object whose placeholders are all whole string values, include every
    name in required and nothing outside required and optional, and whose
    keys, at any depth, never name an account identifier.
    """
    template = _get_str(data, dotted_key)
    try:
        doc = json.loads(template)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config key {dotted_key} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ConfigError(f"config key {dotted_key} must be a JSON object")

    used: set[str] = set()
    for key, value in _walk_json(doc):
        if key in _ACCOUNT_IDENTIFIER_KEYS:
            raise ConfigError(
                f"config key {dotted_key} names account field {key!r}; the account comes from the live "
                'accounts response through "{account}", never from p1.toml'
            )
        if not isinstance(value, str) or not TEMPLATE_PLACEHOLDER_RE.search(value):
            continue
        whole = TEMPLATE_PLACEHOLDER_RE.fullmatch(value)
        if whole is None:
            raise ConfigError(f"config key {dotted_key}: placeholder in {value!r} must be the whole string value")
        used.add(whole.group(1))

    if unknown := sorted(used - required - optional):
        raise ConfigError(f"config key {dotted_key} uses unknown placeholders: {', '.join(unknown)}")
    if missing := sorted(required - used):
        raise ConfigError(f"config key {dotted_key} lacks placeholders: {', '.join(missing)}")
    return template


def _walk_json(node: object, key: str | None = None) -> Iterator[tuple[str | None, object]]:
    """Yield (key, value) for node and every value nested in it; key is None outside an object."""
    yield key, node
    if isinstance(node, dict):
        for child_key, child in node.items():
            yield from _walk_json(child, child_key)
    elif isinstance(node, list):
        for child in node:
            yield from _walk_json(child)


def _parse_events(data: Mapping[str, Any], config_dir: Path) -> EventFields:
    _optional_table(data, "events")
    return EventFields()


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


def _parse_scrub(data: Mapping[str, Any], config_dir: Path) -> ScrubConfig:
    _optional_table(data, "scrub")
    return ScrubConfig()
