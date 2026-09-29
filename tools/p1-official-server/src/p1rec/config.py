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
from decimal import Decimal, InvalidOperation
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
    """JSON pointers into one contract record, and the codes that use ETF ticks.

    The same pointers serve contracts_stock's single record and each
    element of contracts_futures' array, since both are ContractInfoRecord.
    """

    code: str
    limit_up: str
    limit_down: str
    reference: str
    etf_codes: tuple[str, ...]


@dataclass(frozen=True)
class FundsConfig:
    stock_available_pointer: str  # Into the balance response
    futures_available_pointer: str  # Into the margin response

    # Contract code -> initial margin per contract in TWD
    futures_margin_twd: dict[str, Decimal]


@dataclass(frozen=True)
class StreamConfig:
    """Placeholder, filled in once SSE client fields are defined."""


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
    etf_codes = _lookup(data, "contracts.etf_codes")
    if not isinstance(etf_codes, list) or not all(isinstance(code, str) and code for code in etf_codes):
        raise ConfigError("config key contracts.etf_codes must be an array of non-empty strings")
    return ContractFields(
        code=_get_pointer(data, "contracts.code"),
        limit_up=_get_pointer(data, "contracts.limit_up"),
        limit_down=_get_pointer(data, "contracts.limit_down"),
        reference=_get_pointer(data, "contracts.reference"),
        etf_codes=tuple(etf_codes),
    )


def _parse_funds(data: Mapping[str, Any], config_dir: Path) -> FundsConfig:
    _optional_table(data, "funds")
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
        stock_available_pointer=_get_pointer(data, "funds.stock_available_pointer"),
        futures_available_pointer=_get_pointer(data, "funds.futures_available_pointer"),
        futures_margin_twd=margins,
    )


def _parse_stream(data: Mapping[str, Any], config_dir: Path) -> StreamConfig:
    _optional_table(data, "stream")
    return StreamConfig()


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
