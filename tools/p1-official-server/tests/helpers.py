"""Shared constants and type aliases for p1rec tests.

Fixtures live in conftest.py; this module holds the plain data they (and
the test modules that use them) share, so it can be imported directly
without going through pytest's fixture machinery.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from p1rec.config import OrdersConfig

# Shape of the write_config fixture in conftest.py: an optional list of
# TOML lines in, the written file's path out.
WriteConfig = Callable[..., Path]

# Order body templates shaped like the live p1.toml's, on one line each
PLACE_STOCK_TEMPLATE = (
    '{"contract": {"security_type": "{security_type}", "exchange": "{exchange}", "code": "{code}"}, '
    '"stock_order": {"action": "{action}", "price": "{price}", "quantity": "{quantity}", '
    '"price_type": "{price_type}", "order_type": "{order_type}", "order_lot": "{order_lot}", '
    '"custom_field": "{custom_field}", "account": "{account}"}}'
)
PLACE_FUTURES_TEMPLATE = (
    '{"contract": {"security_type": "{security_type}", "exchange": "{exchange}", "code": "{code}"}, '
    '"futures_order": {"action": "{action}", "price": "{price}", "quantity": "{quantity}", '
    '"price_type": "{price_type}", "order_type": "{order_type}", "octype": "{octype}", '
    '"custom_field": "{custom_field}", "account": "{account}"}}'
)

# An OrdersConfig for hand-built P1Configs whose test never runs orders
UNUSED_ORDERS_CONFIG = OrdersConfig(
    body_templates={},
    exchange={},
    trade_id_pointer="",
    filled_pointer="",
    deal_quantity_pointer="",
    step_wait_s=0.0,
    fill_poll_s=0.0,
    fill_timeout_s=0.0,
)

# A minimal but complete p1.toml, shared by tests that need a valid config
# to load and mutate, independent of the real p1.toml.
MINIMAL_CONFIG_LINES = [
    'base_url = "http://127.0.0.1:8080"',
    "http_timeout_s = 35",
    'results_root = "results"',
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
    "",
    # usage and ticks back the [history] section below: _parse_history
    # requires both, the same way [interlock] requires "mode" above.
    "[endpoints.usage]",
    'method = "GET"',
    'path = "/api/v1/auth/usage"',
    "",
    "[endpoints.ticks]",
    'method = "POST"',
    'path = "/api/v1/data/ticks"',
    "",
    "[interlock]",
    'endpoint_op = "mode"',
    'pointer = "/simulation"',
    "",
    # [history] is required by every config since HistoryConfig has no
    # defaults, the same as [interlock] above.
    "[history]",
    'usage_bytes_pointer = "/bytes"',
    "",
    "[[history.query]]",
    'op = "ticks"',
    'body = \'{"contract": {"security_type": "STK", "exchange": "TSE", "code": "2330"}, "date": "2026-09-29"}\'',

    # [contracts] and [funds]: contract limit and available-funds pointers,
    # with the POST endpoint funds.stock_available_op names.
    "",
    "[contracts]",
    'code = "/code"',
    'limit_up = "/limit_up"',
    'limit_down = "/limit_down"',
    'reference = "/reference"',
    'futures_tick = "/tick"',
    'etf_codes = ["0050"]',
    "",
    "[funds]",
    'stock_available_op = "trading_limits"',
    'stock_available_pointer = "/trading_available"',
    'futures_available_pointer = "/available_margin"',
    "",
    "[funds.stock_report_only]",
    "",
    "[funds.futures_margin_twd]",
    "",
    "[endpoints.trading_limits]",
    'method = "POST"',
    'path = "/api/v1/portfolio/trading_limits"',
    # [orders]: one POST endpoint per body template plus trades, which the
    # fill poll reads.
    "",
    "[endpoints.place_futures]",
    'method = "POST"',
    'path = "/api/v1/order/place_order"',
    "",
    "[endpoints.update_price]",
    'method = "POST"',
    'path = "/api/v1/order/update_price"',
    "",
    "[endpoints.update_qty]",
    'method = "POST"',
    'path = "/api/v1/order/update_qty"',
    "",
    "[endpoints.cancel]",
    'method = "POST"',
    'path = "/api/v1/order/cancel_order"',
    "",
    "[endpoints.trades]",
    'method = "POST"',
    'path = "/api/v1/order/trades"',
    "",
    "[orders]",
    'trade_id_pointer = "/order/id"',
    'filled_pointer = "/status/status"',
    'deal_quantity_pointer = "/status/deal_quantity"',
    "step_wait_s = 3",
    "fill_poll_s = 2",
    "fill_timeout_s = 60",
    "",
    "[orders.exchange]",
    'stock = "TSE"',
    'odd = "TSE"',
    'futures = "TAIFEX"',
    "",
    "[orders.body_templates]",
    f"place_stock = '{PLACE_STOCK_TEMPLATE}'",
    f"place_futures = '{PLACE_FUTURES_TEMPLATE}'",
    """update_price = '{"trade_id": "{trade_id}", "price": "{price}"}'""",
    """update_qty = '{"trade_id": "{trade_id}", "quantity": "{quantity}"}'""",
    """cancel = '{"trade_id": "{trade_id}"}'""",
]

SECRET_ENV_VARS = ("SJ_API_KEY", "SJ_SEC_KEY", "SJ_CA_PASSWD", "P1_PERSON_ID", "P1_EGRESS_IP")
