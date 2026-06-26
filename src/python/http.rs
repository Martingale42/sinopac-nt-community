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

//! Python bindings for the Sinopac HTTP client.

use std::str::FromStr;

use nautilus_core::{UnixNanos, python::to_pyruntime_err};
use nautilus_model::{data::BarType, python::instruments::instrument_any_to_pyobject};
use pyo3::{
    conversion::IntoPyObjectExt,
    prelude::*,
    types::{PyDict, PyList},
};

use crate::{
    common::{
        enums::{
            SinopacAction, SinopacMarket, SinopacOCType, SinopacOrderCond, SinopacOrderLot,
            SinopacOrderType, SinopacPriceType,
        },
        parse::parse_instrument_id,
    },
    http::{
        client::SinopacHttpClient,
        models::{CancelOrderRequest, LoginRequest, PlaceOrderRequest, UpdateOrderRequest},
        parse::{
            parse_futures_to_contract, parse_kbars_response, parse_options_to_contract,
            parse_stock_to_equity, parse_ticks_response,
        },
        query::{KBarsQuery, PositionsQuery, TicksQuery},
    },
};

#[pymethods]
impl SinopacHttpClient {
    /// Creates a new Sinopac HTTP client.
    #[new]
    #[pyo3(signature = (base_url=None))]
    fn py_new(base_url: Option<String>) -> PyResult<Self> {
        Self::new(base_url).map_err(to_pyruntime_err)
    }

    /// Returns the base URL of the gateway.
    #[getter]
    #[pyo3(name = "base_url")]
    fn py_base_url(&self) -> &str {
        self.base_url()
    }

    /// Queries the gateway connection status.
    #[pyo3(name = "status")]
    fn py_status<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let status = client.status().await.map_err(to_pyruntime_err)?;
            Ok((status.connected, status.simulation))
        })
    }

    /// Sends a login request to the gateway.
    #[pyo3(name = "login")]
    #[pyo3(signature = (api_key, secret_key, ca_path=None, ca_passwd=None, simulation=false))]
    fn py_login<'py>(
        &self,
        py: Python<'py>,
        api_key: String,
        secret_key: String,
        ca_path: Option<String>,
        ca_passwd: Option<String>,
        simulation: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let request = LoginRequest {
                api_key,
                secret_key,
                ca_path,
                ca_passwd,
                simulation,
            };
            let response = client.login(&request).await.map_err(to_pyruntime_err)?;
            let accounts: Vec<(String, String)> = response
                .accounts
                .into_iter()
                .map(|a| (a.account_type, a.account_id))
                .collect();
            Ok(accounts)
        })
    }

    /// Sends a logout request to the gateway.
    #[pyo3(name = "logout")]
    fn py_logout<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client.logout().await.map_err(to_pyruntime_err)?;
            Ok(())
        })
    }

    /// Fetches all stock contracts and return as Nautilus Equity instruments.
    #[pyo3(name = "request_stock_instruments")]
    fn py_request_stock_instruments<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let contracts = client.list_stocks().await.map_err(to_pyruntime_err)?;
            let ts = nautilus_core::UnixNanos::default();
            let instruments: Vec<_> = contracts
                .iter()
                .filter_map(|c| parse_stock_to_equity(c, ts, ts).ok())
                .collect();
            Python::attach(|py| {
                let py_instruments: PyResult<Vec<_>> = instruments
                    .into_iter()
                    .map(|inst| instrument_any_to_pyobject(py, inst))
                    .collect();
                let pylist = PyList::new(py, py_instruments?)
                    .unwrap()
                    .into_any()
                    .unbind();
                Ok(pylist)
            })
        })
    }

    /// Fetches all futures contracts and return as Nautilus FuturesContract instruments.
    #[pyo3(name = "request_futures_instruments")]
    fn py_request_futures_instruments<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let contracts = client.list_futures().await.map_err(to_pyruntime_err)?;
            let ts = nautilus_core::UnixNanos::default();
            let instruments: Vec<_> = contracts
                .iter()
                .filter_map(|c| parse_futures_to_contract(c, ts, ts).ok())
                .collect();
            Python::attach(|py| {
                let py_instruments: PyResult<Vec<_>> = instruments
                    .into_iter()
                    .map(|inst| instrument_any_to_pyobject(py, inst))
                    .collect();
                let pylist = PyList::new(py, py_instruments?)
                    .unwrap()
                    .into_any()
                    .unbind();
                Ok(pylist)
            })
        })
    }

    /// Fetches all options contracts and return as Nautilus OptionContract instruments.
    #[pyo3(name = "request_options_instruments")]
    fn py_request_options_instruments<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let contracts = client.list_options().await.map_err(to_pyruntime_err)?;
            let ts = nautilus_core::UnixNanos::default();
            let instruments: Vec<_> = contracts
                .iter()
                .filter_map(|c| parse_options_to_contract(c, ts, ts).ok())
                .collect();
            Python::attach(|py| {
                let py_instruments: PyResult<Vec<_>> = instruments
                    .into_iter()
                    .map(|inst| instrument_any_to_pyobject(py, inst))
                    .collect();
                let pylist = PyList::new(py, py_instruments?)
                    .unwrap()
                    .into_any()
                    .unbind();
                Ok(pylist)
            })
        })
    }

    /// Fetches historical ticks for a contract on a given date.
    ///
    /// Returns a list of TradeTick pyo3 objects.
    #[pyo3(name = "request_trade_ticks")]
    #[pyo3(signature = (code, date, price_precision, size_precision, market=None))]
    fn py_request_trade_ticks<'py>(
        &self,
        py: Python<'py>,
        code: String,
        date: String,
        price_precision: u8,
        size_precision: u8,
        market: Option<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let query = TicksQuery {
                code: code.clone(),
                date,
                market,
            };
            let response = client.ticks(&query).await.map_err(to_pyruntime_err)?;
            let instrument_id = parse_instrument_id(&code).map_err(to_pyruntime_err)?;
            let ts_init = UnixNanos::default();

            let trades = parse_ticks_response(
                &response,
                instrument_id,
                price_precision,
                size_precision,
                ts_init,
            )
            .map_err(to_pyruntime_err)?;

            Python::attach(|py| {
                let py_trades: PyResult<Vec<Py<PyAny>>> =
                    trades.into_iter().map(|t| t.into_py_any(py)).collect();
                let pylist = PyList::new(py, py_trades?).unwrap().into_any().unbind();
                Ok(pylist)
            })
        })
    }

    /// Fetches historical OHLCV bars for a contract in a date range.
    ///
    /// Returns a list of Bar pyo3 objects.
    #[pyo3(name = "request_bars")]
    #[pyo3(signature = (code, start, end, bar_type, price_precision, size_precision, market=None))]
    #[allow(clippy::too_many_arguments)]
    fn py_request_bars<'py>(
        &self,
        py: Python<'py>,
        code: String,
        start: String,
        end: String,
        bar_type: String,
        price_precision: u8,
        size_precision: u8,
        market: Option<String>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let query = KBarsQuery {
                code,
                start,
                end,
                market,
            };
            let response = client.kbars(&query).await.map_err(to_pyruntime_err)?;
            let bar_type = BarType::from_str(&bar_type).map_err(to_pyruntime_err)?;
            let ts_init = UnixNanos::default();

            let bars = parse_kbars_response(
                &response,
                bar_type,
                price_precision,
                size_precision,
                ts_init,
            )
            .map_err(to_pyruntime_err)?;

            Python::attach(|py| {
                let py_bars: PyResult<Vec<Py<PyAny>>> =
                    bars.into_iter().map(|b| b.into_py_any(py)).collect();
                let pylist = PyList::new(py, py_bars?).unwrap().into_any().unbind();
                Ok(pylist)
            })
        })
    }

    /// Places an order via the gateway.
    #[pyo3(name = "place_order")]
    #[pyo3(signature = (code, action, price, quantity, price_type=SinopacPriceType::LMT, order_type=SinopacOrderType::ROD, order_cond=SinopacOrderCond::Cash, order_lot=SinopacOrderLot::Common, market=SinopacMarket::Stock, custom_field=None, octype=SinopacOCType::Auto, daytrade_short=false))]
    #[allow(clippy::too_many_arguments)]
    fn py_place_order<'py>(
        &self,
        py: Python<'py>,
        code: String,
        action: SinopacAction,
        price: f64,
        quantity: i64,
        price_type: SinopacPriceType,
        order_type: SinopacOrderType,
        order_cond: SinopacOrderCond,
        order_lot: SinopacOrderLot,
        market: SinopacMarket,
        custom_field: Option<String>,
        octype: SinopacOCType,
        daytrade_short: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        let request = PlaceOrderRequest {
            code,
            action,
            price,
            quantity,
            price_type,
            order_type,
            order_cond,
            order_lot,
            octype,
            daytrade_short,
            market,
            custom_field,
        };
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let resp = client
                .place_order(&request)
                .await
                .map_err(to_pyruntime_err)?;
            Python::attach(|py| {
                let dict = PyDict::new(py);
                dict.set_item("trade_id", resp.trade_id)?;
                dict.set_item("code", resp.code)?;
                dict.set_item("action", resp.action)?;
                dict.set_item("status", resp.status)?;
                Ok(dict.unbind())
            })
        })
    }

    /// Updates (modifies) an existing order.
    #[pyo3(name = "update_order")]
    #[pyo3(signature = (trade_id, price=None, quantity=None))]
    fn py_update_order<'py>(
        &self,
        py: Python<'py>,
        trade_id: String,
        price: Option<f64>,
        quantity: Option<i64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        let request = UpdateOrderRequest {
            trade_id,
            price,
            quantity,
        };
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let resp = client
                .update_order(&request)
                .await
                .map_err(to_pyruntime_err)?;
            Python::attach(|py| {
                let dict = PyDict::new(py);
                dict.set_item("status", resp.status)?;
                dict.set_item("trade_id", resp.trade_id)?;
                Ok(dict.unbind())
            })
        })
    }

    /// Cancels an existing order.
    #[pyo3(name = "cancel_order")]
    fn py_cancel_order<'py>(
        &self,
        py: Python<'py>,
        trade_id: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        let request = CancelOrderRequest { trade_id };
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let resp = client
                .cancel_order(&request)
                .await
                .map_err(to_pyruntime_err)?;
            Python::attach(|py| {
                let dict = PyDict::new(py);
                dict.set_item("status", resp.status)?;
                dict.set_item("trade_id", resp.trade_id)?;
                Ok(dict.unbind())
            })
        })
    }

    /// Lists all trades (orders) from the gateway.
    #[pyo3(name = "list_trades")]
    fn py_list_trades<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let trades = client.list_trades().await.map_err(to_pyruntime_err)?;
            Python::attach(|py| {
                let list = PyList::empty(py);
                for t in &trades {
                    let dict = PyDict::new(py);
                    dict.set_item("trade_id", &t.trade_id)?;
                    dict.set_item("code", &t.code)?;
                    dict.set_item("action", &t.action)?;
                    dict.set_item("price", t.price)?;
                    dict.set_item("quantity", t.quantity)?;
                    dict.set_item("status", &t.status)?;
                    dict.set_item("order_type", &t.order_type)?;
                    dict.set_item("price_type", &t.price_type)?;
                    dict.set_item("filled_qty", t.filled_qty)?;
                    dict.set_item("avg_fill_price", t.avg_fill_price)?;
                    // `custom_field` is `Option<String>`; maps `None` -> Python `None`.
                    dict.set_item("custom_field", t.custom_field.as_deref())?;
                    list.append(dict)?;
                }
                Ok(list.unbind())
            })
        })
    }

    /// Returns the account positions.
    #[pyo3(name = "list_positions")]
    #[pyo3(signature = (market="stock"))]
    fn py_list_positions<'py>(&self, py: Python<'py>, market: &str) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        let query = PositionsQuery {
            market: Some(market.to_string()),
        };
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let positions = client
                .list_positions(&query)
                .await
                .map_err(to_pyruntime_err)?;
            Python::attach(|py| {
                let list = PyList::empty(py);
                for p in &positions {
                    let dict = PyDict::new(py);
                    dict.set_item("code", &p.code)?;
                    dict.set_item("direction", &p.direction)?;
                    dict.set_item("quantity", p.quantity)?;
                    dict.set_item("price", p.price)?;
                    dict.set_item("last_price", p.last_price)?;
                    dict.set_item("pnl", p.pnl)?;
                    dict.set_item("yd_quantity", p.yd_quantity)?;
                    list.append(dict)?;
                }
                Ok(list.unbind())
            })
        })
    }

    /// Returns the account balance.
    #[pyo3(name = "account_balance")]
    fn py_account_balance<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let balance = client.account_balance().await.map_err(to_pyruntime_err)?;
            Python::attach(|py| {
                let dict = PyDict::new(py);
                dict.set_item("date", balance.date)?;
                dict.set_item("balance", balance.balance)?;
                Ok(dict.unbind())
            })
        })
    }

    /// Returns margin info.
    #[pyo3(name = "margin")]
    fn py_margin<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let margin = client.margin().await.map_err(to_pyruntime_err)?;
            Python::attach(|py| {
                let dict = PyDict::new(py);
                dict.set_item("yesterday_balance", margin.yesterday_balance)?;
                dict.set_item("today_balance", margin.today_balance)?;
                dict.set_item("available_margin", margin.available_margin)?;
                dict.set_item("risk_indicator", margin.risk_indicator)?;
                Ok(dict.unbind())
            })
        })
    }
}
