"""Shared constants and type aliases for p1rec tests.

Fixtures live in conftest.py; this module holds the plain data they (and
the test modules that use them) share, so it can be imported directly
without going through pytest's fixture machinery.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

# Shape of the write_config fixture in conftest.py: an optional list of
# TOML lines in, the written file's path out.
WriteConfig = Callable[..., Path]

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
    "[interlock]",
    'endpoint_op = "mode"',
    'pointer = "/simulation"',
    # [events], shaped like the official server's order events and trades list
    "",
    "[events]",
    'order_event_name = "order_event"',
    'order_states = ["StockOrder", "FuturesOrder"]',
    'deal_states = ["StockDeal", "FuturesDeal"]',
    'event_id = "/event_id"',
    'order_key = "/order/id"',
    'op_type = "/operation/op_type"',
    'op_code = "/operation/op_code"',
    'op_code_success = "00"',
    'order_qty = "/status/order_quantity"',
    'cancel_qty = "/status/cancel_quantity"',
    'deal_key = "/trade_id"',
    'deal_qty = "/quantity"',
    'rest_list = ""',
    'rest_key = "/order/id"',
    'rest_status = "/status/status"',
    'rest_filled_qty = "/status/deal_quantity"',
    'exchange_ts_pointers = ["/date", "/time"]',
    'exchange_ts_format = "%Y-%m-%d %H:%M:%S.%f"',
    'heartbeat_event = "heartbeat"',
    "gap_threshold_s = 60.0",
]

SECRET_ENV_VARS = ("SJ_API_KEY", "SJ_SEC_KEY", "SJ_CA_PASSWD", "P1_PERSON_ID", "P1_EGRESS_IP")
