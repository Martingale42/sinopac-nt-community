# sinopac-nt-community

> **Disclaimer:** This is an independent community project. It is not affiliated
> with, endorsed by, or supported by Nautech Systems Pty Ltd or the official
> NautilusTrader project.

An independent community adapter integrating **SinoPac Securities** (Taiwan
markets — TWSE / TPEX / TAIFEX, via the [Shioaji](https://sinotrade.github.io/)
SDK) with [NautilusTrader](https://nautilustrader.io). It provides a live data
client, a live execution client, and an instrument provider, backed by a
Rust-native HTTP/WebSocket core compiled as a Python extension.

- **Pinned to:** `nautilus_trader==1.228.0` (PyPI) / `nautilus-* 0.58.0` (crates.io)
- **Companion gateway:** [shioaji-server](https://github.com/Martingale42/shioaji-server) (a separate process the adapter connects to)
- **License:** LGPL-3.0-or-later

## Requirements

- Python 3.12–3.14
- Rust 1.96.0 (to build the extension; see `rust-toolchain.toml`)
- [uv](https://docs.astral.sh/uv/) and [maturin](https://www.maturin.rs/)
- A running [shioaji-server](https://github.com/Martingale42/shioaji-server) gateway for live data/execution

## Installation

No wheel is published yet — build from source:

```bash
git clone https://github.com/Martingale42/sinopac-nt-community
cd sinopac-nt-community

uv venv --python 3.12
uv pip install nautilus_trader==1.228.0 maturin
uv run maturin develop            # editable install of the sinopac_nt extension
# or build a distributable wheel:
uv run maturin build --release    # -> target/wheels/
```

## Usage

Register the factories on a `TradingNode` and point the configs at your
`shioaji-server` gateway:

```python
from nautilus_trader.live.node import TradingNode
from nautilus_trader.live.config import TradingNodeConfig

from sinopac_nt import (
    SINOPAC,
    SinopacDataClientConfig,
    SinopacExecClientConfig,
    SinopacLiveDataClientFactory,
    SinopacLiveExecClientFactory,
)

config = TradingNodeConfig(
    trader_id="TESTER-001",
    data_clients={SINOPAC: SinopacDataClientConfig(...)},
    exec_clients={SINOPAC: SinopacExecClientConfig(...)},
)

node = TradingNode(config=config)
node.add_data_client_factory(SINOPAC, SinopacLiveDataClientFactory)
node.add_exec_client_factory(SINOPAC, SinopacLiveExecClientFactory)
node.build()
node.run()
```

Runnable end-to-end testers are in [`examples/`](examples/)
(`sinopac_data_tester.py`, `sinopac_exec_tester.py`).

### Taiwan order semantics

Taiwan-venue order parameters with no native Nautilus equivalent (margin/short,
intraday odd lot, futures open-close type, range-market `MKP`, TIF coercion,
limit-up/down) are carried on `order.tags` via `SinopacOrderTags`. See the
`sinopac_nt` package docstring and [`docs/sinopac.md`](docs/sinopac.md) for the
full capability matrix.

## Development

```bash
uv run maturin develop
uv run pytest python/tests -q     # Python integration tests
cargo test                        # Rust unit + integration tests
```

## Maintainer

Maintained by [@Martingale42](https://github.com/Martingale42). Please open an
issue on this repository for bugs, questions, or contributions.

## License

[LGPL-3.0-or-later](LICENSE). This project links against and re-homes code from
NautilusTrader (also LGPL-3.0-or-later); upstream copyright notices are retained
in the source headers.
