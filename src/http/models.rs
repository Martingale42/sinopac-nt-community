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

//! REST API request and response models for the Sinopac gateway.

use serde::{Deserialize, Serialize};

use crate::common::enums::{
    SinopacAction, SinopacMarket, SinopacOCType, SinopacOrderCond, SinopacOrderLot,
    SinopacOrderType, SinopacPriceType,
};

/// Represents a login request payload.
#[derive(Debug, Serialize)]
pub struct LoginRequest {
    /// The API key for authentication.
    pub api_key: String,
    /// The secret key for authentication.
    pub secret_key: String,
    /// The optional path to the CA certificate file.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ca_path: Option<String>,
    /// The optional CA certificate password.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ca_passwd: Option<String>,
    /// Whether to use simulation mode.
    #[serde(default)]
    pub simulation: bool,
}

/// Represents a login response containing account information.
#[derive(Debug, Deserialize)]
pub struct LoginResponse {
    /// The list of account information entries.
    pub accounts: Vec<AccountInfo>,
}

/// Represents account information from the gateway.
#[derive(Debug, Deserialize)]
pub struct AccountInfo {
    /// The account type identifier.
    pub account_type: String,
    /// The account identifier.
    pub account_id: String,
}

/// Represents a gateway status response.
#[derive(Debug, Deserialize)]
pub struct StatusResponse {
    /// Whether the gateway is connected.
    pub connected: bool,
    /// Whether the gateway is in simulation mode.
    pub simulation: bool,
}

/// Represents a generic message response from the gateway.
#[derive(Debug, Deserialize)]
pub struct MessageResponse {
    /// The response status message.
    pub status: String,
}

/// Represents a response containing a trade ID.
#[derive(Debug, Deserialize)]
pub struct TradeIdResponse {
    /// The response status message.
    pub status: String,
    /// The trade identifier.
    pub trade_id: String,
}

/// Represents a stock contract from the gateway.
#[derive(Debug, Clone, Deserialize)]
pub struct StockContract {
    /// The contract code.
    pub code: String,
    /// The contract symbol.
    pub symbol: String,
    /// The contract name.
    pub name: String,
    /// The exchange code.
    pub exchange: String,
    /// The industry category.
    pub category: String,
    /// The daily price limit up.
    pub limit_up: f64,
    /// The daily price limit down.
    pub limit_down: f64,
    /// The reference price.
    pub reference: f64,
    /// The contract update date.
    pub update_date: String,
    /// The day trade eligibility flag.
    pub day_trade: String,
    /// The round-lot / contract unit (shares per lot). `#[serde(default)]` so
    /// older/partial gateway responses still deserialize.
    #[serde(default)]
    pub unit: f64,
    /// The contract multiplier (0 for stocks).
    #[serde(default)]
    pub multiplier: i64,
    /// The quote currency code (e.g. "TWD").
    #[serde(default)]
    pub currency: String,
}

/// Represents a futures contract from the gateway.
#[derive(Debug, Clone, Deserialize)]
pub struct FuturesContract {
    /// The contract code.
    pub code: String,
    /// The contract symbol.
    pub symbol: String,
    /// The contract name.
    pub name: String,
    /// The product category.
    pub category: String,
    /// The delivery month.
    pub delivery_month: String,
    /// The delivery date.
    pub delivery_date: String,
    /// The underlying kind identifier.
    pub underlying_kind: String,
    /// The daily price limit up.
    pub limit_up: f64,
    /// The daily price limit down.
    pub limit_down: f64,
    /// The reference price.
    pub reference: f64,
    /// The contract update date.
    pub update_date: String,
    /// The contract unit (lot size; 1 contract by default). `#[serde(default)]`
    /// so older/partial gateway responses still deserialize.
    #[serde(default)]
    pub unit: f64,
    /// The contract multiplier (TWD per index point; 0 → fall back to table).
    #[serde(default)]
    pub multiplier: i64,
    /// The quote currency code (e.g. "TWD").
    #[serde(default)]
    pub currency: String,
    /// The underlying instrument code (e.g. "TXF"); empty → fall back to root symbol.
    #[serde(default)]
    pub underlying_code: String,
}

/// Represents an options contract from the gateway.
#[derive(Debug, Clone, Deserialize)]
pub struct OptionsContract {
    /// The contract code.
    pub code: String,
    /// The contract symbol.
    pub symbol: String,
    /// The contract name.
    pub name: String,
    /// The product category.
    pub category: String,
    /// The delivery month.
    pub delivery_month: String,
    /// The delivery date.
    pub delivery_date: String,
    /// The option strike price.
    pub strike_price: f64,
    /// The option right type (Call or Put).
    pub option_right: String,
    /// The underlying kind identifier.
    pub underlying_kind: String,
    /// The daily price limit up.
    pub limit_up: f64,
    /// The daily price limit down.
    pub limit_down: f64,
    /// The reference price.
    pub reference: f64,
    /// The contract update date.
    pub update_date: String,
    /// The contract unit (lot size; 1 contract by default). `#[serde(default)]`
    /// so older/partial gateway responses still deserialize.
    #[serde(default)]
    pub unit: f64,
    /// The contract multiplier (TWD per index point; 0 → fall back to table).
    #[serde(default)]
    pub multiplier: i64,
    /// The quote currency code (e.g. "TWD").
    #[serde(default)]
    pub currency: String,
    /// The underlying instrument code (e.g. "TXO"); empty → fall back to root symbol.
    #[serde(default)]
    pub underlying_code: String,
}

/// Represents market snapshot data from the gateway.
#[derive(Debug, Clone, Deserialize)]
pub struct SnapshotData {
    /// The contract code.
    pub code: String,
    /// The exchange code.
    pub exchange: String,
    /// The opening price.
    pub open: f64,
    /// The high price.
    pub high: f64,
    /// The low price.
    pub low: f64,
    /// The closing price.
    pub close: f64,
    /// The last tick volume.
    pub volume: i64,
    /// The total accumulated volume.
    pub total_volume: i64,
    /// The best bid price.
    pub buy_price: f64,
    /// The best bid volume.
    pub buy_volume: f64,
    /// The best ask price.
    pub sell_price: f64,
    /// The best ask volume.
    pub sell_volume: f64,
    /// The price change from reference.
    pub change_price: f64,
    /// The price change rate from reference.
    pub change_rate: f64,
    /// The timestamp in epoch milliseconds.
    pub ts: u64,
}

/// Represents a historical tick data response.
#[derive(Debug, Clone, Deserialize)]
pub struct TicksResponse {
    /// The contract code.
    pub code: String,
    /// The timestamps in epoch milliseconds.
    pub ts: Vec<u64>,
    /// The closing prices.
    pub close: Vec<f64>,
    /// The tick volumes.
    pub volume: Vec<i64>,
    /// The bid prices.
    pub bid_price: Vec<f64>,
    /// The ask prices.
    pub ask_price: Vec<f64>,
    /// The tick type indicators.
    pub tick_type: Vec<i32>,
}

/// Represents a historical OHLCV bar data response.
#[derive(Debug, Clone, Deserialize)]
pub struct KBarsResponse {
    /// The contract code.
    pub code: String,
    /// The bar timestamps in epoch milliseconds.
    pub ts: Vec<u64>,
    /// The opening prices.
    pub open: Vec<f64>,
    /// The high prices.
    pub high: Vec<f64>,
    /// The low prices.
    pub low: Vec<f64>,
    /// The closing prices.
    pub close: Vec<f64>,
    /// The bar volumes.
    pub volume: Vec<i64>,
}

/// Represents a place order request payload.
#[derive(Debug, Serialize)]
pub struct PlaceOrderRequest {
    /// The contract code.
    pub code: String,
    /// The order action (Buy or Sell).
    pub action: SinopacAction,
    /// The order price.
    pub price: f64,
    /// The order quantity.
    pub quantity: i64,
    /// The price type (LMT, MKT, MKP).
    pub price_type: SinopacPriceType,
    /// The order duration type (ROD, IOC, FOK).
    pub order_type: SinopacOrderType,
    /// The order condition (Cash, MarginTrading, ShortSelling).
    pub order_cond: SinopacOrderCond,
    /// The lot size type (Common, Odd, IntradayOdd, Fixing).
    pub order_lot: SinopacOrderLot,
    /// The futures/options open-close type (Auto, New, Cover, DayTrade);
    /// ignored for stocks. `#[serde(default)]` keeps older fixtures that omit
    /// the field deserializable (defaults to `Auto`).
    #[serde(default)]
    pub octype: SinopacOCType,
    /// The stock intraday day-trade short flag (cash-account same-day short); requires `order_cond == Cash`.
    /// `#[serde(default)]` keeps older fixtures that omit the field
    /// deserializable (defaults to `false`).
    #[serde(default)]
    pub daytrade_short: bool,
    /// The market type (stock, futures, options).
    pub market: SinopacMarket,
    /// Free-form tag for order adoption (max 6 ASCII, adapter stores a
    /// deterministic hash of `client_order_id` so timed-out orders can be
    /// recovered from later WS events / reconciliation).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub custom_field: Option<String>,
}

/// Represents an update order request payload.
#[derive(Debug, Serialize)]
pub struct UpdateOrderRequest {
    /// The trade identifier to update.
    pub trade_id: String,
    /// The optional new price.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub price: Option<f64>,
    /// The optional new quantity.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quantity: Option<i64>,
}

/// Represents a cancel order request payload.
#[derive(Debug, Serialize)]
pub struct CancelOrderRequest {
    /// The trade identifier to cancel.
    pub trade_id: String,
}

/// Represents a place order response from the gateway.
#[derive(Debug, Clone, Deserialize)]
pub struct PlaceOrderResponse {
    /// The trade identifier.
    pub trade_id: String,
    /// The contract code.
    pub code: String,
    /// The order action (Buy or Sell).
    pub action: String,
    /// The order status.
    pub status: String,
}

/// Represents active trade information from the gateway.
#[derive(Debug, Clone, Deserialize)]
pub struct TradeInfo {
    /// The trade identifier.
    pub trade_id: String,
    /// The contract code.
    pub code: String,
    /// The order action (Buy or Sell).
    pub action: String,
    /// The order price.
    pub price: f64,
    /// The order quantity.
    pub quantity: i64,
    /// The order status.
    pub status: String,
    /// The order duration type.
    pub order_type: String,
    /// The price type.
    pub price_type: String,
    /// The cumulative filled quantity (shares for stocks, contracts for
    /// futures/options). `#[serde(default)]` keeps compatibility with an older
    /// gateway that omits the field (defaults to 0).
    #[serde(default)]
    pub filled_qty: i64,
    /// The average fill price across all fills. `#[serde(default)]` keeps
    /// compatibility with an older gateway that omits the field (defaults to 0.0).
    #[serde(default)]
    pub avg_fill_price: f64,
    /// The adapter token for order adoption (max 6 ASCII).
    #[serde(default)]
    pub custom_field: Option<String>,
}

/// Represents an account position from the gateway.
#[derive(Debug, Clone, Deserialize)]
pub struct Position {
    /// The contract code.
    pub code: String,
    /// The position direction (Buy or Sell).
    pub direction: String,
    /// The position quantity.
    pub quantity: i64,
    /// The average entry price.
    pub price: f64,
    /// The last traded price.
    pub last_price: f64,
    /// The unrealized profit and loss.
    pub pnl: f64,
    /// The yesterday position quantity.
    pub yd_quantity: i64,
}

/// Represents an account balance from the gateway.
#[derive(Debug, Clone, Deserialize)]
pub struct AccountBalance {
    /// The balance date.
    pub date: String,
    /// The account balance amount.
    pub balance: f64,
}

/// Represents margin information from the gateway.
#[derive(Debug, Clone, Deserialize)]
pub struct MarginInfo {
    /// The yesterday closing balance.
    pub yesterday_balance: f64,
    /// The today current balance.
    pub today_balance: f64,
    /// The available margin amount.
    pub available_margin: f64,
    /// The risk indicator value.
    pub risk_indicator: f64,
}

/// Represents a profit and loss record from the gateway.
#[derive(Debug, Clone, Deserialize)]
pub struct ProfitLoss {
    /// The contract code.
    pub code: String,
    /// The traded quantity.
    pub quantity: i64,
    /// The buy price.
    pub buy_price: f64,
    /// The sell price.
    pub sell_price: f64,
    /// The realized profit and loss.
    pub pnl: f64,
    /// The profit ratio.
    pub pr_ratio: f64,
}

#[cfg(test)]
mod tests {
    use rstest::rstest;

    use super::*;
    use crate::common::testing::load_test_json_as;

    #[rstest]
    fn test_deserialize_stock_contracts() {
        let contracts: Vec<StockContract> = load_test_json_as("contracts_stocks.json");
        assert_eq!(contracts.len(), 2);
        assert_eq!(contracts[0].code, "2330");
        assert!(!contracts[0].name.is_empty());
        assert_eq!(contracts[0].exchange, "TSE");
        assert_eq!(contracts[0].limit_up, 638.0);
        assert_eq!(contracts[0].unit, 1000.0);
        assert_eq!(contracts[0].currency, "TWD");
        assert_eq!(contracts[1].code, "2317");
        // 2317 omits the new fields entirely -> serde(default) fallbacks.
        assert_eq!(contracts[1].unit, 0.0);
        assert_eq!(contracts[1].multiplier, 0);
        assert!(contracts[1].currency.is_empty());
    }

    #[rstest]
    fn test_deserialize_futures_contracts() {
        let contracts: Vec<FuturesContract> = load_test_json_as("contracts_futures.json");
        assert_eq!(contracts.len(), 1);
        assert_eq!(contracts[0].code, "TXFC6");
        assert_eq!(contracts[0].delivery_month, "2026/06");
        assert_eq!(contracts[0].underlying_kind, "I");
        assert_eq!(contracts[0].multiplier, 200);
        assert_eq!(contracts[0].unit, 1.0);
        assert_eq!(contracts[0].currency, "TWD");
        assert_eq!(contracts[0].underlying_code, "TXF");
    }

    #[rstest]
    fn test_deserialize_options_contracts() {
        let contracts: Vec<OptionsContract> = load_test_json_as("contracts_options.json");
        assert_eq!(contracts.len(), 1);
        assert_eq!(contracts[0].code, "TXO20000C6");
        assert_eq!(contracts[0].strike_price, 20000.0);
        assert_eq!(contracts[0].option_right, "C");
        assert_eq!(contracts[0].multiplier, 50);
        assert_eq!(contracts[0].unit, 1.0);
        assert_eq!(contracts[0].currency, "TWD");
        assert_eq!(contracts[0].underlying_code, "TXO");
    }

    #[rstest]
    fn test_deserialize_snapshots() {
        let snapshots: Vec<SnapshotData> = load_test_json_as("market_snapshots.json");
        assert_eq!(snapshots.len(), 1);
        assert_eq!(snapshots[0].code, "2330");
        assert_eq!(snapshots[0].close, 580.0);
        assert_eq!(snapshots[0].buy_price, 580.0);
        assert_eq!(snapshots[0].sell_price, 581.0);
        assert_eq!(snapshots[0].buy_volume, 120.0);
    }

    #[rstest]
    fn test_deserialize_ticks() {
        let ticks: TicksResponse = load_test_json_as("market_ticks.json");
        assert_eq!(ticks.code, "2330");
        assert_eq!(ticks.ts.len(), 2);
        assert_eq!(ticks.close, vec![580.0, 581.0]);
        assert_eq!(ticks.volume, vec![100, 200]);
        assert_eq!(ticks.tick_type, vec![1, 2]);
    }

    #[rstest]
    fn test_deserialize_kbars() {
        let kbars: KBarsResponse = load_test_json_as("market_kbars.json");
        assert_eq!(kbars.code, "2330");
        assert_eq!(kbars.ts.len(), 2);
        assert_eq!(kbars.open, vec![578.0, 580.0]);
        assert_eq!(kbars.volume, vec![5000, 3000]);
    }

    #[rstest]
    fn test_deserialize_account_balance() {
        let balance: AccountBalance = load_test_json_as("account_balance.json");
        assert_eq!(balance.date, "2026-03-02");
        assert_eq!(balance.balance, 1_500_000.0);
    }

    #[rstest]
    fn test_deserialize_positions() {
        let positions: Vec<Position> = load_test_json_as("account_positions.json");
        assert_eq!(positions.len(), 1);
        assert_eq!(positions[0].code, "2330");
        assert_eq!(positions[0].direction, "Buy");
        assert_eq!(positions[0].quantity, 1000);
        assert_eq!(positions[0].pnl, 5000.0);
    }

    #[rstest]
    fn test_deserialize_margin() {
        let margin: MarginInfo = load_test_json_as("account_margin.json");
        assert_eq!(margin.yesterday_balance, 2_000_000.0);
        assert_eq!(margin.available_margin, 1_800_000.0);
    }

    #[rstest]
    fn test_deserialize_profit_loss() {
        let pnl: Vec<ProfitLoss> = load_test_json_as("account_pnl.json");
        assert_eq!(pnl.len(), 1);
        assert_eq!(pnl[0].code, "2330");
        assert_eq!(pnl[0].pnl, 5000.0);
        assert_eq!(pnl[0].pr_ratio, 0.87);
    }

    #[rstest]
    fn test_deserialize_trades() {
        let trades: Vec<TradeInfo> = load_test_json_as("orders_trades.json");
        assert_eq!(trades.len(), 2);
        assert_eq!(trades[0].trade_id, "trade-001");
        assert_eq!(trades[0].code, "2330");
        assert_eq!(trades[0].action, "Buy");
        assert_eq!(trades[0].status, "Filled");
        // New fill fields present on the newer-gateway response.
        assert_eq!(trades[0].filled_qty, 1000);
        assert_eq!(trades[0].avg_fill_price, 580.5);

        // Older-gateway response omits the fill fields -> serde(default) 0 / 0.0.
        assert_eq!(trades[1].trade_id, "trade-002");
        assert_eq!(trades[1].filled_qty, 0);
        assert_eq!(trades[1].avg_fill_price, 0.0);
    }

    #[rstest]
    fn test_trade_info_tolerates_gateway_order_lot_and_cond_fields() {
        // Forward-compatibility: the order-semantics gateway adds order_lot and
        // order_cond observability keys to the /orders/trades response. TradeInfo
        // has no struct fields for them and (with no deny_unknown_fields) must
        // ignore the extra keys rather than fail to deserialize, so a newer
        // gateway never breaks an older adapter build.
        let json = r#"{
            "trade_id": "trade-009",
            "code": "2330",
            "action": "Buy",
            "price": 580.0,
            "quantity": 37,
            "status": "Submitted",
            "order_type": "ROD",
            "price_type": "LMT",
            "filled_qty": 0,
            "avg_fill_price": 0.0,
            "order_lot": "IntradayOdd",
            "order_cond": "Cash"
        }"#;

        let trade: TradeInfo =
            serde_json::from_str(json).expect("deserialize TradeInfo with semantics fields");

        assert_eq!(trade.trade_id, "trade-009");
        assert_eq!(trade.quantity, 37);
    }
}
