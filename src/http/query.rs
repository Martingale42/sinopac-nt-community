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

//! Query parameter builders for Sinopac REST endpoints.

use serde::Serialize;

/// Query parameters for the snapshots endpoint.
#[derive(Debug, Serialize)]
pub struct SnapshotsQuery {
    /// The comma-separated contract codes.
    pub codes: String,
    /// The optional market type filter.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub market: Option<String>,
}

/// Query parameters for the ticks endpoint.
#[derive(Debug, Serialize)]
pub struct TicksQuery {
    /// The contract code.
    pub code: String,
    /// The query date.
    pub date: String,
    /// The optional market type filter.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub market: Option<String>,
}

/// Query parameters for the kbars endpoint.
#[derive(Debug, Serialize)]
pub struct KBarsQuery {
    /// The contract code.
    pub code: String,
    /// The start date for the query range.
    pub start: String,
    /// The end date for the query range.
    pub end: String,
    /// The optional market type filter.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub market: Option<String>,
}

/// Query parameters for the positions endpoint.
#[derive(Debug, Serialize)]
pub struct PositionsQuery {
    /// The optional market type filter.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub market: Option<String>,
}
