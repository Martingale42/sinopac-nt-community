#!/usr/bin/env python3
"""
Example: Sinopac data client tester.

Subscribes to trade ticks, quote ticks (BidAsk top-of-book), and order book
data for Taiwan instruments using the Sinopac gateway adapter.

The gateway streams 5-level BidAsk snapshots via WebSocket.  The adapter
produces three data types from each snapshot:

- ``QuoteTick``          — best bid/ask (subscribe_quotes)
- ``OrderBookDepth10``   — all 5 levels as a snapshot (subscribe_book_depth)
- ``OrderBookDeltas``    — CLEAR + ADD rebuild (subscribe_book_deltas)

Prerequisites:
    1. Sinopac gateway running: ``uvicorn sinopac_server.main:app --port 8000``
    2. Gateway logged in: ``curl -X POST http://localhost:8000/auth/login -d '...'``
"""

from sinopac_nt.config import SinopacDataClientConfig
from sinopac_nt.constants import SINOPAC
from sinopac_nt.factories import SinopacLiveDataClientFactory
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.config import StreamingConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.persistence.writer import RotationMode
from nautilus_trader.test_kit.strategies.tester_data import DataTester
from nautilus_trader.test_kit.strategies.tester_data import DataTesterConfig


# Configuration
gateway_host = "localhost"
gateway_port = 8123  # gateway moved off the popular 8000 (collided with vLLM)

config_node = TradingNodeConfig(
    trader_id=TraderId("SINOPAC-DATA-TESTER-001"),
    logging=LoggingConfig(
        log_level="INFO",
        log_level_file="DEBUG",
        log_directory="./catalog",
        log_file_format="JSON",
        use_pyo3=True
    ),
    data_clients={
        SINOPAC: SinopacDataClientConfig(
            gateway_host=gateway_host,
            gateway_port=gateway_port,
            instrument_provider=InstrumentProviderConfig(load_all=True),
        ),
    },
    # streaming=StreamingConfig(
    #     catalog_path="./catalog",
    #     flush_interval_ms = 5000,
    #     rotation_mode=RotationMode.INTERVAL,
    #     rotation_interval="1h"
    # ),
    timeout_connection=30.0,
    timeout_disconnection=5.0,
    timeout_post_stop=5.0,
)

node = TradingNode(config=config_node)

# Configure instruments to test
instrument_ids = [
    InstrumentId.from_str("2330.SINOPAC"),  # TSMC
    InstrumentId.from_str("2317.SINOPAC"),  # Hon Hai
]

config_tester = DataTesterConfig(
    instrument_ids=instrument_ids,
    subscribe_trades=True,
    subscribe_quotes=True,
    subscribe_book_depth=True,
    subscribe_book_deltas=True,
    manage_book=True,
    log_data=True,
)
data_tester = DataTester(config=config_tester)
node.trader.add_actor(data_tester)

node.add_data_client_factory(SINOPAC, SinopacLiveDataClientFactory)
node.build()

if __name__ == "__main__":
    try:
        node.run()
    finally:
        node.dispose()
