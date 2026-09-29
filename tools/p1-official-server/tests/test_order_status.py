"""Tests for p1rec.order_status: orders and reconcile read a Trade's status the same way."""

from __future__ import annotations

import json

import pytest
from helpers import WriteConfig

from p1rec import orders
from p1rec.capture import CapturedResponse
from p1rec.config import TradeFields, load_config
from p1rec.reconcile import orders_from_rest

_ORDER_ID = "000001"


def _trade(status: str, deal_quantity: int) -> dict[str, object]:
    """Return a Trade, shaped like a place response or a trades element, with the given status."""
    return {
        "order": {"id": _ORDER_ID, "quantity": 1},
        "status": {"id": _ORDER_ID, "status": status, "deal_quantity": deal_quantity, "order_ts": 1790677936.694802},
    }


def _in_orders(trade: dict[str, object], fields: TradeFields) -> tuple[orders._OrderState, bool]:
    """Return the state orders gives trade in a trades poll, and whether a 2xx carrying it counts as accepted."""
    response = CapturedResponse(status=200, body=json.dumps(trade).encode(), recv_ts_ns=0)
    accepted, _ = orders._judge(response, fields)
    return orders._order_state(trade, fields), accepted


def _in_reconcile(trade: dict[str, object], fields: TradeFields) -> str:
    """Return the status reconcile takes REST to report for trade."""
    return orders_from_rest([trade], fields).orders[_ORDER_ID].status


@pytest.mark.parametrize(
    ("status", "deal_quantity", "state", "accepted"),
    [
        ("Filled", 1, orders._OrderState.FILLED, True),
        ("Failed", 0, orders._OrderState.ENDED_UNFILLED, False),
        ("Cancelled", 0, orders._OrderState.ENDED_UNFILLED, True),
    ],
)
def test_prefixed_status_same_in_orders_and_reconcile(
    write_config: WriteConfig, status: str, deal_quantity: int, state: orders._OrderState, accepted: bool
) -> None:
    fields = load_config(write_config()).trades
    plain, prefixed = _trade(status, deal_quantity), _trade(f"Status.{status}", deal_quantity)

    assert _in_orders(prefixed, fields) == _in_orders(plain, fields) == (state, accepted)
    assert _in_reconcile(prefixed, fields) == _in_reconcile(plain, fields) == status
