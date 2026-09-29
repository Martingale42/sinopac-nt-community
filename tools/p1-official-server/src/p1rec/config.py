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


class ConfigError(ValueError):
    """Raised when p1.toml is missing a required key or has the wrong type."""


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str  # May contain {name}, substituted from path_vars at request time


@dataclass(frozen=True)
class InterlockConfig:
    """Placeholder, filled in once mode-interlock checking is implemented."""


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
    """Placeholder, filled in once order/trade event field names are defined."""


@dataclass(frozen=True)
class HistoryConfig:
    """Placeholder, filled in once history-query fields are defined."""


@dataclass(frozen=True)
class ScrubConfig:
    # JSON pointers applied to every element of each recorded accounts
    # response, and to each ca_status response, to find the identifiers to
    # scrub, with the pointer's last token upper-cased naming the category.
    account_id_pointers: tuple[str, ...]
    ca_pointers: tuple[str, ...]

    # Holds the original values themselves, so it must stay under results_root
    map_path: Path

    # fixtures.toml; each entry has name, run_id, source, lines, checklist, instrument
    selection: Path

    # Committed files and directories scrub --scan-only checks besides fixtures_root
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


def _parse_interlock(data: Mapping[str, Any], config_dir: Path) -> InterlockConfig:
    _optional_table(data, "interlock")
    return InterlockConfig()


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
    _optional_table(data, "events")
    return EventFields()


def _parse_history(data: Mapping[str, Any], config_dir: Path) -> HistoryConfig:
    _optional_table(data, "history")
    return HistoryConfig()


def _get_str_tuple(data: Mapping[str, Any], dotted_key: str) -> tuple[str, ...]:
    value = _lookup(data, dotted_key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"config key {dotted_key} must be an array of strings")
    return tuple(value)


# The last token must be a plain field name, since it becomes the
# placeholder category of every value the pointer finds.
_SCRUB_POINTER_RE = re.compile(r"^(/[^/]*)*/[A-Za-z][A-Za-z0-9_]*$")


def _get_scrub_pointers(data: Mapping[str, Any], dotted_key: str) -> tuple[str, ...]:
    pointers = _get_str_tuple(data, dotted_key)
    if not pointers:
        raise ConfigError(f"config key {dotted_key} must list at least one JSON pointer")
    for index, pointer in enumerate(pointers):
        if not _SCRUB_POINTER_RE.fullmatch(pointer):
            raise ConfigError(f"config key {dotted_key}[{index}] must be a JSON pointer ending in a field name")
    return pointers


def _parse_scrub(data: Mapping[str, Any], config_dir: Path) -> ScrubConfig:
    _optional_table(data, "scrub")
    results_root = _get_fs_path(data, "results_root", config_dir)
    map_path = _get_fs_path(data, "scrub.map_path", config_dir)
    if not map_path.is_relative_to(results_root):
        raise ConfigError("config key scrub.map_path must be inside results_root, which stays out of version control")
    return ScrubConfig(
        account_id_pointers=_get_scrub_pointers(data, "scrub.account_id_pointers"),
        ca_pointers=_get_scrub_pointers(data, "scrub.ca_pointers"),
        map_path=map_path,
        selection=_get_fs_path(data, "scrub.selection", config_dir),
        extra_scan_paths=tuple(
            (config_dir / raw).resolve() for raw in _get_str_tuple(data, "scrub.extra_scan_paths")
        ),
    )
