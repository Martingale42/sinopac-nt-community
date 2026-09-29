"""Tests for p1rec.scenarios: loading and validating scenarios.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from p1rec.config import ConfigError
from p1rec.scenarios import EXCEED_FUNDS, Case, Market, PriceRef, Step, load_scenarios, parse_step

_LIFECYCLE_CASE = [
    "[[case]]",
    'id = "lifecycle_2890"',
    'market = "stock"',
    'code = "2890"',
    'action = "Buy"',
    'price_ref = "far_below"',
    'price_type = "LMT"',
    'order_type = "ROD"',
    "quantity = 2",
    "expect_fill = false",
    'steps = ["place", "update_price:far_below_plus_tick", "update_qty:1", "cancel"]',
]

_EXCEED_FUNDS_CASE = [
    "[[case]]",
    'id = "exceed_funds_2890"',
    'market = "stock"',
    'code = "2890"',
    'action = "Buy"',
    'price_ref = "limit_down"',
    'price_type = "LMT"',
    'order_type = "ROD"',
    'quantity = "exceed_funds"',
    'custom_field = "aB3xY9"',
    "expect_fill = false",
    'steps = ["place", "cancel"]',
]


def _write(tmp_path: Path, *cases: list[str]) -> Path:
    path = tmp_path / "scenarios.toml"
    lines = [line for case in cases for line in (*case, "")]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _replace(case: list[str], key: str, new_line: str | None) -> list[str]:
    """Return case with the line setting key replaced by new_line, or dropped if None."""
    out = []
    for line in case:
        if line.startswith(f"{key} ="):
            if new_line is not None:
                out.append(new_line)
        else:
            out.append(line)
    return out


def test_loads_cases_in_file_order(tmp_path: Path) -> None:
    cases = load_scenarios(_write(tmp_path, _LIFECYCLE_CASE, _EXCEED_FUNDS_CASE))

    assert cases == [
        Case(
            id="lifecycle_2890",
            market=Market.STOCK,
            code="2890",
            action="Buy",
            price_ref=PriceRef.FAR_BELOW,
            price_type="LMT",
            order_type="ROD",
            quantity=2,
            custom_field=None,
            expect_fill=False,
            steps=("place", "update_price:far_below_plus_tick", "update_qty:1", "cancel"),
        ),
        Case(
            id="exceed_funds_2890",
            market=Market.STOCK,
            code="2890",
            action="Buy",
            price_ref=PriceRef.LIMIT_DOWN,
            price_type="LMT",
            order_type="ROD",
            quantity=EXCEED_FUNDS,
            custom_field="aB3xY9",
            expect_fill=False,
            steps=("place", "cancel"),
        ),
    ]


def test_duplicate_case_id_rejected(tmp_path: Path) -> None:
    duplicate = _replace(_EXCEED_FUNDS_CASE, "id", 'id = "lifecycle_2890"')

    with pytest.raises(ConfigError, match=r"duplicates case id 'lifecycle_2890'"):
        load_scenarios(_write(tmp_path, _LIFECYCLE_CASE, duplicate))


@pytest.mark.parametrize(
    "step",
    ["modify", "update_price:nowhere", "update_price", "update_qty:0", "update_qty:x", "cancel:now"],
)
def test_unknown_step_rejected(tmp_path: Path, step: str) -> None:
    case = _replace(_LIFECYCLE_CASE, "steps", f'steps = ["place", "{step}"]')

    with pytest.raises(ConfigError, match=r"steps\[1\]"):
        load_scenarios(_write(tmp_path, case))


def test_exceed_funds_with_expect_fill_rejected(tmp_path: Path) -> None:
    case = _replace(_EXCEED_FUNDS_CASE, "expect_fill", "expect_fill = true")

    with pytest.raises(ConfigError, match=r"expect_fill"):
        load_scenarios(_write(tmp_path, case))


@pytest.mark.parametrize("steps", ['["cancel"]', '["update_qty:1", "place"]', '["place", "place"]', "[]"])
def test_steps_must_begin_with_single_place(tmp_path: Path, steps: str) -> None:
    case = _replace(_LIFECYCLE_CASE, "steps", f"steps = {steps}")

    with pytest.raises(ConfigError, match=r"steps"):
        load_scenarios(_write(tmp_path, case))


def test_unknown_key_rejected(tmp_path: Path) -> None:
    case = [*_LIFECYCLE_CASE, "expect_fil = true"]

    with pytest.raises(ConfigError, match=r"unknown keys: expect_fil"):
        load_scenarios(_write(tmp_path, case))


@pytest.mark.parametrize(
    ("key", "line"),
    [
        ("market", 'market = "bond"'),
        ("action", 'action = "buy"'),
        ("price_ref", 'price_ref = "limit_middle"'),
        ("price_type", 'price_type = "STP"'),
        ("order_type", 'order_type = "GTC"'),
        ("quantity", "quantity = 0"),
        ("quantity", "quantity = true"),
        ("expect_fill", 'expect_fill = "no"'),
        ("code", 'code = ""'),
    ],
)
def test_invalid_value_names_case_and_key(tmp_path: Path, key: str, line: str) -> None:
    case = _replace(_LIFECYCLE_CASE, key, line)

    with pytest.raises(ConfigError, match=rf"case\[0\] \(id 'lifecycle_2890'\)\.{key}"):
        load_scenarios(_write(tmp_path, case))


def test_missing_key_rejected(tmp_path: Path) -> None:
    case = _replace(_LIFECYCLE_CASE, "expect_fill", None)

    with pytest.raises(ConfigError, match=r"missing key expect_fill"):
        load_scenarios(_write(tmp_path, case))


def test_no_cases_rejected(tmp_path: Path) -> None:
    path = tmp_path / "scenarios.toml"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"at least one \[\[case\]\]"):
        load_scenarios(path)


def test_parse_step() -> None:
    assert parse_step("place") == Step(verb="place")
    assert parse_step("update_price:far_below_plus_tick") == Step(
        verb="update_price", price_ref=PriceRef.FAR_BELOW_PLUS_TICK
    )
    assert parse_step("update_qty:1") == Step(verb="update_qty", reduce_by=1)
    assert parse_step("cancel") == Step(verb="cancel")
