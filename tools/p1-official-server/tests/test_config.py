"""Tests for p1rec.config: TOML loading, validation and secret handling."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import MINIMAL_CONFIG_LINES, WriteConfig

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


def test_load_config_success(tmp_path: Path, write_config: WriteConfig) -> None:
    config_path = write_config()

    cfg = load_config(config_path)

    assert cfg.base_url == "http://127.0.0.1:8080"
    assert cfg.http_timeout_s == 35.0
    # Relative paths resolve against p1.toml's directory, not the cwd
    assert cfg.results_root == (tmp_path / "results").resolve()
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


def test_load_real_p1_toml() -> None:
    # tests/test_config.py -> tests/ -> tools/p1-official-server/
    repo_p1_toml = Path(__file__).resolve().parent.parent / "p1.toml"

    cfg = load_config(repo_p1_toml)

    assert cfg.base_url == "http://127.0.0.1:8080"
    assert "place_stock" in cfg.endpoints
    assert "stream_connect" not in cfg.endpoints  # left to the stream section


def test_missing_key_names_full_path(write_config: WriteConfig) -> None:
    lines = [line for line in MINIMAL_CONFIG_LINES if line != 'path = "/api/v1/order/place_order"']
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"endpoints\.place_stock\.path"):
        load_config(config_path)


def test_wrong_type_names_full_path(write_config: WriteConfig) -> None:
    lines = [
        'http_timeout_s = "thirty-five"' if line == "http_timeout_s = 35" else line for line in MINIMAL_CONFIG_LINES
    ]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"http_timeout_s"):
        load_config(config_path)


def test_inline_secret_rejected(write_config: WriteConfig) -> None:
    lines = ['api_key = "x"', *MINIMAL_CONFIG_LINES]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"api_key"):
        load_config(config_path)


def test_inline_secret_rejected_when_nested(write_config: WriteConfig) -> None:
    lines = [*MINIMAL_CONFIG_LINES, "", "[some_table]", 'ca_passwd = "y"']
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"some_table\.ca_passwd"):
        load_config(config_path)


def test_inline_secret_rejected_inside_array_of_tables(write_config: WriteConfig) -> None:
    lines = [*MINIMAL_CONFIG_LINES, "", "[[history.query]]", 'api_key = "X"']
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"history\.query\.api_key"):
        load_config(config_path)


def test_secrets_table_itself_is_not_rejected(write_config: WriteConfig) -> None:
    # [secrets] legitimately has keys named api_key/sec_key/ca_passwd whose
    # values are environment variable names, not secret values.
    config_path = write_config()

    load_config(config_path)  # must not raise


def test_secret_env_value_must_look_like_env_var_name(write_config: WriteConfig) -> None:
    bogus_value = "A123456789"  # shaped like a Taiwanese national ID, not an env var name
    lines = [
        f'person_id = "{bogus_value}"' if line == 'person_id = "P1_PERSON_ID"' else line
        for line in MINIMAL_CONFIG_LINES
    ]
    config_path = write_config(lines)

    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert "secrets.person_id" in message
    # The bogus value may itself be a stray secret, so it must never be echoed
    assert bogus_value not in message


def test_section_present_but_not_table_raises(write_config: WriteConfig) -> None:
    lines = ["interlock = 5", *MINIMAL_CONFIG_LINES]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"interlock"):
        load_config(config_path)


def test_missing_secret_env(write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config()
    cfg = load_config(config_path)

    for env_var in cfg.secret_env.values():
        monkeypatch.delenv(env_var, raising=False)

    with pytest.raises(ConfigError, match=r"SJ_API_KEY"):
        read_secrets(cfg)


def test_missing_secret_env_names_the_missing_variable(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_config()
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


def test_empty_secret_env_treated_as_missing(
    write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_config()
    cfg = load_config(config_path)

    monkeypatch.setenv("SJ_API_KEY", "fake-api-key-value")
    monkeypatch.setenv("SJ_SEC_KEY", "fake-sec-key-value")
    monkeypatch.setenv("SJ_CA_PASSWD", "fake-ca-passwd")
    monkeypatch.setenv("P1_PERSON_ID", "A123456789")
    monkeypatch.setenv("P1_EGRESS_IP", "")  # blank, not unset

    with pytest.raises(ConfigError, match=r"P1_EGRESS_IP"):
        read_secrets(cfg)


def test_read_secrets_success(write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config()
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
