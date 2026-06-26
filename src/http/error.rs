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

//! HTTP error types for the Sinopac adapter.

use thiserror::Error;

/// HTTP error types for the Sinopac gateway.
#[derive(Debug, Clone, Error)]
pub enum SinopacHttpError {
    /// HTTP network request failed.
    #[error("HTTP request failed: {0}")]
    NetworkError(String),
    /// JSON deserialization failed.
    #[error("JSON deserialization failed: {0}")]
    JsonError(String),
    /// Gateway returned an error response.
    #[error("Gateway error ({status}): {body}")]
    GatewayError {
        /// The HTTP status code.
        status: u16,
        /// The response body.
        body: String,
    },
    /// Gateway is not connected.
    #[error("Gateway not connected")]
    NotConnected,
}

impl From<serde_json::Error> for SinopacHttpError {
    fn from(e: serde_json::Error) -> Self {
        Self::JsonError(e.to_string())
    }
}

impl From<nautilus_network::http::HttpClientError> for SinopacHttpError {
    fn from(e: nautilus_network::http::HttpClientError) -> Self {
        Self::NetworkError(e.to_string())
    }
}
