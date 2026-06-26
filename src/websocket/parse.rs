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

//! Parsers for Sinopac WebSocket market data messages.

use nautilus_core::UnixNanos;
use nautilus_model::{
    data::{
        BookOrder, OrderBookDelta, OrderBookDeltas, OrderBookDepth10, QuoteTick, TradeTick,
        depth::DEPTH10_LEN,
    },
    enums::{AggressorSide, BookAction, OrderSide, RecordFlag},
    identifiers::{InstrumentId, TradeId},
};

use super::messages::{WsBidAskMsg, WsTickMsg};
use crate::common::parse::{taiwan_naive_to_unix_nanos, try_price, try_qty};

/// Parses a Taiwan local-time timestamp string to `UnixNanos`.
///
/// Format: "YYYY-MM-DD HH:MM:SS.ffffff" (UTC+8)
/// The fractional seconds part is optional.
pub fn parse_taiwan_timestamp(ts: &str) -> anyhow::Result<UnixNanos> {
    let dt = chrono::NaiveDateTime::parse_from_str(ts, "%Y-%m-%d %H:%M:%S%.f")
        .or_else(|_| chrono::NaiveDateTime::parse_from_str(ts, "%Y-%m-%d %H:%M:%S"))?;
    taiwan_naive_to_unix_nanos(dt)
}

/// Parses a WS tick message into a `TradeTick`.
///
/// `tick_type`: 1 = Buy (aggressor = buyer), 2 = Sell (aggressor = seller).
/// For futures/options where `tick_type` is absent, defaults to `NoAggressor`.
pub fn parse_ws_tick_to_trade_tick(
    msg: &WsTickMsg,
    instrument_id: InstrumentId,
    price_precision: u8,
    size_precision: u8,
    ts_event: UnixNanos,
    ts_init: UnixNanos,
) -> anyhow::Result<TradeTick> {
    let aggressor_side = match msg.data.tick_type {
        Some(1) => AggressorSide::Buyer,
        Some(2) => AggressorSide::Seller,
        _ => AggressorSide::NoAggressor,
    };

    TradeTick::new_checked(
        instrument_id,
        try_price(msg.data.close, price_precision)?,
        try_qty(msg.data.volume as f64, size_precision)?,
        aggressor_side,
        TradeId::new(format!("{}-{}", msg.code, msg.data.timestamp)),
        ts_event,
        ts_init,
    )
}

/// Parses a WS bidask message into a `QuoteTick` (top of book).
///
/// Uses `bid_price[0]`/`bid_volume[0]` and `ask_price[0]`/`ask_volume[0]`.
/// Returns an error if the top-of-book level has zero volume (e.g. market closed).
pub fn parse_ws_bidask_to_quote_tick(
    msg: &WsBidAskMsg,
    instrument_id: InstrumentId,
    price_precision: u8,
    size_precision: u8,
    ts_event: UnixNanos,
    ts_init: UnixNanos,
) -> anyhow::Result<QuoteTick> {
    // Guard both price AND volume arrays: a price array with data but an empty
    // volume array (or vice versa) must error here rather than panic on `[0]`.
    if msg.data.bid_price.is_empty()
        || msg.data.ask_price.is_empty()
        || msg.data.bid_volume.is_empty()
        || msg.data.ask_volume.is_empty()
    {
        anyhow::bail!(
            "Empty bid/ask price or volume arrays for {code}",
            code = msg.code
        );
    }

    if msg.data.bid_volume[0] <= 0 || msg.data.ask_volume[0] <= 0 {
        anyhow::bail!("No valid top-of-book for {code}", code = msg.code);
    }

    QuoteTick::new_checked(
        instrument_id,
        try_price(msg.data.bid_price[0], price_precision)?,
        try_price(msg.data.ask_price[0], price_precision)?,
        try_qty(msg.data.bid_volume[0] as f64, size_precision)?,
        try_qty(msg.data.ask_volume[0] as f64, size_precision)?,
        ts_event,
        ts_init,
    )
}

/// Parses a WS bidask message into an `OrderBookDepth10` snapshot.
///
/// Fills up to 5 bid and 5 ask levels from the gateway data, skipping levels
/// with non-positive volume. Remaining slots are padded with default (null) orders.
/// Returns an error if no valid levels exist (e.g. all-zero snapshot).
pub fn parse_ws_bidask_to_order_book_depth10(
    msg: &WsBidAskMsg,
    instrument_id: InstrumentId,
    price_precision: u8,
    size_precision: u8,
    ts_event: UnixNanos,
    ts_init: UnixNanos,
) -> anyhow::Result<OrderBookDepth10> {
    if msg.data.bid_price.is_empty() || msg.data.ask_price.is_empty() {
        anyhow::bail!("Empty bid/ask price arrays for {code}", code = msg.code);
    }

    let mut bids = [BookOrder::default(); DEPTH10_LEN];
    let mut asks = [BookOrder::default(); DEPTH10_LEN];
    let mut bid_counts = [0u32; DEPTH10_LEN];
    let mut ask_counts = [0u32; DEPTH10_LEN];

    let mut bid_idx = 0;
    for (&price, &volume) in msg.data.bid_price.iter().zip(msg.data.bid_volume.iter()) {
        if volume <= 0 || bid_idx >= DEPTH10_LEN {
            continue;
        }
        bids[bid_idx] = BookOrder::new(
            OrderSide::Buy,
            try_price(price, price_precision)?,
            try_qty(volume as f64, size_precision)?,
            0,
        );
        bid_counts[bid_idx] = 1;
        bid_idx += 1;
    }

    let mut ask_idx = 0;
    for (&price, &volume) in msg.data.ask_price.iter().zip(msg.data.ask_volume.iter()) {
        if volume <= 0 || ask_idx >= DEPTH10_LEN {
            continue;
        }
        asks[ask_idx] = BookOrder::new(
            OrderSide::Sell,
            try_price(price, price_precision)?,
            try_qty(volume as f64, size_precision)?,
            0,
        );
        ask_counts[ask_idx] = 1;
        ask_idx += 1;
    }

    if bid_idx == 0 && ask_idx == 0 {
        anyhow::bail!("No valid book levels for {code}", code = msg.code);
    }

    Ok(OrderBookDepth10::new(
        instrument_id,
        bids,
        asks,
        bid_counts,
        ask_counts,
        RecordFlag::F_LAST as u8 | RecordFlag::F_SNAPSHOT as u8,
        0,
        ts_event,
        ts_init,
    ))
}

/// Parses a WS bidask message into `OrderBookDeltas` (CLEAR + ADD pattern).
///
/// Produces a CLEAR delta followed by one ADD per level with positive volume,
/// suitable for building and maintaining an `OrderBook` via `apply_deltas()`.
/// Levels with non-positive volume are skipped (Hyperliquid pattern).
pub fn parse_ws_bidask_to_order_book_deltas(
    msg: &WsBidAskMsg,
    instrument_id: InstrumentId,
    price_precision: u8,
    size_precision: u8,
    ts_event: UnixNanos,
    ts_init: UnixNanos,
) -> anyhow::Result<OrderBookDeltas> {
    if msg.data.bid_price.is_empty() || msg.data.ask_price.is_empty() {
        anyhow::bail!("Empty bid/ask price arrays for {code}", code = msg.code);
    }

    let bid_count = msg.data.bid_price.len();
    let ask_count = msg.data.ask_price.len();
    let mut deltas = Vec::with_capacity(1 + bid_count + ask_count);

    deltas.push(OrderBookDelta::clear(instrument_id, 0, ts_event, ts_init));

    for (&price, &volume) in msg.data.bid_price.iter().zip(msg.data.bid_volume.iter()) {
        if volume <= 0 {
            continue;
        }
        deltas.push(OrderBookDelta::new(
            instrument_id,
            BookAction::Add,
            BookOrder::new(
                OrderSide::Buy,
                try_price(price, price_precision)?,
                try_qty(volume as f64, size_precision)?,
                0,
            ),
            0,
            0,
            ts_event,
            ts_init,
        ));
    }

    for (&price, &volume) in msg.data.ask_price.iter().zip(msg.data.ask_volume.iter()) {
        if volume <= 0 {
            continue;
        }
        deltas.push(OrderBookDelta::new(
            instrument_id,
            BookAction::Add,
            BookOrder::new(
                OrderSide::Sell,
                try_price(price, price_precision)?,
                try_qty(volume as f64, size_precision)?,
                0,
            ),
            0,
            0,
            ts_event,
            ts_init,
        ));
    }

    if let Some(last) = deltas.last_mut() {
        last.flags |= RecordFlag::F_LAST as u8;
    }

    Ok(OrderBookDeltas::new(instrument_id, deltas))
}

#[cfg(test)]
mod tests {
    use nautilus_model::{
        enums::BookAction,
        identifiers::{Symbol, Venue},
        types::{Price, Quantity},
    };
    use rstest::rstest;

    use super::*;
    use crate::{common::testing::load_test_json_as, websocket::messages::WsIncomingMsg};

    #[rstest]
    fn test_parse_taiwan_timestamp_with_microseconds() {
        let ts = parse_taiwan_timestamp("2026-03-02 09:30:00.123456").unwrap();
        assert!(ts.as_u64() > 0);
    }

    #[rstest]
    fn test_parse_taiwan_timestamp_without_fractional() {
        let ts = parse_taiwan_timestamp("2026-03-02 09:30:00").unwrap();
        assert!(ts.as_u64() > 0);
    }

    #[rstest]
    fn test_parse_taiwan_timestamp_utc_offset() {
        // 2026-03-02 00:00:00 Taiwan = 2026-03-01 16:00:00 UTC
        let ts = parse_taiwan_timestamp("2026-03-02 00:00:00").unwrap();
        let expected = chrono::NaiveDate::from_ymd_opt(2026, 3, 1)
            .unwrap()
            .and_hms_opt(16, 0, 0)
            .unwrap()
            .and_utc()
            .timestamp_nanos_opt()
            .unwrap() as u64;
        assert_eq!(ts.as_u64(), expected);
    }

    fn test_instrument_id() -> InstrumentId {
        InstrumentId::new(Symbol::new("2330"), Venue::new("SINOPAC"))
    }

    #[rstest]
    fn test_parse_ws_tick_buy_aggressor() {
        let msg: WsIncomingMsg = load_test_json_as("ws_tick_stock.json");
        if let WsIncomingMsg::Tick(tick) = msg {
            let trade = parse_ws_tick_to_trade_tick(
                &tick,
                test_instrument_id(),
                1,
                0,
                UnixNanos::from(1_740_900_000_000_000_000u64),
                UnixNanos::default(),
            )
            .unwrap();

            assert_eq!(trade.instrument_id, test_instrument_id());
            assert_eq!(trade.price, Price::new(580.0, 1));
            assert_eq!(trade.aggressor_side, AggressorSide::Buyer);
        } else {
            panic!("Expected Tick message");
        }
    }

    #[rstest]
    fn test_parse_ws_tick_futures_no_aggressor() {
        let msg: WsIncomingMsg = load_test_json_as("ws_tick_futures.json");
        if let WsIncomingMsg::Tick(tick) = msg {
            let instrument_id = InstrumentId::new(Symbol::new("TXFC6"), Venue::new("SINOPAC"));
            let trade = parse_ws_tick_to_trade_tick(
                &tick,
                instrument_id,
                0,
                0,
                UnixNanos::from(1_740_900_000_000_000_000u64),
                UnixNanos::default(),
            )
            .unwrap();

            assert_eq!(trade.aggressor_side, AggressorSide::NoAggressor);
            assert_eq!(trade.price, Price::new(20050.0, 0));
        } else {
            panic!("Expected Tick message");
        }
    }

    #[rstest]
    fn test_parse_ws_bidask_top_of_book() {
        let msg: WsIncomingMsg = load_test_json_as("ws_bidask.json");
        if let WsIncomingMsg::BidAsk(ba) = msg {
            let quote = parse_ws_bidask_to_quote_tick(
                &ba,
                test_instrument_id(),
                1,
                0,
                UnixNanos::from(1_740_900_000_000_000_000u64),
                UnixNanos::default(),
            )
            .unwrap();

            assert_eq!(quote.instrument_id, test_instrument_id());
            assert_eq!(quote.bid_price, Price::new(580.0, 1));
            assert_eq!(quote.ask_price, Price::new(581.0, 1));
            assert_eq!(quote.bid_size, Quantity::new(120.0, 0));
            assert_eq!(quote.ask_size, Quantity::new(85.0, 0));
        } else {
            panic!("Expected BidAsk message");
        }
    }

    #[rstest]
    fn test_parse_ws_bidask_to_depth10() {
        let msg: WsIncomingMsg = load_test_json_as("ws_bidask.json");
        if let WsIncomingMsg::BidAsk(ba) = msg {
            let ts = UnixNanos::from(1_740_900_000_000_000_000u64);
            let depth = parse_ws_bidask_to_order_book_depth10(
                &ba,
                test_instrument_id(),
                1,
                0,
                ts,
                UnixNanos::default(),
            )
            .unwrap();

            assert_eq!(depth.instrument_id, test_instrument_id());

            // 5 real bid levels
            assert_eq!(depth.bids[0].price, Price::new(580.0, 1));
            assert_eq!(depth.bids[0].size, Quantity::new(120.0, 0));
            assert_eq!(depth.bids[0].side, OrderSide::Buy);
            assert_eq!(depth.bids[4].price, Price::new(576.0, 1));
            assert_eq!(depth.bid_counts[0], 1);
            assert_eq!(depth.bid_counts[4], 1);

            // Padded levels are default (zero)
            assert_eq!(depth.bid_counts[5], 0);
            assert_eq!(depth.ask_counts[5], 0);

            // 5 real ask levels
            assert_eq!(depth.asks[0].price, Price::new(581.0, 1));
            assert_eq!(depth.asks[0].size, Quantity::new(85.0, 0));
            assert_eq!(depth.asks[0].side, OrderSide::Sell);
            assert_eq!(depth.asks[4].price, Price::new(585.0, 1));

            // Flags
            assert_ne!(depth.flags & RecordFlag::F_SNAPSHOT as u8, 0);
            assert_ne!(depth.flags & RecordFlag::F_LAST as u8, 0);
        } else {
            panic!("Expected BidAsk message");
        }
    }

    #[rstest]
    fn test_parse_ws_bidask_to_deltas() {
        let msg: WsIncomingMsg = load_test_json_as("ws_bidask.json");
        if let WsIncomingMsg::BidAsk(ba) = msg {
            let ts = UnixNanos::from(1_740_900_000_000_000_000u64);
            let deltas = parse_ws_bidask_to_order_book_deltas(
                &ba,
                test_instrument_id(),
                1,
                0,
                ts,
                UnixNanos::default(),
            )
            .unwrap();

            assert_eq!(deltas.instrument_id, test_instrument_id());

            let inner = deltas.deltas;
            // 1 CLEAR + 5 bids + 5 asks = 11
            assert_eq!(inner.len(), 11);

            // First delta is CLEAR
            assert_eq!(inner[0].action, BookAction::Clear);

            // Next 5 are bid ADDs
            assert_eq!(inner[1].action, BookAction::Add);
            assert_eq!(inner[1].order.side, OrderSide::Buy);
            assert_eq!(inner[1].order.price, Price::new(580.0, 1));
            assert_eq!(inner[5].order.price, Price::new(576.0, 1));

            // Last 5 are ask ADDs
            assert_eq!(inner[6].action, BookAction::Add);
            assert_eq!(inner[6].order.side, OrderSide::Sell);
            assert_eq!(inner[6].order.price, Price::new(581.0, 1));
            assert_eq!(inner[10].order.price, Price::new(585.0, 1));

            // Last delta has F_LAST flag
            assert_ne!(inner[10].flags & RecordFlag::F_LAST as u8, 0);
        } else {
            panic!("Expected BidAsk message");
        }
    }

    #[rstest]
    fn test_parse_ws_bidask_zeros_quote_tick_valid_top() {
        let msg: WsIncomingMsg = load_test_json_as("ws_bidask_zeros.json");
        if let WsIncomingMsg::BidAsk(ba) = msg {
            let quote = parse_ws_bidask_to_quote_tick(
                &ba,
                test_instrument_id(),
                1,
                0,
                UnixNanos::from(1u64),
                UnixNanos::default(),
            )
            .unwrap();

            assert_eq!(quote.bid_price, Price::new(1795.0, 1));
            assert_eq!(quote.ask_price, Price::new(1800.0, 1));
        } else {
            panic!("Expected BidAsk message");
        }
    }

    #[rstest]
    fn test_parse_ws_bidask_all_zeros_quote_tick_bails() {
        let msg: WsIncomingMsg = load_test_json_as("ws_bidask_all_zeros.json");
        if let WsIncomingMsg::BidAsk(ba) = msg {
            let result = parse_ws_bidask_to_quote_tick(
                &ba,
                test_instrument_id(),
                1,
                0,
                UnixNanos::from(1u64),
                UnixNanos::default(),
            );
            assert!(result.is_err());
        } else {
            panic!("Expected BidAsk message");
        }
    }

    #[rstest]
    fn test_parse_ws_bidask_zeros_depth10_skips_zero_levels() {
        let msg: WsIncomingMsg = load_test_json_as("ws_bidask_zeros.json");
        if let WsIncomingMsg::BidAsk(ba) = msg {
            let depth = parse_ws_bidask_to_order_book_depth10(
                &ba,
                test_instrument_id(),
                1,
                0,
                UnixNanos::from(1u64),
                UnixNanos::default(),
            )
            .unwrap();

            // 2 valid bid levels, rest are default
            assert_eq!(depth.bids[0].price, Price::new(1795.0, 1));
            assert_eq!(depth.bids[1].price, Price::new(1790.0, 1));
            assert_eq!(depth.bid_counts[0], 1);
            assert_eq!(depth.bid_counts[1], 1);
            assert_eq!(depth.bid_counts[2], 0); // skipped

            // 2 valid ask levels
            assert_eq!(depth.asks[0].price, Price::new(1800.0, 1));
            assert_eq!(depth.asks[1].price, Price::new(1805.0, 1));
            assert_eq!(depth.ask_counts[2], 0); // skipped
        } else {
            panic!("Expected BidAsk message");
        }
    }

    #[rstest]
    fn test_parse_ws_bidask_all_zeros_depth10_bails() {
        let msg: WsIncomingMsg = load_test_json_as("ws_bidask_all_zeros.json");
        if let WsIncomingMsg::BidAsk(ba) = msg {
            let result = parse_ws_bidask_to_order_book_depth10(
                &ba,
                test_instrument_id(),
                1,
                0,
                UnixNanos::from(1u64),
                UnixNanos::default(),
            );
            assert!(result.is_err());
        } else {
            panic!("Expected BidAsk message");
        }
    }

    #[rstest]
    fn test_parse_ws_bidask_zeros_deltas_skips_zero_levels() {
        let msg: WsIncomingMsg = load_test_json_as("ws_bidask_zeros.json");
        if let WsIncomingMsg::BidAsk(ba) = msg {
            let deltas = parse_ws_bidask_to_order_book_deltas(
                &ba,
                test_instrument_id(),
                1,
                0,
                UnixNanos::from(1u64),
                UnixNanos::default(),
            )
            .unwrap();

            let inner = &deltas.deltas;
            // 1 CLEAR + 2 bids + 2 asks = 5
            assert_eq!(inner.len(), 5);
            assert_eq!(inner[0].action, BookAction::Clear);
            assert_eq!(inner[1].order.side, OrderSide::Buy);
            assert_eq!(inner[3].order.side, OrderSide::Sell);

            // Last delta has F_LAST
            assert_ne!(inner[4].flags & RecordFlag::F_LAST as u8, 0);
        } else {
            panic!("Expected BidAsk message");
        }
    }

    #[rstest]
    fn test_parse_ws_bidask_all_zeros_deltas_clear_only() {
        let msg: WsIncomingMsg = load_test_json_as("ws_bidask_all_zeros.json");
        if let WsIncomingMsg::BidAsk(ba) = msg {
            let deltas = parse_ws_bidask_to_order_book_deltas(
                &ba,
                test_instrument_id(),
                1,
                0,
                UnixNanos::from(1u64),
                UnixNanos::default(),
            )
            .unwrap();

            let inner = &deltas.deltas;
            // Only CLEAR, no ADDs
            assert_eq!(inner.len(), 1);
            assert_eq!(inner[0].action, BookAction::Clear);
            // F_LAST set on CLEAR
            assert_ne!(inner[0].flags & RecordFlag::F_LAST as u8, 0);
        } else {
            panic!("Expected BidAsk message");
        }
    }

    // --- Malformed gateway values must return an error, never panic ------------------------

    fn load_tick_msg() -> WsTickMsg {
        match load_test_json_as("ws_tick_stock.json") {
            WsIncomingMsg::Tick(tick) => tick,
            _ => panic!("Expected Tick message"),
        }
    }

    fn load_bidask_msg() -> WsBidAskMsg {
        match load_test_json_as("ws_bidask.json") {
            WsIncomingMsg::BidAsk(ba) => ba,
            _ => panic!("Expected BidAsk message"),
        }
    }

    #[rstest]
    fn test_parse_ws_tick_nan_close_errors() {
        let mut tick = load_tick_msg();
        tick.data.close = f64::NAN;
        let result = parse_ws_tick_to_trade_tick(
            &tick,
            test_instrument_id(),
            1,
            0,
            UnixNanos::from(1u64),
            UnixNanos::default(),
        );
        assert!(result.is_err());
    }

    #[rstest]
    fn test_parse_ws_tick_negative_volume_errors() {
        let mut tick = load_tick_msg();
        tick.data.volume = -1;
        let result = parse_ws_tick_to_trade_tick(
            &tick,
            test_instrument_id(),
            1,
            0,
            UnixNanos::from(1u64),
            UnixNanos::default(),
        );
        assert!(result.is_err());
    }

    #[rstest]
    fn test_parse_ws_bidask_empty_volume_array_errors_not_panics() {
        // Price array has data but volume array is empty: indexing `[0]` would
        // panic without the extended emptiness guard.
        let mut ba = load_bidask_msg();
        ba.data.bid_price = vec![580.0];
        ba.data.bid_volume = vec![];
        let result = parse_ws_bidask_to_quote_tick(
            &ba,
            test_instrument_id(),
            1,
            0,
            UnixNanos::from(1u64),
            UnixNanos::default(),
        );
        assert!(result.is_err());
    }

    #[rstest]
    fn test_parse_ws_bidask_nan_price_errors() {
        let mut ba = load_bidask_msg();
        ba.data.bid_price[0] = f64::NAN;
        let result = parse_ws_bidask_to_quote_tick(
            &ba,
            test_instrument_id(),
            1,
            0,
            UnixNanos::from(1u64),
            UnixNanos::default(),
        );
        assert!(result.is_err());
    }
}
