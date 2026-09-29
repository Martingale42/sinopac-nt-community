"""Tests for p1rec.config: TOML loading, validation and secret handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from p1rec.config import (
    ConfigError,
    ContractFields,
    Endpoint,
    EventFields,
    FundsConfig,
    HistoryConfig,
    InterlockConfig,
    OrdersConfig,
    ScrubConfig,
    StreamConfig,
    load_config,
    read_secrets,
)

# A minimal but complete p1.toml, copied and mutated by individual tests
# rather than the real p1.toml, so they stay independent of it.
_VALID_LINES = [
    'base_url = "http://127.0.0.1:8080"',
    "http_timeout_s = 35",
    'results_root = "results/p1"',
    'fixtures_root = "fixtures"',
    "",
    "[secrets]",
    'api_key = "SJ_API_KEY"',
    'sec_key = "SJ_SEC_KEY"',
    'ca_passwd = "SJ_CA_PASSWD"',
    'person_id = "P1_PERSON_ID"',
    'egress_ip = "P1_EGRESS_IP"',
    "",
    "[endpoints.mode]",
    'method = "GET"',
    'path = "/api/v1/info"',
    "",
    "[endpoints.place_stock]",
    'method = "POST"',
    'path = "/api/v1/order/place_order"',
]


def _write_config(tmp_path: Path, lines: list[str]) -> Path:
    config_path = tmp_path / "p1.toml"
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


def test_load_config_success(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, _VALID_LINES)

    cfg = load_config(config_path)

    assert cfg.base_url == "http://127.0.0.1:8080"
    assert cfg.http_timeout_s == 35.0
    # Relative paths resolve against p1.toml's directory, not the cwd
    assert cfg.results_root == (tmp_path / "results/p1").resolve()
    assert cfg.fixtures_root == (tmp_path / "fixtures").resolve()
    assert cfg.secret_env == {
        "api_key": "SJ_API_KEY",
        "sec_key": "SJ_SEC_KEY",
        "ca_passwd": "SJ_CA_PASSWD",
        "person_id": "P1_PERSON_ID",
        "egress_ip": "P1_EGRESS_IP",
    }
    assert cfg.endpoints == {
        "mode": Endpoint(method="GET", path="/api/v1/info"),
        "place_stock": Endpoint(method="POST", path="/api/v1/order/place_order"),
    }
    # These placeholder types only need to construct and load here
    assert cfg.interlock == InterlockConfig()
    assert cfg.contracts == ContractFields()
    assert cfg.funds == FundsConfig()
    assert cfg.stream == StreamConfig()
    assert cfg.orders == OrdersConfig()
    assert cfg.events == EventFields()
    assert cfg.history == HistoryConfig()
    assert cfg.scrub == ScrubConfig()


def test_missing_key_names_full_path(tmp_path: Path) -> None:
    lines = [line for line in _VALID_LINES if line != 'path = "/api/v1/order/place_order"']
    config_path = _write_config(tmp_path, lines)

    with pytest.raises(ConfigError, match=r"endpoints\.place_stock\.path"):
        load_config(config_path)


def test_wrong_type_names_full_path(tmp_path: Path) -> None:
    lines = ["http_timeout_s = \"thirty-five\"" if line == "http_timeout_s = 35" else line for line in _VALID_LINES]
    config_path = _write_config(tmp_path, lines)

    with pytest.raises(ConfigError, match=r"http_timeout_s"):
        load_config(config_path)


def test_inline_secret_rejected(tmp_path: Path) -> None:
    lines = ['api_key = "x"', *_VALID_LINES]
    config_path = _write_config(tmp_path, lines)

    with pytest.raises(ConfigError, match=r"api_key"):
        load_config(config_path)


def test_inline_secret_rejected_when_nested(tmp_path: Path) -> None:
    lines = [*_VALID_LINES, "", "[some_table]", 'ca_passwd = "y"']
    config_path = _write_config(tmp_path, lines)

    with pytest.raises(ConfigError, match=r"some_table\.ca_passwd"):
        load_config(config_path)


def test_secrets_table_itself_is_not_rejected(tmp_path: Path) -> None:
    # [secrets] legitimately has keys named api_key/sec_key/ca_passwd whose
    # values are environment variable names, not secret values.
    config_path = _write_config(tmp_path, _VALID_LINES)

    load_config(config_path)  # must not raise


def test_secret_env_value_must_look_like_env_var_name(tmp_path: Path) -> None:
    bogus_value = "sj-not-an-env-var"
    lines = [f'api_key = "{bogus_value}"' if line == 'api_key = "SJ_API_KEY"' else line for line in _VALID_LINES]
    config_path = _write_config(tmp_path, lines)

    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert "secrets.api_key" in message
    # The bogus value may itself be a stray secret, so it must never be echoed
    assert bogus_value not in message


def test_missing_secret_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _write_config(tmp_path, _VALID_LINES)
    cfg = load_config(config_path)

    for env_var in cfg.secret_env.values():
        monkeypatch.delenv(env_var, raising=False)

    with pytest.raises(ConfigError, match=r"SJ_API_KEY"):
        read_secrets(cfg)


def test_missing_secret_env_names_the_missing_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _write_config(tmp_path, _VALID_LINES)
    cfg = load_config(config_path)

    for env_var in cfg.secret_env.values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("SJ_API_KEY", "fake-api-key-value")
    monkeypatch.setenv("SJ_SEC_KEY", "fake-sec-key-value")
    monkeypatch.setenv("SJ_CA_PASSWD", "fake-ca-passwd")
    monkeypatch.setenv("P1_EGRESS_IP", "203.0.113.1")
    # P1_PERSON_ID stays unset

    with pytest.raises(ConfigError, match=r"P1_PERSON_ID"):
        read_secrets(cfg)


def test_read_secrets_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _write_config(tmp_path, _VALID_LINES)
    cfg = load_config(config_path)

    monkeypatch.setenv("SJ_API_KEY", "fake-api-key-value")
    monkeypatch.setenv("SJ_SEC_KEY", "fake-sec-key-value")
    monkeypatch.setenv("SJ_CA_PASSWD", "fake-ca-passwd")
    monkeypatch.setenv("P1_PERSON_ID", "A123456789")
    monkeypatch.setenv("P1_EGRESS_IP", "203.0.113.1")

    secrets = read_secrets(cfg)

    assert secrets == {
        "api_key": "fake-api-key-value",
        "sec_key": "fake-sec-key-value",
        "ca_passwd": "fake-ca-passwd",
        "person_id": "A123456789",
        "egress_ip": "203.0.113.1",
    }
