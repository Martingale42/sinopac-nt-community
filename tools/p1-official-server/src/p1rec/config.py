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
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
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
    """Placeholder, filled in once contract fixture field names are defined."""


@dataclass(frozen=True)
class FundsConfig:
    """Placeholder, filled in once funds calibration fields are defined."""


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

    # (logical operation, JSON body or "@account:<account type>")
    startup_requests: tuple[tuple[str, str], ...] = ()
    retry_s: float = 0.0
    instruments: tuple[Instrument, ...] = ()
    actions: tuple[StreamAction, ...] = ()


@dataclass(frozen=True)
class OrdersConfig:
    """Placeholder, filled in once order-scenario fields are defined."""


@dataclass(frozen=True)
class EventFields:
    """Placeholder, filled in once order/trade event field names are defined."""


@dataclass(frozen=True)
class HistoryConfig:
    """Placeholder, filled in once history-query fields are defined."""


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


_DEFAULT_INTERLOCK_ENDPOINT_OP = "mode"


def _parse_interlock(data: Mapping[str, Any], config_dir: Path) -> InterlockConfig:
    table = _optional_table(data, "interlock")
    if "endpoint_op" in table:
        endpoint_op = _get_str(data, "interlock.endpoint_op")
    else:
        endpoint_op = _DEFAULT_INTERLOCK_ENDPOINT_OP
    pointer = _get_pointer(data, "interlock.pointer")

    known_endpoints = _lookup(data, "endpoints")
    if not isinstance(known_endpoints, Mapping) or endpoint_op not in known_endpoints:
        raise ConfigError(f"config key interlock.endpoint_op names unknown endpoint: {endpoint_op!r}")

    # A POST that reports simulation mode as a side effect would still be
    # a mutation the interlock must not itself trigger, so only a GET may
    # back the check.
    endpoint_table = known_endpoints[endpoint_op]
    endpoint_method = endpoint_table.get("method") if isinstance(endpoint_table, Mapping) else None
    if endpoint_method != "GET":
        raise ConfigError(
            f"config key interlock.endpoint_op must name a GET endpoint, but {endpoint_op!r} is {endpoint_method!r}"
        )

    return InterlockConfig(endpoint_op=endpoint_op, pointer=pointer)


def _parse_contracts(data: Mapping[str, Any], config_dir: Path) -> ContractFields:
    _optional_table(data, "contracts")
    return ContractFields()


def _parse_funds(data: Mapping[str, Any], config_dir: Path) -> FundsConfig:
    _optional_table(data, "funds")
    return FundsConfig()


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
ACCOUNT_BODY_RE = re.compile(r"@account:(S|F|H)")

# AccountRequest fields that identify the account holder, refused in a
# literal startup body for the same reason.
_ACCOUNT_IDENTIFIER_KEYS = frozenset({"account_id", "person_id"})

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

    retry_s = _get_number(data, "stream.retry_s")
    if retry_s <= 0:
        raise ConfigError("config key stream.retry_s must be positive")

    instruments = _parse_instruments(table)
    return StreamConfig(
        streams=_parse_stream_endpoints(data),
        subscribe=_named_endpoint(data, STREAM_SUBSCRIBE_OP),
        unsubscribe=_named_endpoint(data, STREAM_UNSUBSCRIBE_OP),
        subscribe_body=_get_body_template(data, "stream.subscribe_body"),
        unsubscribe_body=_get_body_template(data, "stream.unsubscribe_body"),
        startup_requests=_parse_startup_requests(data),
        retry_s=retry_s,
        instruments=instruments,
        actions=_parse_actions(table, instruments),
    )


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
    return text


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


def _parse_startup_requests(data: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    dotted_key = "stream.startup_requests"
    raw = _lookup(data, dotted_key)
    if not isinstance(raw, list):
        raise ConfigError(f"config key {dotted_key} must be an array of [operation, body] pairs")
    endpoints = _lookup(data, "endpoints")

    requests: list[tuple[str, str]] = []
    for index, item in enumerate(raw):
        where = f"{dotted_key}[{index}]"
        if not (isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item)):
            raise ConfigError(f"config key {where} must be an [operation, body] pair of strings")
        op, body = item
        if not isinstance(endpoints, Mapping) or op not in endpoints:
            raise ConfigError(f"config key {where} names unknown endpoint: {op!r}")
        if ACCOUNT_BODY_RE.fullmatch(body):
            if ACCOUNTS_OP not in endpoints:
                raise ConfigError(f"config key {where} uses {body!r}, which needs endpoints.{ACCOUNTS_OP}")
        else:
            try:
                parsed = json.loads(body)
            except ValueError as exc:
                raise ConfigError(f'config key {where} body must be JSON or "@account:<S|F|H>"') from exc
            if isinstance(parsed, dict) and _ACCOUNT_IDENTIFIER_KEYS & parsed.keys():
                raise ConfigError(f'config key {where} body must not name the account; use "@account:<S|F|H>"')
        requests.append((op, body))
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


def _parse_orders(data: Mapping[str, Any], config_dir: Path) -> OrdersConfig:
    _optional_table(data, "orders")
    return OrdersConfig()


def _parse_events(data: Mapping[str, Any], config_dir: Path) -> EventFields:
    _optional_table(data, "events")
    return EventFields()


def _parse_history(data: Mapping[str, Any], config_dir: Path) -> HistoryConfig:
    _optional_table(data, "history")
    return HistoryConfig()


def _parse_scrub(data: Mapping[str, Any], config_dir: Path) -> ScrubConfig:
    _optional_table(data, "scrub")
    return ScrubConfig()
