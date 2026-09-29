"""Tests for p1rec.prices: tick sizes, price references and funds requirements."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

import pytest

from p1rec.config import ConfigError
from p1rec.prices import ContractLimits, FundsRequirement, required_funds, resolve_price, tick_size
from p1rec.scenarios import Case, Market, PriceRef

_TMF = ContractLimits(code="TMFJ6", limit_up=Decimal("52559"), limit_down=Decimal("43003"), reference=Decimal("47781"))
_STOCK = ContractLimits(code="9999", limit_up=Decimal("12.15"), limit_down=Decimal("9.95"), reference=Decimal("11.05"))


def _fill(case_id: str, market: Market, code: str, quantity: int) -> Case:
    return Case(
        id=case_id,
        market=market,
        code=code,
        action="Buy",
        price_ref=PriceRef.LIMIT_UP,
        price_type="MKT",
        order_type="IOC",
        quantity=quantity,
        custom_field=None,
        expect_fill=True,
        steps=("place",),
    )


def _resting(
    case_id: str,
    market: Market,
    code: str,
    quantity: int | Literal["exceed_funds"],
    *,
    action: Literal["Buy", "Sell"] = "Buy",
    price_ref: PriceRef = PriceRef.FAR_BELOW,
    price_type: Literal["LMT", "MKT", "MKP"] = "LMT",
    steps: tuple[str, ...] = ("place", "cancel"),
) -> Case:
    return Case(
        id=case_id,
        market=market,
        code=code,
        action=action,
        price_ref=price_ref,
        price_type=price_type,
        order_type="ROD",
        quantity=quantity,
        custom_field=None,
        expect_fill=False,
        steps=steps,
    )


_LIMITS = {
    "1111": ContractLimits(code="1111", limit_up=Decimal("25.3"), limit_down=Decimal("20.7"), reference=Decimal("23")),
    "2222": ContractLimits(code="2222", limit_up=Decimal("11"), limit_down=Decimal("9"), reference=Decimal("10")),
    "0050": ContractLimits(code="0050", limit_up=Decimal("60.5"), limit_down=Decimal("49.5"), reference=Decimal("55")),
}
_NO_ETFS: frozenset[str] = frozenset()


@pytest.mark.parametrize(
    ("price", "tick"),
    [
        ("9.99", "0.01"),
        ("10", "0.05"),
        ("49.95", "0.05"),
        ("50", "0.1"),
        ("100", "0.5"),
        ("500", "1"),
        ("999", "1"),
        ("1000", "5"),
    ],
)
@pytest.mark.parametrize("market", [Market.STOCK, Market.ODD])
def test_stock_tick_bands(market: Market, price: str, tick: str) -> None:
    assert tick_size(market, Decimal(price), is_etf=False) == Decimal(tick)


@pytest.mark.parametrize(("price", "tick"), [("49.99", "0.01"), ("50", "0.05"), ("150", "0.05")])
@pytest.mark.parametrize("market", [Market.STOCK, Market.ODD])
def test_etf_tick_bands(market: Market, price: str, tick: str) -> None:
    assert tick_size(market, Decimal(price), is_etf=True) == Decimal(tick)


@pytest.mark.parametrize("price", ["1", "9.99", "47781", "123456"])
def test_futures_tick_is_one_point(price: str) -> None:
    assert tick_size(Market.FUTURES, Decimal(price), is_etf=False) == Decimal("1")


def test_tick_size_rejects_non_positive_price() -> None:
    with pytest.raises(ValueError, match=r"positive"):
        tick_size(Market.STOCK, Decimal("0"), is_etf=False)


def test_limit_down_plus_tick() -> None:
    price = resolve_price(PriceRef.LIMIT_DOWN_PLUS_TICK, _STOCK, Market.STOCK, is_etf=False)

    assert price == Decimal("9.96")


def test_far_below_futures() -> None:
    # 47781 * 0.95 = 45391.95, floored to a whole point
    assert resolve_price(PriceRef.FAR_BELOW, _TMF, Market.FUTURES, is_etf=False) == Decimal("45391")
    assert resolve_price(PriceRef.FAR_BELOW, _STOCK, Market.STOCK, is_etf=False) == Decimal("9.95")


def test_far_below_futures_from_float_formatted_reference() -> None:
    # The server writes whole numbers as JSON floats such as 47781.0
    limits = ContractLimits(
        code="TMFJ6", limit_up=Decimal("52559.0"), limit_down=Decimal("43003.0"), reference=Decimal("47781.0")
    )

    price = resolve_price(PriceRef.FAR_BELOW, limits, Market.FUTURES, is_etf=False)

    assert price == Decimal("45391")
    assert format(price, "f") == "45391"


def test_far_below_plus_tick() -> None:
    assert resolve_price(PriceRef.FAR_BELOW_PLUS_TICK, _TMF, Market.FUTURES, is_etf=False) == Decimal("45392")
    assert resolve_price(PriceRef.FAR_BELOW_PLUS_TICK, _STOCK, Market.ODD, is_etf=False) == Decimal("9.96")


def test_direct_references_return_contract_values() -> None:
    assert resolve_price(PriceRef.LIMIT_UP, _STOCK, Market.STOCK, is_etf=False) == _STOCK.limit_up
    assert resolve_price(PriceRef.LIMIT_DOWN, _STOCK, Market.STOCK, is_etf=False) == _STOCK.limit_down
    assert resolve_price(PriceRef.REFERENCE, _STOCK, Market.STOCK, is_etf=False) == _STOCK.reference


def test_limit_down_plus_tick_uses_etf_table() -> None:
    etf = ContractLimits(code="0050", limit_up=Decimal("60.5"), limit_down=Decimal("49.5"), reference=Decimal("55"))

    assert resolve_price(PriceRef.LIMIT_DOWN_PLUS_TICK, etf, Market.STOCK, is_etf=True) == Decimal("49.51")
    assert resolve_price(PriceRef.LIMIT_DOWN_PLUS_TICK, etf, Market.STOCK, is_etf=False) == Decimal("49.55")


def test_required_funds_single_position() -> None:
    cases = [_fill("a", Market.STOCK, "1111", 1), _fill("b", Market.STOCK, "2222", 2)]

    requirement = required_funds(cases, _LIMITS, {}, etf_codes=_NO_ETFS)

    # 1 * 1000 * 25.3 = 25300 beats 2 * 1000 * 11 = 22000; the two are never held together
    assert requirement == FundsRequirement(stock_twd=Decimal("25300"), futures_margin_twd=Decimal("0"))


def test_required_funds_resting_order_dominates_fill() -> None:
    cases = [_fill("fill", Market.STOCK, "1111", 1), _resting("far", Market.STOCK, "1111", 2)]

    requirement = required_funds(cases, _LIMITS, {}, etf_codes=_NO_ETFS)

    # The resting 2 lots reserve 2 * 1000 * 20.7 = 41400 at placement, more than the 25300 fill
    assert requirement.stock_twd == Decimal("41400")


def test_required_funds_update_price_raises_reservation() -> None:
    case = _resting("lifecycle", Market.STOCK, "1111", 2, steps=("place", "update_price:far_below_plus_tick", "cancel"))

    requirement = required_funds([case], _LIMITS, {}, etf_codes=_NO_ETFS)

    # far_below 20.7 plus one 0.05 tick, held on 2 * 1000 shares
    assert requirement.stock_twd == Decimal("41500")


def test_required_funds_resting_market_order_reserves_limit_up() -> None:
    case = _resting("odd_mkt", Market.ODD, "2222", 10, price_type="MKT")

    assert required_funds([case], _LIMITS, {}, etf_codes=_NO_ETFS).stock_twd == Decimal("110")


def test_required_funds_uses_etf_ticks() -> None:
    case = _resting("etf", Market.ODD, "0050", 100, price_ref=PriceRef.LIMIT_DOWN_PLUS_TICK)

    assert required_funds([case], _LIMITS, {}, etf_codes={"0050"}).stock_twd == Decimal("4951")
    assert required_funds([case], _LIMITS, {}, etf_codes=_NO_ETFS).stock_twd == Decimal("4955")


def test_required_funds_odd_lot_counts_shares() -> None:
    requirement = required_funds([_fill("odd", Market.ODD, "0050", 3)], _LIMITS, {}, etf_codes=_NO_ETFS)

    assert requirement.stock_twd == Decimal("181.5")


def test_required_funds_ignores_exceed_funds_and_resting_stock_sell() -> None:
    cases = [
        _resting("exceed", Market.STOCK, "1111", "exceed_funds", price_ref=PriceRef.LIMIT_DOWN),
        _resting("sell", Market.STOCK, "1111", 5, action="Sell"),
    ]

    assert required_funds(cases, _LIMITS, {}, etf_codes=_NO_ETFS) == FundsRequirement(
        stock_twd=Decimal("0"), futures_margin_twd=Decimal("0")
    )


def test_required_funds_futures_uses_largest_margin() -> None:
    cases = [
        _fill("f1", Market.FUTURES, "TMFJ6", 1),
        _resting("buy", Market.FUTURES, "TMFJ6", 2),
        _resting("sell", Market.FUTURES, "TMFJ6", 3, action="Sell"),
    ]

    requirement = required_funds(cases, {}, {"TMFJ6": Decimal("12000")}, etf_codes=_NO_ETFS)

    # A resting futures order reserves margin whichever side it is on
    assert requirement == FundsRequirement(stock_twd=Decimal("0"), futures_margin_twd=Decimal("36000"))


def test_required_funds_missing_margin_names_code() -> None:
    with pytest.raises(ConfigError, match=r"'TMFJ6'"):
        required_funds([_resting("far", Market.FUTURES, "TMFJ6", 1)], {}, {}, etf_codes=_NO_ETFS)
