"""Tests for p1rec.broker: queries shared by preflight and the order runner."""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

import pytest
from helpers import WriteConfig
from mock_official import STOCK_ACCOUNT, FakeServer, futures_record, preflight_config, stock_record

from p1rec.broker import (
    BrokerError,
    account_for_market,
    fetch_accounts,
    fetch_available_twd,
    fetch_funds_value,
    fetch_limits,
)
from p1rec.capture import CapturingClient
from p1rec.config import P1Config
from p1rec.prices import ContractLimits
from p1rec.redact import setup_logging
from p1rec.scenarios import Case, Market, PriceRef


def _client(tmp_path: Path, cfg: P1Config, server: FakeServer) -> CapturingClient:
    return CapturingClient(cfg.base_url, "test", tmp_path / "rest.jsonl", 5.0, transport=server.transport())


def _case(market: Market, code: str) -> Case:
    return Case(
        id=f"{market.value}_{code}",
        market=market,
        code=code,
        action="Buy",
        price_ref=PriceRef.FAR_BELOW,
        price_type="LMT",
        order_type="ROD",
        quantity=1,
        custom_field=None,
        expect_fill=False,
        steps=("place", "cancel"),
    )


def test_fetch_accounts_redacts_identifiers(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = preflight_config(write_config)
    setup_logging(tmp_path, secrets=[], level="INFO")

    with _client(tmp_path, cfg, FakeServer()) as client:
        fetch_accounts(client, cfg)
    logging.getLogger("p1rec.test").error("account %s", STOCK_ACCOUNT["account_id"])
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "account ***" in (tmp_path / "recorder.log").read_text(encoding="utf-8")


def test_fetch_accounts_redacts_even_when_shape_is_rejected(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = preflight_config(write_config)
    server = FakeServer(accounts=[dict(STOCK_ACCOUNT), {"account_id": "0000009", "username": "other-user"}])
    setup_logging(tmp_path, secrets=[], level="INFO")

    with _client(tmp_path, cfg, server) as client, pytest.raises(BrokerError):
        fetch_accounts(client, cfg)
    logging.getLogger("p1rec.test").error("saw %s %s", "0000009", "other-user")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "saw *** ***" in (tmp_path / "recorder.log").read_text(encoding="utf-8")


def test_account_for_market_maps_odd_lot_to_stock_account(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = preflight_config(write_config)
    with _client(tmp_path, cfg, FakeServer()) as client:
        accounts = fetch_accounts(client, cfg)

    assert account_for_market(accounts, Market.ODD)["account_type"] == "S"
    with pytest.raises(BrokerError, match=r"'F'"):
        account_for_market([a for a in accounts if a["account_type"] != "F"], Market.FUTURES)


def test_stock_available_uses_configured_source(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = preflight_config(write_config)
    server = FakeServer(trading_limits={"trading_available": 123456})

    with _client(tmp_path, cfg, server) as client:
        available = fetch_available_twd(client, cfg, Market.STOCK, STOCK_ACCOUNT)

    assert available == Decimal("123456")
    assert server.paths() == ["/api/v1/portfolio/trading_limits"]


def test_balance_errmsg_raises(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = preflight_config(write_config)
    server = FakeServer(balance={"acc_balance": 0.0, "date": "2026-09-29", "errmsg": "account 0000001 not found"})

    with _client(tmp_path, cfg, server) as client, pytest.raises(BrokerError, match=r"reports an error") as exc:
        fetch_funds_value(client, cfg, "balance", "/acc_balance", STOCK_ACCOUNT)

    # The message may quote the account, so it is never repeated
    assert "0000001" not in str(exc.value)


def test_balance_empty_errmsg_accepted(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = preflight_config(write_config)

    with _client(tmp_path, cfg, FakeServer()) as client:
        assert fetch_funds_value(client, cfg, "balance", "/acc_balance", STOCK_ACCOUNT) == Decimal("1000000.0")


def test_futures_limits_come_from_matching_code_in_root(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = preflight_config(write_config)
    server = FakeServer()

    with _client(tmp_path, cfg, server) as client:
        limits = fetch_limits(client, cfg, [_case(Market.FUTURES, "TMFJ6")])

    assert [dict(r.url.params) for r in server.requests] == [{"root": "TMF"}]
    assert limits == {
        "TMFJ6": ContractLimits(
            code="TMFJ6", limit_up=Decimal("52559.0"), limit_down=Decimal("43003.0"), reference=Decimal("47781.0")
        )
    }


def test_continuous_month_alias_resolves_its_root(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = preflight_config(write_config)
    server = FakeServer()

    with _client(tmp_path, cfg, server) as client:
        limits = fetch_limits(client, cfg, [_case(Market.FUTURES, "TMFR1"), _case(Market.FUTURES, "TMFJ6")])

    # Both codes share root TMF, which is queried once
    assert [dict(r.url.params) for r in server.requests] == [{"root": "TMF"}]
    assert set(limits) == {"TMFR1", "TMFJ6"}


@pytest.mark.parametrize(
    "records",
    [
        [futures_record("TMFK6", 53000.0, 43400.0, 48200.0)],
        [futures_record("TMFJ6", 52559.0, 43003.0, 47781.0), futures_record("TMFJ6", 52559.0, 43003.0, 47781.0)],
    ],
    ids=["absent", "ambiguous"],
)
def test_futures_code_must_appear_exactly_once(
    tmp_path: Path, write_config: WriteConfig, records: list[dict[str, object]]
) -> None:
    cfg = preflight_config(write_config)
    server = FakeServer(futures_contracts={"TMF": records})

    with _client(tmp_path, cfg, server) as client, pytest.raises(BrokerError, match=r"'TMFJ6'"):
        fetch_limits(client, cfg, [_case(Market.FUTURES, "TMFJ6")])


@pytest.mark.parametrize(
    "record",
    [
        stock_record("2890", 0.0, 20.7, 23.0),  # Non-positive limit_up
        stock_record("2890", 22.0, 20.7, 23.0),  # limit_up below reference
        stock_record("2890", 25.33, 20.7, 23.0),  # limit_up off its 0.05 tick
        stock_record("2890", 25.3, 20.72, 23.0),  # limit_down off its 0.05 tick
    ],
)
def test_malformed_stock_limits_fail_closed(
    tmp_path: Path, write_config: WriteConfig, record: dict[str, object]
) -> None:
    cfg = preflight_config(write_config)
    server = FakeServer(stock_contracts={"2890": record})

    with _client(tmp_path, cfg, server) as client, pytest.raises(BrokerError, match=r"'2890'"):
        fetch_limits(client, cfg, [_case(Market.STOCK, "2890")])


def test_etf_limits_checked_against_etf_ticks(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = preflight_config(write_config)

    # 49.51 sits on the ETF grid but not on the ordinary stock grid
    server = FakeServer(stock_contracts={"0050": stock_record("0050", 60.5, 49.51, 55.0)})

    with _client(tmp_path, cfg, server) as client:
        limits = fetch_limits(client, cfg, [_case(Market.ODD, "0050")])

    assert limits["0050"].limit_down == Decimal("49.51")


def test_futures_tick_mismatch_fails_closed(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = preflight_config(write_config)
    server = FakeServer(futures_contracts={"TMF": [futures_record("TMFJ6", 52559.0, 43003.0, 47781.0, tick=0.5)]})

    with _client(tmp_path, cfg, server) as client, pytest.raises(BrokerError, match=r"tick"):
        fetch_limits(client, cfg, [_case(Market.FUTURES, "TMFJ6")])


def test_futures_record_without_tick_accepted(tmp_path: Path, write_config: WriteConfig) -> None:
    cfg = preflight_config(write_config)
    server = FakeServer(futures_contracts={"TMF": [futures_record("TMFJ6", 52559.0, 43003.0, 47781.0, tick=None)]})

    with _client(tmp_path, cfg, server) as client:
        assert set(fetch_limits(client, cfg, [_case(Market.FUTURES, "TMFJ6")])) == {"TMFJ6"}
