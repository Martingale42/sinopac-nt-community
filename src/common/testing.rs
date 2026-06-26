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

//! Test fixtures and helpers for Sinopac adapter tests.

use std::path::PathBuf;

/// Returns the path to the test data directory.
pub fn get_test_data_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("test_data")
}

/// Loads a JSON test fixture by filename.
pub fn load_test_json(filename: &str) -> String {
    std::fs::read_to_string(get_test_data_dir().join(filename))
        .unwrap_or_else(|_| panic!("Failed to load test fixture: {filename}"))
}

/// Loads and deserializes a JSON test fixture.
pub fn load_test_json_as<T: serde::de::DeserializeOwned>(filename: &str) -> T {
    let json = load_test_json(filename);
    serde_json::from_str(&json)
        .unwrap_or_else(|e| panic!("Failed to parse fixture {filename}: {e}"))
}
