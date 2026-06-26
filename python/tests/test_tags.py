# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2026 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------

import pytest

from sinopac_nt.tags import TAG_PREFIX
from sinopac_nt.tags import SinopacOrderTags


def test_default_tags_have_expected_values():
    tags = SinopacOrderTags()

    assert tags.order_lot == "Common"
    assert tags.order_cond == "Cash"
    assert tags.daytrade_short is False
    assert tags.octype == "Auto"


def test_value_is_prefixed_json():
    tags = SinopacOrderTags(order_lot="IntradayOdd")

    value = tags.value

    assert value.startswith(TAG_PREFIX)
    assert '"order_lot":"IntradayOdd"' in value


@pytest.mark.parametrize(
    "tags",
    [
        SinopacOrderTags(),
        SinopacOrderTags(order_lot="IntradayOdd"),
        SinopacOrderTags(order_cond="MarginTrading"),
        SinopacOrderTags(order_cond="ShortSelling", daytrade_short=False),
        SinopacOrderTags(daytrade_short=True),
        SinopacOrderTags(octype="Cover"),
        SinopacOrderTags(
            order_lot="IntradayOdd",
            order_cond="MarginTrading",
            daytrade_short=True,
            octype="DayTrade",
        ),
    ],
)
def test_round_trip_via_value_and_from_tags(tags):
    recovered = SinopacOrderTags.from_tags([tags.value])

    assert recovered == tags


def test_from_tags_none_returns_defaults():
    assert SinopacOrderTags.from_tags(None) == SinopacOrderTags()


def test_from_tags_empty_list_returns_defaults():
    assert SinopacOrderTags.from_tags([]) == SinopacOrderTags()


def test_from_tags_ignores_foreign_tags():
    foreign = ["IBOrderTags:{}", "SOME_OTHER:value", "plain-string"]

    assert SinopacOrderTags.from_tags(foreign) == SinopacOrderTags()


def test_from_tags_picks_sinopac_tag_among_foreign_tags():
    tags = SinopacOrderTags(order_lot="IntradayOdd", order_cond="MarginTrading")
    mixed = ["IBOrderTags:{}", tags.value, "FOO:bar"]

    assert SinopacOrderTags.from_tags(mixed) == tags


def test_from_tags_raises_on_malformed_sinopac_tag():
    malformed = [TAG_PREFIX + "not-valid-json{"]

    with pytest.raises(Exception):
        SinopacOrderTags.from_tags(malformed)
