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

//! Integration tests for the Sinopac WebSocket client using a mock Axum server.

use std::{
    net::SocketAddr,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use axum::{
    Router,
    extract::{
        State,
        ws::{Message, WebSocket, WebSocketUpgrade},
    },
    response::IntoResponse,
    routing::get,
};
use futures_util::{SinkExt, StreamExt};
use nautilus_common::testing::wait_until_async;
use rstest::rstest;
use sinopac_nt::{
    common::enums::SinopacQuoteType,
    websocket::{client::SinopacWebSocketClient, messages::WsIncomingMsg},
};

fn load_test_json(filename: &str) -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("test_data")
        .join(filename);
    std::fs::read_to_string(&path)
        .unwrap_or_else(|_| panic!("Failed to load test fixture: {}", path.display()))
}

/// Mock WebSocket handler that responds to subscribe commands with an ack
/// then sends tick/bidask data from test fixtures based on the quote_type.
async fn ws_handler(ws: WebSocket) {
    let (mut sink, mut stream) = ws.split();

    while let Some(Ok(msg)) = stream.next().await {
        match msg {
            Message::Text(text) => {
                if let Ok(cmd) = serde_json::from_str::<serde_json::Value>(&text) {
                    let action = cmd.get("action").and_then(|v| v.as_str()).unwrap_or("");
                    let code = cmd
                        .get("contract_code")
                        .and_then(|v| v.as_str())
                        .unwrap_or("2330");
                    let quote_type = cmd.get("quote_type").and_then(|v| v.as_str()).unwrap_or("");

                    if action == "subscribe" {
                        let ack = serde_json::json!({
                            "type": "subscribed",
                            "code": code,
                            "quote_type": quote_type
                        });
                        let _ = sink.send(Message::Text(ack.to_string().into())).await;

                        let data = match quote_type {
                            "tick" => load_test_json("ws_tick_stock.json"),
                            "bidask" => load_test_json("ws_bidask.json"),
                            _ => continue,
                        };
                        let _ = sink.send(Message::Text(data.into())).await;
                    }
                }
            }
            Message::Close(_) => break,
            _ => {}
        }
    }
}

async fn ws_upgrade(ws: WebSocketUpgrade) -> impl IntoResponse {
    ws.on_upgrade(ws_handler)
}

async fn start_ws_server() -> SocketAddr {
    let router = Router::new().route("/ws", get(ws_upgrade));

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        axum::serve(listener, router.into_make_service())
            .await
            .unwrap();
    });

    // Wait for server to accept connections
    wait_until_async(
        || async move { tokio::net::TcpStream::connect(addr).await.is_ok() },
        Duration::from_secs(5),
    )
    .await;

    addr
}

fn create_ws_url(addr: SocketAddr) -> String {
    format!("ws://{addr}/ws")
}

#[rstest]
#[tokio::test]
async fn test_connect_disconnect() {
    let addr = start_ws_server().await;
    let url = create_ws_url(addr);
    let client = SinopacWebSocketClient::new(Some(url));

    assert!(!client.is_connected());

    client.connect().await.expect("connect failed");

    // Wait for the handler task to set is_connected
    wait_until_async(
        || {
            let client = client.clone();
            async move { client.is_connected() }
        },
        Duration::from_secs(5),
    )
    .await;

    assert!(client.is_connected());

    client.disconnect().await.expect("disconnect failed");
}

#[rstest]
#[tokio::test]
async fn test_subscribe_tick() {
    let addr = start_ws_server().await;
    let url = create_ws_url(addr);
    let client = SinopacWebSocketClient::new(Some(url));

    client.connect().await.expect("connect failed");

    wait_until_async(
        || {
            let client = client.clone();
            async move { client.is_connected() }
        },
        Duration::from_secs(5),
    )
    .await;

    client
        .subscribe("2330", SinopacQuoteType::Tick)
        .await
        .expect("subscribe failed");

    let msg = client.next_message().await.expect("expected a message");
    match msg {
        WsIncomingMsg::Subscribed(confirm) => {
            assert_eq!(confirm.code, "2330");
            assert_eq!(confirm.quote_type, "tick");
        }
        other => panic!("Expected Subscribed, was: {other:?}"),
    }

    let msg = client.next_message().await.expect("expected tick data");
    match msg {
        WsIncomingMsg::Tick(tick) => {
            assert_eq!(tick.code, "2330");
            assert_eq!(tick.data.close, 580.0);
        }
        other => panic!("Expected Tick, was: {other:?}"),
    }
}

#[rstest]
#[tokio::test]
async fn test_subscribe_bidask() {
    let addr = start_ws_server().await;
    let url = create_ws_url(addr);
    let client = SinopacWebSocketClient::new(Some(url));

    client.connect().await.expect("connect failed");

    wait_until_async(
        || {
            let client = client.clone();
            async move { client.is_connected() }
        },
        Duration::from_secs(5),
    )
    .await;

    client
        .subscribe("2330", SinopacQuoteType::BidAsk)
        .await
        .expect("subscribe failed");

    let msg = client.next_message().await.expect("expected a message");
    match msg {
        WsIncomingMsg::Subscribed(confirm) => {
            assert_eq!(confirm.code, "2330");
            assert_eq!(confirm.quote_type, "bidask");
        }
        other => panic!("Expected Subscribed, was: {other:?}"),
    }

    let msg = client.next_message().await.expect("expected bidask data");
    match msg {
        WsIncomingMsg::BidAsk(ba) => {
            assert_eq!(ba.code, "2330");
            assert_eq!(ba.data.bid_price.len(), 5);
            assert_eq!(ba.data.ask_price.len(), 5);
        }
        other => panic!("Expected BidAsk, was: {other:?}"),
    }
}

/// Mock handler that interleaves a poison (undeserializable) frame between two
/// valid subscription acks to verify a bad frame does not stop later delivery.
async fn ws_poison_handler(ws: WebSocket) {
    let (mut sink, mut stream) = ws.split();

    while let Some(Ok(msg)) = stream.next().await {
        match msg {
            Message::Text(text) => {
                if let Ok(cmd) = serde_json::from_str::<serde_json::Value>(&text) {
                    let action = cmd.get("action").and_then(|v| v.as_str()).unwrap_or("");
                    let code = cmd
                        .get("contract_code")
                        .and_then(|v| v.as_str())
                        .unwrap_or("2330");

                    if action == "subscribe" {
                        // A poison frame the client cannot deserialize as WsIncomingMsg.
                        let _ = sink
                            .send(Message::Text("{not valid json at all".to_string().into()))
                            .await;

                        // A valid tick must still be delivered after the poison frame.
                        let _ = sink
                            .send(Message::Text(load_test_json("ws_tick_stock.json").into()))
                            .await;

                        let ack = serde_json::json!({
                            "type": "subscribed",
                            "code": code,
                            "quote_type": "tick"
                        });
                        let _ = sink.send(Message::Text(ack.to_string().into())).await;
                    }
                }
            }
            Message::Close(_) => break,
            _ => {}
        }
    }
}

async fn ws_poison_upgrade(ws: WebSocketUpgrade) -> impl IntoResponse {
    ws.on_upgrade(ws_poison_handler)
}

async fn start_poison_ws_server() -> SocketAddr {
    let router = Router::new().route("/ws", get(ws_poison_upgrade));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        axum::serve(listener, router.into_make_service())
            .await
            .unwrap();
    });

    wait_until_async(
        || async move { tokio::net::TcpStream::connect(addr).await.is_ok() },
        Duration::from_secs(5),
    )
    .await;

    addr
}

/// A poison (undeserializable) frame must be dropped without stopping the
/// handler, so subsequent valid messages are still delivered (defense-in-depth
/// behind the Result-based parsers, Task 2.4).
#[rstest]
#[tokio::test]
async fn test_poison_frame_does_not_stop_delivery() {
    let addr = start_poison_ws_server().await;
    let url = create_ws_url(addr);
    let client = SinopacWebSocketClient::new(Some(url));

    client.connect().await.expect("connect failed");

    wait_until_async(
        || {
            let client = client.clone();
            async move { client.is_connected() }
        },
        Duration::from_secs(5),
    )
    .await;

    client
        .subscribe("2330", SinopacQuoteType::Tick)
        .await
        .expect("subscribe failed");

    // The poison frame is silently dropped by the handler; the next deliverable
    // messages are the valid tick and the subscription ack (any order-agnostic
    // assertion: both must arrive).
    let mut got_tick = false;
    let mut got_subscribed = false;
    for _ in 0..2 {
        let msg = client
            .next_message()
            .await
            .expect("expected a message after poison frame");
        match msg {
            WsIncomingMsg::Tick(tick) => {
                assert_eq!(tick.code, "2330");
                assert_eq!(tick.data.close, 580.0);
                got_tick = true;
            }
            WsIncomingMsg::Subscribed(confirm) => {
                assert_eq!(confirm.code, "2330");
                got_subscribed = true;
            }
            other => panic!("Unexpected message after poison frame: {other:?}"),
        }
    }
    assert!(got_tick, "valid tick was not delivered after poison frame");
    assert!(
        got_subscribed,
        "subscription ack was not delivered after poison frame"
    );
}

/// Mock handler whose first connection closes immediately after acking the
/// subscription (forcing the client to reconnect), and whose later connections
/// ack normally. `conn_count` tracks how many times the endpoint was hit so the
/// test can assert a reconnection (and hence re-subscription) occurred.
async fn ws_reconnect_handler(ws: WebSocket, conn_count: Arc<AtomicUsize>) {
    let n = conn_count.fetch_add(1, Ordering::SeqCst);
    let (mut sink, mut stream) = ws.split();

    while let Some(Ok(msg)) = stream.next().await {
        match msg {
            Message::Text(text) => {
                if let Ok(cmd) = serde_json::from_str::<serde_json::Value>(&text) {
                    let action = cmd.get("action").and_then(|v| v.as_str()).unwrap_or("");
                    let code = cmd
                        .get("contract_code")
                        .and_then(|v| v.as_str())
                        .unwrap_or("2330");

                    if action == "subscribe" {
                        let ack = serde_json::json!({
                            "type": "subscribed",
                            "code": code,
                            "quote_type": "tick"
                        });
                        let _ = sink.send(Message::Text(ack.to_string().into())).await;

                        // First connection: drop the socket right after the ack to
                        // trigger the client's automatic reconnection.
                        if n == 0 {
                            let _ = sink.send(Message::Close(None)).await;
                            break;
                        }
                    }
                }
            }
            Message::Close(_) => break,
            _ => {}
        }
    }
}

async fn ws_reconnect_upgrade(
    ws: WebSocketUpgrade,
    State(conn_count): State<Arc<AtomicUsize>>,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| ws_reconnect_handler(socket, conn_count))
}

async fn start_reconnect_ws_server(conn_count: Arc<AtomicUsize>) -> SocketAddr {
    let router = Router::new()
        .route("/ws", get(ws_reconnect_upgrade))
        .with_state(conn_count);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        axum::serve(listener, router.into_make_service())
            .await
            .unwrap();
    });

    wait_until_async(
        || async move { tokio::net::TcpStream::connect(addr).await.is_ok() },
        Duration::from_secs(5),
    )
    .await;

    addr
}

/// After a transport reconnect, the feed handler re-subscribes all tracked
/// topics and then emits the synthetic `Reconnected` sentinel on the out-channel
/// in order (SINOPAC-02). The test forces a reconnect by dropping the first
/// connection and asserts the client eventually observes `Reconnected` and that
/// re-subscription happened (a second server connection).
#[rstest]
#[tokio::test]
async fn test_reconnect_emits_sentinel_after_resubscribe() {
    let conn_count = Arc::new(AtomicUsize::new(0));
    let addr = start_reconnect_ws_server(Arc::clone(&conn_count)).await;
    let url = create_ws_url(addr);
    let client = SinopacWebSocketClient::new(Some(url));

    client.connect().await.expect("connect failed");

    wait_until_async(
        || {
            let client = client.clone();
            async move { client.is_connected() }
        },
        Duration::from_secs(5),
    )
    .await;

    client
        .subscribe("2330", SinopacQuoteType::Tick)
        .await
        .expect("subscribe failed");

    // Drain messages until the Reconnected sentinel arrives. The first ack, the
    // reconnect, the resubscription ack, etc. all ride the same channel; the
    // sentinel must appear after the handler has issued re-subscription.
    let mut saw_reconnected = false;
    for _ in 0..10 {
        let next = tokio::time::timeout(Duration::from_secs(10), client.next_message()).await;
        match next {
            Ok(Some(WsIncomingMsg::Reconnected)) => {
                saw_reconnected = true;
                break;
            }
            Ok(Some(_)) => {} // Subscribed acks / data — keep draining.
            Ok(None) => break,
            Err(_) => break, // Timed out waiting for the next message.
        }
    }

    assert!(
        saw_reconnected,
        "expected a Reconnected sentinel after transport reconnect"
    );
    // A reconnection (hence re-subscription) must have established a second
    // server connection.
    assert!(
        conn_count.load(Ordering::SeqCst) >= 2,
        "expected the client to reconnect (>= 2 server connections), got {}",
        conn_count.load(Ordering::SeqCst)
    );

    client.disconnect().await.expect("disconnect failed");
}
