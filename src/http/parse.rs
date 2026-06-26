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

//! Parsers that convert Sinopac REST responses to Nautilus domain types.

use nautilus_core::{Params, UnixNanos};
use nautilus_model::{
    data::{Bar, BarType, QuoteTick, TradeTick},
    enums::{AggressorSide, AssetClass, OptionKind},
    identifiers::{InstrumentId, Symbol, TradeId},
    instruments::{
        Equity, FuturesContract as NautilusFuturesContract, InstrumentAny,
        OptionContract as NautilusOptionContract,
    },
    types::Currency,
};
use ustr::Ustr;

use super::models::{
    FuturesContract, KBarsResponse, OptionsContract, SnapshotData, StockContract, TicksResponse,
};
use crate::common::{
    instrument::{
        CONTRACT_LOT_SIZE, DEFAULT_CONTRACT_MULTIPLIER, SIZE_PRECISION, STOCK_LOT_SIZE,
        futures_multiplier, options_multiplier,
    },
    parse::{parse_instrument_id, taiwan_naive_to_unix_nanos, try_price, try_qty},
    tick_size::{
        index_futures_tick_size, options_tick_size, single_stock_futures_tick_size,
        twse_etf_tick_size, twse_stock_tick_size,
    },
};

/// Returns the decimal precision required to represent an option strike price.
///
/// Definition: The number of decimal places needed so the strike round-trips
/// exactly, distinguishing TAIFEX single-stock-option half-point strikes
/// (e.g. `67.5`) from integer index-option strikes (e.g. `20000`).
/// Formula:    precision = 1 if frac(strike) != 0 else 0,
///             where frac(x) = x - floor(x).
/// Domain:     `strike` is a non-negative gateway strike price. TAIFEX strikes
///             step at whole points for index options and at 0.5 for some
///             single-stock options, so one decimal place is always sufficient;
///             NaN/infinite inputs fall through to precision 0 and are rejected
///             downstream by `try_price`.
/// Returns:    `0` for integer-valued strikes, `1` for half-point strikes.
fn strike_precision(strike: f64) -> u8 {
    u8::from(strike.fract() != 0.0)
}

/// Parses a `SnapshotData` into a `QuoteTick` (top-of-book bid/ask).
pub fn parse_snapshot_to_quote_tick(
    snapshot: &SnapshotData,
    instrument_id: InstrumentId,
    price_precision: u8,
    size_precision: u8,
    ts_init: UnixNanos,
) -> anyhow::Result<QuoteTick> {
    QuoteTick::new_checked(
        instrument_id,
        try_price(snapshot.buy_price, price_precision)?,
        try_price(snapshot.sell_price, price_precision)?,
        try_qty(snapshot.buy_volume, size_precision)?,
        try_qty(snapshot.sell_volume, size_precision)?,
        UnixNanos::from(snapshot.ts),
        ts_init,
    )
}

/// Parses a gateway `TicksResponse` into `Vec<TradeTick>`.
///
/// Iterates parallel arrays: ts, close, volume, tick_type.
/// `tick_type`: 1 = Buy (aggressor = buyer), 2 = Sell (aggressor = seller), 0 = unknown.
pub fn parse_ticks_response(
    ticks: &TicksResponse,
    instrument_id: InstrumentId,
    price_precision: u8,
    size_precision: u8,
    ts_init: UnixNanos,
) -> anyhow::Result<Vec<TradeTick>> {
    let n = ticks.ts.len();
    anyhow::ensure!(
        ticks.close.len() == n && ticks.volume.len() == n && ticks.tick_type.len() == n,
        "ticks arrays length mismatch for {}",
        ticks.code
    );

    let mut result = Vec::with_capacity(n);

    for i in 0..n {
        let aggressor_side = match ticks.tick_type[i] {
            1 => AggressorSide::Buyer,
            2 => AggressorSide::Seller,
            _ => AggressorSide::NoAggressor,
        };

        let trade = TradeTick::new_checked(
            instrument_id,
            try_price(ticks.close[i], price_precision)?,
            try_qty(ticks.volume[i] as f64, size_precision)?,
            aggressor_side,
            TradeId::new(format!("{}-{}", ticks.code, ticks.ts[i])),
            UnixNanos::from(ticks.ts[i]),
            ts_init,
        )?;
        result.push(trade);
    }

    Ok(result)
}

/// Parses a gateway `KBarsResponse` into `Vec<Bar>`.
///
/// Iterates parallel arrays: ts, open, high, low, close, volume.
///
/// # Errors
///
/// Returns an error if the OHLCV arrays have mismatched lengths, if any
/// price/volume value is malformed (NaN, infinite, or out of range), or if the
/// OHLC values violate bar cross-field invariants (e.g. `high < low`).
pub fn parse_kbars_response(
    kbars: &KBarsResponse,
    bar_type: BarType,
    price_precision: u8,
    size_precision: u8,
    ts_init: UnixNanos,
) -> anyhow::Result<Vec<Bar>> {
    let n = kbars.ts.len();
    anyhow::ensure!(
        kbars.open.len() == n
            && kbars.high.len() == n
            && kbars.low.len() == n
            && kbars.close.len() == n
            && kbars.volume.len() == n,
        "kbars arrays length mismatch for {}",
        kbars.code
    );

    let mut result = Vec::with_capacity(n);

    for i in 0..n {
        let bar = Bar::new_checked(
            bar_type,
            try_price(kbars.open[i], price_precision)?,
            try_price(kbars.high[i], price_precision)?,
            try_price(kbars.low[i], price_precision)?,
            try_price(kbars.close[i], price_precision)?,
            try_qty(kbars.volume[i] as f64, size_precision)?,
            UnixNanos::from(kbars.ts[i]),
            ts_init,
        )
        .map_err(|e| anyhow::anyhow!("invalid kbar OHLC for {}: {e}", kbars.code))?;
        result.push(bar);
    }

    Ok(result)
}

/// Builds the instrument `info` map carrying the contract reference price.
///
/// Strategies read `instrument.info["reference"]` to place reference-price
/// (flat / unchanged-price) orders; the daily limit-up/limit-down already
/// surface as `max_price`/`min_price`. The map preserves insertion order via `Params`
/// (`IndexMap<String, serde_json::Value>`), matching how sibling adapters
/// populate `info`.
fn build_reference_info(reference: f64) -> Params {
    let mut params = Params::new();
    params.insert("reference".to_string(), serde_json::Value::from(reference));
    params
}

/// Parses a gateway `StockContract` into a Nautilus `Equity` instrument.
///
/// - `InstrumentId` = `{code}.SINOPAC`
/// - Tick size and precision derived from reference price via TWSE schedule
/// - Currency from `contract.currency` (fallback TWD)
/// - Lot size from `contract.unit` (fallback `STOCK_LOT_SIZE` = 1000 shares)
/// - `info["reference"]` carries the contract reference price for reference-price orders
pub fn parse_stock_to_equity(
    contract: &StockContract,
    ts_event: UnixNanos,
    ts_init: UnixNanos,
) -> anyhow::Result<InstrumentAny> {
    let instrument_id = parse_instrument_id(&contract.code)?;
    let raw_symbol = Symbol::new(&contract.code);
    let currency = parse_currency_or_twd(&contract.currency);

    // ETFs / beneficiary certificates (TWSE category "00") follow a different
    // tick schedule than common stocks (e.g. 0050 @104 ticks 0.05 not 0.50;
    // 00631L @36 ticks 0.01 not 0.05).
    let (tick_size, price_precision) = if contract.category == "00" {
        twse_etf_tick_size(contract.reference)
    } else {
        twse_stock_tick_size(contract.reference)
    };
    let price_increment = try_price(tick_size, price_precision)?;
    let lot_size_val = if contract.unit > 0.0 {
        contract.unit
    } else {
        STOCK_LOT_SIZE
    };
    let lot_size = Some(try_qty(lot_size_val, SIZE_PRECISION)?);

    let max_price = Some(try_price(contract.limit_up, price_precision)?);
    let min_price = Some(try_price(contract.limit_down, price_precision)?);

    let equity = Equity::new(
        instrument_id,
        raw_symbol,
        None, // isin
        currency,
        price_precision,
        price_increment,
        lot_size,
        None, // max_quantity
        None, // min_quantity
        max_price,
        min_price,
        None, // margin_init
        None, // margin_maint
        None, // maker_fee
        None, // taker_fee
        Some(build_reference_info(contract.reference)),
        ts_event,
        ts_init,
    );

    Ok(InstrumentAny::Equity(equity))
}

/// Selects the TAIFEX tick size and precision for a futures contract from its
/// contract evidence (SINOPAC-09/10).
///
/// Definition: Routes a futures contract to the correct TAIFEX minimum-price-
/// fluctuation schedule using the live-verified `underlying_kind` /
/// `underlying_code` semantics rather than the inconsistent `category` field.
/// Formula:    - `underlying_kind == "I"` (index, empty `underlying_code`):
///               look up the index/sector root table; unknown root warns and
///               falls back to `(1.0, 0)`.
///             - common-stock underlying (`underlying_kind == "S"`, non-empty
///               `underlying_code`): price-tiered common-stock single-stock-
///               futures grid via [`single_stock_futures_tick_size`].
///             - ETF underlying (`underlying_kind == "E"`): the *ETF*-futures
///               grid (< 50 -> 0.01, >= 50 -> 0.05) via [`twse_etf_tick_size`],
///               which differs from the common-stock grid above 50 TWD. ETF
///               futures share the cash-ETF schedule per the TAIFEX spec.
///             - any other kind (e.g. commodity `"C"`, for which TAIFEX ticks
///               are product-specific and not tabled here): warn and fall back
///               to `(1.0, 0)`.
/// Domain:     `contract.underlying_kind` is one of the live-dumped values
///             {"S","I","E","C"}; `reference` is the contract reference price.
/// Returns:    `(tick_size, precision)` in TWD for the contract's price grid.
///
/// Source: TAIFEX Single Stock Futures / ETF Futures spec (separate common-stock
/// and ETF minimum-price-fluctuation tables), <https://www.taifex.com.tw/enl/eng2/sSF>.
fn futures_tick_for_contract(contract: &FuturesContract) -> (f64, u8) {
    let root = contract.category.as_str();
    match contract.underlying_kind.as_str() {
        // Index / sector-index futures (TXF, MXF, sector roots, ...).
        "I" => index_futures_tick_size(root).unwrap_or_else(|| {
            log::warn!("Unknown futures root {root}, using default tick 1.0");
            (1.0, 0)
        }),
        // Common-stock single-stock futures: price-tiered common-stock grid.
        "S" => single_stock_futures_tick_size(contract.reference),
        // ETF-underlying futures use the distinct cash-ETF grid (< 50 -> 0.01,
        // >= 50 -> 0.05), NOT the common-stock tiers (which are coarser above 50).
        "E" => twse_etf_tick_size(contract.reference),
        // Commodity ("C") and any future kind: no tabled tick -> documented
        // unknown-root fallback.
        _ => {
            log::warn!("Unknown futures root {root}, using default tick 1.0");
            (1.0, 0)
        }
    }
}

/// Parses a gateway `FuturesContract` into a Nautilus `FuturesContract` instrument.
///
/// - Multiplier from `contract.multiplier` (Shioaji authoritative); falls back
///   to the `futures_multiplier(root_symbol)` table when `multiplier == 0`, and
///   to `DEFAULT_CONTRACT_MULTIPLIER` (with a warn) for an unknown root
/// - Lot size from `contract.unit` (fallback `CONTRACT_LOT_SIZE`)
/// - Underlying from `contract.underlying_code` (fallback root symbol / category)
/// - Currency from `contract.currency` (fallback TWD)
/// - Tick size selected by contract evidence via [`futures_tick_for_contract`];
///   expiration from `delivery_date`
/// - `info["reference"]` carries the contract reference price for reference-price orders
pub fn parse_futures_to_contract(
    contract: &FuturesContract,
    ts_event: UnixNanos,
    ts_init: UnixNanos,
) -> anyhow::Result<InstrumentAny> {
    let instrument_id = parse_instrument_id(&contract.code)?;
    let raw_symbol = Symbol::new(&contract.code);
    let currency = parse_currency_or_twd(&contract.currency);

    let root_symbol = &contract.category;
    let (tick_size, price_precision) = futures_tick_for_contract(contract);
    let price_increment = try_price(tick_size, price_precision)?;
    let multiplier_val = if contract.multiplier > 0 {
        // SDK-authoritative multiplier takes priority (no fallback, no warn).
        contract.multiplier as f64
    } else {
        // SDK did not transmit a multiplier (the normal sim path): use the known
        // root table silently; only an unknown root warrants a warn.
        futures_multiplier(root_symbol).unwrap_or_else(|| {
            log::warn!(
                "Unknown futures root {root_symbol}, using default multiplier {DEFAULT_CONTRACT_MULTIPLIER}"
            );
            DEFAULT_CONTRACT_MULTIPLIER
        })
    };
    let multiplier = try_qty(multiplier_val, 0)?;
    let lot_size_val = if contract.unit > 0.0 {
        contract.unit
    } else {
        CONTRACT_LOT_SIZE
    };
    let lot_size = try_qty(lot_size_val, SIZE_PRECISION)?;
    let underlying = if contract.underlying_code.is_empty() {
        Ustr::from(root_symbol)
    } else {
        Ustr::from(contract.underlying_code.as_str())
    };

    let expiration_ns = parse_date_to_nanos(&contract.delivery_date)?;
    let activation_ns = parse_date_to_nanos(&contract.update_date).unwrap_or(ts_event);

    let asset_class = match contract.underlying_kind.as_str() {
        "I" => AssetClass::Index,
        _ => AssetClass::Equity,
    };

    let max_price = Some(try_price(contract.limit_up, price_precision)?);
    let min_price = Some(try_price(contract.limit_down, price_precision)?);

    let futures = NautilusFuturesContract::new(
        instrument_id,
        raw_symbol,
        asset_class,
        Some(Ustr::from("TAIFEX")),
        underlying,
        activation_ns,
        expiration_ns,
        currency,
        price_precision,
        price_increment,
        multiplier,
        lot_size,
        None, // max_quantity
        None, // min_quantity
        max_price,
        min_price,
        None, // margin_init
        None, // margin_maint
        None, // maker_fee
        None, // taker_fee
        Some(build_reference_info(contract.reference)),
        ts_event,
        ts_init,
    );

    Ok(InstrumentAny::FuturesContract(futures))
}

/// Parses a gateway `OptionsContract` into a Nautilus `OptionContract` instrument.
///
/// - Multiplier from `contract.multiplier` (Shioaji authoritative); falls back
///   to the `options_multiplier(root_symbol)` table when `multiplier == 0`, and
///   to `DEFAULT_CONTRACT_MULTIPLIER` (with a warn) for an unknown root
/// - Lot size from `contract.unit` (fallback `CONTRACT_LOT_SIZE`)
/// - Underlying from `contract.underlying_code` (fallback root symbol / category)
/// - Currency from `contract.currency` (fallback TWD)
/// - Tick size from `options_tick_size()` based on reference premium
/// - Option kind parsed from `option_right` ("C" = Call / "P" = Put)
/// - `info["reference"]` carries the contract reference price for reference-price orders
pub fn parse_options_to_contract(
    contract: &OptionsContract,
    ts_event: UnixNanos,
    ts_init: UnixNanos,
) -> anyhow::Result<InstrumentAny> {
    let instrument_id = parse_instrument_id(&contract.code)?;
    let raw_symbol = Symbol::new(&contract.code);
    let currency = parse_currency_or_twd(&contract.currency);

    let root_symbol = &contract.category;
    let (tick_size, price_precision) = options_tick_size(contract.reference);
    let price_increment = try_price(tick_size, price_precision)?;
    let multiplier_val = if contract.multiplier > 0 {
        // SDK-authoritative multiplier takes priority (no fallback, no warn).
        contract.multiplier as f64
    } else {
        // SDK did not transmit a multiplier (the normal sim path): use the known
        // root table silently; only an unknown root warrants a warn.
        options_multiplier(root_symbol).unwrap_or_else(|| {
            log::warn!(
                "Unknown options root {root_symbol}, using default multiplier {DEFAULT_CONTRACT_MULTIPLIER}"
            );
            DEFAULT_CONTRACT_MULTIPLIER
        })
    };
    let multiplier = try_qty(multiplier_val, 0)?;
    let lot_size_val = if contract.unit > 0.0 {
        contract.unit
    } else {
        CONTRACT_LOT_SIZE
    };
    let lot_size = try_qty(lot_size_val, SIZE_PRECISION)?;
    let underlying = if contract.underlying_code.is_empty() {
        Ustr::from(root_symbol)
    } else {
        Ustr::from(contract.underlying_code.as_str())
    };

    let option_kind = match contract.option_right.as_str() {
        "C" => OptionKind::Call,
        "P" => OptionKind::Put,
        other => anyhow::bail!("Unknown option_right {other:?} (expected 'C'/'P')"),
    };

    let strike_price = try_price(
        contract.strike_price,
        strike_precision(contract.strike_price),
    )?;

    let expiration_ns = parse_date_to_nanos(&contract.delivery_date)?;
    let activation_ns = parse_date_to_nanos(&contract.update_date).unwrap_or(ts_event);

    let asset_class = match contract.underlying_kind.as_str() {
        "I" => AssetClass::Index,
        _ => AssetClass::Equity,
    };

    let max_price = Some(try_price(contract.limit_up, price_precision)?);
    let min_price = Some(try_price(contract.limit_down, price_precision)?);

    let option = NautilusOptionContract::new(
        instrument_id,
        raw_symbol,
        asset_class,
        Some(Ustr::from("TAIFEX")),
        underlying,
        option_kind,
        strike_price,
        currency,
        activation_ns,
        expiration_ns,
        price_precision,
        price_increment,
        multiplier,
        lot_size,
        None, // max_quantity
        None, // min_quantity
        max_price,
        min_price,
        None, // margin_init
        None, // margin_maint
        None, // maker_fee
        None, // taker_fee
        Some(build_reference_info(contract.reference)),
        ts_event,
        ts_init,
    );

    Ok(InstrumentAny::OptionContract(option))
}

/// Resolves a gateway currency code string to a Nautilus `Currency`.
///
/// Falls back to TWD when the code is empty or not a recognized ISO code, so a
/// missing/partial gateway field never panics. All Taiwan venue instruments are
/// quoted in TWD, making it a safe default.
fn parse_currency_or_twd(code: &str) -> Currency {
    if code.is_empty() {
        return Currency::TWD();
    }
    Currency::try_from_str(code).unwrap_or_else(Currency::TWD)
}

/// Parses a date string like "2026/06/17" or "2026-06-17" to `UnixNanos`.
///
/// Treats the date as midnight in Taiwan time (UTC+8).
fn parse_date_to_nanos(date_str: &str) -> anyhow::Result<UnixNanos> {
    let normalized = date_str.replace('/', "-");
    let date = chrono::NaiveDate::parse_from_str(&normalized, "%Y-%m-%d")?;
    let datetime = date
        .and_hms_opt(0, 0, 0)
        .ok_or_else(|| anyhow::anyhow!("Invalid date: {date_str}"))?;
    taiwan_naive_to_unix_nanos(datetime)
}

#[cfg(test)]
mod tests {
    use nautilus_model::{
        data::BarSpecification,
        enums::{AggregationSource, BarAggregation, PriceType},
        identifiers::{Symbol, Venue},
        instruments::Instrument,
        types::Price,
    };
    use rstest::rstest;

    use super::*;
    use crate::{
        common::testing::load_test_json_as,
        http::models::{FuturesContract, OptionsContract},
    };

    fn test_instrument_id() -> InstrumentId {
        InstrumentId::new(Symbol::new("2330"), Venue::new("SINOPAC"))
    }

    #[rstest]
    fn test_parse_snapshot_to_quote_tick() {
        let snapshots: Vec<SnapshotData> = load_test_json_as("market_snapshots.json");
        let quote = parse_snapshot_to_quote_tick(
            &snapshots[0],
            test_instrument_id(),
            1,
            0,
            UnixNanos::default(),
        )
        .unwrap();

        assert_eq!(quote.instrument_id, test_instrument_id());
        assert_eq!(quote.bid_price, Price::new(580.0, 1));
        assert_eq!(quote.ask_price, Price::new(581.0, 1));
    }

    #[rstest]
    fn test_parse_ticks_response() {
        let ticks: TicksResponse = load_test_json_as("market_ticks.json");
        let trades =
            parse_ticks_response(&ticks, test_instrument_id(), 1, 0, UnixNanos::default()).unwrap();

        assert_eq!(trades.len(), 2);
        assert_eq!(trades[0].price, Price::new(580.0, 1));
        assert_eq!(trades[0].aggressor_side, AggressorSide::Buyer);
        assert_eq!(trades[1].price, Price::new(581.0, 1));
        assert_eq!(trades[1].aggressor_side, AggressorSide::Seller);
    }

    #[rstest]
    fn test_parse_kbars_response() {
        let kbars: KBarsResponse = load_test_json_as("market_kbars.json");
        let bar_type = BarType::new(
            test_instrument_id(),
            BarSpecification::new(1, BarAggregation::Minute, PriceType::Last),
            AggregationSource::External,
        );
        let bars = parse_kbars_response(&kbars, bar_type, 1, 0, UnixNanos::default()).unwrap();

        assert_eq!(bars.len(), 2);
        assert_eq!(bars[0].open, Price::new(578.0, 1));
        assert_eq!(bars[0].high, Price::new(582.0, 1));
        assert_eq!(bars[0].close, Price::new(580.0, 1));
        assert_eq!(bars[1].open, Price::new(580.0, 1));
    }

    #[rstest]
    fn test_parse_stock_to_equity_tsmc() {
        let contracts: Vec<StockContract> = load_test_json_as("contracts_stocks.json");
        let equity = parse_stock_to_equity(
            &contracts[0], // 2330 TSMC, reference=580.0
            UnixNanos::default(),
            UnixNanos::default(),
        )
        .unwrap();

        match equity {
            InstrumentAny::Equity(e) => {
                assert_eq!(e.id().to_string(), "2330.SINOPAC");
                assert_eq!(e.price_precision(), 1); // 580 TWD -> tick=1.0 -> precision 1
                assert_eq!(e.quote_currency().code.as_str(), "TWD");
                assert!(e.lot_size().is_some());
                assert_eq!(e.lot_size().unwrap().as_f64(), 1000.0);
                assert!(e.max_price().is_some());
            }
            _ => panic!("Expected Equity"),
        }
    }

    #[rstest]
    fn test_parse_futures_to_contract_txf() {
        let contracts: Vec<FuturesContract> = load_test_json_as("contracts_futures.json");
        let instrument = parse_futures_to_contract(
            &contracts[0], // TXFC6
            UnixNanos::default(),
            UnixNanos::default(),
        )
        .unwrap();

        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.id().to_string(), "TXFC6.SINOPAC");
                assert_eq!(f.underlying().unwrap().as_str(), "TXF");
                assert_eq!(f.multiplier().as_f64(), 200.0);
                assert_eq!(f.price_precision(), 0); // TXF tick=1.0
                assert_eq!(f.lot_size().unwrap().as_f64(), 1.0);
                assert_eq!(f.quote_currency().code.as_str(), "TWD");
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_parse_options_to_contract_call() {
        let contracts: Vec<OptionsContract> = load_test_json_as("contracts_options.json");
        let instrument = parse_options_to_contract(
            &contracts[0], // TXO20000C6, strike=20000, Call
            UnixNanos::default(),
            UnixNanos::default(),
        )
        .unwrap();

        match instrument {
            InstrumentAny::OptionContract(o) => {
                assert_eq!(o.id().to_string(), "TXO20000C6.SINOPAC");
                assert_eq!(o.option_kind(), Some(OptionKind::Call));
                assert_eq!(o.strike_price().unwrap().as_f64(), 20000.0);
                assert_eq!(o.underlying().unwrap().as_str(), "TXO");
                assert_eq!(o.multiplier().as_f64(), 50.0);
                assert_eq!(o.quote_currency().code.as_str(), "TWD");
            }
            _ => panic!("Expected OptionContract"),
        }
    }

    #[rstest]
    fn test_parse_stock_info_contains_reference() {
        // Strategies read instrument.info["reference"] for reference-price
        // orders; the 2330 fixture reference is 580.0.
        let contracts: Vec<StockContract> = load_test_json_as("contracts_stocks.json");
        let equity =
            parse_stock_to_equity(&contracts[0], UnixNanos::default(), UnixNanos::default())
                .unwrap();

        match equity {
            InstrumentAny::Equity(e) => {
                let info = e.info.as_ref().expect("info should be populated");
                assert_eq!(info.get("reference").and_then(|v| v.as_f64()), Some(580.0));
            }
            _ => panic!("Expected Equity"),
        }
    }

    #[rstest]
    fn test_parse_futures_info_contains_reference() {
        // The TXFC6 fixture reference is 20000.0.
        let contracts: Vec<FuturesContract> = load_test_json_as("contracts_futures.json");
        let instrument =
            parse_futures_to_contract(&contracts[0], UnixNanos::default(), UnixNanos::default())
                .unwrap();

        match instrument {
            InstrumentAny::FuturesContract(f) => {
                let info = f.info.as_ref().expect("info should be populated");
                assert_eq!(
                    info.get("reference").and_then(|v| v.as_f64()),
                    Some(20000.0)
                );
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_parse_options_info_contains_reference() {
        // The TXO20000C6 fixture reference (premium) is 500.0.
        let contracts: Vec<OptionsContract> = load_test_json_as("contracts_options.json");
        let instrument =
            parse_options_to_contract(&contracts[0], UnixNanos::default(), UnixNanos::default())
                .unwrap();

        match instrument {
            InstrumentAny::OptionContract(o) => {
                let info = o.info.as_ref().expect("info should be populated");
                assert_eq!(info.get("reference").and_then(|v| v.as_f64()), Some(500.0));
            }
            _ => panic!("Expected OptionContract"),
        }
    }

    #[rstest]
    fn test_parse_date_to_nanos_slash_format() {
        // 2026/06/17 00:00:00 +08:00 = 2026-06-16T16:00:00Z
        let nanos = parse_date_to_nanos("2026/06/17").unwrap();
        assert_eq!(nanos.as_u64(), 1_781_625_600_000_000_000);
    }

    #[rstest]
    fn test_parse_date_to_nanos_dash_format() {
        // 2026-03-02 00:00:00 +08:00 = 2026-03-01T16:00:00Z
        let nanos = parse_date_to_nanos("2026-03-02").unwrap();
        assert_eq!(nanos.as_u64(), 1_772_380_800_000_000_000);
    }

    #[rstest]
    fn test_parse_all_instrument_types() {
        // Stocks -> Equity
        let stocks: Vec<StockContract> = load_test_json_as("contracts_stocks.json");
        for stock in &stocks {
            let instrument =
                parse_stock_to_equity(stock, UnixNanos::default(), UnixNanos::default());
            assert!(instrument.is_ok(), "Failed to parse stock: {}", stock.code);
            match instrument.unwrap() {
                InstrumentAny::Equity(_) => {}
                other => panic!("Expected Equity for {}, got {other:?}", stock.code),
            }
        }

        // Futures -> FuturesContract
        let futures: Vec<FuturesContract> = load_test_json_as("contracts_futures.json");
        for f in &futures {
            let instrument =
                parse_futures_to_contract(f, UnixNanos::default(), UnixNanos::default());
            assert!(instrument.is_ok(), "Failed to parse futures: {}", f.code);
            match instrument.unwrap() {
                InstrumentAny::FuturesContract(_) => {}
                other => panic!("Expected FuturesContract for {}, got {other:?}", f.code),
            }
        }

        // Options -> OptionContract
        let options: Vec<OptionsContract> = load_test_json_as("contracts_options.json");
        for opt in &options {
            let instrument =
                parse_options_to_contract(opt, UnixNanos::default(), UnixNanos::default());
            assert!(instrument.is_ok(), "Failed to parse option: {}", opt.code);
            match instrument.unwrap() {
                InstrumentAny::OptionContract(_) => {}
                other => panic!("Expected OptionContract for {}, got {other:?}", opt.code),
            }
        }
    }

    #[rstest]
    fn test_parse_stock_low_price_different_tick_size() {
        let contract = StockContract {
            code: "9999".to_string(),
            symbol: "TSE9999".to_string(),
            name: "Test".to_string(),
            exchange: "TSE".to_string(),
            category: "Test".to_string(),
            limit_up: 8.8,
            limit_down: 7.2,
            reference: 8.0, // < 10 TWD -> tick=0.01, precision=2
            update_date: "2026-03-04".to_string(),
            day_trade: "No".to_string(),
            unit: 0.0,
            multiplier: 0,
            currency: String::new(),
        };

        let instrument =
            parse_stock_to_equity(&contract, UnixNanos::default(), UnixNanos::default()).unwrap();
        match instrument {
            InstrumentAny::Equity(e) => {
                assert_eq!(e.price_precision(), 2); // 0.01 tick -> 2 decimals
            }
            _ => panic!("Expected Equity"),
        }
    }

    fn etf_contract(code: &str, reference: f64) -> StockContract {
        StockContract {
            code: code.to_string(),
            symbol: format!("TSE{code}"),
            name: "ETF".to_string(),
            exchange: "TSE".to_string(),
            category: "00".to_string(), // TWSE ETF / beneficiary-cert category
            limit_up: reference * 1.1,
            limit_down: reference * 0.9,
            reference,
            update_date: "2026-06-08".to_string(),
            day_trade: "Yes".to_string(),
            unit: 1000.0,
            multiplier: 0,
            currency: "TWD".to_string(),
        }
    }

    #[rstest]
    fn test_parse_etf_above_50_uses_etf_tick() {
        // 0050 @104.15: ETF tick 0.05 (NOT the stock-tier 0.50).
        let contract = etf_contract("0050", 104.15);
        let instrument =
            parse_stock_to_equity(&contract, UnixNanos::default(), UnixNanos::default()).unwrap();
        match instrument {
            InstrumentAny::Equity(e) => {
                assert_eq!(e.price_increment().as_f64(), 0.05);
                assert_eq!(e.price_precision(), 2);
            }
            _ => panic!("Expected Equity"),
        }
    }

    #[rstest]
    fn test_parse_etf_below_50_uses_etf_tick() {
        // 00631L @36.67: ETF tick 0.01 (NOT the stock-tier 0.05).
        let contract = etf_contract("00631L", 36.67);
        let instrument =
            parse_stock_to_equity(&contract, UnixNanos::default(), UnixNanos::default()).unwrap();
        match instrument {
            InstrumentAny::Equity(e) => {
                assert_eq!(e.price_increment().as_f64(), 0.01);
                assert_eq!(e.price_precision(), 2);
            }
            _ => panic!("Expected Equity"),
        }
    }

    #[rstest]
    fn test_parse_stock_same_reference_uses_stock_tick() {
        // Same 104.15 reference but a non-ETF category -> stock-tier 0.50.
        let mut contract = etf_contract("2330", 104.15);
        contract.category = "24".to_string(); // semiconductor industry, not ETF
        let instrument =
            parse_stock_to_equity(&contract, UnixNanos::default(), UnixNanos::default()).unwrap();
        match instrument {
            InstrumentAny::Equity(e) => {
                assert_eq!(e.price_increment().as_f64(), 0.50);
            }
            _ => panic!("Expected Equity"),
        }
    }

    // --- Financial-correctness helpers + tests for WS-B authoritative parsing ----------

    /// Builds a minimal `FuturesContract` for parse tests with explicit
    /// `multiplier`/`unit` so we can verify authoritative-vs-fallback behaviour.
    fn make_futures_contract(multiplier: i64, unit: f64) -> FuturesContract {
        FuturesContract {
            code: "TXFC6".to_string(),
            symbol: "TXFC6".to_string(),
            name: "Test".to_string(),
            category: "TXF".to_string(),
            delivery_month: "2026/06".to_string(),
            delivery_date: "2026/06/17".to_string(),
            underlying_kind: "I".to_string(),
            limit_up: 22000.0,
            limit_down: 18000.0,
            reference: 20000.0,
            update_date: "2026-03-02".to_string(),
            unit,
            multiplier,
            currency: "TWD".to_string(),
            underlying_code: "TXF".to_string(),
        }
    }

    /// Builds a minimal `OptionsContract` for parse tests with explicit
    /// `option_right`/`multiplier`/`unit`.
    fn make_options_contract(option_right: &str, multiplier: i64, unit: f64) -> OptionsContract {
        OptionsContract {
            code: "TXO20000C6".to_string(),
            symbol: "TXO20000C6".to_string(),
            name: "Test".to_string(),
            category: "TXO".to_string(),
            delivery_month: "2026/06".to_string(),
            delivery_date: "2026/06/17".to_string(),
            strike_price: 20000.0,
            option_right: option_right.to_string(),
            underlying_kind: "I".to_string(),
            limit_up: 2200.0,
            limit_down: 0.1,
            reference: 500.0,
            update_date: "2026-03-02".to_string(),
            unit,
            multiplier,
            currency: "TWD".to_string(),
            underlying_code: "TXO".to_string(),
        }
    }

    #[rstest]
    fn test_option_right_c_parses_call() {
        let contract = make_options_contract("C", 50, 1.0);
        let instrument =
            parse_options_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::OptionContract(o) => {
                assert_eq!(o.option_kind(), Some(OptionKind::Call));
            }
            _ => panic!("Expected OptionContract"),
        }
    }

    #[rstest]
    fn test_option_right_p_parses_put() {
        let contract = make_options_contract("P", 50, 1.0);
        let instrument =
            parse_options_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::OptionContract(o) => {
                assert_eq!(o.option_kind(), Some(OptionKind::Put));
            }
            _ => panic!("Expected OptionContract"),
        }
    }

    #[rstest]
    fn test_option_right_unknown_bails() {
        // Pre-WS-A spelling "Call" is now an unknown value and must bail!.
        let contract = make_options_contract("Call", 50, 1.0);
        let result =
            parse_options_to_contract(&contract, UnixNanos::default(), UnixNanos::default());
        assert!(result.is_err());
    }

    #[rstest]
    fn test_futures_uses_authoritative_multiplier() {
        // 777 is NOT in the hardcoded table -> proves contract.multiplier is used.
        let contract = make_futures_contract(777, 1.0);
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.multiplier().as_f64(), 777.0);
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_futures_multiplier_zero_falls_back_to_table() {
        // multiplier == 0 -> fallback to futures_multiplier("TXF") == 200.
        let contract = make_futures_contract(0, 1.0);
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.multiplier().as_f64(), 200.0);
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_options_uses_authoritative_multiplier() {
        // 99 is NOT the TXO table value (50) -> proves contract.multiplier is used.
        let contract = make_options_contract("C", 99, 1.0);
        let instrument =
            parse_options_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::OptionContract(o) => {
                assert_eq!(o.multiplier().as_f64(), 99.0);
            }
            _ => panic!("Expected OptionContract"),
        }
    }

    #[rstest]
    fn test_options_multiplier_zero_falls_back_to_table() {
        // multiplier == 0 -> fallback to options_multiplier("TXO") == 50.
        let contract = make_options_contract("C", 0, 1.0);
        let instrument =
            parse_options_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::OptionContract(o) => {
                assert_eq!(o.multiplier().as_f64(), 50.0);
            }
            _ => panic!("Expected OptionContract"),
        }
    }

    #[rstest]
    fn test_futures_unit_sets_lot_size() {
        // Non-default unit (5) must flow through to lot_size.
        let contract = make_futures_contract(200, 5.0);
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.lot_size().unwrap().as_f64(), 5.0);
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_stock_unit_sets_lot_size() {
        let contract = StockContract {
            code: "2330".to_string(),
            symbol: "TSE2330".to_string(),
            name: "Test".to_string(),
            exchange: "TSE".to_string(),
            category: "Electronics".to_string(),
            limit_up: 638.0,
            limit_down: 522.0,
            reference: 580.0,
            update_date: "2026-03-02".to_string(),
            day_trade: "Yes".to_string(),
            unit: 100.0, // odd-lot style unit -> lot_size must follow
            multiplier: 0,
            currency: "TWD".to_string(),
        };
        let instrument =
            parse_stock_to_equity(&contract, UnixNanos::default(), UnixNanos::default()).unwrap();
        match instrument {
            InstrumentAny::Equity(e) => {
                assert_eq!(e.lot_size().unwrap().as_f64(), 100.0);
            }
            _ => panic!("Expected Equity"),
        }
    }

    #[rstest]
    fn test_stock_missing_unit_falls_back_to_default_lot() {
        // unit == 0 -> fallback to STOCK_LOT_SIZE (1000).
        let contract = StockContract {
            code: "2330".to_string(),
            symbol: "TSE2330".to_string(),
            name: "Test".to_string(),
            exchange: "TSE".to_string(),
            category: "Electronics".to_string(),
            limit_up: 638.0,
            limit_down: 522.0,
            reference: 580.0,
            update_date: "2026-03-02".to_string(),
            day_trade: "Yes".to_string(),
            unit: 0.0,
            multiplier: 0,
            currency: String::new(), // empty -> TWD fallback
        };
        let instrument =
            parse_stock_to_equity(&contract, UnixNanos::default(), UnixNanos::default()).unwrap();
        match instrument {
            InstrumentAny::Equity(e) => {
                assert_eq!(e.lot_size().unwrap().as_f64(), 1000.0);
                assert_eq!(e.quote_currency().code.as_str(), "TWD");
            }
            _ => panic!("Expected Equity"),
        }
    }

    // --- Bounds-safety and value-range validation (Task 2.3) -------------------------------

    #[rstest]
    fn test_parse_ticks_mismatched_lengths_errors() {
        // `close` is shorter than `ts`: indexing would panic without the guard.
        let ticks = TicksResponse {
            code: "2330".to_string(),
            ts: vec![1_000, 2_000, 3_000],
            close: vec![580.0, 581.0],
            volume: vec![100, 200, 300],
            bid_price: vec![],
            ask_price: vec![],
            tick_type: vec![1, 2, 1],
        };
        let result = parse_ticks_response(&ticks, test_instrument_id(), 1, 0, UnixNanos::default());
        assert!(result.is_err());
    }

    #[rstest]
    fn test_parse_kbars_mismatched_lengths_errors() {
        // `volume` is shorter than `ts`: indexing would panic without the guard.
        let kbars = KBarsResponse {
            code: "2330".to_string(),
            ts: vec![1_000, 2_000],
            open: vec![578.0, 580.0],
            high: vec![582.0, 583.0],
            low: vec![577.0, 579.0],
            close: vec![580.0, 581.0],
            volume: vec![5_000],
        };
        let bar_type = BarType::new(
            test_instrument_id(),
            BarSpecification::new(1, BarAggregation::Minute, PriceType::Last),
            AggregationSource::External,
        );
        let result = parse_kbars_response(&kbars, bar_type, 1, 0, UnixNanos::default());
        assert!(result.is_err());
    }

    #[rstest]
    fn test_parse_kbars_scrambled_ohlc_errors_not_panics() {
        // Each field is individually valid (finite, in range), but the OHLC
        // ordering is logically inconsistent (high < low). `Bar::new` would
        // panic on this cross-field invariant; `Bar::new_checked` must Err.
        let kbars = KBarsResponse {
            code: "2330".to_string(),
            ts: vec![1_000],
            open: vec![580.0],
            high: vec![570.0], // high < low: invariant violation
            low: vec![590.0],
            close: vec![580.0],
            volume: vec![5_000],
        };
        let bar_type = BarType::new(
            test_instrument_id(),
            BarSpecification::new(1, BarAggregation::Minute, PriceType::Last),
            AggregationSource::External,
        );
        let result = parse_kbars_response(&kbars, bar_type, 1, 0, UnixNanos::default());
        assert!(result.is_err());
    }

    #[rstest]
    fn test_parse_stock_infinite_unit_errors_not_panics() {
        // `f64::INFINITY > 0.0` is true, so an infinite gateway `unit` defeats
        // the `unit > 0.0` guard and would reach the panicking `Quantity::new`.
        // Routing through `try_qty(...)?` must Err instead of aborting the
        // provider load.
        let contract = StockContract {
            code: "2330".to_string(),
            symbol: "TSE2330".to_string(),
            name: "Test".to_string(),
            exchange: "TSE".to_string(),
            category: "Electronics".to_string(),
            limit_up: 638.0,
            limit_down: 522.0,
            reference: 580.0,
            update_date: "2026-03-02".to_string(),
            day_trade: "Yes".to_string(),
            unit: f64::INFINITY,
            multiplier: 0,
            currency: "TWD".to_string(),
        };
        let result = parse_stock_to_equity(&contract, UnixNanos::default(), UnixNanos::default());
        assert!(result.is_err());
    }

    #[rstest]
    fn test_parse_futures_infinite_unit_errors_not_panics() {
        let contract = make_futures_contract(200, f64::INFINITY);
        let result =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default());
        assert!(result.is_err());
    }

    #[rstest]
    fn test_parse_options_infinite_unit_errors_not_panics() {
        let contract = make_options_contract("C", 50, f64::INFINITY);
        let result =
            parse_options_to_contract(&contract, UnixNanos::default(), UnixNanos::default());
        assert!(result.is_err());
    }

    #[rstest]
    fn test_strike_precision_fractional_and_integer() {
        assert_eq!(strike_precision(12.5), 1);
        assert_eq!(strike_precision(67.5), 1);
        assert_eq!(strike_precision(20000.0), 0);
    }

    #[rstest]
    fn test_parse_options_fractional_strike_round_trips() {
        // TAIFEX single-stock-option half-point strike (live-confirmed) must
        // round-trip exactly at precision 1, not truncate to an integer.
        let mut contract = make_options_contract("C", 50, 1.0);
        contract.strike_price = 12.5;
        let instrument =
            parse_options_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::OptionContract(o) => {
                let strike = o.strike_price().unwrap();
                assert_eq!(strike.as_f64(), 12.5);
                assert_eq!(strike.precision, 1);
            }
            _ => panic!("Expected OptionContract"),
        }
    }

    #[rstest]
    fn test_parse_snapshot_nan_price_errors() {
        let mut snapshots: Vec<SnapshotData> = load_test_json_as("market_snapshots.json");
        snapshots[0].buy_price = f64::NAN;
        let result = parse_snapshot_to_quote_tick(
            &snapshots[0],
            test_instrument_id(),
            1,
            0,
            UnixNanos::default(),
        );
        assert!(result.is_err());
    }

    // --- Task 2.5: schedule selection from live contract-dump evidence ----------------------
    //
    // Fixtures mirror the field shapes of
    // `shioaji-server/tests/fixtures/contracts_dump/*.json` (live sim dump, Task
    // 0.1): every futures/options `multiplier` is 0 (the SDK does not transmit
    // it in sim), so the table-fallback path is the normal path. Schedule is
    // keyed off `underlying_kind` + `underlying_code`, NOT `category`.

    /// Builds a `FuturesContract` mirroring the live single-stock-future dump
    /// (e.g. `CDFF6`: TSMC future, root `CDF`, `underlying_kind == "S"`,
    /// `underlying_code == "2330"`, `multiplier == 0`).
    fn dump_single_stock_future() -> FuturesContract {
        FuturesContract {
            code: "CDFF6".to_string(),
            symbol: "CDF202606".to_string(),
            name: "台積電期貨06".to_string(),
            category: "CDF".to_string(),
            delivery_month: "202606".to_string(),
            delivery_date: "2026/06/17".to_string(),
            underlying_kind: "S".to_string(),
            limit_up: 2485.0,
            limit_down: 2035.0,
            reference: 2260.0,
            update_date: "2026/06/10".to_string(),
            unit: 1.0,
            multiplier: 0,
            currency: "TWD".to_string(),
            underlying_code: "2330".to_string(),
        }
    }

    /// Builds a `FuturesContract` mirroring the live index-future dump
    /// (e.g. `TXFF6`: TAIEX future, root `TXF`, `underlying_kind == "I"`,
    /// empty `underlying_code`, `multiplier == 0`).
    fn dump_index_future() -> FuturesContract {
        FuturesContract {
            code: "TXFF6".to_string(),
            symbol: "TXF202606".to_string(),
            name: "臺股期貨06".to_string(),
            category: "TXF".to_string(),
            delivery_month: "202606".to_string(),
            delivery_date: "2026/06/17".to_string(),
            underlying_kind: "I".to_string(),
            limit_up: 47703.0,
            limit_down: 39031.0,
            reference: 43367.0,
            update_date: "2026/06/10".to_string(),
            unit: 1.0,
            multiplier: 0,
            currency: "TWD".to_string(),
            underlying_code: String::new(),
        }
    }

    #[rstest]
    fn test_dump_single_stock_future_uses_price_tiered_tick() {
        // CDFF6 ref=2260 -> single-stock-futures tier (>=1000) tick 5.0,
        // precision 1. Root CDF has no multiplier table entry -> default 2000.
        let contract = dump_single_stock_future();
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.price_increment().as_f64(), 5.0);
                assert_eq!(f.price_precision(), 1);
                assert_eq!(f.multiplier().as_f64(), 2000.0); // unknown-root default
                assert_eq!(f.underlying().unwrap().as_str(), "2330");
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_dump_index_future_uses_root_table_tick_and_multiplier() {
        // TXFF6: index root TXF -> tick 1.0 precision 0, multiplier 200 from the
        // known table (silent, no warn, even though SDK multiplier == 0).
        let contract = dump_index_future();
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.price_increment().as_f64(), 1.0);
                assert_eq!(f.price_precision(), 0);
                assert_eq!(f.multiplier().as_f64(), 200.0); // known TXF table value
                assert_eq!(f.underlying().unwrap().as_str(), "TXF");
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_sector_future_zef_tick_and_multiplier() {
        // ZEF (Mini-Electronics) index future: tick 0.05 (precision 2),
        // multiplier 500. tick_value = 0.05 * 500 = NTD 25, matching the
        // published ZEF tick value (TAIFEX ZEF Trading Rules Art. 5 & 6,
        // <https://www.taifex.com.tw/enl/eng2/zEF>). The old wrong table gave
        // 0.2 / 4000 (an off-grid tick and an 8x-too-large multiplier).
        let mut contract = dump_index_future();
        contract.code = "ZEFF6".to_string();
        contract.category = "ZEF".to_string();
        contract.reference = 1000.0;
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.price_increment().as_f64(), 0.05);
                assert_eq!(f.price_precision(), 2);
                assert_eq!(f.multiplier().as_f64(), 500.0);
                // tick_value identity: 0.05 * 500 = NTD 25/tick.
                assert_eq!(f.price_increment().as_f64() * f.multiplier().as_f64(), 25.0);
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_sector_future_zff_tick_and_multiplier() {
        // ZFF (Mini-Finance) index future: tick 0.2 (precision 1), multiplier
        // 250. tick_value = 0.2 * 250 = NTD 50, matching the published ZFF tick
        // value (<https://www.taifex.com.tw/enl/eng2/zFF>). The old wrong table
        // gave a 4x-too-large multiplier (1000, the full-size TF value).
        let mut contract = dump_index_future();
        contract.code = "ZFFF6".to_string();
        contract.category = "ZFF".to_string();
        contract.reference = 1500.0;
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.price_increment().as_f64(), 0.2);
                assert_eq!(f.price_precision(), 1);
                assert_eq!(f.multiplier().as_f64(), 250.0);
                // tick_value identity: 0.2 * 250 = NTD 50/tick.
                assert_eq!(f.price_increment().as_f64() * f.multiplier().as_f64(), 50.0);
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_sector_future_xif_tick_and_multiplier() {
        // XIF (Non-Fin-Non-Elec) index future: tick 1.0 (precision 0),
        // multiplier 100. tick_value = 1.0 * 100 = NTD 100/pt
        // (<https://www.taifex.com.tw/enl/eng2/xIF>). The old wrong table gave a
        // 2x-too-large multiplier (200).
        let mut contract = dump_index_future();
        contract.code = "XIFF6".to_string();
        contract.category = "XIF".to_string();
        contract.reference = 800.0;
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.price_increment().as_f64(), 1.0);
                assert_eq!(f.price_precision(), 0);
                assert_eq!(f.multiplier().as_f64(), 100.0);
                // tick_value identity: 1.0 * 100 = NTD 100/pt.
                assert_eq!(
                    f.price_increment().as_f64() * f.multiplier().as_f64(),
                    100.0
                );
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_dump_single_stock_future_low_price_tier() {
        // A single-stock future on a low-priced underlying (ref 31.7, the CAO
        // underlying tier) -> tick 0.05, precision 2.
        let mut contract = dump_single_stock_future();
        contract.reference = 31.7;
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.price_increment().as_f64(), 0.05);
                assert_eq!(f.price_precision(), 2);
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_etf_underlying_future_uses_etf_tick_above_50() {
        // ETF-underlying futures (underlying_kind == "E", 0050 in the dump) use
        // the ETF-futures grid, NOT the common-stock single-stock-futures tiers.
        // At ref 103.5 (>= 50) the ETF tick is 0.05 (precision 2); the common-
        // stock tier would wrongly give 0.50. TAIFEX Single Stock / ETF Futures
        // spec, ETF table: <https://www.taifex.com.tw/enl/eng2/sSF>.
        let mut contract = dump_single_stock_future();
        contract.underlying_kind = "E".to_string();
        contract.underlying_code = "0050".to_string();
        contract.reference = 103.5; // 0050 dump reference -> ETF >=50 tier
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.price_increment().as_f64(), 0.05);
                assert_eq!(f.price_precision(), 2);
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    #[case(49.9, 0.01)] // ETF < 50 -> 0.01
    #[case(50.0, 0.05)] // ETF boundary, >= 50 -> 0.05
    fn test_etf_underlying_future_grid_boundary(
        #[case] reference: f64,
        #[case] expected_tick: f64,
    ) {
        // ETF-futures grid boundary at 50 TWD (cash-ETF schedule):
        // < 50 -> 0.01, >= 50 -> 0.05, both precision 2.
        let mut contract = dump_single_stock_future();
        contract.underlying_kind = "E".to_string();
        contract.underlying_code = "0050".to_string();
        contract.reference = reference;
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.price_increment().as_f64(), expected_tick);
                assert_eq!(f.price_precision(), 2);
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_unknown_index_root_falls_back_to_default_tick() {
        // An index future with a root absent from the table must fall back to
        // (1.0, 0) (the warn fires; the instrument still parses).
        let mut contract = dump_index_future();
        contract.category = "ZZZ".to_string();
        contract.underlying_kind = "I".to_string();
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.price_increment().as_f64(), 1.0);
                assert_eq!(f.price_precision(), 0);
                assert_eq!(f.multiplier().as_f64(), 2000.0); // unknown-root default
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_commodity_future_falls_back_to_default_tick() {
        // Commodity futures (underlying_kind == "C", 23 in the dump) have no
        // tabled tick here -> documented (1.0, 0) fallback.
        let mut contract = dump_single_stock_future();
        contract.underlying_kind = "C".to_string();
        contract.category = "GDF".to_string(); // e.g. gold future root
        contract.reference = 5000.0;
        let instrument =
            parse_futures_to_contract(&contract, UnixNanos::default(), UnixNanos::default())
                .unwrap();
        match instrument {
            InstrumentAny::FuturesContract(f) => {
                assert_eq!(f.price_increment().as_f64(), 1.0);
                assert_eq!(f.price_precision(), 0);
            }
            _ => panic!("Expected FuturesContract"),
        }
    }

    #[rstest]
    fn test_dump_etf_uses_etf_tick_category_00() {
        // ETF 0050 dump: category "00" -> ETF tick schedule. ref 103.5 (>=50) ->
        // 0.05 (NOT the stock-tier 0.50). Confirms the `category == "00"` branch
        // against the live dump.
        let contract = StockContract {
            code: "0050".to_string(),
            symbol: "TSE0050".to_string(),
            name: "元大台灣50".to_string(),
            exchange: "TSE".to_string(),
            category: "00".to_string(),
            limit_up: 113.85,
            limit_down: 93.15,
            reference: 103.5,
            update_date: "2026/06/10".to_string(),
            day_trade: "Yes".to_string(),
            unit: 1000.0,
            multiplier: 0,
            currency: "TWD".to_string(),
        };
        let instrument =
            parse_stock_to_equity(&contract, UnixNanos::default(), UnixNanos::default()).unwrap();
        match instrument {
            InstrumentAny::Equity(e) => {
                assert_eq!(e.price_increment().as_f64(), 0.05);
                assert_eq!(e.price_precision(), 2);
            }
            _ => panic!("Expected Equity"),
        }
    }
}
