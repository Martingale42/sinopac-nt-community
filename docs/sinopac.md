# Sinopac

[SinoPac Securities](https://www.sinotrade.com.tw/) (永豐金證券) is a major Taiwanese
brokerage providing access to stocks (TWSE/TPEX), futures, and options (TAIFEX).

This integration supports live market data ingest and order execution through a
self-hosted FastAPI gateway that bridges the [Shioaji](https://sinotrade.github.io/)
Python SDK.

:::warning
This is an **independent, community-maintained** adapter distributed as the
`sinopac-nt-community` package. It is **not** part of, bundled with, or endorsed by
NautilusTrader or Nautech Systems. It is installed separately and is not present in
the standard NautilusTrader distribution.
:::

## Installation

The adapter is published as a standalone package that depends on a matching
NautilusTrader release:

```bash
pip install sinopac-nt-community
```

It exposes its public API under the `sinopac_nt` top-level package (for example
`from sinopac_nt import SinopacDataClientConfig`).

## Overview

This adapter is implemented in Rust with Python bindings. It connects to a
self-hosted gateway rather than directly to the exchange, keeping the Shioaji
Python SDK dependency isolated from the Rust core.

The Sinopac adapter includes multiple components:

- `SinopacHttpClient`: Low-level HTTP API connectivity to the gateway.
- `SinopacWebSocketClient`: Low-level WebSocket API connectivity for streaming data.
- `SinopacInstrumentProvider`: Instrument parsing and loading functionality.
- `SinopacDataClient`: Market data feed manager.
- `SinopacExecutionClient`: Account management and trade execution gateway.
- `SinopacLiveDataClientFactory`: Factory for Sinopac data clients (used by the trading node builder).
- `SinopacLiveExecClientFactory`: Factory for Sinopac execution clients (used by the trading node builder).

:::note
Most users will define a configuration for a live trading node (as shown below)
and won't need to work directly with these lower-level components.
:::

## Wire units

Quantities are denominated in their natural exchange unit and remain so all the
way from the gateway through `list_trades` to NautilusTrader reconciliation:

- **Stocks**: quantities are expressed in **shares**. A common lot is 1000 shares,
  and the gateway converts shares to Shioaji lots (÷1000) at its SDK boundary, so
  a common-lot order quantity must be a multiple of 1000 shares. Odd-lot
  quantities (1–999 shares) pass through as shares (see [Odd-lot trading](#odd-lot-trading)).
- **Futures and options**: quantities are expressed in **contracts**.

So in a strategy, an order for one common lot of a stock uses `quantity = 1000`
(shares), not `1`. A non-1000-multiple quantity submitted as a common-lot order is
rejected by the gateway (see [Order rejection](#order-rejection)).

`list_trades` reports each working/closed order with its `quantity` in shares plus
a `filled_qty` field (the deal-aggregated filled share count) and an
`avg_fill_price` (deal VWAP); both are `0` / `0.0` while the order is unfilled. The
execution client uses `quantity` and `filled_qty` when rebuilding
`OrderStatusReport` instances for reconciliation, and emits individual fills from
per-deal WebSocket events (`last_qty` in shares, `last_px` the deal price).

## Examples

You can find live example scripts in the [`examples/`](https://github.com/Martingale42/sinopac-nt-community/tree/main/examples) directory of this repository:

- `sinopac_data_tester.py`: subscribes to trade ticks, quote ticks, and order book
  data for Taiwan instruments (gateway port `8123`).
- `sinopac_exec_tester.py`: exercises order submission, modification, and
  cancellation (gateway port `8123`). The tester uses
  `trade_size = Decimal(1000)` — that is 1000 shares, i.e. one common lot. It runs
  with `dry_run=False` so that real orders are placed and the full path reaches the
  gateway; the configured gateway must therefore be a **simulation** gateway, never
  a live one.

:::note
A simulation-guarded `market_open_verify.sh` cron wrapper that drives the exec
tester during market hours lives in the
[shioaji-server](https://github.com/Martingale42/shioaji-server) repository. It
refuses to run unless the target gateway reports `simulation=true`.
:::

## Gateway setup

The Sinopac adapter requires a self-hosted FastAPI gateway
([shioaji-server](https://github.com/Martingale42/shioaji-server)) that translates
between NautilusTrader's Rust networking layer and the Shioaji Python SDK. The
gateway exposes typed REST and WebSocket endpoints for market data and order execution.

The gateway listens on port **8123** by default (moved off the popular `8000` to
avoid collisions with other local services). The port is configurable on the
gateway via the `SHIOAJI_SERVER_PORT` environment variable in the gateway's `.env`,
and on the adapter side via the `gateway_port` configuration option. The example
scripts and configurations below use `gateway_port=8123`.

:::warning
The gateway must be running and logged in (with the CA activated for order
placement) before starting the trading node.
:::

## Product support

| Product Type | Data Feed | Trading | Notes                                    |
|--------------|-----------|---------|------------------------------------------|
| Stocks       | ✓         | ✓       | TWSE and TPEX listed equities.           |
| Futures      | ✓         | ✓       | TAIFEX index, sector, single-stock, and ETF futures. |
| Options      | ✓         | ✓       | TAIFEX index options.                    |

## Symbology

Instruments use native Sinopac contract codes as symbols:

### Stocks

Format: `{code}` (numeric stock code)

Examples:

- `2330` — TSMC
- `2317` — Hon Hai Precision

To subscribe in your strategy:

```python
InstrumentId.from_str("2330.SINOPAC")
InstrumentId.from_str("2317.SINOPAC")
```

### Futures

Format: `{root}{delivery}` (product root + delivery month/year code)

Examples:

- `TXFC6` — TAIEX futures, March 2026
- `MXFC6` — Mini-TAIEX futures, March 2026

```python
InstrumentId.from_str("TXFC6.SINOPAC")
```

### Options

Format: `{root}{delivery}{strike}{type}` (product root + delivery + strike + C/P)

```python
InstrumentId.from_str("TXO19000C6.SINOPAC")
```

## Instrument schedules (tick size & multiplier)

The adapter ships complete TAIFEX tick-size and contract-multiplier tables so
instruments are constructed with correct price grids and tick values, rather than
relying on gateway-supplied increments. The schedule is selected by the contract's
`underlying_kind` plus the underlying code:

- **Index / sector futures** (`underlying_kind = "I"`): a root table covering
  `TXF`, `MXF`, `T5F`, `XIF` (tick `1.0`), `ZEF` (tick `0.05`), and `ZFF`
  (tick `0.2`), with their corresponding multipliers (e.g. `TXF` = 200, `MXF` = 50,
  `ZEF` = 500, `ZFF` = 250).
- **Single-stock futures** (`underlying_kind = "S"`): a price-tiered tick grid
  keyed on the reference price (e.g. `< 10` → `0.01`, `< 50` → `0.05`, … up to
  `5.0` for the highest tier).
- **ETF futures** (`underlying_kind = "E"`): a two-tier grid (`< 50` → `0.01`,
  otherwise `0.05`).

Unknown futures roots fall back to a default tick (`1.0`) and default multiplier,
emitting a warning so the misclassification is visible in the logs. Fractional
option strikes (e.g. `67.5`) are preserved at the correct price precision.

## Data subscriptions

| Data type         | Subscription | Historical | Nautilus type        | Notes                              |
|-------------------|--------------|------------|----------------------|------------------------------------|
| Trade ticks       | ✓            | ✓          | `TradeTick`          | Via WebSocket tick stream.         |
| Quote ticks       | ✓            | -          | `QuoteTick`          | Top-of-book from BidAsk stream.   |
| Order book depth  | ✓            | -          | `OrderBookDepth10`   | 5-level depth from BidAsk stream. |
| Order book deltas | ✓            | -          | `OrderBookDeltas`    | CLEAR + ADD snapshot pattern.     |
| Bars              | -            | ✓          | `Bar`                | Historical only via REST.         |

:::note
Quote ticks, order book depth, and order book deltas all derive from the same
BidAsk WebSocket stream. The adapter only parses and delivers the data types with
active subscriptions, using per-instrument emit flags for efficiency.
:::

## Order capability

### Order types

| Order Type | Stocks | Futures | Options | Notes |
|------------|--------|---------|---------|-------|
| `MARKET`   | ✓      | ✓       | ✓       |       |
| `LIMIT`    | ✓      | ✓       | ✓       |       |

The execution client snaps limit prices onto the instrument's tick grid using
round-half-even (banker's rounding) before sending. This handles grids such as
`0.05` that an instrument's price precision alone cannot express; an off-grid price
is adjusted to the nearest tick and the adjustment is logged.

### Lots and odd-lot trading

NautilusTrader orders are placed as **common lots only**. The execution client's
`_submit_order` does not send an `order_lot` field, so every order defaults to the
gateway's `Common` lot. Consequently the order quantity (in shares) must be a
multiple of 1000; a non-1000-multiple quantity is rejected (see
[Order rejection](#order-rejection)).

#### Odd-lot trading

The gateway itself supports `order_lot` ∈ `Common` / `Odd` / `IntradayOdd` /
`Fixing`, with odd-lot quantities expressed in shares:

- `IntradayOdd` — intraday odd-lot (盤中零股), 09:00–13:30.
- `Odd` — after-hours odd-lot (盤後零股), 13:40–14:30.
- `Fixing` — fixing session.

:::warning
Odd-lot trading is currently a **gateway HTTP capability only**. The NautilusTrader
execution client does not yet send `order_lot`, so it cannot place odd-lot, intraday
odd-lot, or fixing orders — every NT order is a common lot. NT-side odd-lot support
is a planned future enhancement and is **not implemented**.
:::

### Time in force

| Time in force | Stocks | Futures | Options | Notes               |
|---------------|--------|---------|---------|---------------------|
| `DAY`         | ✓      | ✓       | ✓       | Rest of day (ROD).  |
| `IOC`         | ✓      | ✓       | ✓       | Immediate or cancel.|
| `FOK`         | ✓      | ✓       | ✓       | Fill or kill.       |

### Order operations

| Operation    | Stocks | Futures | Options | Notes                         |
|--------------|--------|---------|---------|-------------------------------|
| Submit order | ✓      | ✓       | ✓       |                               |
| Modify order | ✓      | ✓       | ✓       | Price and quantity.            |
| Cancel order | ✓      | ✓       | ✓       |                               |

## Order rejection

Order rejection has two paths, of which the asynchronous one is dominant:

- **Asynchronous (dominant)**: `POST /place` returns HTTP `200` with status
  `PendingSubmit`, so the execution client marks the order `ACCEPTED`. The actual
  venue rejection (off-tick, over price band, margin, etc.) arrives later via the
  order-event stream as a `New` order event with status `OrderStatus.Failed` and an
  `op_code != "00"`. The execution client then drives the order from `ACCEPTED` to
  `REJECTED` (a legal NT transition) — there is no placeholder order. An already
  partially-filled or filled order is never rejected this way, to avoid an illegal
  state transition.
- **Synchronous (second line of defense)**: a synchronously-rejecting SDK call
  causes the gateway to return HTTP `422`. The pyo3 HTTP client raises, and the
  execution client rejects the order directly. A non-1000-multiple common-lot
  quantity is rejected on this path.

## Order books

Order books are maintained via the BidAsk WebSocket stream. Each message delivers
a 5-level snapshot. The adapter supports two consumption patterns:

- **`OrderBookDepth10`**: Direct 5-level snapshot with bid/ask arrays.
- **`OrderBookDeltas`**: CLEAR + ADD pattern for incremental book maintenance.

:::note
There is a limitation of one order book per instrument per trader instance.
:::

## Connection management

The data and execution clients share a single WebSocket connection through a
lifecycle-bound dispatcher. The dispatcher fans out incoming messages to the
registered clients and holds a connection refcount: each client increments the
refcount on connect and decrements it on disconnect, and the shared socket is closed
only when the last client disconnects. An execution-only node therefore establishes
the WebSocket independently, so order and fill events still arrive even with no data
client present.

The adapter automatically reconnects on WebSocket disconnection using exponential
backoff (starting at 500ms, up to 5s). On reconnect, all active subscriptions are
resubscribed automatically and a heartbeat ping is sent every 30 seconds.

### Reconnection and reconciliation

After resubscription completes, the WebSocket emits a `{"event":"reconnected"}`
sentinel. On receiving it, the execution client schedules a mass-status
reconciliation — it regenerates the full `ExecutionMassStatus` (order and position
reports via `list_trades` and `list_positions`) and hands it to the engine's
mass-status reconciliation entrypoint. This adopts any fills, cancels, or rejections
that landed while the WebSocket was down, so an in-gap fill converges within one
reconnect cycle.

## Panic safety

This is a real-money trading adapter, so all gateway-fed numbers cross a hard
panic-safety boundary (in the Rust core) before entering the domain model:

- Prices and quantities are built through checked constructors (`try_price` /
  `try_qty`), so NaN, infinite, negative, out-of-range, or over-precision values
  return errors instead of panicking.
- Length-mismatched bid/ask and OHLCV arrays are rejected rather than indexed out
  of bounds.
- KBar OHLC cross-field invariants (e.g. high ≥ low) are validated.
- A non-finite instrument `unit` (lot size) is rejected.
- A single poisoned WebSocket frame is contained with `catch_unwind` and logged,
  without killing the WebSocket receive loop.

## Configuration

### Data client configuration options

| Option           | Default       | Description                               |
|------------------|---------------|-------------------------------------------|
| `venue`          | `"SINOPAC"`   | Venue identifier.                         |
| `gateway_host`   | `"localhost"` | Gateway host address.                     |
| `gateway_port`   | `8000`        | Gateway HTTP/WS port (set to `8123` to match the gateway default). |
| `gateway_ws_path`| `"/ws"`       | WebSocket endpoint path on the gateway.   |

### Execution client configuration options

| Option           | Default       | Description                                                                 |
|------------------|---------------|-----------------------------------------------------------------------------|
| `venue`          | `"SINOPAC"`   | Venue identifier.                                                           |
| `account_id`     | `None`        | Sinopac account identifier. Loaded from `SINOPAC_ACCOUNT_ID` when omitted.  |
| `gateway_host`   | `"localhost"` | Gateway host address.                                                       |
| `gateway_port`   | `8000`        | Gateway HTTP/WS port (set to `8123` to match the gateway default).         |
| `gateway_ws_path`| `"/ws"`       | WebSocket endpoint path on the gateway.                                     |

:::note
The adapter's `gateway_port` default remains `8000` for backward compatibility, but
the gateway now listens on `8123` by default. Set `gateway_port=8123` (as in the
examples below) to connect to a default gateway deployment.
:::

### Configuration example

```python
from sinopac_nt import SINOPAC
from sinopac_nt import SinopacDataClientConfig
from sinopac_nt import SinopacExecClientConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import TradingNodeConfig

config = TradingNodeConfig(
    data_clients={
        SINOPAC: SinopacDataClientConfig(
            instrument_provider=InstrumentProviderConfig(load_all=True),
            gateway_host="localhost",
            gateway_port=8123,
        ),
    },
    exec_clients={
        SINOPAC: SinopacExecClientConfig(
            instrument_provider=InstrumentProviderConfig(load_all=True),
            account_id=None,  # Loads from SINOPAC_ACCOUNT_ID env var
            gateway_host="localhost",
            gateway_port=8123,
        ),
    },
)
```

Then, create a `TradingNode` and add the client factories:

```python
from sinopac_nt import SINOPAC
from sinopac_nt import SinopacLiveDataClientFactory
from sinopac_nt import SinopacLiveExecClientFactory
from nautilus_trader.live.node import TradingNode

# Instantiate the live trading node with a configuration
node = TradingNode(config=config)

# Register the client factories with the node
node.add_data_client_factory(SINOPAC, SinopacLiveDataClientFactory)
node.add_exec_client_factory(SINOPAC, SinopacLiveExecClientFactory)

# Finally build the node
node.build()
```

## API credentials

Set the following environment variable for execution client authentication:

- `SINOPAC_ACCOUNT_ID`: Your Sinopac brokerage account identifier.

:::tip
We recommend using environment variables to manage your credentials.
:::

:::note
API key and secret for the Shioaji SDK are configured on the gateway side,
not in the NautilusTrader adapter configuration.
:::

## Verification status

- Unit and integration suites are green:
  - Rust: `cargo test`
  - Python: `uv run pytest python/tests`
- Live acceptance gates live in the
  [shioaji-server](https://github.com/Martingale42/shioaji-server) repository
  (`tests/acceptance/test_live_gate.py`, marker `gateway`, configured via the
  `SINOPAC_GATEWAY_URL` environment variable).
- The stock deal-unit contract (lots vs. shares) is verified intraday against a
  simulation gateway.

## Contributing

:::info
For additional features or to contribute to this community adapter, please open an
issue or pull request on the
[sinopac-nt-community](https://github.com/Martingale42/sinopac-nt-community)
repository.
:::
