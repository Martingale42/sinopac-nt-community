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

//! Python bindings for the Sinopac WebSocket client.

use std::{collections::HashMap, panic::AssertUnwindSafe, sync::Arc};

use nautilus_common::live::get_runtime;
use nautilus_core::{UnixNanos, python::to_pyruntime_err};
use nautilus_model::{
    data::{Data, OrderBookDeltas_API},
    instruments::Instrument,
    python::{data::data_to_pycapsule, instruments::pyobject_to_instrument_any},
};
use pyo3::{prelude::*, types::PyDict};

use crate::{
    common::enums::SinopacQuoteType,
    websocket::{
        client::{
            BIDASK_EMIT_DELTAS, BIDASK_EMIT_DEPTH, BIDASK_EMIT_QUOTE, SinopacWebSocketClient,
        },
        messages::WsIncomingMsg,
        order_parse::order_event_to_pydict,
        parse::{
            parse_taiwan_timestamp, parse_ws_bidask_to_order_book_deltas,
            parse_ws_bidask_to_order_book_depth10, parse_ws_bidask_to_quote_tick,
            parse_ws_tick_to_trade_tick,
        },
    },
};

#[pymethods]
impl SinopacWebSocketClient {
    /// Creates a new Sinopac WebSocket client.
    #[new]
    #[pyo3(signature = (url=None))]
    fn py_new(url: Option<String>) -> Self {
        Self::new(url)
    }

    /// Returns whether the client is currently connected.
    #[pyo3(name = "is_connected")]
    fn py_is_connected(&self) -> bool {
        self.is_connected()
    }

    /// Subscribes to quote data for a contract.
    #[pyo3(name = "subscribe")]
    fn py_subscribe<'py>(
        &self,
        py: Python<'py>,
        code: String,
        quote_type: SinopacQuoteType,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client
                .subscribe(&code, quote_type)
                .await
                .map_err(to_pyruntime_err)
        })
    }

    /// Unsubscribes from quote data for a contract.
    #[pyo3(name = "unsubscribe")]
    fn py_unsubscribe<'py>(
        &self,
        py: Python<'py>,
        code: String,
        quote_type: SinopacQuoteType,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client
                .unsubscribe(&code, quote_type)
                .await
                .map_err(to_pyruntime_err)
        })
    }

    /// Sets which data types to emit from BidAsk messages for a contract.
    #[pyo3(name = "set_bidask_outputs")]
    fn py_set_bidask_outputs(&self, code: &str, quote: bool, depth: bool, deltas: bool) {
        let mut flags = 0u8;
        if quote {
            flags |= BIDASK_EMIT_QUOTE;
        }
        if depth {
            flags |= BIDASK_EMIT_DEPTH;
        }
        if deltas {
            flags |= BIDASK_EMIT_DELTAS;
        }
        self.set_bidask_emit_for(code, flags);
    }

    /// Connects to the gateway WS and starts the message processing loop.
    ///
    /// `instruments` — list of pyo3 InstrumentAny objects (for ID/precision lookup)
    /// `callback` — Python callable invoked with each parsed data PyCapsule
    #[pyo3(name = "connect")]
    fn py_connect<'py>(
        &self,
        py: Python<'py>,
        instruments: Vec<Py<PyAny>>,
        callback: Py<PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        // Build instrument lookup: code -> InstrumentAny
        let mut instrument_map = HashMap::new();
        for inst_obj in instruments {
            let inst_any = pyobject_to_instrument_any(py, inst_obj)?;
            let code = inst_any.id().symbol.as_str().to_string();
            instrument_map.insert(code, inst_any);
        }
        let instruments = Arc::new(instrument_map);

        let client = self.clone();

        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            // Establish WS connection (creates channels + spawns handler)
            client.connect().await.map_err(to_pyruntime_err)?;

            // Take msg_rx out of client — move it into the callback task
            let msg_rx = client.take_msg_rx();

            // Spawn background message processing task
            get_runtime().spawn(async move {
                // Keep client alive for the entire task lifetime
                let _client_guard = client;

                if let Some(mut rx) = msg_rx {
                    log::info!("Sinopac WS callback loop started");
                    let mut msg_count: u64 = 0;

                    while let Some(msg) = rx.recv().await {
                        msg_count += 1;
                        // Defense-in-depth behind the Result-based parsers (Tasks 2.1-2.3):
                        // a residual panic in message processing must not kill the loop and
                        // silently stop all subsequent market-data and order events.
                        let result = std::panic::catch_unwind(AssertUnwindSafe(|| match msg {
                            WsIncomingMsg::Tick(ref tick_msg) => {
                                let Some(inst) = instruments.get(&tick_msg.code) else {
                                    log::debug!("Tick for unknown code: {}", tick_msg.code);
                                    return;
                                };
                                let ts_event =
                                    match parse_taiwan_timestamp(&tick_msg.data.timestamp) {
                                        Ok(ts) => ts,
                                        Err(e) => {
                                            log::warn!(
                                                "Bad timestamp for tick {}: {e}",
                                                tick_msg.code
                                            );
                                            return;
                                        }
                                    };
                                let ts_init = UnixNanos::default();

                                match parse_ws_tick_to_trade_tick(
                                    tick_msg,
                                    inst.id(),
                                    inst.price_precision(),
                                    inst.size_precision(),
                                    ts_event,
                                    ts_init,
                                ) {
                                    Ok(trade) => Python::attach(|py| {
                                        let capsule = data_to_pycapsule(py, Data::Trade(trade));
                                        call_python(py, &callback, capsule);
                                    }),
                                    Err(e) => {
                                        log::warn!(
                                            "Failed to parse tick for {}: {e}",
                                            tick_msg.code
                                        );
                                    }
                                }
                            }
                            WsIncomingMsg::BidAsk(ref ba_msg) => {
                                let Some(inst) = instruments.get(&ba_msg.code) else {
                                    log::debug!("BidAsk for unknown code: {}", ba_msg.code);
                                    return;
                                };
                                let ts_event = match parse_taiwan_timestamp(&ba_msg.data.timestamp)
                                {
                                    Ok(ts) => ts,
                                    Err(e) => {
                                        log::warn!("Bad timestamp for bidask {}: {e}", ba_msg.code);
                                        return;
                                    }
                                };
                                let ts_init = UnixNanos::default();
                                let emit = _client_guard.bidask_emit_for(&ba_msg.code);
                                if emit == 0 {
                                    return;
                                }

                                let id = inst.id();
                                let pp = inst.price_precision();
                                let sp = inst.size_precision();

                                Python::attach(|py| {
                                    if emit & BIDASK_EMIT_QUOTE != 0 {
                                        match parse_ws_bidask_to_quote_tick(
                                            ba_msg, id, pp, sp, ts_event, ts_init,
                                        ) {
                                            Ok(quote) => {
                                                let c = data_to_pycapsule(py, Data::Quote(quote));
                                                call_python(py, &callback, c);
                                            }
                                            Err(e) => log::warn!(
                                                "Failed to parse bidask quote for {}: {e}",
                                                ba_msg.code
                                            ),
                                        }
                                    }

                                    if emit & BIDASK_EMIT_DEPTH != 0 {
                                        match parse_ws_bidask_to_order_book_depth10(
                                            ba_msg, id, pp, sp, ts_event, ts_init,
                                        ) {
                                            Ok(depth) => {
                                                let c = data_to_pycapsule(
                                                    py,
                                                    Data::Depth10(Box::new(depth)),
                                                );
                                                call_python(py, &callback, c);
                                            }
                                            Err(e) => log::warn!(
                                                "Failed to parse bidask depth for {}: {e}",
                                                ba_msg.code
                                            ),
                                        }
                                    }

                                    if emit & BIDASK_EMIT_DELTAS != 0 {
                                        match parse_ws_bidask_to_order_book_deltas(
                                            ba_msg, id, pp, sp, ts_event, ts_init,
                                        ) {
                                            Ok(deltas) => {
                                                let c = data_to_pycapsule(
                                                    py,
                                                    Data::Deltas(OrderBookDeltas_API::new(deltas)),
                                                );
                                                call_python(py, &callback, c);
                                            }
                                            Err(e) => log::warn!(
                                                "Failed to parse bidask deltas for {}: {e}",
                                                ba_msg.code
                                            ),
                                        }
                                    }
                                });
                            }
                            WsIncomingMsg::OrderUpdate(ref order_msg) => {
                                match order_msg.parse_event() {
                                    Ok(event) => {
                                        Python::attach(|py| {
                                            match order_event_to_pydict(py, &event) {
                                                Ok(dict) => {
                                                    call_python(py, &callback, dict.into_any());
                                                }
                                                Err(e) => {
                                                    log::error!(
                                                        "Failed to convert order event to dict: {e}"
                                                    );
                                                }
                                            }
                                        });
                                    }
                                    Err(e) => {
                                        log::warn!(
                                            "Failed to parse order event '{}': {e}",
                                            order_msg.event
                                        );
                                    }
                                }
                            }
                            WsIncomingMsg::Subscribed(ref confirm) => {
                                log::info!("Subscribed: {} ({})", confirm.code, confirm.quote_type);
                            }
                            WsIncomingMsg::Unsubscribed(ref confirm) => {
                                log::info!(
                                    "Unsubscribed: {} ({})",
                                    confirm.code,
                                    confirm.quote_type
                                );
                            }
                            WsIncomingMsg::Error(ref e) => {
                                log::error!("WS error: {}", e.detail);
                            }
                            WsIncomingMsg::Reconnected => {
                                Python::attach(|py| {
                                    let dict = PyDict::new(py);
                                    match dict.set_item("event", "reconnected") {
                                        Ok(()) => {
                                            call_python(py, &callback, dict.into_any().unbind());
                                        }
                                        Err(e) => log::error!(
                                            "Failed to build reconnected event dict: {e}"
                                        ),
                                    }
                                });
                            }
                        }));

                        if result.is_err() {
                            log::error!("Sinopac WS message processing panicked, continuing loop");
                        }
                    }

                    log::warn!("Sinopac WS callback loop ended after {msg_count} messages");
                }
            });

            Ok(())
        })
    }

    /// Disconnects from the gateway WS.
    #[pyo3(name = "disconnect")]
    fn py_disconnect<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client.disconnect().await.map_err(to_pyruntime_err)?;
            Ok(())
        })
    }

    /// Waits until the WS connection is active.
    #[pyo3(name = "wait_until_active")]
    fn py_wait_until_active<'py>(
        &self,
        py: Python<'py>,
        timeout_secs: f64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let start = std::time::Instant::now();
            let timeout = std::time::Duration::from_secs_f64(timeout_secs);
            while !client.is_connected() {
                if start.elapsed() > timeout {
                    return Err(pyo3::exceptions::PyTimeoutError::new_err(format!(
                        "WS connection timeout after {timeout_secs}s"
                    )));
                }
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            }
            Ok(())
        })
    }
}

fn call_python(py: Python, callback: &Py<PyAny>, py_obj: Py<PyAny>) {
    if let Err(e) = callback.call1(py, (py_obj,)) {
        log::error!("Error calling Python callback: {e}");
    }
}
