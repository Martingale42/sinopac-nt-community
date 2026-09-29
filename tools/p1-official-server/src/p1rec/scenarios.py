"""Load order scenarios from scenarios.toml into Case records.

Each [[case]] table describes one order: its contract, its price as a
reference to the contract's limits, and the steps to run against it. The
file is validated as a whole before anything is returned, so a typo in
any case stops the run before a single request reaches the server.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast, get_args

from p1rec.config import ConfigError


class Market(StrEnum):
    STOCK = "stock"
    ODD = "odd"
    FUTURES = "futures"


class PriceRef(StrEnum):
    LIMIT_DOWN = "limit_down"
    LIMIT_UP = "limit_up"
    REFERENCE = "reference"
    LIMIT_DOWN_PLUS_TICK = "limit_down_plus_tick"
    FAR_BELOW = "far_below"
    FAR_BELOW_PLUS_TICK = "far_below_plus_tick"


Action = Literal["Buy", "Sell"]
PriceType = Literal["LMT", "MKT", "MKP"]
OrderType = Literal["ROD", "IOC", "FOK"]
StepVerb = Literal["place", "update_price", "update_qty", "cancel"]

# Quantity marker for the case that deliberately asks for more than the
# account can pay, sized from the live balance when the order is placed.
EXCEED_FUNDS: Literal["exceed_funds"] = "exceed_funds"


@dataclass(frozen=True)
class Case:
    id: str
    market: Market
    code: str
    action: Action
    price_ref: PriceRef
    price_type: PriceType
    order_type: OrderType

    # Lots for stock, shares for odd lots, contracts for futures
    quantity: int | Literal["exceed_funds"]

    custom_field: str | None
    expect_fill: bool

    # "place" | "update_price:<PriceRef>" | "update_qty:<reduce by>" | "cancel"
    steps: tuple[str, ...]


@dataclass(frozen=True)
class Step:
    """One parsed entry of Case.steps."""

    verb: StepVerb
    price_ref: PriceRef | None = None  # Set only for update_price
    reduce_by: int | None = None  # Set only for update_qty


_CASE_KEYS = frozenset(
    {
        "id",
        "market",
        "code",
        "action",
        "price_ref",
        "price_type",
        "order_type",
        "quantity",
        "custom_field",
        "expect_fill",
        "steps",
    }
)


def parse_step(raw: str) -> Step:
    """Parse one step string, raising ValueError if its verb or argument is unknown."""
    verb, sep, arg = raw.partition(":")
    if verb in ("place", "cancel"):
        if sep:
            raise ValueError(f"step {raw!r}: {verb} takes no argument")
        return Step(verb=cast(StepVerb, verb))
    if verb == "update_price":
        try:
            return Step(verb="update_price", price_ref=PriceRef(arg))
        except ValueError:
            raise ValueError(f"step {raw!r}: unknown price reference {arg!r}") from None
    if verb == "update_qty":
        if not (arg.isascii() and arg.isdigit()) or int(arg) <= 0:
            raise ValueError(f"step {raw!r}: quantity reduction must be a positive integer")
        return Step(verb="update_qty", reduce_by=int(arg))
    raise ValueError(f"step {raw!r}: unknown verb {verb!r}")


def load_scenarios(path: Path) -> list[Case]:
    """Load and validate every [[case]] in a scenarios.toml file, in file order.

    Raises ConfigError naming the offending case and key for a missing or
    unknown key, a value of the wrong type or outside its allowed set, a
    duplicate case id, an unknown step, a steps list that does not begin
    with its only "place", a step after "cancel", update_qty reductions
    that add up to more than the case's quantity, or an expect_fill case
    whose quantity is exceed_funds (such an order is priced at limit_down
    precisely so it never fills). Any top-level key other than [[case]]
    is rejected too, so a misspelt table name cannot silently drop cases.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read scenarios file: {path} ({exc})") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    unknown = sorted(set(data) - {"case"})
    if unknown:
        raise ConfigError(f"{path}: unknown top-level keys: {', '.join(unknown)}")

    raw_cases = data.get("case")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ConfigError(f"{path}: expected at least one [[case]] table")

    cases: list[Case] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_cases):
        case = _parse_case(raw, f"case[{index}]")
        if case.id in seen_ids:
            raise ConfigError(f"{path}: case[{index}].id duplicates case id {case.id!r}")
        seen_ids.add(case.id)
        cases.append(case)
    return cases


def _parse_case(raw: object, where: str) -> Case:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where} must be a table")
    unknown = sorted(set(raw) - _CASE_KEYS)
    if unknown:
        raise ConfigError(f"{where} has unknown keys: {', '.join(unknown)}")

    case_id = _non_empty_str(raw, "id", where)
    where = f"{where} (id {case_id!r})"

    quantity = _quantity(raw, where)
    expect_fill = _required(raw, "expect_fill", where)
    if not isinstance(expect_fill, bool):
        raise ConfigError(f"{where}.expect_fill must be a boolean")
    if expect_fill and quantity == EXCEED_FUNDS:
        raise ConfigError(f"{where}: an expect_fill case cannot use quantity {EXCEED_FUNDS!r}")

    custom_field = raw.get("custom_field")
    if custom_field is not None and not isinstance(custom_field, str):
        raise ConfigError(f"{where}.custom_field must be a string")

    return Case(
        id=case_id,
        market=_enum(raw, "market", Market, where),
        code=_non_empty_str(raw, "code", where),
        action=cast(Action, _literal(raw, "action", Action, where)),
        price_ref=_enum(raw, "price_ref", PriceRef, where),
        price_type=cast(PriceType, _literal(raw, "price_type", PriceType, where)),
        order_type=cast(OrderType, _literal(raw, "order_type", OrderType, where)),
        quantity=quantity,
        custom_field=custom_field,
        expect_fill=expect_fill,
        steps=_steps(raw, where, quantity),
    )


def _required(raw: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in raw:
        raise ConfigError(f"{where} is missing key {key}")
    return raw[key]


def _non_empty_str(raw: Mapping[str, Any], key: str, where: str) -> str:
    value = _required(raw, key, where)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}.{key} must be a non-empty string")
    return value


def _enum[E: StrEnum](raw: Mapping[str, Any], key: str, enum_type: type[E], where: str) -> E:
    value = _required(raw, key, where)
    try:
        return enum_type(value)
    except ValueError:
        allowed = ", ".join(member.value for member in enum_type)
        raise ConfigError(f"{where}.{key} must be one of {allowed}, got {value!r}") from None


def _literal(raw: Mapping[str, Any], key: str, literal_type: object, where: str) -> str:
    value = _required(raw, key, where)
    allowed = get_args(literal_type)
    if value not in allowed:
        raise ConfigError(f"{where}.{key} must be one of {', '.join(allowed)}, got {value!r}")
    return cast(str, value)


def _quantity(raw: Mapping[str, Any], where: str) -> int | Literal["exceed_funds"]:
    value = _required(raw, "quantity", where)
    if value == EXCEED_FUNDS:
        return EXCEED_FUNDS
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where}.quantity must be a positive integer or {EXCEED_FUNDS!r}")
    return value


def _steps(raw: Mapping[str, Any], where: str, quantity: int | Literal["exceed_funds"]) -> tuple[str, ...]:
    value = _required(raw, "steps", where)
    if not isinstance(value, list) or not value or not all(isinstance(step, str) for step in value):
        raise ConfigError(f"{where}.steps must be a non-empty array of strings")

    reduced = 0
    for step_index, step in enumerate(value):
        try:
            parsed = parse_step(step)
        except ValueError as exc:
            raise ConfigError(f"{where}.steps[{step_index}]: {exc}") from None
        if step_index > 0 and value[step_index - 1] == "cancel":
            raise ConfigError(f"{where}.steps[{step_index}]: no step may follow 'cancel'")
        if parsed.reduce_by is not None:
            reduced += parsed.reduce_by

            # An exceed_funds quantity is only known once the live balance is known
            if isinstance(quantity, int) and reduced > quantity:
                raise ConfigError(
                    f"{where}.steps[{step_index}]: update_qty reductions total {reduced}, "
                    f"more than the case quantity {quantity}"
                )

    # Every later step acts on the order the place step created, so
    # without exactly one leading place there is nothing for them to act on.
    if value[0] != "place" or value.count("place") != 1:
        raise ConfigError(f"{where}.steps must begin with its only 'place' step")
    return tuple(value)
