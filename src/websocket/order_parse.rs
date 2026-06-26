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

//! Parsers for Sinopac WebSocket order events.

use pyo3::{prelude::*, types::PyDict};

use super::messages::{OperationInfo, OrderEvent, OrderStatusInfo};

/// Sets order status fields common to both stock and futures order events.
#[allow(clippy::too_many_arguments)]
fn set_order_fields(
    dict: &Bound<'_, PyDict>,
    event_type: &str,
    op: &OperationInfo,
    order_id: &str,
    ordno: &str,
    action: &str,
    price: f64,
    quantity: i64,
    order_type: &str,
    price_type: &str,
    code: &str,
    status: &OrderStatusInfo,
    custom_field: Option<&str>,
) -> PyResult<()> {
    dict.set_item("event_type", event_type)?;
    dict.set_item("op_type", &op.op_type)?;
    dict.set_item("op_code", &op.op_code)?;
    dict.set_item("op_msg", &op.op_msg)?;
    dict.set_item("order_id", order_id)?;
    dict.set_item("ordno", ordno)?;
    dict.set_item("action", action)?;
    dict.set_item("price", price)?;
    dict.set_item("quantity", quantity)?;
    dict.set_item("order_type", order_type)?;
    dict.set_item("price_type", price_type)?;
    dict.set_item("code", code)?;
    dict.set_item("exchange_ts", status.exchange_ts)?;
    dict.set_item("cancel_quantity", status.cancel_quantity)?;
    dict.set_item("order_quantity", status.order_quantity)?;
    dict.set_item("modified_price", status.modified_price)?;
    // `custom_field` is `Option<&str>`; pyo3 maps `None` -> Python `None`.
    dict.set_item("custom_field", custom_field)?;
    Ok(())
}

/// Sets deal (fill) fields common to both stock and futures deal events.
#[allow(clippy::too_many_arguments)]
fn set_deal_fields(
    dict: &Bound<'_, PyDict>,
    event_type: &str,
    trade_id: &str,
    seqno: &str,
    ordno: &str,
    exchange_seq: Option<&str>,
    action: &str,
    code: &str,
    price: f64,
    quantity: i64,
    ts: f64,
) -> PyResult<()> {
    dict.set_item("event_type", event_type)?;
    dict.set_item("trade_id", trade_id)?;
    // Per-fill uniqueness (per Shioaji deal-event semantics,
    // sinotrade.github.io/tutor/order_deal_event):
    //   - `seqno`        == the ORDER's seqno  -> SAME across all partial fills of an
    //                       order (per-ORDER). Exposed for logging only; it MUST NOT
    //                       be used as the per-fill TradeId key.
    //   - `ordno`        == the deal-level order number: 5-char order prefix + last 3
    //                       chars = the deal sequence (001/002/003...) -> per-fill UNIQUE.
    //   - `exchange_seq` == the exchange's per-deal sequence -> per-fill UNIQUE
    //                       (Option: may be absent, e.g. simulation / pre-confirmation).
    // The Python client builds the NT TradeId from `exchange_seq` (fallback `ordno`)
    // so partial fills do not collide and corrupt the ledger.
    dict.set_item("seqno", seqno)?;
    dict.set_item("ordno", ordno)?;
    // `exchange_seq` is `Option<String>`; pyo3 maps `None` -> Python `None`, so
    // `event.get("exchange_seq")` returns the string when present and `None` otherwise.
    dict.set_item("exchange_seq", exchange_seq)?;
    dict.set_item("action", action)?;
    dict.set_item("code", code)?;
    dict.set_item("price", price)?;
    dict.set_item("quantity", quantity)?;
    dict.set_item("ts", ts)?;
    Ok(())
}

/// Converts an [`OrderEvent`] to a Python dict for the execution client to process.
pub fn order_event_to_pydict(py: Python<'_>, event: &OrderEvent) -> PyResult<Py<PyDict>> {
    let dict = PyDict::new(py);

    match event {
        OrderEvent::StockOrder(data) => set_order_fields(
            &dict,
            "stock_order",
            &data.operation,
            &data.order.id,
            &data.order.ordno,
            &data.order.action,
            data.order.price,
            data.order.quantity,
            &data.order.order_type,
            &data.order.price_type,
            &data.contract.code,
            &data.status,
            data.order.custom_field.as_deref(),
        )?,
        OrderEvent::StockDeal(data) => set_deal_fields(
            &dict,
            "stock_deal",
            &data.trade_id,
            &data.seqno,
            &data.ordno,
            data.exchange_seq.as_deref(),
            &data.action,
            &data.code,
            data.price,
            data.quantity,
            data.ts,
        )?,
        OrderEvent::FuturesOrder(data) => set_order_fields(
            &dict,
            "futures_order",
            &data.operation,
            &data.order.id,
            &data.order.ordno,
            &data.order.action,
            data.order.price,
            data.order.quantity,
            &data.order.order_type,
            &data.order.price_type,
            &data.contract.code,
            &data.status,
            data.order.custom_field.as_deref(),
        )?,
        OrderEvent::FuturesDeal(data) => set_deal_fields(
            &dict,
            "futures_deal",
            &data.trade_id,
            &data.seqno,
            &data.ordno,
            data.exchange_seq.as_deref(),
            &data.action,
            &data.code,
            data.price,
            data.quantity,
            data.ts,
        )?,
    }

    Ok(dict.into())
}

#[cfg(test)]
mod tests {
    use std::sync::Once;

    use pyo3::Python;
    use rstest::rstest;

    use super::*;
    use crate::{common::testing::load_test_json_as, websocket::messages::WsIncomingMsg};

    fn ensure_python_initialized() {
        static INIT: Once = Once::new();
        INIT.call_once(|| {
            Python::initialize();
        });
    }

    #[rstest]
    fn test_deal_pydict_exposes_per_fill_unique_keys() {
        // A stock deal event must expose the per-fill-unique keys to Python so the
        // execution client can build a non-colliding NT TradeId. Per Shioaji deal
        // semantics the per-fill-unique keys are `exchange_seq` (exchange per-deal
        // sequence) and the deal-level `ordno` (last 3 chars = deal sequence).
        // `seqno` is per-ORDER (repeats across partial fills) and is exposed for
        // logging only -- it MUST NOT be used as the fill key.
        ensure_python_initialized();
        let msg: WsIncomingMsg = load_test_json_as("ws_deal_stock.json");
        let WsIncomingMsg::OrderUpdate(update) = msg else {
            panic!("Expected OrderUpdate message");
        };
        let event = update.parse_event().expect("parse_event failed");

        Python::attach(|py| {
            let dict = order_event_to_pydict(py, &event).expect("order_event_to_pydict failed");
            let bound = dict.bind(py);

            // The per-fill-unique exchange sequence must be exposed for keying.
            assert!(
                bound.contains("exchange_seq").expect("contains failed"),
                "deal pydict must contain exchange_seq",
            );
            let exchange_seq: String = bound
                .get_item("exchange_seq")
                .expect("get_item failed")
                .expect("exchange_seq missing")
                .extract()
                .expect("exchange_seq not a string");
            assert_eq!(exchange_seq, "E5678");

            // The deal-level `ordno` (per-fill-unique fallback) must be exposed.
            assert!(
                bound.contains("ordno").expect("contains failed"),
                "deal pydict must contain ordno",
            );
            let ordno: String = bound
                .get_item("ordno")
                .expect("get_item failed")
                .expect("ordno missing")
                .extract()
                .expect("ordno not a string");
            assert_eq!(ordno, "A1234");

            // `seqno` (per-ORDER) is still exposed for logging but must never be the key.
            let seqno: String = bound
                .get_item("seqno")
                .expect("get_item failed")
                .expect("seqno missing")
                .extract()
                .expect("seqno not a string");
            assert_eq!(seqno, "123456");

            // The chosen fill key (exchange_seq) must differ from the per-ORDER seqno.
            assert_ne!(exchange_seq, seqno);
        });
    }
}
