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
Venue-specific order tags for the Sinopac (Shioaji) adapter.
"""

from typing import Final

from nautilus_trader.config import NautilusConfig


TAG_PREFIX: Final[str] = "SINOPAC:"


class SinopacOrderTags(NautilusConfig, frozen=True):
    """
    Sinopac (Shioaji) venue-specific order parameters, attached via ``order.tags``.

    Field names mirror the Shioaji SDK ``Order`` parameters verbatim (see the
    design appendix; ``IBOrderTags`` precedent). The tags are parsed by the
    Sinopac execution client, validated against Taiwan order rules, and passed
    through as strongly-typed gateway parameters. In backtest the tags are
    ignored (parity limitation documented in the adapter module).

    Parameters
    ----------
    order_lot : str, default "Common"
        The stock order lot. One of ``Common`` (round lot, 1000 shares) or
        ``IntradayOdd`` (intraday odd lot, 1-999 shares). The post-market
        ``Odd`` and ``Fixing`` lots are out of scope and rejected.
    order_cond : str, default "Cash"
        The stock order condition. One of ``Cash``, ``MarginTrading``, or
        ``ShortSelling``.
    daytrade_short : bool, default False
        If the stock order is a day-trade short (xian gu dang chong). Requires
        ``order_cond`` to be ``Cash``.
    octype : str, default "Auto"
        The futures/options open-close type. One of ``Auto``, ``New``,
        ``Cover``, or ``DayTrade``. Ignored for stock orders.

    """

    order_lot: str = "Common"
    order_cond: str = "Cash"
    daytrade_short: bool = False
    octype: str = "Auto"

    @property
    def value(self) -> str:
        """
        Return the encoded tag string for attaching to ``order.tags``.

        Returns
        -------
        str
            The prefixed JSON encoding, ``"SINOPAC:{json}"``.

        """
        return TAG_PREFIX + self.json().decode()

    @classmethod
    def from_tags(cls, tags: list[str] | None) -> "SinopacOrderTags":
        """
        Parse a ``SinopacOrderTags`` from a list of order tags.

        The first tag carrying the ``SINOPAC:`` prefix is decoded. Tags without
        the prefix are ignored. When no Sinopac tag is present, an instance with
        all default values is returned.

        Parameters
        ----------
        tags : list[str] | None
            The order tags to scan.

        Returns
        -------
        SinopacOrderTags

        Raises
        ------
        Exception
            If a ``SINOPAC:`` prefixed tag is present but malformed (propagated
            from the underlying decoder for the caller to reject the order).

        """
        for tag in tags or []:
            if tag.startswith(TAG_PREFIX):
                return cls.parse(tag[len(TAG_PREFIX) :].encode())
        return cls()
