// -------------------------------------------------------------------------------------------------
//  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
//  https://nautechsystems.io
//
//  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
//  You may not use this file except in compliance with the License.
//  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
//
//  Unless required by applicable law or agreed to in writing, software
//  distributed under the License is distributed on an "AS IS" BASIS,
//  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//  See the License for the specific language governing permissions and
//  limitations under the License.
// -------------------------------------------------------------------------------------------------

//! Python bindings for the Sinopac adapter.

pub mod http;
pub mod websocket;

use pyo3::prelude::*;

use crate::common::enums::*;

/// Loaded as `sinopac_nt._sinopac`.
#[pymodule]
pub fn _sinopac(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Constants
    m.add("SINOPAC", crate::common::consts::SINOPAC)?;

    // Enums
    m.add_class::<SinopacAction>()?;
    m.add_class::<SinopacPriceType>()?;
    m.add_class::<SinopacOrderType>()?;
    m.add_class::<SinopacOrderCond>()?;
    m.add_class::<SinopacOrderLot>()?;
    m.add_class::<SinopacOCType>()?;
    m.add_class::<SinopacQuoteType>()?;
    m.add_class::<SinopacMarket>()?;
    m.add_class::<SinopacExchange>()?;

    // Clients
    m.add_class::<crate::http::client::SinopacHttpClient>()?;
    m.add_class::<crate::websocket::client::SinopacWebSocketClient>()?;

    Ok(())
}
