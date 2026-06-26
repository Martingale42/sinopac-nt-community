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

//! Shared parsing helpers for Sinopac adapter.

use anyhow::Context;
use nautilus_core::UnixNanos;
use nautilus_model::{
    enums::OrderSide,
    identifiers::InstrumentId,
    types::{Price, Quantity},
};

use super::consts::SINOPAC;

/// Builds a `Price` from gateway data, returning an error instead of panicking.
///
/// Wraps [`Price::new_checked`] so malformed gateway floats (NaN, infinite,
/// out-of-range, or too many decimals for `precision`) are surfaced as an
/// error rather than aborting the calling task.
///
/// # Errors
///
/// Returns an error if `value` is NaN, infinite, negative beyond the allowed
/// range, or otherwise invalid for the given `precision`.
pub fn try_price(value: f64, precision: u8) -> anyhow::Result<Price> {
    Price::new_checked(value, precision).map_err(|e| anyhow::anyhow!("invalid price {value}: {e}"))
}

/// Builds a `Quantity` from gateway data; rejects NaN/infinite/negative values.
///
/// Wraps [`Quantity::new_checked`] so malformed gateway floats are surfaced as
/// an error rather than aborting the calling task.
///
/// # Errors
///
/// Returns an error if `value` is NaN, infinite, negative, or otherwise invalid
/// for the given `precision`.
pub fn try_qty(value: f64, precision: u8) -> anyhow::Result<Quantity> {
    Quantity::new_checked(value, precision)
        .map_err(|e| anyhow::anyhow!("invalid quantity {value}: {e}"))
}

/// Constructs an `InstrumentId` from a Sinopac contract code.
///
/// Example: `"2330"` → `InstrumentId("2330.SINOPAC")`
pub fn parse_instrument_id(code: &str) -> anyhow::Result<InstrumentId> {
    InstrumentId::from_as_ref(format!("{code}.{SINOPAC}"))
}

/// Converts a Taiwan local time (UTC+8) `NaiveDateTime` to `UnixNanos`.
pub fn taiwan_naive_to_unix_nanos(dt: chrono::NaiveDateTime) -> anyhow::Result<UnixNanos> {
    let utc = dt - chrono::TimeDelta::hours(8);
    let nanos = utc
        .and_utc()
        .timestamp_nanos_opt()
        .ok_or_else(|| anyhow::anyhow!("Timestamp overflow for {dt}"))?;
    let nanos = u64::try_from(nanos).context("timestamp before unix epoch")?;
    Ok(UnixNanos::from(nanos))
}

/// Maps a Sinopac action string to a Nautilus `OrderSide`.
pub fn parse_order_side(action: &str) -> anyhow::Result<OrderSide> {
    match action {
        "Buy" => Ok(OrderSide::Buy),
        "Sell" => Ok(OrderSide::Sell),
        other => anyhow::bail!("Unknown Sinopac action: {other}"),
    }
}

#[cfg(test)]
mod tests {
    use rstest::rstest;

    use super::*;

    #[rstest]
    fn test_parse_instrument_id() {
        let id = parse_instrument_id("2330").unwrap();
        assert_eq!(id.to_string(), "2330.SINOPAC");
    }

    #[rstest]
    fn test_parse_instrument_id_futures() {
        let id = parse_instrument_id("TXFC6").unwrap();
        assert_eq!(id.to_string(), "TXFC6.SINOPAC");
    }

    #[rstest]
    fn test_parse_order_side_buy() {
        assert_eq!(parse_order_side("Buy").unwrap(), OrderSide::Buy);
    }

    #[rstest]
    fn test_parse_order_side_sell() {
        assert_eq!(parse_order_side("Sell").unwrap(), OrderSide::Sell);
    }

    #[rstest]
    fn test_parse_order_side_unknown() {
        assert!(parse_order_side("Unknown").is_err());
    }

    // `Price` accepts negative values by design (NT spreads), so `-1.0` is valid.
    // Malformed gateway floats are NaN, infinite, or out of the representable range.
    #[rstest]
    #[case(f64::NAN)]
    #[case(f64::INFINITY)]
    #[case(f64::NEG_INFINITY)]
    #[case(1e20)]
    #[case(-1e20)]
    fn test_try_price_rejects_malformed(#[case] value: f64) {
        assert!(try_price(value, 1).is_err());
    }

    #[rstest]
    #[case(f64::NAN)]
    #[case(f64::INFINITY)]
    #[case(f64::NEG_INFINITY)]
    #[case(-1.0)]
    #[case(1e20)]
    fn test_try_qty_rejects_malformed(#[case] value: f64) {
        assert!(try_qty(value, 0).is_err());
    }

    #[rstest]
    fn test_try_price_round_trips_valid() {
        let price = try_price(580.5, 1).unwrap();
        assert_eq!(price, Price::new(580.5, 1));
    }

    #[rstest]
    fn test_try_qty_round_trips_valid() {
        let qty = try_qty(120.0, 0).unwrap();
        assert_eq!(qty, Quantity::new(120.0, 0));
    }

    #[rstest]
    fn test_taiwan_naive_to_unix_nanos_rejects_pre_epoch() {
        // 1960-01-01 Taiwan time is before the unix epoch -> negative nanos.
        let dt = chrono::NaiveDate::from_ymd_opt(1960, 1, 1)
            .unwrap()
            .and_hms_opt(0, 0, 0)
            .unwrap();
        assert!(taiwan_naive_to_unix_nanos(dt).is_err());
    }
}
