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

//! Tick size rules for TWSE/TAIFEX instruments.

/// Returns the TWSE tick size and price precision for a given stock reference price.
///
/// Official TWSE tick size schedule (as of 2020 revision):
///   Price < 10:       tick = 0.01  (precision 2)
///   10 <= Price < 50:  tick = 0.05  (precision 2)
///   50 <= Price < 100: tick = 0.10  (precision 2)
///   100 <= Price < 500: tick = 0.50 (precision 2)
///   500 <= Price < 1000: tick = 1.0 (precision 1)
///   Price >= 1000:     tick = 5.0   (precision 1)
///
/// `reference` is the contract's reference price (yesterday's close / IPO price).
pub fn twse_stock_tick_size(reference: f64) -> (f64, u8) {
    if reference < 10.0 {
        (0.01, 2)
    } else if reference < 50.0 {
        (0.05, 2)
    } else if reference < 100.0 {
        (0.10, 2)
    } else if reference < 500.0 {
        (0.50, 2)
    } else if reference < 1000.0 {
        (1.0, 1)
    } else {
        (5.0, 1)
    }
}

/// Returns the TWSE tick size and price precision for an ETF reference price.
///
/// ETFs / beneficiary certificates (TWSE `category == "00"`) follow a different,
/// coarser-below-50 / finer-above schedule than common stocks:
///   Price < 50:   tick = 0.01 (precision 2)
///   Price >= 50:  tick = 0.05 (precision 2)
///
/// This differs from [`twse_stock_tick_size`]: e.g. an ETF at 104 TWD ticks at
/// 0.05 (not the stock-tier 0.50), and an ETF at 36 TWD ticks at 0.01 (not the
/// stock-tier 0.05). `reference` is the contract's reference price.
pub fn twse_etf_tick_size(reference: f64) -> (f64, u8) {
    if reference < 50.0 { (0.01, 2) } else { (0.05, 2) }
}

/// Returns the tick size and precision for a TAIFEX index / sector futures root.
///
/// Definition: Looks up the official TAIFEX minimum price fluctuation for an
/// index or sector-index futures product by its root symbol, returning `None`
/// for an unrecognized root so the caller can apply a documented fallback.
/// Formula:    tick_size(root) = table[root]; precision = decimals(tick_size).
///             TAIEX-family index futures quote in index points (tick 1.0,
///             precision 0). Sector-index ticks are product-specific: the
///             Mini-Electronics (ZEF) tick is 0.05 index points (precision 2),
///             while the Mini-Finance (ZFF) tick is 0.2 index points
///             (precision 1), per the TAIFEX product specs.
/// Domain:     `root` is the futures root symbol (e.g. "TXF"), i.e. the gateway
///             `category` for an index future. Only index / sector roots belong
///             here; equity- and ETF-underlying (single-stock) futures use
///             [`single_stock_futures_tick_size`] instead, and commodity roots
///             are not tabled.
/// Returns:    `Some((tick_size, precision))` for a known index/sector root,
///             else `None`.
///
/// Source: TAIFEX Equity-Index futures specs, <https://www.taifex.com.tw/enl/eng2/tX>.
///   - XIF tick 1 index point: <https://www.taifex.com.tw/enl/eng2/xIF>.
///   - ZEF tick 0.05 index points (NTD 25/tick): Mini Electronics Sector Index
///     Futures Trading Rules Art. 6, <https://www.taifex.com.tw/enl/eng2/zEF>.
///   - ZFF tick 0.2 index points (NTD 50/tick): <https://www.taifex.com.tw/enl/eng2/zFF>.
pub fn index_futures_tick_size(root: &str) -> Option<(f64, u8)> {
    match root {
        // TAIEX / Mini-TAIEX / TAIEX-50 / Non-Fin-Non-Elec: 1 index point.
        "TXF" | "MXF" | "T5F" | "XIF" => Some((1.0, 0)),
        // Mini-Electronics sector index futures: 0.05 index points (NTD 25/tick,
        // tick_value = 0.05 * 500). NOT 0.2 (the full-size TE tick is also 0.05).
        "ZEF" => Some((0.05, 2)),
        // Mini-Finance sector index futures: 0.2 index points (NTD 50/tick,
        // tick_value = 0.2 * 250).
        "ZFF" => Some((0.2, 1)),
        _ => None,
    }
}

/// Returns the tick size and price precision for a TAIFEX single-stock /
/// ETF-underlying (equity) futures contract by its reference price.
///
/// Definition: The TAIFEX minimum price fluctuation for equity-underlying
/// futures, which is price-tiered on the contract's reference price and mirrors
/// the underlying TWSE cash-equity schedule (single-stock futures trade in the
/// same price grid as their underlying stock).
/// Formula:    tick(P) = 0.01  if P < 10
///                       0.05  if 10  <= P < 50
///                       0.10  if 50  <= P < 100
///                       0.50  if 100 <= P < 500
///                       1.00  if 500 <= P < 1000
///                       5.00  if P >= 1000
///             precision(P) = 2 for P < 500, else 1.
/// Domain:     `reference` is the contract reference price (TWD, prior close /
///             listing price), assumed finite and non-negative. A NaN/negative
///             reference falls through to the lowest tier and is rejected
///             downstream by `try_price`.
/// Returns:    `(tick_size, precision)` in TWD; e.g. a TSMC single-stock future
///             at 2260 TWD ticks at 5.0 (precision 1), a 67.5 TWD name at 0.05
///             (precision 2).
///
/// Source: TAIFEX Single Stock Futures / ETF Futures specs,
/// <https://www.taifex.com.tw/enl/eng2/sSF> (minimum price fluctuation table).
pub fn single_stock_futures_tick_size(reference: f64) -> (f64, u8) {
    if reference < 10.0 {
        (0.01, 2)
    } else if reference < 50.0 {
        (0.05, 2)
    } else if reference < 100.0 {
        (0.10, 2)
    } else if reference < 500.0 {
        (0.50, 2)
    } else if reference < 1000.0 {
        (1.0, 1)
    } else {
        (5.0, 1)
    }
}

/// Returns the tick size and precision for TAIFEX options contracts.
///
/// TXO (TAIEX options): premium < 10 -> tick 0.1, premium >= 10 -> tick 1.0.
/// Uses reference price as proxy for current premium level.
pub fn options_tick_size(reference: f64) -> (f64, u8) {
    if reference < 10.0 { (0.1, 1) } else { (1.0, 0) }
}

#[cfg(test)]
mod tests {
    use rstest::rstest;

    use super::*;

    #[rstest]
    fn test_twse_tick_size_under_10() {
        assert_eq!(twse_stock_tick_size(5.0), (0.01, 2));
    }

    #[rstest]
    fn test_twse_tick_size_10_to_50() {
        assert_eq!(twse_stock_tick_size(25.0), (0.05, 2));
    }

    #[rstest]
    fn test_twse_tick_size_50_to_100() {
        assert_eq!(twse_stock_tick_size(75.0), (0.10, 2));
    }

    #[rstest]
    fn test_twse_tick_size_100_to_500() {
        assert_eq!(twse_stock_tick_size(300.0), (0.50, 2));
    }

    #[rstest]
    fn test_twse_tick_size_500_to_1000() {
        assert_eq!(twse_stock_tick_size(750.0), (1.0, 1));
    }

    #[rstest]
    fn test_twse_tick_size_above_1000() {
        assert_eq!(twse_stock_tick_size(1500.0), (5.0, 1));
    }

    #[rstest]
    fn test_twse_tick_size_boundary_at_10() {
        assert_eq!(twse_stock_tick_size(10.0), (0.05, 2));
    }

    #[rstest]
    fn test_twse_tick_size_tsmc_reference() {
        // TSMC at ~580 TWD
        assert_eq!(twse_stock_tick_size(580.0), (1.0, 1));
    }

    #[rstest]
    fn test_etf_tick_size_below_50() {
        // 00631L at ~36.67 TWD -> ETF tick 0.01 (NOT the stock-tier 0.05)
        assert_eq!(twse_etf_tick_size(36.67), (0.01, 2));
    }

    #[rstest]
    fn test_etf_tick_size_at_or_above_50() {
        // 0050 at ~104 TWD -> ETF tick 0.05 (NOT the stock-tier 0.50)
        assert_eq!(twse_etf_tick_size(104.15), (0.05, 2));
        assert_eq!(twse_etf_tick_size(50.0), (0.05, 2));
    }

    #[rstest]
    fn test_etf_vs_stock_tick_diverge() {
        // Same reference, different schedule: ETF coarser below 50, finer above.
        assert_ne!(twse_etf_tick_size(104.15), twse_stock_tick_size(104.15));
        assert_eq!(twse_etf_tick_size(104.15), (0.05, 2));
        assert_eq!(twse_stock_tick_size(104.15), (0.50, 2));
    }

    #[rstest]
    fn test_index_futures_tick_size_txf() {
        assert_eq!(index_futures_tick_size("TXF"), Some((1.0, 0)));
    }

    #[rstest]
    fn test_index_futures_tick_size_sector() {
        // ZEF (Mini-Electronics): 0.05 index points, precision 2. Official
        // tick value is NTD 25 = 0.05 * 500 (multiplier), per TAIFEX ZEF
        // Trading Rules Art. 6 / <https://www.taifex.com.tw/enl/eng2/zEF>.
        assert_eq!(index_futures_tick_size("ZEF"), Some((0.05, 2)));
        // ZFF (Mini-Finance): 0.2 index points, precision 1. Official tick value
        // is NTD 50 = 0.2 * 250 (multiplier), per <https://www.taifex.com.tw/enl/eng2/zFF>.
        assert_eq!(index_futures_tick_size("ZFF"), Some((0.2, 1)));
    }

    #[rstest]
    fn test_index_futures_tick_size_unknown_root_is_none() {
        // An unknown index root returns None so the caller can warn + fall back.
        assert_eq!(index_futures_tick_size("ZZZ"), None);
    }

    #[rstest]
    #[case(8.0, (0.01, 2))]
    #[case(9.99, (0.01, 2))]
    #[case(10.0, (0.05, 2))]
    #[case(49.99, (0.05, 2))]
    #[case(50.0, (0.10, 2))]
    #[case(99.99, (0.10, 2))]
    #[case(100.0, (0.50, 2))]
    #[case(499.99, (0.50, 2))]
    #[case(500.0, (1.0, 1))]
    #[case(999.99, (1.0, 1))]
    #[case(1000.0, (5.0, 1))]
    #[case(2260.0, (5.0, 1))] // CDFF6 (TSMC single-stock future) reference.
    #[case(31.7, (0.05, 2))] // CAO underlying-tier price.
    fn test_single_stock_futures_tick_size_tiers(
        #[case] reference: f64,
        #[case] expected: (f64, u8),
    ) {
        assert_eq!(single_stock_futures_tick_size(reference), expected);
    }

    #[rstest]
    fn test_options_tick_size_low_premium() {
        assert_eq!(options_tick_size(5.0), (0.1, 1));
    }

    #[rstest]
    fn test_options_tick_size_high_premium() {
        assert_eq!(options_tick_size(50.0), (1.0, 0));
    }
}
