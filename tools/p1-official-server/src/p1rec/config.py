"""Load and validate p1.toml into a P1Config.

Each of InterlockConfig, ContractFields, FundsConfig, StreamConfig,
OrdersConfig, EventFields, HistoryConfig and ScrubConfig is filled in by
its own `_parse_<section>` function below, which receives the whole
parsed document plus the config file's directory, so a full dotted key
(for validation errors) or a relative path can be resolved without
touching load_config itself.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
class StreamConfig:
    """Placeholder, filled in once SSE client fields are defined."""


@dataclass(frozen=True)
class OrdersConfig:
    """Placeholder, filled in once order-scenario fields are defined."""


@dataclass(frozen=True)
class EventFields:
    """Where order reports, the trades list and stream timing live in recorded JSON."""

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

    # JSON pointers into the trades response: the list ("" for a bare
    # array), then key, status and filled quantity within each item.
    rest_list: str
    rest_key: str
    rest_status: str
    rest_filled_qty: str

    # Pointer into each trades item to its order time in epoch seconds,
    # which dates the snapshot when no --trading-day is given and windows
    # the REST side by calendar date.
    rest_order_ts: str

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


def _parse_stream(data: Mapping[str, Any], config_dir: Path) -> StreamConfig:
    _optional_table(data, "stream")
    return StreamConfig()


def _parse_orders(data: Mapping[str, Any], config_dir: Path) -> OrdersConfig:
    _optional_table(data, "orders")
    return OrdersConfig()


def _parse_events(data: Mapping[str, Any], config_dir: Path) -> EventFields:
    table = _optional_table(data, "events")

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

    def pointer(key: str, *, allow_root: bool = False) -> str:
        return _get_pointer(data, f"events.{key}", allow_root=allow_root)

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
        rest_list=pointer("rest_list", allow_root=True),
        rest_key=pointer("rest_key"),
        rest_status=pointer("rest_status"),
        rest_filled_qty=pointer("rest_filled_qty"),
        rest_order_ts=pointer("rest_order_ts"),
        exchange_ts_pointers=exchange_ts_pointers,
        exchange_ts_format=exchange_ts_format,
        heartbeat_event=name("heartbeat_event"),
        gap_threshold_s=gap_threshold_s,
    )


def _parse_history(data: Mapping[str, Any], config_dir: Path) -> HistoryConfig:
    _optional_table(data, "history")
    return HistoryConfig()


def _parse_scrub(data: Mapping[str, Any], config_dir: Path) -> ScrubConfig:
    _optional_table(data, "scrub")
    return ScrubConfig()
