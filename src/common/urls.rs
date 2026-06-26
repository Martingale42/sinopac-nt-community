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

//! URL resolution for the Sinopac gateway.

/// Returns the gateway HTTP base URL, with optional override.
pub fn gateway_http_url(base_url: &str) -> String {
    format!("{base_url}/api")
}

/// Returns the gateway WebSocket URL, with optional override.
pub fn gateway_ws_url(base_url: &str) -> String {
    if let Some(host) = base_url.strip_prefix("http://") {
        format!("ws://{host}/ws")
    } else if let Some(host) = base_url.strip_prefix("https://") {
        format!("wss://{host}/ws")
    } else {
        format!("{base_url}/ws")
    }
}
