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

//! WebSocket error types for the Sinopac adapter.

use thiserror::Error;

/// WebSocket error types for the Sinopac adapter.
#[derive(Debug, Clone, Error)]
pub enum SinopacWsError {
    /// WebSocket is not connected.
    #[error("WebSocket not connected")]
    NotConnected,
    /// WebSocket send operation failed.
    #[error("Send failed: {0}")]
    Send(String),
    /// WebSocket connection attempt failed.
    #[error("Connection failed: {0}")]
    Connection(String),
    /// JSON serialization or deserialization failed.
    #[error("JSON error: {0}")]
    Json(String),
}
