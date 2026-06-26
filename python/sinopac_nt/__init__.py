# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
Provides a trading adapter for Sinopac (SinoPac Securities), via the Shioaji SDK.

Taiwan order semantics
----------------------
Taiwan-venue order parameters that have no native Nautilus equivalent are carried
on ``order.tags`` through :class:`SinopacOrderTags` (the ``IBOrderTags`` pattern).
The execution client parses the tag, validates it fail-fast against the Shioaji
rules, and forwards strongly-typed parameters to the gateway. The gateway remains
the authoritative validator. Field names mirror the Shioaji SDK verbatim.

Attach a tag by appending ``SinopacOrderTags(...).value`` to the order's ``tags``::

    from sinopac_nt import SinopacOrderTags

    # Intraday odd lot (pan-zhong ling-gu): 37 shares, must be LIMIT + DAY(ROD)
    order = self.order_factory.limit(
        instrument_id=instrument_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_int(37),
        price=price,
        time_in_force=TimeInForce.DAY,
        tags=[SinopacOrderTags(order_lot="IntradayOdd").value],
    )

    # Margin / short (rong-zi / rong-quan): round lot
    tags = [SinopacOrderTags(order_cond="MarginTrading").value]
    tags = [SinopacOrderTags(order_cond="ShortSelling").value]

    # Day-trade short (xian-gu dang-chong): requires order_cond="Cash"
    tags = [SinopacOrderTags(daytrade_short=True).value]

    # Futures open-close type: Cover an existing position
    tags = [SinopacOrderTags(octype="Cover").value]

Capability matrix
~~~~~~~~~~~~~~~~~~
- ``order_lot``: ``Common`` (round lot, 1000 shares) | ``IntradayOdd`` (1-999
  shares). Post-market ``Odd`` and ``Fixing`` lots are out of scope (rejected
  locally; tracked as backlog item B3).
- ``order_cond``: ``Cash`` | ``MarginTrading`` | ``ShortSelling`` (stocks only).
- ``daytrade_short``: stock day-trade short flag; requires ``order_cond="Cash"``.
- ``octype``: ``Auto`` | ``New`` | ``Cover`` | ``DayTrade`` (futures/options).
  Ignored for stock orders (downgraded to ``Auto`` with a warning).

Range-market (MKP) orders
~~~~~~~~~~~~~~~~~~~~~~~~~~~
A range-market (fan-wei shi-jia, MKP) order is expressed as
``OrderType.MARKET_TO_LIMIT``. MKP is futures/options-only on the Shioaji stock
side, so a stock ``MARKET_TO_LIMIT`` is rejected locally (it would otherwise be
an HTTP 500 at the gateway). Use ``LIMIT`` or ``MARKET`` for stocks.

Time-in-force coercion
~~~~~~~~~~~~~~~~~~~~~~~~
Taiwan venues accept only ROD/IOC/FOK; GTC is not supported and market/range
orders must be IOC or FOK. The adapter coerces or rejects as follows:

================ ============= ================ ====================================
Order type       Time-in-force Result           Note
================ ============= ================ ====================================
LIMIT            DAY           ROD              direct
LIMIT            GTC           ROD              coerced, warning (no GTC on TWSE)
LIMIT            IOC / FOK     IOC / FOK        direct
MARKET / MKP     IOC / FOK     IOC / FOK        direct
MARKET / MKP     DAY / GTC     IOC              coerced, warning (market needs IOC)
any              GTD / AT_THE_* rejected         rejected locally (unsupported)
================ ============= ================ ====================================

Limit-up / limit-down / reference price
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
No special order type is needed. The daily price band is loaded onto the
instrument:

- Limit-up (zhang-ting) buy: ``LIMIT @ instrument.max_price``.
- Limit-down (die-ting) sell: ``LIMIT @ instrument.min_price``.
- Reference / flat price (ping-pan) orders: ``instrument.info["reference"]``.

Odd-lot modification
~~~~~~~~~~~~~~~~~~~~~
An intraday odd-lot order cannot change its price; only the quantity may be
reduced. A price modification is rejected locally; a quantity-only modify
proceeds.

Backtest parity
~~~~~~~~~~~~~~~~
``SinopacOrderTags`` are a live-venue concern: the backtest matching engine
ignores them. Strategies relying on margin/short, odd-lot, or ``octype`` behavior
must validate against the live (simulation) gateway, not the backtest.

Out of scope
~~~~~~~~~~~~
Post-market odd lot (``Odd``), fixed-price session (``Fixing``), and TAIFEX
option combo orders (plus reserve and stop orders) are not supported and are
tracked as backlog item B3. Single-leg option orders (including ``octype``) are
fully supported.

"""

from sinopac_nt.config import SinopacDataClientConfig
from sinopac_nt.config import SinopacExecClientConfig
from sinopac_nt.constants import SINOPAC
from sinopac_nt.constants import SINOPAC_CLIENT_ID
from sinopac_nt.constants import SINOPAC_VENUE
from sinopac_nt.data import SinopacDataClient
from sinopac_nt.execution import SinopacExecutionClient
from sinopac_nt.factories import SinopacLiveDataClientFactory
from sinopac_nt.factories import SinopacLiveExecClientFactory
from sinopac_nt.factories import get_sinopac_http_client
from sinopac_nt.factories import get_sinopac_instrument_provider
from sinopac_nt.factories import get_sinopac_ws_client
from sinopac_nt.providers import SinopacInstrumentProvider
from sinopac_nt.tags import SinopacOrderTags


__all__ = [
    "SINOPAC",
    "SINOPAC_CLIENT_ID",
    "SINOPAC_VENUE",
    "SinopacDataClient",
    "SinopacDataClientConfig",
    "SinopacExecClientConfig",
    "SinopacExecutionClient",
    "SinopacInstrumentProvider",
    "SinopacLiveDataClientFactory",
    "SinopacLiveExecClientFactory",
    "SinopacOrderTags",
    "get_sinopac_http_client",
    "get_sinopac_instrument_provider",
    "get_sinopac_ws_client",
]
