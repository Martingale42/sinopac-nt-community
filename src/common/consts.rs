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

//! Constants for the Sinopac adapter.

use std::sync::LazyLock;

use nautilus_model::identifiers::Venue;

/// The Sinopac venue string.
pub const SINOPAC: &str = "SINOPAC";
/// The Sinopac venue identifier.
pub static SINOPAC_VENUE: LazyLock<Venue> = LazyLock::new(|| Venue::new(SINOPAC));

/// Default Sinopac gateway HTTP URL.
pub const SINOPAC_GATEWAY_HTTP_URL: &str = "http://localhost:8000";
/// Default Sinopac gateway WebSocket URL.
pub const SINOPAC_GATEWAY_WS_URL: &str = "ws://localhost:8000/ws";
