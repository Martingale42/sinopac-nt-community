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

//! Sinopac venue-specific enumerations.

use serde::{Deserialize, Serialize};

/// Represents a trading action (buy or sell).
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(
        eq,
        eq_int,
        frozen,
        module = "nautilus_trader.core.nautilus_pyo3.sinopac",
        rename_all = "SCREAMING_SNAKE_CASE",
        from_py_object,
    )
)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SinopacAction {
    /// Buy action.
    Buy,
    /// Sell action.
    Sell,
}

/// Represents a price type for order submission.
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(
        eq,
        eq_int,
        frozen,
        module = "nautilus_trader.core.nautilus_pyo3.sinopac",
        rename_all = "SCREAMING_SNAKE_CASE",
        from_py_object,
    )
)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SinopacPriceType {
    /// Limit price.
    LMT,
    /// Market price.
    MKT,
    /// Market price with protection.
    MKP,
}

/// Represents an order duration (time in force).
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(
        eq,
        eq_int,
        frozen,
        module = "nautilus_trader.core.nautilus_pyo3.sinopac",
        rename_all = "SCREAMING_SNAKE_CASE",
        from_py_object,
    )
)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SinopacOrderType {
    /// Rest of day duration.
    ROD,
    /// Immediate or cancel duration.
    IOC,
    /// Fill or kill duration.
    FOK,
}

/// Represents a stock order credit condition.
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(
        eq,
        eq_int,
        frozen,
        module = "nautilus_trader.core.nautilus_pyo3.sinopac",
        rename_all = "SCREAMING_SNAKE_CASE",
        from_py_object,
    )
)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SinopacOrderCond {
    /// Cash order condition.
    Cash,
    /// Margin trading condition.
    MarginTrading,
    /// Short selling condition.
    ShortSelling,
}

/// Represents a stock lot size type.
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(
        eq,
        eq_int,
        frozen,
        module = "nautilus_trader.core.nautilus_pyo3.sinopac",
        rename_all = "SCREAMING_SNAKE_CASE",
        from_py_object,
    )
)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SinopacOrderLot {
    /// Common lot size (1000 shares).
    Common,
    /// Odd lot size.
    Odd,
    /// Intraday odd lot size.
    IntradayOdd,
    /// Fixing session lot size.
    Fixing,
}

/// Represents a futures/options open-close type (`octype`).
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(
        eq,
        eq_int,
        frozen,
        module = "nautilus_trader.core.nautilus_pyo3.sinopac",
        rename_all = "SCREAMING_SNAKE_CASE",
        from_py_object,
    )
)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SinopacOCType {
    /// Auto open-close (gateway decides based on net position).
    Auto,
    /// Open a new position.
    New,
    /// Cover (close) an existing position.
    Cover,
    /// Day-trade open-close.
    DayTrade,
}

impl Default for SinopacOCType {
    /// Returns the default open-close type, which delegates the open-close
    /// decision to the gateway based on the net position.
    fn default() -> Self {
        Self::Auto
    }
}

/// Represents a quote subscription type.
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(
        eq,
        eq_int,
        frozen,
        module = "nautilus_trader.core.nautilus_pyo3.sinopac",
        rename_all = "SCREAMING_SNAKE_CASE",
        from_py_object,
    )
)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SinopacQuoteType {
    /// Tick-by-tick quote type.
    Tick,
    /// Bid/ask quote type.
    BidAsk,
}

/// Represents a market type for endpoint routing.
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(
        eq,
        eq_int,
        frozen,
        module = "nautilus_trader.core.nautilus_pyo3.sinopac",
        rename_all = "SCREAMING_SNAKE_CASE",
        from_py_object,
    )
)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SinopacMarket {
    /// Stock market.
    Stock,
    /// Futures market.
    Futures,
    /// Options market.
    Options,
}

/// Represents an exchange code for Taiwan markets.
#[cfg_attr(
    feature = "python",
    pyo3::pyclass(
        eq,
        eq_int,
        frozen,
        module = "nautilus_trader.core.nautilus_pyo3.sinopac",
        rename_all = "SCREAMING_SNAKE_CASE",
        from_py_object,
    )
)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SinopacExchange {
    /// Taiwan Stock Exchange.
    TSE,
    /// Over-the-Counter (Taipei Exchange).
    OTC,
}

/// Represents order update event types from the gateway WebSocket.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SinopacOrderEvent {
    /// Stock order state update event.
    #[serde(rename = "OrderState.StockOrder")]
    StockOrder,
    /// Stock deal (fill) event.
    #[serde(rename = "OrderState.StockDeal")]
    StockDeal,
    /// Futures order state update event.
    #[serde(rename = "OrderState.FuturesOrder")]
    FuturesOrder,
    /// Futures deal (fill) event.
    #[serde(rename = "OrderState.FuturesDeal")]
    FuturesDeal,
}

#[cfg(test)]
mod tests {
    use rstest::rstest;

    use super::*;

    /// The `octype` wire value must be the bare enum member name, byte-identical
    /// to the gateway `OCType` StrEnum (Auto/New/Cover/DayTrade) which is
    /// resolved via `getattr(sj.constant.FuturesOCType, value)`. A rename here
    /// would be a critical cross-repo mismatch on a real-money order field.
    #[rstest]
    #[case(SinopacOCType::Auto, "\"Auto\"")]
    #[case(SinopacOCType::New, "\"New\"")]
    #[case(SinopacOCType::Cover, "\"Cover\"")]
    #[case(SinopacOCType::DayTrade, "\"DayTrade\"")]
    fn test_octype_serializes_to_member_name(
        #[case] octype: SinopacOCType,
        #[case] expected: &str,
    ) {
        assert_eq!(serde_json::to_string(&octype).unwrap(), expected);
        // Round-trips back from the gateway wire value.
        assert_eq!(
            serde_json::from_str::<SinopacOCType>(expected).unwrap(),
            octype
        );
    }
}
