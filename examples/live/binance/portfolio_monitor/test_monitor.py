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

"""Tests for the NautilusTrader Portfolio Margin monitor."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import monitor


def test_snapshot_reads_account_info_and_reconciled_positions() -> None:
    """Keep exact venue account strings while displaying cached position quantity."""
    state = SimpleNamespace(
        info={
            "binance_portfolio_margin": {
                "accountStatus": "NORMAL",
                "uniMMR": "1.234567890123456789",
            },
            "binance_portfolio_balances": [
                {
                    "asset": "USDT",
                    "totalWalletBalance": "100.00000001",
                    "crossMarginBorrowed": "2.5",
                    "crossMarginInterest": "0.00000001",
                },
            ],
            "binance_portfolio_um_position_risks": [
                {
                    "symbol": "龙虾USDT",
                    "positionAmt": "-1925",
                    "entryPrice": "0.1036900",
                    "markPrice": "0.1012345",
                    "liquidationPrice": "0.2345678",
                    "unRealizedProfit": "4.7273750",
                    "leverage": "20",
                    "notional": "-194.8764125",
                    "positionSide": "BOTH",
                    "updateTime": 1_797_777_777_777,
                },
            ],
            "binance_portfolio_cm_position_risks": [],
        },
    )
    position = SimpleNamespace(
        instrument_id="ETHUSDT-PERP.BINANCE",
        side="SHORT",
        quantity="0.125",
        id="ETHUSDT-PERP.BINANCE-SHORT",
    )

    output = monitor.render_snapshot(state, [position])

    assert "uniMMR=1.234567890123456789" in output
    assert "wallet=100.00000001 borrowed=2.5 interest=0.00000001" in output
    assert "UM 龙虾USDT side=BOTH qty=-1925 leverage=20x" in output
    assert "entry=0.1036900 mark=0.1012345 liquidation=0.2345678" in output
    assert "unrealizedPnl=4.7273750 notional=-194.8764125" in output


def test_redis_snapshot_preserves_exchange_values_and_unicode() -> None:
    """Store a versioned JSON snapshot with exact exchange strings."""
    state = SimpleNamespace(
        info={
            "binance_portfolio_margin": {
                "uniMMR": "1.234567890123456789",
                "accountEquity": "100.00000001",
            },
            "binance_portfolio_balances": [
                {"asset": "USDT", "totalWalletBalance": "100.00000001"},
            ],
            "binance_portfolio_um_position_risks": [
                {
                    "symbol": "龙虾USDT",
                    "positionAmt": "-1925",
                    "markPrice": "0.1012345",
                    "liquidationPrice": "0.2345678",
                    "unRealizedProfit": "4.7273750",
                },
                {"symbol": "BTCUSDT", "positionAmt": "0"},
            ],
            "binance_portfolio_cm_position_risks": [],
        },
    )
    redis = Mock()

    monitor.write_redis_snapshot(redis, state, 60)

    redis.set.assert_called_once()
    key, encoded = redis.set.call_args.args
    payload = json.loads(encoded)
    assert key == "account:v1:binance:portfolio"
    assert redis.set.call_args.kwargs == {"ex": 60}
    assert payload["schema_version"] == 1
    assert payload["account"]["uniMMR"] == "1.234567890123456789"
    assert payload["assets"][0]["totalWalletBalance"] == "100.00000001"
    assert payload["positions"] == [
        {
            "product": "UM",
            "symbol": "龙虾USDT",
            "positionAmt": "-1925",
            "markPrice": "0.1012345",
            "liquidationPrice": "0.2345678",
            "unRealizedProfit": "4.7273750",
        },
    ]
    assert "龙虾USDT" in encoded
