"""Tick sizes, price references and the funds a set of scenarios needs.

Every price is a Decimal end to end, because a binary float rounding
error can move a price off the tick grid and turn an order the scenario
meant to rest into one the exchange rejects.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
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

# The simulated matcher accepted a buy at this fraction of reference where
# it rejected one at the contract file's limit_down, for futures and stock
# alike (docs/official-server-contract.md section 2).
_FAR_BELOW_RATIO = Decimal("0.95")

# TWSE maximum brokerage commission rate, added to every stock requirement
# since a buy reserves its commission along with its price.
STOCK_BUY_FEE_RATE = Decimal("0.001425")


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


def format_decimal(value: Decimal) -> str:
    """Render value in plain positional notation, never exponent notation such as 1E+3."""
    return format(value, "f")


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
                far_below = floor(reference * 0.95 / tick) * tick;
                far_below_plus_tick = far_below + tick(far_below).
    Domain:     limits from the live contract response that passed
                check_limits; is_etf selects the ETF tick table for stock and
                odd lots. See _far_below for why it is not limit_down.
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


def check_limits(limits: ContractLimits, market: Market, *, is_etf: bool) -> None:
    """Raise ValueError unless a contract's price band is well formed.

    Definition: A sanity check on the limits a contract response reports.
    Formula:    0 < limit_down < reference < limit_up, and for each limit L,
                L mod tick(L) = 0.
    Domain:     limits from one live contract record; market and is_etf pick
                the tick table as in tick_size. A limit off its tick grid or
                out of order means the record is not what p1rec expects, so
                every price derived from it is untrustworthy.
    Returns:    None when the band is well formed; otherwise raises ValueError
                naming the first violation.
    """
    if not 0 < limits.limit_down < limits.reference < limits.limit_up:
        raise ValueError(
            f"limits out of order: limit_down {format_decimal(limits.limit_down)}, "
            f"reference {format_decimal(limits.reference)}, limit_up {format_decimal(limits.limit_up)}"
        )
    for name, value in (("limit_down", limits.limit_down), ("limit_up", limits.limit_up)):
        tick = tick_size(market, value, is_etf=is_etf)
        if value % tick != 0:
            raise ValueError(f"{name} {format_decimal(value)} is not a multiple of its tick {format_decimal(tick)}")


def _far_below(limits: ContractLimits, market: Market, *, is_etf: bool) -> Decimal:
    """Return a buy price far enough below the market to rest without filling.

    Definition: The lowest price a resting buy can use that the simulated
                matcher is known to accept.
    Formula:    floor(reference * 0.95 / tick) * tick, the tick taken at
                reference * 0.95 from the stock table, ETF or not.
    Domain:     limits passed check_limits. The simulated matcher rejected
                a buy at the contract file's limit_down as outside the price
                band, a futures buy in the night session and a stock buy in
                the day session alike, and accepted one at this fraction of
                reference (docs/official-server-contract.md section 2).
    Returns:    Price in TWD (stock, odd lot) or index points (futures), on
                the tick grid.
    """
    target = limits.reference * _FAR_BELOW_RATIO
    # The stock table's tick is a multiple of the ETF table's at every price,
    # and the simulated matcher rejected an ETF odd-lot price off its grid.
    tick = tick_size(market, target, is_etf=False)
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
                The stock figure is then raised by the brokerage commission:
                stock_twd = ceil(stock_twd * (1 + STOCK_BUY_FEE_RATE)).
    Domain:     A case is counted unless its quantity is exceed_funds (sized to
                be refused, never to be funded) or it is a stock or odd-lot Sell
                that does not expect a fill, which reserves shares rather than
                cash; a futures order reserves margin on either side. A resting
                buy reserves its limit price and a market order up to limit_up;
                an update_qty step only lowers the reservation. limits holds
                every counted stock and odd-lot code, margins_twd the
                per-contract initial margin in TWD of every counted futures
                code, and etf_codes selects the ETF tick table for prices one
                tick off a limit. Securities transaction tax falls on sells
                only and is not included. Raises ConfigError naming the code if
                a margin is missing, and LookupError if a stock or odd-lot code
                has no limits.
    Returns:    FundsRequirement in TWD, the stock figure a whole TWD, to
                compare against the stock account's available amount and the
                futures account's available margin respectively.
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
    stock_twd = (stock_twd * (1 + STOCK_BUY_FEE_RATE)).to_integral_value(rounding=ROUND_CEILING)
    return FundsRequirement(stock_twd=stock_twd, futures_margin_twd=futures_margin_twd)


def _reserved_price(case: Case, limits: ContractLimits, *, is_etf: bool) -> Decimal:
    """Return the highest per-share price a stock or odd-lot case's order can hold.

    Definition: The price a case's order reserves funds at, at its most
                expensive point while it rests or fills.
    Formula:    limit_up if case.expect_fill or case.price_type is MKT or MKP;
                else max(resolve_price(r)) over r in {case.price_ref} and the
                price_ref of every update_price step.
    Domain:     case is a stock or odd-lot case; limits passed check_limits.
                A market order, or any case expected to fill, may pay
                anything up to limit_up, so both reserve at limit_up.
    Returns:    Price per share in TWD.
    """
    if case.expect_fill or case.price_type != "LMT":
        return limits.limit_up
    refs = [case.price_ref]
    refs += [step.price_ref for step in map(parse_step, case.steps) if step.price_ref is not None]
    return max(resolve_price(ref, limits, case.market, is_etf=is_etf) for ref in refs)
