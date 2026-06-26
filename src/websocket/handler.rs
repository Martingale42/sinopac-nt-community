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

//! WebSocket feed handler for the Sinopac adapter.

use std::{
    collections::HashSet,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};

use nautilus_network::websocket::WebSocketClient;
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::Message;

use super::messages::{WsIncomingMsg, WsSubscribeMsg};
use crate::common::enums::SinopacQuoteType;

/// Commands sent from the client to the feed handler.
#[derive(Debug)]
pub(crate) enum HandlerCommand {
    /// Sets the WebSocket client for the handler to own.
    SetClient(WebSocketClient),
    /// Subscribes to quote data for a contract.
    Subscribe {
        code: String,
        quote_type: SinopacQuoteType,
    },
    /// Unsubscribes from quote data for a contract.
    Unsubscribe {
        code: String,
        quote_type: SinopacQuoteType,
    },
    /// Disconnects the WebSocket client.
    Disconnect,
}

/// Feed handler that owns the WebSocket client and processes messages.
pub(super) struct FeedHandler {
    signal: Arc<AtomicBool>,
    client: Option<WebSocketClient>,
    cmd_rx: mpsc::UnboundedReceiver<HandlerCommand>,
    raw_rx: mpsc::UnboundedReceiver<Message>,
    subscriptions: HashSet<(String, SinopacQuoteType)>,
}

impl FeedHandler {
    /// Creates a new [`FeedHandler`] instance.
    pub(super) fn new(
        signal: Arc<AtomicBool>,
        cmd_rx: mpsc::UnboundedReceiver<HandlerCommand>,
        raw_rx: mpsc::UnboundedReceiver<Message>,
    ) -> Self {
        Self {
            signal,
            client: None,
            cmd_rx,
            raw_rx,
            subscriptions: HashSet::new(),
        }
    }

    /// Returns the next parsed incoming message, or `None` when the handler should stop.
    pub(super) async fn next(&mut self) -> Option<WsIncomingMsg> {
        loop {
            if self.signal.load(Ordering::Relaxed) {
                log::debug!("Stop signal received");
                return None;
            }

            tokio::select! {
                Some(cmd) = self.cmd_rx.recv() => {
                    match cmd {
                        HandlerCommand::SetClient(client) => {
                            log::debug!("WebSocketClient received by handler");
                            self.client = Some(client);
                        }
                        HandlerCommand::Subscribe { code, quote_type } => {
                            let msg = WsSubscribeMsg {
                                action: "subscribe".to_string(),
                                contract_code: code.clone(),
                                quote_type,
                            };
                            let text = serde_json::to_string(&msg).expect("serialize subscribe");

                            if let Some(client) = &self.client
                                && let Err(e) = client.send_text(text, None).await
                            {
                                log::error!("Failed to send subscribe for {code}: {e}");
                            }

                            self.subscriptions.insert((code, quote_type));
                        }
                        HandlerCommand::Unsubscribe { code, quote_type } => {
                            let msg = WsSubscribeMsg {
                                action: "unsubscribe".to_string(),
                                contract_code: code.clone(),
                                quote_type,
                            };
                            let text = serde_json::to_string(&msg).expect("serialize unsubscribe");

                            if let Some(client) = &self.client
                                && let Err(e) = client.send_text(text, None).await
                            {
                                log::error!("Failed to send unsubscribe for {code}: {e}");
                            }

                            self.subscriptions.remove(&(code, quote_type));
                        }
                        HandlerCommand::Disconnect => {
                            log::debug!("Disconnect command received");

                            if let Some(client) = self.client.take() {
                                client.disconnect().await;
                            }

                            return None;
                        }
                    }
                }

                msg = self.raw_rx.recv() => {
                    let msg = match msg {
                        Some(msg) => msg,
                        None => {
                            log::debug!("WebSocket stream closed");
                            return None;
                        }
                    };

                    match msg {
                        Message::Text(text) => {
                            if text.as_str() == nautilus_network::RECONNECTED {
                                log::info!("Received WebSocket reconnected signal");
                                self.resubscribe_all().await;
                                // Emit the sentinel on the out-channel in order, after the
                                // re-subscription commands, so downstream consumers can trigger
                                // reconnect reconciliation (SINOPAC-02).
                                return Some(WsIncomingMsg::Reconnected);
                            }

                            match serde_json::from_str::<WsIncomingMsg>(&text) {
                                Ok(incoming) => return Some(incoming),
                                Err(e) => {
                                    log::warn!("Failed to deserialize WS message: {e}, raw: {text}");
                                }
                            }
                        }
                        Message::Close(_) => {
                            log::info!("WebSocket close frame received");
                            return None;
                        }
                        _ => {} // Ping/Pong handled by nautilus_network
                    }
                }
            }
        }
    }

    /// Re-subscribes all tracked subscriptions after a reconnection.
    async fn resubscribe_all(&self) {
        if self.subscriptions.is_empty() {
            return;
        }

        log::info!(
            "Re-subscribing {} topics after reconnection",
            self.subscriptions.len()
        );

        if let Some(client) = &self.client {
            for (code, quote_type) in &self.subscriptions {
                let msg = WsSubscribeMsg {
                    action: "subscribe".to_string(),
                    contract_code: code.clone(),
                    quote_type: *quote_type,
                };
                let text = serde_json::to_string(&msg).expect("serialize subscribe");

                if let Err(e) = client.send_text(text, None).await {
                    log::error!("Failed to re-subscribe {code}: {e}");
                }
            }
        }
    }
}
