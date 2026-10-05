"""Tests for p1rec.config: TOML loading, validation and secret handling."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from helpers import FAKE_SECRET_ENV, MINIMAL_CONFIG_LINES, PLACE_FUTURES_TEMPLATE, PLACE_STOCK_TEMPLATE, WriteConfig

from p1rec.config import (
    ConfigError,
    ContractFields,
    Endpoint,
    EventFields,
    FundsConfig,
    HistoryConfig,
    HistoryQuery,
    InterlockConfig,
    OrdersConfig,
    ScrubConfig,
    StreamConfig,
    TradeFields,
    load_config,
    read_secret_role,
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
        "usage": Endpoint(method="GET", path="/api/v1/auth/usage"),
        "ticks": Endpoint(method="POST", path="/api/v1/data/ticks"),
        "trading_limits": Endpoint(method="POST", path="/api/v1/portfolio/trading_limits"),
        "place_futures": Endpoint(method="POST", path="/api/v1/order/place_order"),
        "update_price": Endpoint(method="POST", path="/api/v1/order/update_price"),
        "update_qty": Endpoint(method="POST", path="/api/v1/order/update_qty"),
        "cancel": Endpoint(method="POST", path="/api/v1/order/cancel_order"),
        "trades": Endpoint(method="POST", path="/api/v1/order/trades"),
    }
    assert cfg.interlock == InterlockConfig(endpoint_op="mode", pointer="/simulation")
    assert cfg.contracts == ContractFields(
        code="/code",
        limit_up="/limit_up",
        limit_down="/limit_down",
        reference="/reference",
        futures_tick="/tick",
        etf_codes=("0050",),
    )
    assert cfg.funds == FundsConfig(
        stock_available_op="trading_limits",
        stock_available_pointer="/trading_available",
        stock_report_only={},
        futures_available_pointer="/available_margin",
        futures_margin_twd={},
    )
    assert cfg.orders == OrdersConfig(
        body_templates={
            "place_stock": PLACE_STOCK_TEMPLATE,
            "place_futures": PLACE_FUTURES_TEMPLATE,
            "update_price": '{"trade_id": "{trade_id}", "price": "{price}"}',
            "update_qty": '{"trade_id": "{trade_id}", "quantity": "{quantity}"}',
            "cancel": '{"trade_id": "{trade_id}"}',
        },
        exchange={"stock": "TSE", "odd": "TSE", "futures": "TAIFEX"},
        step_wait_s=3.0,
        fill_poll_s=2.0,
        fill_timeout_s=60.0,
    )
    assert cfg.trades == TradeFields(
        list="",
        id="/order/id",
        status="/status/status",
        deal_quantity="/status/deal_quantity",
        order_ts="/status/order_ts",
        account_types=("F",),
    )
    # These remaining placeholder types only need to construct and load here
    assert cfg.stream == StreamConfig()
    assert cfg.events == EventFields(
        order_event_name="order_event",
        order_states=("StockOrder", "FuturesOrder"),
        deal_states=("StockDeal", "FuturesDeal"),
        event_id="/event_id",
        order_key="/order/id",
        op_type="/operation/op_type",
        op_code="/operation/op_code",
        order_qty="/order/quantity",
        cancel_qty="/status/cancel_quantity",
        deal_key="/trade_id",
        deal_qty="/quantity",
        op_code_success="00",
        order_exchange_ts="/status/exchange_ts",
        deal_exchange_ts="/ts",
        exchange_ts_pointers=("/date", "/time"),
        exchange_ts_format="%Y-%m-%d %H:%M:%S.%f",
        heartbeat_event="heartbeat",
        gap_threshold_s=60.0,
    )
    assert cfg.history == HistoryConfig(
        queries=(
            HistoryQuery(
                op="ticks",
                body='{"contract": {"security_type": "STK", "exchange": "TSE", "code": "2330"}, "date": "2026-09-29"}',
            ),
        ),
        usage_bytes_pointer="/bytes",
    )
    assert cfg.scrub == ScrubConfig(
        account_id_pointers=("/account_id", "/username", "/person_id"),
        ca_pointers=("/person_id",),
        account_id_keys=("subaccount",),
        map_path=(tmp_path / "results" / "scrub-map.json").resolve(),
        selection=(tmp_path / "fixtures.toml").resolve(),
        contract_doc=(tmp_path / "contract.md").resolve(),
        extra_scan_paths=(),
        ipv4_allow=frozenset(),
    )


def test_load_real_p1_toml() -> None:
    # tests/test_config.py -> tests/ -> tools/p1-official-server/
    repo_p1_toml = Path(__file__).resolve().parent.parent / "p1.toml"

    cfg = load_config(repo_p1_toml)

    assert cfg.base_url == "http://127.0.0.1:18080"
    assert "place_stock" in cfg.endpoints
    assert "stream_connect" not in cfg.endpoints  # left to the stream section
    assert cfg.interlock == InterlockConfig(endpoint_op="mode", pointer="/simulation")


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


def test_inline_secret_rejected_for_person_id_and_egress_ip(write_config: WriteConfig) -> None:
    bogus_value = "A123456789"  # shaped like a Taiwanese national ID
    lines = [*MINIMAL_CONFIG_LINES, "", "[notes]", f'person_id = "{bogus_value}"']
    config_path = write_config(lines)

    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert "notes.person_id" in message
    assert bogus_value not in message


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
    # MINIMAL_CONFIG_LINES already has a [interlock] table; drop it so the
    # top-level `interlock = 5` below is the only definition, since TOML
    # rejects a key defined twice.
    interlock_section_lines = {"[interlock]", 'endpoint_op = "mode"', 'pointer = "/simulation"'}
    without_interlock_table = [line for line in MINIMAL_CONFIG_LINES if line not in interlock_section_lines]
    lines = ["interlock = 5", *without_interlock_table]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"interlock"):
        load_config(config_path)


def test_interlock_endpoint_op_defaults_to_mode(write_config: WriteConfig) -> None:
    lines = [line for line in MINIMAL_CONFIG_LINES if line != 'endpoint_op = "mode"']
    config_path = write_config(lines)

    cfg = load_config(config_path)

    assert cfg.interlock.endpoint_op == "mode"


def test_interlock_missing_pointer_names_full_path(write_config: WriteConfig) -> None:
    lines = [line for line in MINIMAL_CONFIG_LINES if line != 'pointer = "/simulation"']
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"interlock\.pointer"):
        load_config(config_path)


def test_interlock_unknown_endpoint_op_rejected(write_config: WriteConfig) -> None:
    lines = [
        'endpoint_op = "no_such_op"' if line == 'endpoint_op = "mode"' else line for line in MINIMAL_CONFIG_LINES
    ]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"interlock\.endpoint_op"):
        load_config(config_path)


def test_interlock_endpoint_op_must_be_get(write_config: WriteConfig) -> None:
    # place_stock exists in [endpoints] but is a POST; the interlock must
    # never itself be backed by a mutating endpoint.
    lines = [
        'endpoint_op = "place_stock"' if line == 'endpoint_op = "mode"' else line for line in MINIMAL_CONFIG_LINES
    ]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"interlock\.endpoint_op"):
        load_config(config_path)


def test_interlock_pointer_must_start_with_slash(write_config: WriteConfig) -> None:
    lines = [
        'pointer = "simulation"' if line == 'pointer = "/simulation"' else line for line in MINIMAL_CONFIG_LINES
    ]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"interlock\.pointer"):
        load_config(config_path)


def test_interlock_pointer_rejects_empty_string(write_config: WriteConfig) -> None:
    # The empty pointer resolves to the whole response body, so any
    # endpoint whose body happened to be the bare JSON value true would
    # pass, regardless of what that body actually means.
    lines = ['pointer = ""' if line == 'pointer = "/simulation"' else line for line in MINIMAL_CONFIG_LINES]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"interlock\.pointer"):
        load_config(config_path)


def test_history_query_rejects_unknown_op(write_config: WriteConfig) -> None:
    lines = ['op = "unknown"' if line == 'op = "ticks"' else line for line in MINIMAL_CONFIG_LINES]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"history\.query\[0\]\.op"):
        load_config(config_path)


def test_history_query_rejects_non_json_body(write_config: WriteConfig) -> None:
    lines = ["body = 'not json'" if line.startswith("body = ") else line for line in MINIMAL_CONFIG_LINES]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"history\.query\[0\]\.body"):
        load_config(config_path)


def test_history_query_rejects_non_object_body(write_config: WriteConfig) -> None:
    lines = ["body = '[1, 2, 3]'" if line.startswith("body = ") else line for line in MINIMAL_CONFIG_LINES]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"history\.query\[0\]\.body"):
        load_config(config_path)


def test_history_usage_bytes_pointer_required(write_config: WriteConfig) -> None:
    lines = [line for line in MINIMAL_CONFIG_LINES if line != 'usage_bytes_pointer = "/bytes"']
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"history\.usage_bytes_pointer"):
        load_config(config_path)


def test_history_query_must_not_be_empty(write_config: WriteConfig) -> None:
    # Keep [history] and usage_bytes_pointer, drop the one [[history.query]]
    # table (header, op and body), so the array itself is what's missing.
    start = MINIMAL_CONFIG_LINES.index("[[history.query]]")
    lines = MINIMAL_CONFIG_LINES[:start] + MINIMAL_CONFIG_LINES[start + 3 :]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"history\.query"):
        load_config(config_path)


def test_history_query_op_must_name_known_endpoint(write_config: WriteConfig) -> None:
    # "kbars" is a valid op, but MINIMAL_CONFIG_LINES defines no
    # [endpoints.kbars], so the query cannot be sent.
    lines = ['op = "kbars"' if line == 'op = "ticks"' else line for line in MINIMAL_CONFIG_LINES]
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"history\.query\[0\]\.op"):
        load_config(config_path)


def test_history_query_op_must_be_post(write_config: WriteConfig) -> None:
    lines = list(MINIMAL_CONFIG_LINES)
    lines[lines.index("[endpoints.ticks]") + 1] = 'method = "GET"'
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"history\.query\[0\]\.op"):
        load_config(config_path)


def test_history_usage_endpoint_must_be_get(write_config: WriteConfig) -> None:
    lines = list(MINIMAL_CONFIG_LINES)
    lines[lines.index("[endpoints.usage]") + 1] = 'method = "POST"'
    config_path = write_config(lines)

    with pytest.raises(ConfigError, match=r"endpoints\.usage"):
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
        "api_key": ("fake-api-key-value",),
        "sec_key": ("fake-sec-key-value",),
        "ca_passwd": ("fake-ca-passwd",),
        "person_id": ("A123456789",),
        "egress_ip": ("203.0.113.1",),
    }


def _set_fake_secrets(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """Set every secret variable to a made-up value, with values overriding some by variable name."""
    for var, value in {**FAKE_SECRET_ENV, **values}.items():
        monkeypatch.setenv(var, value)


def test_read_secrets_splits_egress_list(write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(write_config())
    _set_fake_secrets(monkeypatch, P1_EGRESS_IP="203.0.113.1, 203.0.113.2,203.0.113.1")

    assert read_secrets(cfg)["egress_ip"] == ("203.0.113.1", "203.0.113.2")
    assert read_secret_role(cfg, "egress_ip") == ("203.0.113.1", "203.0.113.2")


def test_read_secrets_rejects_non_ipv4_egress_item(write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(write_config())
    _set_fake_secrets(monkeypatch, P1_EGRESS_IP="203.0.113.1,egress-host-q7")

    with pytest.raises(ConfigError, match="P1_EGRESS_IP") as exc_info:
        read_secrets(cfg)
    assert "egress-host-q7" not in str(exc_info.value)


def test_read_secrets_keeps_comma_in_other_roles(write_config: WriteConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(write_config())
    _set_fake_secrets(monkeypatch, SJ_CA_PASSWD="a,b")

    assert read_secrets(cfg)["ca_passwd"] == ("a,b",)


def _with_margins(*entries: str) -> list[str]:
    """Return MINIMAL_CONFIG_LINES with entries placed in [funds.futures_margin_twd]."""
    lines = list(MINIMAL_CONFIG_LINES)
    at = lines.index("[funds.futures_margin_twd]") + 1
    lines[at:at] = list(entries)
    return lines


def test_futures_margins_accept_integer_and_decimal_string(write_config: WriteConfig) -> None:
    cfg = load_config(write_config(_with_margins("TMFJ6 = 12000", 'TXFJ6 = "184000.5"')))

    assert cfg.funds.futures_margin_twd == {"TMFJ6": Decimal("12000"), "TXFJ6": Decimal("184000.5")}


@pytest.mark.parametrize("entry", ["TMFJ6 = 12000.5", "TMFJ6 = 0", 'TMFJ6 = "abc"', "TMFJ6 = true"])
def test_futures_margin_rejects_float_and_non_positive(write_config: WriteConfig, entry: str) -> None:
    with pytest.raises(ConfigError, match=r"funds\.futures_margin_twd\.TMFJ6"):
        load_config(write_config(_with_margins(entry)))


def test_funds_pointer_validated(write_config: WriteConfig) -> None:
    lines = [
        'stock_available_pointer = "acc_balance"' if line.startswith("stock_available_pointer") else line
        for line in MINIMAL_CONFIG_LINES
    ]

    with pytest.raises(ConfigError, match=r"funds\.stock_available_pointer"):
        load_config(write_config(lines))


def test_contracts_etf_codes_must_be_strings(write_config: WriteConfig) -> None:
    lines = ["etf_codes = [50]" if line.startswith("etf_codes") else line for line in MINIMAL_CONFIG_LINES]

    with pytest.raises(ConfigError, match=r"contracts\.etf_codes"):
        load_config(write_config(lines))


def test_missing_contracts_section_names_key(write_config: WriteConfig) -> None:
    start = MINIMAL_CONFIG_LINES.index("[contracts]")
    end = MINIMAL_CONFIG_LINES.index("[funds]")
    lines = MINIMAL_CONFIG_LINES[:start] + MINIMAL_CONFIG_LINES[end:]

    with pytest.raises(ConfigError, match=r"contracts\."):
        load_config(write_config(lines))


def _with_funds_line(key: str, line: str) -> list[str]:
    return [line if entry.startswith(f"{key} =") else entry for entry in MINIMAL_CONFIG_LINES]


@pytest.mark.parametrize("op", ["mode", "no_such_op"])
def test_stock_available_op_must_be_post_endpoint(write_config: WriteConfig, op: str) -> None:
    lines = _with_funds_line("stock_available_op", f'stock_available_op = "{op}"')

    with pytest.raises(ConfigError, match=r"funds\.stock_available_op must name a POST endpoint"):
        load_config(write_config(lines))


def test_stock_report_only_entries_validated(write_config: WriteConfig) -> None:
    at = MINIMAL_CONFIG_LINES.index("[funds.stock_report_only]") + 1

    unknown = [*MINIMAL_CONFIG_LINES[:at], 'balance = "/acc_balance"', *MINIMAL_CONFIG_LINES[at:]]
    with pytest.raises(ConfigError, match=r"funds\.stock_report_only\.balance must name a POST endpoint"):
        load_config(write_config(unknown))

    repeated = [*MINIMAL_CONFIG_LINES[:at], 'trading_limits = "/trading_available"', *MINIMAL_CONFIG_LINES[at:]]
    with pytest.raises(ConfigError, match=r"repeats funds\.stock_available_op"):
        load_config(write_config(repeated))


def _with_trades_line(key: str, line: str | None) -> list[str]:
    """Return MINIMAL_CONFIG_LINES with the [trades] line setting key replaced by line, or dropped if it is None."""
    lines = list(MINIMAL_CONFIG_LINES)
    start = lines.index("[trades]") + 1
    index = next(i for i in range(start, len(lines)) if lines[i].startswith(f"{key} = "))
    if line is None:
        del lines[index]
    else:
        lines[index] = line
    return lines


@pytest.mark.parametrize(
    ("key", "line", "message"),
    [
        ("status", 'status = "status"', r"trades\.status is not a valid JSON pointer"),
        ("id", 'id = ""', r"trades\.id must not be the empty pointer"),
        ("id", 'id = "/order/account/account_id"', r"trades\.id must not point at an account identifier"),
        ("deal_quantity", 'deal_quantity = "/x/person_id"', r"trades\.deal_quantity must not point at an account"),
        ("order_ts", None, r"missing config key: trades\.order_ts"),
        ("list", 'list = ""\nlist_pointer = ""', r"unknown config key: trades\.list_pointer"),
    ],
)
def test_trades_pointers_validated(write_config: WriteConfig, key: str, line: str | None, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(_with_trades_line(key, line)))


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (None, r"missing config key: trades\.account_types"),
        ("account_types = []", r"trades\.account_types must be a non-empty array of non-empty strings"),
        ('account_types = "F"', r"trades\.account_types must be a non-empty array of non-empty strings"),
        ('account_types = ["S", 1]', r"trades\.account_types must be a non-empty array of non-empty strings"),
        ('account_types = ["S", ""]', r"trades\.account_types must be a non-empty array of non-empty strings"),
        ('account_types = ["S", "F", "S"]', r"trades\.account_types repeats S$"),
    ],
)
def test_trades_account_types_validated(write_config: WriteConfig, line: str | None, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(_with_trades_line("account_types", line)))


@pytest.mark.parametrize(
    ("table", "line"),
    [
        ("orders", 'trade_id_pointer = "/order/id"'),
        ("orders", 'filled_pointer = "/status/status"'),
        ("orders", 'deal_quantity_pointer = "/status/deal_quantity"'),
        ("events", 'rest_list = ""'),
        ("events", 'rest_key = "/order/id"'),
        ("events", 'rest_status = "/status/status"'),
        ("events", 'rest_filled_qty = "/status/deal_quantity"'),
        ("events", 'rest_order_ts = "/status/order_ts"'),
    ],
)
def test_old_trades_keys_rejected(write_config: WriteConfig, table: str, line: str) -> None:
    lines = list(MINIMAL_CONFIG_LINES)
    lines.insert(lines.index(f"[{table}]") + 1, line)
    key = line.partition(" = ")[0]

    with pytest.raises(ConfigError, match=rf"unknown config key: {table}\.{key}$"):
        load_config(write_config(lines))
