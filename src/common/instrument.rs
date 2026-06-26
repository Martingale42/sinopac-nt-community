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

//! Instrument helpers for Taiwan market contracts.

/// Default stock lot size for Common orders (1000 shares = 1 lot in Taiwan market).
pub const STOCK_LOT_SIZE: f64 = 1000.0;

/// Default lot size for futures/options contracts (1 contract).
pub const CONTRACT_LOT_SIZE: f64 = 1.0;

/// Size precision for all Taiwan instruments (whole shares/contracts).
pub const SIZE_PRECISION: u8 = 0;

/// TWD currency code.
pub const TWD: &str = "TWD";

/// Default contract multiplier (TWD per point) for futures/options whose root
/// is not in the known table. Single-stock futures/options conventionally carry
/// a 2000-share multiplier on TAIFEX.
pub const DEFAULT_CONTRACT_MULTIPLIER: f64 = 2000.0;

/// Returns the known contract multiplier for a TAIFEX futures product.
///
/// Definition: Maps a futures root symbol to its TAIFEX contract multiplier
/// (TWD value of one index point), returning `None` for an unrecognized root so
/// the caller can apply [`DEFAULT_CONTRACT_MULTIPLIER`] and warn.
/// Formula:    multiplier(root) = table[root]; `None` when absent.
/// Domain:     `symbol` is the root symbol (e.g. "TXF", "MXF"), not the delivery
///             code. Only used as a fallback when the gateway does not transmit
///             an authoritative `multiplier` (`multiplier == 0`).
/// Returns:    `Some(multiplier)` in TWD-per-point for a known root, else `None`.
///
/// Source: TAIFEX contract specs (cross-checked against the per-product tick so
/// tick_value = tick * multiplier matches the published NTD-per-tick value):
///   - TXF 200, MXF 50, T5F 100: <https://www.taifex.com.tw/enl/eng2/tX>.
///   - XIF 100 (NTD 100 x per index point): <https://www.taifex.com.tw/enl/eng2/xIF>.
///   - ZEF 500 (NTD 500 x price; tick_value = 0.05 * 500 = NTD 25): Mini
///     Electronics Sector Index Futures Trading Rules Art. 5,
///     <https://www.taifex.com.tw/enl/eng2/zEF>. NOT 4000 (that is the full-size
///     TE multiplier; 1 TE offsets 8 ZEF, 4000/8 = 500).
///   - ZFF 250 (NTD 250 x price; tick_value = 0.2 * 250 = NTD 50):
///     <https://www.taifex.com.tw/enl/eng2/zFF>. NOT 1000 (full-size TF; 1000/4 = 250).
pub fn futures_multiplier(symbol: &str) -> Option<f64> {
    match symbol {
        "TXF" => Some(200.0), // TAIEX futures: 200 TWD per point
        "MXF" => Some(50.0),  // Mini-TAIEX: 50 TWD per point
        "T5F" => Some(100.0), // TAIEX 50 futures
        "XIF" => Some(100.0), // Non-finance/electronics futures: NTD 100 per point
        "ZEF" => Some(500.0), // Mini-Electronics sector futures: 500 TWD per point
        "ZFF" => Some(250.0), // Mini-Finance sector futures: 250 TWD per point
        _ => None,            // Unknown root -> caller applies default + warn
    }
}

/// Returns the known contract multiplier for a TAIFEX options product.
///
/// Definition: Maps an options root symbol to its TAIFEX contract multiplier
/// (TWD per index point), returning `None` for an unrecognized root so the
/// caller can apply [`DEFAULT_CONTRACT_MULTIPLIER`] and warn.
/// Formula:    multiplier(root) = table[root]; `None` when absent.
/// Domain:     `symbol` is the root symbol (e.g. "TXO"). Fallback only, used when
///             the gateway does not transmit an authoritative `multiplier`.
/// Returns:    `Some(multiplier)` in TWD-per-point for a known root, else `None`.
pub fn options_multiplier(symbol: &str) -> Option<f64> {
    match symbol {
        "TXO" => Some(50.0), // TAIEX options: 50 TWD per point
        _ => None,           // Unknown root -> caller applies default + warn
    }
}

#[cfg(test)]
mod tests {
    use rstest::rstest;

    use super::*;

    #[rstest]
    fn test_futures_multiplier_txf() {
        assert_eq!(futures_multiplier("TXF"), Some(200.0));
    }

    #[rstest]
    fn test_futures_multiplier_mxf() {
        assert_eq!(futures_multiplier("MXF"), Some(50.0));
    }

    #[rstest]
    fn test_futures_multiplier_t5f() {
        assert_eq!(futures_multiplier("T5F"), Some(100.0));
    }

    #[rstest]
    fn test_futures_multiplier_xif() {
        // XIF: NTD 100 per index point (tick_value = 1.0 * 100 = NTD 100),
        // per <https://www.taifex.com.tw/enl/eng2/xIF>. NOT 200.
        assert_eq!(futures_multiplier("XIF"), Some(100.0));
    }

    #[rstest]
    fn test_futures_multiplier_zef() {
        // ZEF (Mini-Electronics): NTD 500 per point. tick_value = 0.05 * 500 =
        // NTD 25, matching the published ZEF tick value. NOT 4000 (full-size TE).
        assert_eq!(futures_multiplier("ZEF"), Some(500.0));
    }

    #[rstest]
    fn test_futures_multiplier_zff() {
        // ZFF (Mini-Finance): NTD 250 per point. tick_value = 0.2 * 250 = NTD 50,
        // matching the published ZFF tick value. NOT 1000 (full-size TF).
        assert_eq!(futures_multiplier("ZFF"), Some(250.0));
    }

    #[rstest]
    fn test_futures_multiplier_unknown_is_none() {
        // Unknown root -> None so the caller applies the default + warns.
        assert_eq!(futures_multiplier("ABC"), None);
    }

    #[rstest]
    fn test_options_multiplier_txo() {
        assert_eq!(options_multiplier("TXO"), Some(50.0));
    }

    #[rstest]
    fn test_options_multiplier_unknown_is_none() {
        assert_eq!(options_multiplier("ABC"), None);
    }
}
