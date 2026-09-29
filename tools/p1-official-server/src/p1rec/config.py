"""Load and validate p1.toml into a P1Config.

Every later task's section (interlock, contracts, funds, stream, orders,
events, history, scrub) has its own `_parse_<section>` function below. Each
takes that section's raw TOML table (an empty dict if the section is
absent) and returns its dataclass, so a later task can add required keys
and validation to its own function without touching load_config itself.
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

# Keys that must never carry a literal secret value in p1.toml, since inside
# [secrets] these same names are role -> env-var-name mappings instead, not
# secret values, which is why that table is exempt in _reject_inline_secrets.
_INLINE_SECRET_KEYS = frozenset({"api_key", "sec_key", "ca_passwd"})

# Shape every [secrets] value must have, since it names an environment
# variable rather than carrying a secret itself.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


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
        interlock=_parse_interlock(data.get("interlock", {})),
        contracts=_parse_contracts(data.get("contracts", {})),
        funds=_parse_funds(data.get("funds", {})),
        stream=_parse_stream(data.get("stream", {})),
        orders=_parse_orders(data.get("orders", {})),
        events=_parse_events(data.get("events", {})),
        history=_parse_history(data.get("history", {})),
        scrub=_parse_scrub(data.get("scrub", {})),
    )


def read_secrets(cfg: P1Config) -> dict[str, str]:
    """Read secret values from the environment variables named in cfg.secret_env."""
    secrets: dict[str, str] = {}
    for role, env_var in cfg.secret_env.items():
        value = os.environ.get(env_var)
        if value is None:
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
        if isinstance(value, Mapping):
            _reject_inline_secrets(value, current)


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


def _parse_interlock(_raw: Mapping[str, Any]) -> InterlockConfig:
    return InterlockConfig()


def _parse_contracts(_raw: Mapping[str, Any]) -> ContractFields:
    return ContractFields()


def _parse_funds(_raw: Mapping[str, Any]) -> FundsConfig:
    return FundsConfig()


def _parse_stream(_raw: Mapping[str, Any]) -> StreamConfig:
    return StreamConfig()


def _parse_orders(_raw: Mapping[str, Any]) -> OrdersConfig:
    return OrdersConfig()


def _parse_events(_raw: Mapping[str, Any]) -> EventFields:
    return EventFields()


def _parse_history(_raw: Mapping[str, Any]) -> HistoryConfig:
    return HistoryConfig()


def _parse_scrub(_raw: Mapping[str, Any]) -> ScrubConfig:
    return ScrubConfig()
