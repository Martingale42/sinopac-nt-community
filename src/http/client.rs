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

//! HTTP client for the Sinopac FastAPI gateway.

use std::collections::HashMap;

use nautilus_network::http::{HttpClient, Method};
use serde::{Serialize, de::DeserializeOwned};

use super::{
    error::SinopacHttpError,
    models::*,
    query::{KBarsQuery, PositionsQuery, SnapshotsQuery, TicksQuery},
};
use crate::common::{consts::SINOPAC_GATEWAY_HTTP_URL, urls::gateway_http_url};

/// HTTP client for communicating with the Sinopac FastAPI gateway.
#[derive(Clone, Debug)]
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(module = "nautilus_pyo3.sinopac", skip_from_py_object)
)]
pub struct SinopacHttpClient {
    base_url: String,
    client: HttpClient,
}

impl SinopacHttpClient {
    /// Creates a new [`SinopacHttpClient`].
    ///
    /// The `base_url` is the raw gateway URL (e.g. `http://localhost:8000`).
    /// The `/api` prefix is appended automatically via [`gateway_http_url`].
    pub fn new(base_url: Option<String>) -> Result<Self, SinopacHttpError> {
        let raw_base = base_url.unwrap_or_else(|| SINOPAC_GATEWAY_HTTP_URL.to_string());
        let base_url = gateway_http_url(&raw_base);
        let client = HttpClient::new(HashMap::new(), Vec::new(), Vec::new(), None, Some(30), None)?;
        Ok(Self { base_url, client })
    }

    /// Returns the base URL for the gateway.
    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    /// Sends a GET request and deserializes the JSON response.
    async fn get<T: DeserializeOwned>(&self, path: &str) -> Result<T, SinopacHttpError> {
        let url = format!("{}{path}", self.base_url);
        let response = self
            .client
            .request(Method::GET, url, None, None, None, None, None)
            .await?;

        if response.status.as_u16() >= 400 {
            let body = String::from_utf8_lossy(&response.body).to_string();
            return Err(SinopacHttpError::GatewayError {
                status: response.status.as_u16(),
                body,
            });
        }

        serde_json::from_slice(&response.body).map_err(SinopacHttpError::from)
    }

    /// Sends a GET request with query parameters and deserializes the JSON response.
    async fn get_with_params<T: DeserializeOwned, P: Serialize>(
        &self,
        path: &str,
        params: &P,
    ) -> Result<T, SinopacHttpError> {
        let url = format!("{}{path}", self.base_url);
        let response = self
            .client
            .request_with_params(Method::GET, url, Some(params), None, None, None, None)
            .await?;

        if response.status.as_u16() >= 400 {
            let body = String::from_utf8_lossy(&response.body).to_string();
            return Err(SinopacHttpError::GatewayError {
                status: response.status.as_u16(),
                body,
            });
        }

        serde_json::from_slice(&response.body).map_err(SinopacHttpError::from)
    }

    /// Sends an HTTP request with a JSON body and deserializes the JSON response.
    async fn send_json<T: DeserializeOwned, B: Serialize>(
        &self,
        method: Method,
        path: &str,
        body: &B,
    ) -> Result<T, SinopacHttpError> {
        let url = format!("{}{path}", self.base_url);
        let body_bytes = serde_json::to_vec(body)?;
        let mut headers = HashMap::new();
        headers.insert("Content-Type".to_string(), "application/json".to_string());
        let response = self
            .client
            .request(
                method,
                url,
                None,
                Some(headers),
                Some(body_bytes),
                None,
                None,
            )
            .await?;

        if response.status.as_u16() >= 400 {
            let body = String::from_utf8_lossy(&response.body).to_string();
            return Err(SinopacHttpError::GatewayError {
                status: response.status.as_u16(),
                body,
            });
        }

        serde_json::from_slice(&response.body).map_err(SinopacHttpError::from)
    }

    /// Sends a login request to the gateway.
    pub async fn login(&self, request: &LoginRequest) -> Result<LoginResponse, SinopacHttpError> {
        self.send_json(Method::POST, "/auth/login", request).await
    }

    /// Sends a logout request to the gateway.
    pub async fn logout(&self) -> Result<MessageResponse, SinopacHttpError> {
        self.send_json(
            Method::POST,
            "/auth/logout",
            &serde_json::Value::Object(Default::default()),
        )
        .await
    }

    /// Queries the gateway connection status.
    pub async fn status(&self) -> Result<StatusResponse, SinopacHttpError> {
        self.get("/auth/status").await
    }

    /// Fetches all stock contracts from the gateway.
    pub async fn list_stocks(&self) -> Result<Vec<StockContract>, SinopacHttpError> {
        self.get("/contracts/stocks").await
    }

    /// Fetches a single stock contract by code.
    pub async fn get_stock(&self, code: &str) -> Result<StockContract, SinopacHttpError> {
        self.get(&format!("/contracts/stocks/{code}")).await
    }

    /// Fetches all futures contracts from the gateway.
    pub async fn list_futures(&self) -> Result<Vec<FuturesContract>, SinopacHttpError> {
        self.get("/contracts/futures").await
    }

    /// Fetches all options contracts from the gateway.
    pub async fn list_options(&self) -> Result<Vec<OptionsContract>, SinopacHttpError> {
        self.get("/contracts/options").await
    }

    /// Fetches market snapshots for the given codes.
    pub async fn snapshots(
        &self,
        query: &SnapshotsQuery,
    ) -> Result<Vec<SnapshotData>, SinopacHttpError> {
        self.get_with_params("/market/snapshots", query).await
    }

    /// Fetches historical tick data.
    pub async fn ticks(&self, query: &TicksQuery) -> Result<TicksResponse, SinopacHttpError> {
        self.get_with_params("/market/ticks", query).await
    }

    /// Fetches historical OHLCV bar data.
    pub async fn kbars(&self, query: &KBarsQuery) -> Result<KBarsResponse, SinopacHttpError> {
        self.get_with_params("/market/kbars", query).await
    }

    /// Submits a new order to the gateway.
    pub async fn place_order(
        &self,
        request: &PlaceOrderRequest,
    ) -> Result<PlaceOrderResponse, SinopacHttpError> {
        self.send_json(Method::POST, "/orders/place", request).await
    }

    /// Modifies an existing order on the gateway.
    pub async fn update_order(
        &self,
        request: &UpdateOrderRequest,
    ) -> Result<TradeIdResponse, SinopacHttpError> {
        self.send_json(Method::PUT, "/orders/update", request).await
    }

    /// Cancels an order on the gateway.
    pub async fn cancel_order(
        &self,
        request: &CancelOrderRequest,
    ) -> Result<TradeIdResponse, SinopacHttpError> {
        self.send_json(Method::DELETE, "/orders/cancel", request)
            .await
    }

    /// Fetches all active trades from the gateway.
    pub async fn list_trades(&self) -> Result<Vec<TradeInfo>, SinopacHttpError> {
        self.get("/orders/trades").await
    }

    /// Fetches positions for the given market type.
    pub async fn list_positions(
        &self,
        query: &PositionsQuery,
    ) -> Result<Vec<Position>, SinopacHttpError> {
        self.get_with_params("/account/positions", query).await
    }

    /// Fetches the account balance from the gateway.
    pub async fn account_balance(&self) -> Result<AccountBalance, SinopacHttpError> {
        self.get("/account/balance").await
    }

    /// Fetches margin information from the gateway.
    pub async fn margin(&self) -> Result<MarginInfo, SinopacHttpError> {
        self.get("/account/margin").await
    }

    /// Fetches profit and loss records from the gateway.
    pub async fn list_profit_loss(&self) -> Result<Vec<ProfitLoss>, SinopacHttpError> {
        self.get("/account/pnl").await
    }
}
