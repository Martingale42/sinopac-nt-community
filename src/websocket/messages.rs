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

//! WebSocket message types for the Sinopac gateway.

use serde::{Deserialize, Serialize};

use crate::common::enums::SinopacQuoteType;

/// Represents a WebSocket subscribe/unsubscribe command message.
#[derive(Debug, Serialize)]
pub struct WsSubscribeMsg {
    /// The subscription action (subscribe or unsubscribe).
    pub action: String,
    /// The contract code to subscribe to.
    pub contract_code: String,
    /// The quote type (tick or bidask).
    pub quote_type: SinopacQuoteType,
}

/// Represents a raw WebSocket message envelope. The `type` field determines the payload shape.
#[derive(Debug, Deserialize)]
#[serde(tag = "type")]
pub enum WsIncomingMsg {
    /// A tick data message.
    #[serde(rename = "tick")]
    Tick(WsTickMsg),
    /// A bid/ask data message.
    #[serde(rename = "bidask")]
    BidAsk(WsBidAskMsg),
    /// An order update event message.
    #[serde(rename = "order_update")]
    OrderUpdate(WsOrderUpdateMsg),
    /// A subscription confirmation message.
    #[serde(rename = "subscribed")]
    Subscribed(WsConfirmMsg),
    /// An unsubscription confirmation message.
    #[serde(rename = "unsubscribed")]
    Unsubscribed(WsConfirmMsg),
    /// An error message from the gateway.
    #[serde(rename = "error")]
    Error(WsErrorMsg),
    /// A synthetic reconnection sentinel emitted by the feed handler after a
    /// transport reconnect and successful re-subscription. It is never received
    /// from the gateway wire, so `#[serde(skip)]` keeps it out of deserialization.
    #[serde(skip)]
    Reconnected,
}

/// Represents a WebSocket tick message.
#[derive(Debug, Clone, Deserialize)]
pub struct WsTickMsg {
    /// The contract code.
    pub code: String,
    /// The tick data payload.
    pub data: WsTickData,
}

/// Represents a WebSocket tick data payload.
#[derive(Debug, Clone, Deserialize)]
pub struct WsTickData {
    /// The closing price.
    pub close: f64,
    /// The tick volume.
    pub volume: i64,
    /// The total accumulated volume.
    pub total_volume: i64,
    /// The opening price.
    pub open: f64,
    /// The high price.
    pub high: f64,
    /// The low price.
    pub low: f64,
    /// The total bid side volume.
    pub bid_side_total_vol: i64,
    /// The total ask side volume.
    pub ask_side_total_vol: i64,
    /// The tick timestamp string.
    pub timestamp: String,
    /// The tick type indicator (stock-only, None for futures/options).
    // Stock-only fields (None for futures/options)
    pub tick_type: Option<i32>,
    /// The average price (stock-only, None for futures/options).
    pub avg_price: Option<f64>,
    /// The tick amount (stock-only, None for futures/options).
    pub amount: Option<f64>,
    /// The percentage change (stock-only, None for futures/options).
    pub pct_chg: Option<f64>,
    /// The underlying price (futures/options-only).
    // Futures/options-only field
    pub underlying_price: Option<f64>,
}

/// Represents a WebSocket bid/ask message.
#[derive(Debug, Clone, Deserialize)]
pub struct WsBidAskMsg {
    /// The contract code.
    pub code: String,
    /// The bid/ask data payload.
    pub data: WsBidAskData,
}

/// Represents a WebSocket bid/ask data payload.
#[derive(Debug, Clone, Deserialize)]
pub struct WsBidAskData {
    /// The bid prices.
    pub bid_price: Vec<f64>,
    /// The bid volumes.
    pub bid_volume: Vec<i64>,
    /// The ask prices.
    pub ask_price: Vec<f64>,
    /// The ask volumes.
    pub ask_volume: Vec<i64>,
    /// The quote timestamp string.
    pub timestamp: String,
}

/// Represents a raw order update envelope. The `event` field discriminates the data type.
#[derive(Debug, Clone, Deserialize)]
pub struct WsOrderUpdateMsg {
    /// The order event type string.
    pub event: String,
    /// The raw order event data as JSON.
    pub data: serde_json::Value,
}

impl WsOrderUpdateMsg {
    /// Parses the data field into a typed event based on the event string.
    pub fn parse_event(&self) -> anyhow::Result<OrderEvent> {
        match self.event.as_str() {
            "OrderState.StockOrder" => {
                let data: StockOrderEventData = serde_json::from_value(self.data.clone())?;
                Ok(OrderEvent::StockOrder(data))
            }
            "OrderState.StockDeal" => {
                let data: StockDealEventData = serde_json::from_value(self.data.clone())?;
                Ok(OrderEvent::StockDeal(data))
            }
            "OrderState.FuturesOrder" => {
                let data: FuturesOrderEventData = serde_json::from_value(self.data.clone())?;
                Ok(OrderEvent::FuturesOrder(data))
            }
            "OrderState.FuturesDeal" => {
                let data: FuturesDealEventData = serde_json::from_value(self.data.clone())?;
                Ok(OrderEvent::FuturesDeal(data))
            }
            other => anyhow::bail!("Unknown order event type: {other}"),
        }
    }
}

/// Represents a typed order event after parsing the `data` field.
#[derive(Debug, Clone)]
pub enum OrderEvent {
    /// A stock order state event.
    StockOrder(StockOrderEventData),
    /// A stock deal (fill) event.
    StockDeal(StockDealEventData),
    /// A futures order state event.
    FuturesOrder(FuturesOrderEventData),
    /// A futures deal (fill) event.
    FuturesDeal(FuturesDealEventData),
}

/// Represents operation information for order events.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct OperationInfo {
    /// The operation type (New, Cancel, etc.).
    pub op_type: String,
    /// The operation result code.
    pub op_code: String,
    /// The operation result message.
    pub op_msg: String,
}

/// Represents order status information from the gateway.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct OrderStatusInfo {
    /// The order status identifier.
    pub id: String,
    /// The exchange timestamp.
    pub exchange_ts: f64,
    /// The modified price after amendment.
    pub modified_price: f64,
    /// The cancelled quantity.
    pub cancel_quantity: i64,
    /// The total order quantity.
    pub order_quantity: i64,
}

/// Represents stock contract information from order events.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct StockContractInfo {
    /// The security type identifier.
    pub security_type: String,
    /// The exchange code.
    pub exchange: String,
    /// The contract code.
    pub code: String,
    /// The contract symbol.
    pub symbol: String,
    /// The contract name.
    pub name: String,
}

/// Represents futures contract information from order events.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct FuturesContractInfo {
    /// The security type identifier.
    pub security_type: String,
    /// The contract code.
    pub code: String,
    /// The exchange code.
    pub exchange: String,
    /// The optional delivery month.
    #[serde(default)]
    pub delivery_month: Option<String>,
    /// The optional strike price.
    #[serde(default)]
    pub strike_price: Option<f64>,
    /// The optional option right type.
    #[serde(default)]
    pub option_right: Option<String>,
}

/// Represents stock order information from order events.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct StockOrderInfo {
    /// The order identifier.
    pub id: String,
    /// The sequence number.
    pub seqno: String,
    /// The order number.
    pub ordno: String,
    /// The order action (Buy or Sell).
    pub action: String,
    /// The order price.
    pub price: f64,
    /// The order quantity.
    pub quantity: i64,
    /// The order duration type.
    pub order_type: String,
    /// The price type.
    pub price_type: String,
    /// The optional order condition.
    #[serde(default)]
    pub order_cond: Option<String>,
    /// The optional lot size type.
    #[serde(default)]
    pub order_lot: Option<String>,
    /// The adapter token for order adoption (max 6 ASCII).
    #[serde(default)]
    pub custom_field: Option<String>,
}

/// Represents futures order information from order events.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct FuturesOrderInfo {
    /// The order identifier.
    pub id: String,
    /// The sequence number.
    pub seqno: String,
    /// The order number.
    pub ordno: String,
    /// The order action (Buy or Sell).
    pub action: String,
    /// The order price.
    pub price: f64,
    /// The order quantity.
    pub quantity: i64,
    /// The order duration type.
    pub order_type: String,
    /// The price type.
    pub price_type: String,
    /// The optional market type (Day or Night).
    #[serde(default)]
    pub market_type: Option<String>,
    /// The optional open/close type (New or Cover).
    #[serde(default)]
    pub oc_type: Option<String>,
    /// Whether this is a combo order.
    #[serde(default)]
    pub combo: Option<bool>,
    /// The adapter token for order adoption (max 6 ASCII).
    #[serde(default)]
    pub custom_field: Option<String>,
}

/// Represents stock order event data from the gateway.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct StockOrderEventData {
    /// The operation information.
    pub operation: OperationInfo,
    /// The stock order details.
    pub order: StockOrderInfo,
    /// The order status information.
    pub status: OrderStatusInfo,
    /// The stock contract information.
    pub contract: StockContractInfo,
}

/// Represents stock deal event data from the gateway.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct StockDealEventData {
    /// The trade identifier.
    pub trade_id: String,
    /// The sequence number.
    pub seqno: String,
    /// The order number.
    pub ordno: String,
    /// The optional exchange sequence number.
    #[serde(default)]
    pub exchange_seq: Option<String>,
    /// The broker identifier.
    pub broker_id: String,
    /// The account identifier.
    pub account_id: String,
    /// The deal action (Buy or Sell).
    pub action: String,
    /// The contract code.
    pub code: String,
    /// The deal price.
    pub price: f64,
    /// The deal quantity.
    pub quantity: i64,
    /// The deal timestamp.
    pub ts: f64,
    /// The optional order condition.
    #[serde(default)]
    pub order_cond: Option<String>,
    /// The optional lot size type.
    #[serde(default)]
    pub order_lot: Option<String>,
}

/// Represents futures order event data from the gateway.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct FuturesOrderEventData {
    /// The operation information.
    pub operation: OperationInfo,
    /// The futures order details.
    pub order: FuturesOrderInfo,
    /// The order status information.
    pub status: OrderStatusInfo,
    /// The futures contract information.
    pub contract: FuturesContractInfo,
}

/// Represents futures deal event data from the gateway.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct FuturesDealEventData {
    /// The trade identifier.
    pub trade_id: String,
    /// The sequence number.
    pub seqno: String,
    /// The order number.
    pub ordno: String,
    /// The optional exchange sequence number.
    #[serde(default)]
    pub exchange_seq: Option<String>,
    /// The broker identifier.
    pub broker_id: String,
    /// The account identifier.
    pub account_id: String,
    /// The deal action (Buy or Sell).
    pub action: String,
    /// The contract code.
    pub code: String,
    /// The deal price.
    pub price: f64,
    /// The deal quantity.
    pub quantity: i64,
    /// The deal timestamp.
    pub ts: f64,
    /// The optional security type.
    #[serde(default)]
    pub security_type: Option<String>,
    /// The optional market type (Day or Night).
    #[serde(default)]
    pub market_type: Option<String>,
    /// Whether this is a combo order.
    #[serde(default)]
    pub combo: Option<bool>,
}

/// Represents a WebSocket subscription confirmation message.
#[derive(Debug, Clone, Deserialize)]
pub struct WsConfirmMsg {
    /// The contract code.
    pub code: String,
    /// The quote type (tick or bidask).
    pub quote_type: String,
}

/// Represents a WebSocket error message from the gateway.
#[derive(Debug, Clone, Deserialize)]
pub struct WsErrorMsg {
    /// The error detail message.
    pub detail: String,
}

#[cfg(test)]
mod tests {
    use rstest::rstest;

    use super::*;
    use crate::common::testing::load_test_json_as;

    #[rstest]
    fn test_deserialize_ws_tick_stock() {
        let msg: WsIncomingMsg = load_test_json_as("ws_tick_stock.json");
        match msg {
            WsIncomingMsg::Tick(tick) => {
                assert_eq!(tick.code, "2330");
                assert_eq!(tick.data.close, 580.0);
                assert!(tick.data.tick_type.is_some());
                assert!(tick.data.underlying_price.is_none());
            }
            _ => panic!("Expected Tick message"),
        }
    }

    #[rstest]
    fn test_deserialize_ws_tick_futures() {
        let msg: WsIncomingMsg = load_test_json_as("ws_tick_futures.json");
        match msg {
            WsIncomingMsg::Tick(tick) => {
                assert_eq!(tick.code, "TXFC6");
                assert!(tick.data.tick_type.is_none());
                assert!(tick.data.underlying_price.is_some());
            }
            _ => panic!("Expected Tick message"),
        }
    }

    #[rstest]
    fn test_deserialize_ws_bidask() {
        let msg: WsIncomingMsg = load_test_json_as("ws_bidask.json");
        match msg {
            WsIncomingMsg::BidAsk(ba) => {
                assert_eq!(ba.code, "2330");
                assert_eq!(ba.data.bid_price.len(), 5);
                assert_eq!(ba.data.ask_price.len(), 5);
                assert_eq!(ba.data.bid_volume.len(), 5);
                assert_eq!(ba.data.ask_volume.len(), 5);
            }
            _ => panic!("Expected BidAsk message"),
        }
    }

    #[rstest]
    fn test_deserialize_ws_order_update() {
        let msg: WsIncomingMsg = load_test_json_as("ws_order_update.json");
        match msg {
            WsIncomingMsg::OrderUpdate(update) => {
                assert_eq!(update.event, "Submitted");
                assert!(update.data.is_object());
            }
            _ => panic!("Expected OrderUpdate message"),
        }
    }

    #[rstest]
    fn test_deserialize_ws_subscribed() {
        let msg: WsIncomingMsg = load_test_json_as("ws_subscribed.json");
        match msg {
            WsIncomingMsg::Subscribed(confirm) => {
                assert_eq!(confirm.code, "2330");
                assert_eq!(confirm.quote_type, "tick");
            }
            _ => panic!("Expected Subscribed message"),
        }
    }

    #[rstest]
    fn test_deserialize_ws_error() {
        let msg: WsIncomingMsg = load_test_json_as("ws_error.json");
        match msg {
            WsIncomingMsg::Error(e) => {
                assert!(e.detail.contains("Missing"));
            }
            _ => panic!("Expected Error message"),
        }
    }

    #[rstest]
    fn test_parse_order_event_stock_order() {
        let msg: WsIncomingMsg = load_test_json_as("ws_order_stock.json");
        match msg {
            WsIncomingMsg::OrderUpdate(update) => {
                assert_eq!(update.event, "OrderState.StockOrder");
                let event = update.parse_event().expect("parse_event failed");
                match event {
                    OrderEvent::StockOrder(data) => {
                        assert_eq!(data.operation.op_type, "New");
                        assert_eq!(data.operation.op_code, "00");
                        assert_eq!(data.order.id, "abc123");
                        assert_eq!(data.order.action, "Buy");
                        assert_eq!(data.order.price, 580.0);
                        assert_eq!(data.order.quantity, 1);
                        assert_eq!(data.order.order_type, "ROD");
                        assert_eq!(data.order.price_type, "LMT");
                        assert_eq!(data.status.order_quantity, 1);
                        assert_eq!(data.contract.code, "2330");
                        assert_eq!(data.contract.security_type, "STK");
                    }
                    _ => panic!("Expected StockOrder event"),
                }
            }
            _ => panic!("Expected OrderUpdate message"),
        }
    }

    #[rstest]
    fn test_parse_order_event_stock_deal() {
        let msg: WsIncomingMsg = load_test_json_as("ws_deal_stock.json");
        match msg {
            WsIncomingMsg::OrderUpdate(update) => {
                assert_eq!(update.event, "OrderState.StockDeal");
                let event = update.parse_event().expect("parse_event failed");
                match event {
                    OrderEvent::StockDeal(data) => {
                        assert_eq!(data.trade_id, "abc123");
                        assert_eq!(data.ordno, "A1234");
                        assert_eq!(data.action, "Buy");
                        assert_eq!(data.code, "2330");
                        assert_eq!(data.price, 580.0);
                        assert_eq!(data.quantity, 1);
                        assert_eq!(data.ts, 1709352601.0);
                    }
                    _ => panic!("Expected StockDeal event"),
                }
            }
            _ => panic!("Expected OrderUpdate message"),
        }
    }

    #[rstest]
    fn test_parse_order_event_futures_order() {
        let msg: WsIncomingMsg = load_test_json_as("ws_order_futures.json");
        match msg {
            WsIncomingMsg::OrderUpdate(update) => {
                assert_eq!(update.event, "OrderState.FuturesOrder");
                let event = update.parse_event().expect("parse_event failed");
                match event {
                    OrderEvent::FuturesOrder(data) => {
                        assert_eq!(data.operation.op_type, "New");
                        assert_eq!(data.operation.op_code, "00");
                        assert_eq!(data.order.id, "fut001");
                        assert_eq!(data.order.action, "Buy");
                        assert_eq!(data.order.price, 18000.0);
                        assert_eq!(data.order.quantity, 2);
                        assert_eq!(data.order.market_type.as_deref(), Some("Day"));
                        assert_eq!(data.order.oc_type.as_deref(), Some("New"));
                        assert_eq!(data.contract.code, "TXFC6");
                        assert_eq!(data.contract.security_type, "FUT");
                    }
                    _ => panic!("Expected FuturesOrder event"),
                }
            }
            _ => panic!("Expected OrderUpdate message"),
        }
    }

    #[rstest]
    fn test_parse_order_event_futures_deal() {
        let msg: WsIncomingMsg = load_test_json_as("ws_deal_futures.json");
        match msg {
            WsIncomingMsg::OrderUpdate(update) => {
                assert_eq!(update.event, "OrderState.FuturesDeal");
                let event = update.parse_event().expect("parse_event failed");
                match event {
                    OrderEvent::FuturesDeal(data) => {
                        assert_eq!(data.trade_id, "fut001");
                        assert_eq!(data.ordno, "F5678");
                        assert_eq!(data.action, "Buy");
                        assert_eq!(data.code, "TXFC6");
                        assert_eq!(data.price, 18000.0);
                        assert_eq!(data.quantity, 2);
                        assert_eq!(data.ts, 1709352701.0);
                        assert_eq!(data.security_type.as_deref(), Some("FUT"));
                    }
                    _ => panic!("Expected FuturesDeal event"),
                }
            }
            _ => panic!("Expected OrderUpdate message"),
        }
    }

    #[rstest]
    fn test_parse_order_event_unknown_type() {
        let msg = WsOrderUpdateMsg {
            event: "OrderState.Unknown".to_string(),
            data: serde_json::json!({}),
        };
        assert!(msg.parse_event().is_err());
    }
}
