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

//! Integration tests for the Sinopac HTTP client using a mock Axum server.

use std::{net::SocketAddr, path::PathBuf, time::Duration};

use axum::{Router, routing::get};
use nautilus_common::testing::wait_until_async;
use rstest::rstest;
use sinopac_nt::{
    common::enums::{
        SinopacAction, SinopacMarket, SinopacOCType, SinopacOrderCond, SinopacOrderLot,
        SinopacOrderType, SinopacPriceType,
    },
    http::{client::SinopacHttpClient, models::PlaceOrderRequest, query::SnapshotsQuery},
};

fn load_test_json(filename: &str) -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("test_data")
        .join(filename);
    std::fs::read_to_string(&path)
        .unwrap_or_else(|_| panic!("Failed to load test fixture: {}", path.display()))
}

/// Starts a mock Axum server that serves JSON test fixtures.
///
/// The Sinopac HTTP client constructs URLs as `{raw_base}/api/{path}`, so
/// all mock routes are prefixed with `/api/`.
async fn start_test_server() -> SocketAddr {
    let router = Router::new()
        .route(
            "/api/contracts/stocks",
            get(|| async { load_test_json("contracts_stocks.json") }),
        )
        .route(
            "/api/contracts/futures",
            get(|| async { load_test_json("contracts_futures.json") }),
        )
        .route(
            "/api/contracts/options",
            get(|| async { load_test_json("contracts_options.json") }),
        )
        .route(
            "/api/auth/status",
            get(|| async { r#"{"connected": true, "simulation": false}"# }),
        )
        .route(
            "/api/market/snapshots",
            get(|| async { load_test_json("market_snapshots.json") }),
        )
        .route(
            "/api/orders/trades",
            get(|| async { load_test_json("orders_trades.json") }),
        );

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

fn create_client(addr: SocketAddr) -> SinopacHttpClient {
    let base_url = format!("http://{addr}");
    SinopacHttpClient::new(Some(base_url)).expect("Failed to create SinopacHttpClient")
}

#[rstest]
#[tokio::test]
async fn test_list_stocks() {
    let addr = start_test_server().await;
    let client = create_client(addr);

    let stocks = client.list_stocks().await.expect("list_stocks failed");
    assert_eq!(stocks.len(), 2);
    assert_eq!(stocks[0].code, "2330");
    assert!(!stocks[0].name.is_empty());
    assert_eq!(stocks[0].exchange, "TSE");
    assert_eq!(stocks[1].code, "2317");
}

#[rstest]
#[tokio::test]
async fn test_list_futures() {
    let addr = start_test_server().await;
    let client = create_client(addr);

    let futures = client.list_futures().await.expect("list_futures failed");
    assert_eq!(futures.len(), 1);
    assert_eq!(futures[0].code, "TXFC6");
    assert_eq!(futures[0].delivery_month, "2026/06");
    assert_eq!(futures[0].underlying_kind, "I");
}

#[rstest]
#[tokio::test]
async fn test_list_options() {
    let addr = start_test_server().await;
    let client = create_client(addr);

    let options = client.list_options().await.expect("list_options failed");
    assert_eq!(options.len(), 1);
    assert_eq!(options[0].code, "TXO20000C6");
    assert_eq!(options[0].strike_price, 20000.0);
    // WS-A now serializes option_right as the enum value "C"/"P" (was "Call"/"Put").
    assert_eq!(options[0].option_right, "C");
    assert_eq!(options[0].multiplier, 50);
    assert_eq!(options[0].underlying_code, "TXO");
}

#[rstest]
#[tokio::test]
async fn test_status() {
    let addr = start_test_server().await;
    let client = create_client(addr);

    let status = client.status().await.expect("status failed");
    assert!(status.connected);
    assert!(!status.simulation);
}

#[rstest]
#[tokio::test]
async fn test_snapshots() {
    let addr = start_test_server().await;
    let client = create_client(addr);

    let query = SnapshotsQuery {
        codes: "2330".to_string(),
        market: None,
    };
    let snapshots = client.snapshots(&query).await.expect("snapshots failed");
    assert_eq!(snapshots.len(), 1);
    assert_eq!(snapshots[0].code, "2330");
    assert_eq!(snapshots[0].close, 580.0);
    assert_eq!(snapshots[0].buy_price, 580.0);
    assert_eq!(snapshots[0].sell_price, 581.0);
}

#[rstest]
#[tokio::test]
async fn test_list_trades_filled_fields() {
    let addr = start_test_server().await;
    let client = create_client(addr);

    let trades = client.list_trades().await.expect("list_trades failed");
    assert_eq!(trades.len(), 2);

    // Newer-gateway response carries the fill fields end-to-end (SINOPAC-05).
    assert_eq!(trades[0].trade_id, "trade-001");
    assert_eq!(trades[0].filled_qty, 1000);
    assert_eq!(trades[0].avg_fill_price, 580.5);

    // Older-gateway response omits them -> serde(default) 0 / 0.0.
    assert_eq!(trades[1].trade_id, "trade-002");
    assert_eq!(trades[1].filled_qty, 0);
    assert_eq!(trades[1].avg_fill_price, 0.0);
}

#[rstest]
fn test_place_order_request_serializes_octype_and_daytrade_short() {
    // A futures order opening a new position serializes the bare member name
    // "New" (NOT "NewPosition") so it is byte-identical to the gateway OCType
    // StrEnum resolved via getattr(sj.constant.FuturesOCType, value).
    let request = PlaceOrderRequest {
        code: "TXFC6".to_string(),
        action: SinopacAction::Buy,
        price: 20000.0,
        quantity: 1,
        price_type: SinopacPriceType::LMT,
        order_type: SinopacOrderType::ROD,
        order_cond: SinopacOrderCond::Cash,
        order_lot: SinopacOrderLot::Common,
        octype: SinopacOCType::New,
        daytrade_short: true,
        market: SinopacMarket::Futures,
        custom_field: None,
    };

    let json = serde_json::to_value(&request).expect("serialize PlaceOrderRequest");
    assert_eq!(json["octype"], "New");
    assert_eq!(json["daytrade_short"], true);
}

#[rstest]
fn test_place_order_request_serializes_default_octype_and_daytrade_short() {
    // The defaults serialize as "Auto" / false so a plain stock order keeps the
    // gateway's auto open-close behaviour and no day-trade short flag.
    let request = PlaceOrderRequest {
        code: "2330".to_string(),
        action: SinopacAction::Buy,
        price: 580.0,
        quantity: 1000,
        price_type: SinopacPriceType::LMT,
        order_type: SinopacOrderType::ROD,
        order_cond: SinopacOrderCond::Cash,
        order_lot: SinopacOrderLot::Common,
        octype: SinopacOCType::default(),
        daytrade_short: false,
        market: SinopacMarket::Stock,
        custom_field: None,
    };

    let json = serde_json::to_value(&request).expect("serialize PlaceOrderRequest");
    assert_eq!(json["octype"], "Auto");
    assert_eq!(json["daytrade_short"], false);
}
