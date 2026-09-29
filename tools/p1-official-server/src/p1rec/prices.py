"""Tick sizes, price references and the funds a set of scenarios needs.

Every price is a Decimal end to end, because a binary float rounding
error can move a price off the tick grid and turn an order the scenario
meant to rest into one the exchange rejects.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import assert_never

from p1rec.config import ConfigError
from p1rec.scenarios import Case, Market, PriceRef, parse_step

# TWSE board lot
SHARES_PER_BOARD_LOT = 1000

# An odd-lot order's quantity is already a number of shares
_SHARES_PER_UNIT: Mapping[Market, int] = {Market.STOCK: SHARES_PER_BOARD_LOT, Market.ODD: 1}

# TWSE tick size rules, as (exclusive upper price bound, tick) in rising
# order, followed by the tick for every price at or above the last bound.
_STOCK_TICK_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("10"), Decimal("0.01")),
    (Decimal("50"), Decimal("0.05")),
    (Decimal("100"), Decimal("0.1")),
    (Decimal("500"), Decimal("0.5")),
    (Decimal("1000"), Decimal("1")),
)
_STOCK_TOP_TICK = Decimal("5")
_ETF_TICK_BANDS: tuple[tuple[Decimal, Decimal], ...] = ((Decimal("50"), Decimal("0.01")),)
_ETF_TOP_TICK = Decimal("0.05")

# TAIFEX contract specifications for TXF, MXF and TMF
_FUTURES_TICK = Decimal("1")

# The simulated matcher rejects a futures order at the contract's
# limit_down but accepts one at this fraction of reference
# (docs/official-server-contract.md section 2).
_FUTURES_FAR_BELOW_RATIO = Decimal("0.95")


@dataclass(frozen=True)
class ContractLimits:
    code: str
    limit_up: Decimal
    limit_down: Decimal
    reference: Decimal


@dataclass(frozen=True)
class FundsRequirement:
    stock_twd: Decimal
    futures_margin_twd: Decimal


def tick_size(market: Market, price: Decimal, *, is_etf: bool) -> Decimal:
    """Return the tick that applies to an order at price.

    Definition: The minimum price increment the exchange accepts at a given price.
    Formula:    stock and odd lot: the band b with lower <= price < upper in the
                TWSE table (ETF table if is_etf); futures: 1 point at any price.
    Domain:     price > 0, in TWD for stock and odd lots, index points for
                futures; odd lots follow the same table as board lots; is_etf
                is ignored for futures. Raises ValueError for price <= 0.
    Returns:    Tick in the same unit as price; a valid order price is a whole
                multiple of it.
    """
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")
    if market is Market.FUTURES:
        return _FUTURES_TICK
    bands, top_tick = (_ETF_TICK_BANDS, _ETF_TOP_TICK) if is_etf else (_STOCK_TICK_BANDS, _STOCK_TOP_TICK)
    for upper, tick in bands:
        if price < upper:
            return tick
    return top_tick


def resolve_price(ref: PriceRef, limits: ContractLimits, market: Market, *, is_etf: bool) -> Decimal:
    """Return the order price a price reference names for one contract.

    Definition: Map a symbolic price reference to a concrete price from the
                contract's limits.
    Formula:    limit_down, limit_up, reference: the contract's own value;
                limit_down_plus_tick = limit_down + tick(limit_down);
                far_below = limit_down for stock and odd lots,
                floor(reference * 0.95 / tick) * tick for futures;
                far_below_plus_tick = far_below + tick(far_below).
    Domain:     limits from the live contract response, all positive and on
                the tick grid; is_etf selects the ETF tick table for stock and
                odd lots. Futures use a fraction of reference instead of
                limit_down because the simulated matcher rejects futures
                orders at limit_down.
    Returns:    Price in TWD (stock, odd lot) or index points (futures); a
                far_below price is meant to rest without filling.
    """
    match ref:
        case PriceRef.LIMIT_DOWN:
            return limits.limit_down
        case PriceRef.LIMIT_UP:
            return limits.limit_up
        case PriceRef.REFERENCE:
            return limits.reference
        case PriceRef.LIMIT_DOWN_PLUS_TICK:
            return limits.limit_down + tick_size(market, limits.limit_down, is_etf=is_etf)
        case PriceRef.FAR_BELOW:
            return _far_below(limits, market, is_etf=is_etf)
        case PriceRef.FAR_BELOW_PLUS_TICK:
            far_below = _far_below(limits, market, is_etf=is_etf)
            return far_below + tick_size(market, far_below, is_etf=is_etf)
    assert_never(ref)


def _far_below(limits: ContractLimits, market: Market, *, is_etf: bool) -> Decimal:
    if market is not Market.FUTURES:
        return limits.limit_down
    target = limits.reference * _FUTURES_FAR_BELOW_RATIO
    tick = tick_size(market, target, is_etf=is_etf)
    return (target / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def required_funds(
    cases: Sequence[Case],
    limits: Mapping[str, ContractLimits],
    margins_twd: Mapping[str, Decimal],
    *,
    etf_codes: Collection[str],
) -> FundsRequirement:
    """Return the most funds any single case can tie up while it runs.

    Definition: The largest cash (stock) and initial margin (futures) any one
                case reserves while its order rests or holds once it fills,
                since cases run one at a time and each ends cancelled or
                closed before the next starts.
    Formula:    stock_twd = max over counted stock and odd-lot cases of
                quantity * shares_per_unit * price, where shares_per_unit is
                1000 for stock and 1 for odd lots, and price is limit_up for an
                expect_fill or market (MKT, MKP) case, else the highest
                resolve_price among its price_ref and its update_price steps;
                futures_margin_twd = max over counted futures cases of
                quantity * margins_twd[code]; 0 when a market has no counted case.
    Domain:     A case is counted unless its quantity is exceed_funds (sized to
                be refused, never to be funded) or it is a stock or odd-lot Sell
                that does not expect a fill, which reserves shares rather than
                cash; a futures order reserves margin on either side. A resting
                buy reserves its limit price and a market order up to limit_up;
                an update_qty step only lowers the reservation. limits holds
                every counted stock and odd-lot code, margins_twd the
                per-contract initial margin in TWD of every counted futures
                code, and etf_codes selects the ETF tick table for prices one
                tick off a limit. Fees and taxes are not included. Raises
                ConfigError naming the code if a margin is missing, and
                LookupError if a stock or odd-lot code has no limits.
    Returns:    FundsRequirement in TWD, to compare against the stock
                account's available balance and the futures account's
                available margin respectively.
    """
    stock_twd = Decimal(0)
    futures_margin_twd = Decimal(0)
    for case in cases:
        # An exceed_funds case is sized to be refused, never to be funded
        if not isinstance(case.quantity, int):
            continue
        if case.market is Market.FUTURES:
            margin = margins_twd.get(case.code)
            if margin is None:
                raise ConfigError(
                    f"funds.futures_margin_twd has no per-contract margin for {case.code!r}, needed by case {case.id!r}"
                )
            futures_margin_twd = max(futures_margin_twd, case.quantity * margin)
        elif case.action == "Buy" or case.expect_fill:
            contract = limits.get(case.code)
            if contract is None:
                raise LookupError(f"no contract limits for {case.code!r}, needed by case {case.id!r}")
            price = _reserved_price(case, contract, is_etf=case.code in etf_codes)
            stock_twd = max(stock_twd, case.quantity * _SHARES_PER_UNIT[case.market] * price)
    return FundsRequirement(stock_twd=stock_twd, futures_margin_twd=futures_margin_twd)


def _reserved_price(case: Case, limits: ContractLimits, *, is_etf: bool) -> Decimal:
    """Return the highest per-share price case's order can hold while it rests or fills."""
    if case.expect_fill or case.price_type != "LMT":
        return limits.limit_up
    refs = [case.price_ref]
    refs += [step.price_ref for step in map(parse_step, case.steps) if step.price_ref is not None]
    return max(resolve_price(ref, limits, case.market, is_etf=is_etf) for ref in refs)
