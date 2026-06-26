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

import asyncio
from typing import Any

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import instruments_from_pyo3

from sinopac_nt import _sinopac


class SinopacInstrumentProvider(InstrumentProvider):
    """
    Provides Nautilus instrument definitions from the Sinopac (SinoPac) gateway.

    Load stocks, futures, and options contracts via the Rust HTTP client
    and convert them to Nautilus instrument types.

    Parameters
    ----------
    client : _sinopac.SinopacHttpClient
        The Sinopac gateway HTTP client.
    config : InstrumentProviderConfig, optional
        The instrument provider configuration, by default None.

    """

    def __init__(
        self,
        client: _sinopac.SinopacHttpClient,
        config: InstrumentProviderConfig | None = None,
    ) -> None:
        super().__init__(config=config)
        self._client = client

        self._instruments_pyo3: list[Any] = []

    def instruments_pyo3(self) -> list[Any]:
        """
        Return the raw pyo3 instruments for passing to the Rust client.

        Returns
        -------
        list[Instrument]

        """
        return self._instruments_pyo3

    async def _fetch_all_instruments(self) -> list[Any]:
        results = await asyncio.gather(
            self._client.request_stock_instruments(),
            self._client.request_futures_instruments(),
            self._client.request_options_instruments(),
            return_exceptions=True,
        )

        all_instruments: list[Any] = []
        labels = ("stocks", "futures", "options")
        for label, result in zip(labels, results, strict=False):
            if isinstance(result, BaseException):
                self._log.error(f"Failed to load {label}: {result}")
            else:
                all_instruments.extend(result)
                self._log.info(f"Loaded {len(result)} {label} instruments")

        return all_instruments

    async def load_all_async(self, filters: dict | None = None) -> None:
        """
        Load all instruments from the Sinopac gateway.

        Parameters
        ----------
        filters : dict, optional
            Not implemented for Sinopac (all contracts are loaded).

        """
        all_pyo3_instruments = await self._fetch_all_instruments()
        self._instruments_pyo3 = all_pyo3_instruments

        instruments = instruments_from_pyo3(all_pyo3_instruments)
        for instrument in instruments:
            self.add(instrument=instrument)

        self._log.info(
            f"Total instruments loaded: {len(instruments)} ({self.count} registered)",
        )

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        """
        Load specific instruments by ID from Sinopac.

        Parameters
        ----------
        instrument_ids : list[InstrumentId]
            The instrument IDs to load.
        filters : dict, optional
            Not implemented for Sinopac.

        """
        if not instrument_ids:
            self._log.warning("No instrument IDs given for loading")
            return

        # Sinopac doesn't support per-instrument queries, load all and filter
        all_pyo3_instruments = await self._fetch_all_instruments()
        self._instruments_pyo3 = all_pyo3_instruments

        instruments = instruments_from_pyo3(all_pyo3_instruments)
        for instrument in instruments:
            if instrument.id not in instrument_ids:
                continue
            self.add(instrument=instrument)

        self._log.info(f"Loaded {len(self._instruments)} instruments from Sinopac")

    async def load_async(
        self,
        instrument_id: InstrumentId,
        filters: dict | None = None,
    ) -> None:
        """
        Load a single instrument by ID from Sinopac.

        Parameters
        ----------
        instrument_id : InstrumentId
            The instrument ID to load.
        filters : dict, optional
            Not implemented for Sinopac.

        """
        PyCondition.not_none(instrument_id, "instrument_id")
        await self.load_ids_async([instrument_id], filters)
