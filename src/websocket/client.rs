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

//! WebSocket client for Sinopac gateway streaming data.

use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU8, Ordering},
    },
    time::Duration,
};

use arc_swap::ArcSwap;
use dashmap::DashMap;
use nautilus_common::live::get_runtime;
use nautilus_network::{
    mode::ConnectionMode,
    websocket::{WebSocketClient, WebSocketConfig, channel_message_handler},
};
use tokio::sync::mpsc;

use super::{
    error::SinopacWsError,
    handler::{FeedHandler, HandlerCommand},
    messages::WsIncomingMsg,
};
use crate::common::{consts::SINOPAC_GATEWAY_WS_URL, enums::SinopacQuoteType};

/// Emit `QuoteTick` from BidAsk messages.
pub(crate) const BIDASK_EMIT_QUOTE: u8 = 0b001;

/// Emit `OrderBookDepth10` from BidAsk messages.
pub(crate) const BIDASK_EMIT_DEPTH: u8 = 0b010;

/// Emit `OrderBookDeltas` from BidAsk messages.
pub(crate) const BIDASK_EMIT_DELTAS: u8 = 0b100;

/// WebSocket client for streaming market data and order updates
/// from the Sinopac FastAPI gateway.
///
/// Wraps `nautilus_network::WebSocketClient` for automatic reconnection
/// support. Uses interior mutability for connection state so that
/// PyO3 `#[pymethods]` (which receive `&self`) can connect/disconnect.
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(module = "nautilus_pyo3.sinopac", skip_from_py_object)
)]
pub struct SinopacWebSocketClient {
    url: String,
    connection_mode: Arc<ArcSwap<AtomicU8>>,
    cmd_tx: Arc<tokio::sync::RwLock<mpsc::UnboundedSender<HandlerCommand>>>,
    out_rx: Arc<Mutex<Option<mpsc::UnboundedReceiver<WsIncomingMsg>>>>,
    signal: Arc<AtomicBool>,
    task_handle: Arc<Mutex<Option<tokio::task::JoinHandle<()>>>>,
    bidask_emit_flags: Arc<DashMap<String, u8>>,
}

impl Clone for SinopacWebSocketClient {
    fn clone(&self) -> Self {
        Self {
            url: self.url.clone(),
            connection_mode: Arc::clone(&self.connection_mode),
            cmd_tx: Arc::clone(&self.cmd_tx),
            out_rx: Arc::clone(&self.out_rx),
            signal: Arc::clone(&self.signal),
            task_handle: Arc::clone(&self.task_handle),
            bidask_emit_flags: Arc::clone(&self.bidask_emit_flags),
        }
    }
}

impl SinopacWebSocketClient {
    /// Creates a new [`SinopacWebSocketClient`].
    #[must_use]
    pub fn new(url: Option<String>) -> Self {
        let url = url.unwrap_or_else(|| SINOPAC_GATEWAY_WS_URL.to_string());

        // Placeholder channel — receiver is immediately dropped.
        // connect() swaps in the real channel.
        let (cmd_tx, _cmd_rx) = mpsc::unbounded_channel::<HandlerCommand>();

        let initial_mode = AtomicU8::new(ConnectionMode::Closed.as_u8());
        let connection_mode = Arc::new(ArcSwap::from_pointee(initial_mode));

        Self {
            url,
            connection_mode,
            cmd_tx: Arc::new(tokio::sync::RwLock::new(cmd_tx)),
            out_rx: Arc::new(Mutex::new(None)),
            signal: Arc::new(AtomicBool::new(false)),
            task_handle: Arc::new(Mutex::new(None)),
            bidask_emit_flags: Arc::new(DashMap::new()),
        }
    }

    /// Returns the WS URL.
    #[must_use]
    pub fn url(&self) -> &str {
        &self.url
    }

    /// Connects to the gateway WebSocket endpoint.
    ///
    /// Creates a `nautilus_network::WebSocketClient` in handler mode with
    /// automatic reconnection, spawns a feed handler task that deserializes
    /// raw frames into `WsIncomingMsg`, and re-subscribes after reconnections.
    pub async fn connect(&self) -> Result<(), SinopacWsError> {
        if self.is_connected() {
            return Ok(());
        }

        self.signal.store(false, Ordering::Relaxed);

        log::info!("Connecting to WebSocket: {}", self.url);

        let (raw_handler, raw_rx) = channel_message_handler();

        let config = WebSocketConfig {
            url: self.url.clone(),
            headers: vec![],
            heartbeat: Some(30),
            heartbeat_msg: None,
            reconnect_timeout_ms: Some(5_000),
            reconnect_delay_initial_ms: Some(500),
            reconnect_delay_max_ms: Some(5_000),
            reconnect_backoff_factor: Some(1.5),
            reconnect_jitter_ms: Some(250),
            reconnect_max_attempts: None,
            idle_timeout_ms: None,
        };

        let client = WebSocketClient::connect(
            config,
            Some(raw_handler),
            None,
            None, // Reconnection re-subscribe handled by feed handler
            vec![],
            None,
        )
        .await
        .map_err(|e| SinopacWsError::Connection(e.to_string()))?;

        // Store connection mode (lock-free via ArcSwap)
        self.connection_mode.store(client.connection_mode_atomic());

        // Create output channel for parsed messages
        let (out_tx, out_rx) = mpsc::unbounded_channel();
        *self.out_rx.lock().unwrap() = Some(out_rx);

        // Create command channel and update cmd_tx
        let (cmd_tx, cmd_rx) = mpsc::unbounded_channel::<HandlerCommand>();
        *self.cmd_tx.write().await = cmd_tx;

        // Send the client to the handler
        self.cmd_tx
            .read()
            .await
            .send(HandlerCommand::SetClient(client))
            .map_err(|e| SinopacWsError::Send(e.to_string()))?;

        // Create and spawn the feed handler
        let signal = Arc::clone(&self.signal);
        let handler = FeedHandler::new(signal, cmd_rx, raw_rx);

        let handle = get_runtime().spawn(async move {
            let mut handler = handler;
            while let Some(msg) = handler.next().await {
                if out_tx.send(msg).is_err() {
                    log::debug!("Message receiver dropped, stopping handler");
                    break;
                }
            }
            log::debug!("Handler task exiting");
        });

        *self.task_handle.lock().unwrap() = Some(handle);

        log::debug!("WebSocket connected");
        Ok(())
    }

    /// Disconnects from the gateway.
    pub async fn disconnect(&self) -> Result<(), SinopacWsError> {
        self.signal.store(true, Ordering::Relaxed);

        if let Err(e) = self.cmd_tx.read().await.send(HandlerCommand::Disconnect) {
            log::debug!("Failed to send disconnect command: {e}");
        }

        let handle = self.task_handle.lock().unwrap().take();
        if let Some(handle) = handle {
            match tokio::time::timeout(Duration::from_secs(2), handle).await {
                Ok(Ok(())) => log::debug!("Handler task completed"),
                Ok(Err(e)) => log::debug!("Handler task error: {e}"),
                Err(_) => log::debug!("Handler task timed out, aborting"),
            }
        }

        *self.out_rx.lock().unwrap() = None;

        log::debug!("WebSocket disconnected");
        Ok(())
    }

    /// Returns the BidAsk emit flags for a contract code, or 0 if none set.
    pub fn bidask_emit_for(&self, code: &str) -> u8 {
        self.bidask_emit_flags.get(code).map_or(0, |v| *v)
    }

    /// Sets or removes BidAsk emit flags for a contract code.
    pub fn set_bidask_emit_for(&self, code: &str, flags: u8) {
        if flags == 0 {
            self.bidask_emit_flags.remove(code);
        } else {
            self.bidask_emit_flags.insert(code.to_string(), flags);
        }
    }

    /// Returns whether the client is currently connected.
    pub fn is_connected(&self) -> bool {
        let mode_ref = self.connection_mode.load();
        ConnectionMode::from_atomic(&mode_ref).is_active()
    }

    /// Subscribes to quote data for a contract.
    pub async fn subscribe(
        &self,
        code: &str,
        quote_type: SinopacQuoteType,
    ) -> Result<(), SinopacWsError> {
        self.cmd_tx
            .read()
            .await
            .send(HandlerCommand::Subscribe {
                code: code.to_string(),
                quote_type,
            })
            .map_err(|e| SinopacWsError::Send(e.to_string()))
    }

    /// Unsubscribes from quote data for a contract.
    pub async fn unsubscribe(
        &self,
        code: &str,
        quote_type: SinopacQuoteType,
    ) -> Result<(), SinopacWsError> {
        self.cmd_tx
            .read()
            .await
            .send(HandlerCommand::Unsubscribe {
                code: code.to_string(),
                quote_type,
            })
            .map_err(|e| SinopacWsError::Send(e.to_string()))
    }

    /// Takes the message receiver out of the client.
    ///
    /// Returns the parsed message receiver for use by the PyO3 connect
    /// method to move into a spawned callback task. Returns `None` if
    /// already taken.
    pub fn take_msg_rx(&self) -> Option<mpsc::UnboundedReceiver<WsIncomingMsg>> {
        self.out_rx.lock().unwrap().take()
    }

    /// Reads the next parsed message from the WebSocket.
    pub async fn next_message(&self) -> Option<WsIncomingMsg> {
        let rx = {
            let mut guard = self.out_rx.lock().unwrap();
            guard.take()
        };
        let mut rx = rx?;
        let msg = rx.recv().await;
        self.out_rx.lock().unwrap().replace(rx);
        msg
    }
}
