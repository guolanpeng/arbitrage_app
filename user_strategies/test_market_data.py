"""Tests for the public market data normalization and Redis contract."""

import json
import unittest
from typing import Self
from unittest.mock import AsyncMock, patch

import httpx

from user_strategies import market_data


class MarketDataTests(unittest.IsolatedAsyncioTestCase):
    def test_quote_keeps_decimal_strings_and_rejects_missing_prices(self) -> None:
        key, quote = market_data._quote(
            "BTC-USDT-SWAP", "0.00000001", "0.00000002", quote_turnover_24h="123.456789"
        )
        self.assertEqual(key, "BTCUSDT")
        self.assertEqual(quote["bid"], "0.00000001")
        self.assertEqual(quote["quote_turnover_24h"], "123.456789")
        self.assertIsNone(market_data._quote("BTC-USD-SWAP", "1", "2"))
        self.assertIsNone(market_data._quote("BTCUSDT", "0", "2"))

    async def test_five_exchange_response_shapes(self) -> None:
        quote = {"symbol": "BTCUSDT", "bid1Price": "1", "ask1Price": "2"}
        cases = [
            (
                "binance",
                [
                    [{"symbol": "BTCUSDT", "bidPrice": "1", "askPrice": "2"}],
                    [{"symbol": "BTCUSDT", "lastPrice": "1.5"}],
                    [{"symbol": "BTCUSDT", "bidPrice": "1", "askPrice": "2"}],
                    [{"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}],
                    [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}],
                ],
                "0.0001",
            ),
            (
                "gate",
                [
                    [
                        {
                            "currency_pair": "BTC_USDT",
                            "highest_bid": "1",
                            "lowest_ask": "2",
                        }
                    ],
                    [
                        {
                            "contract": "BTC_USDT",
                            "highest_bid": "1",
                            "lowest_ask": "2",
                            "funding_rate": "0.0002",
                        }
                    ],
                    [
                        {
                            "name": "BTC_USDT",
                            "status": "trading",
                            "funding_interval": 28800,
                            "funding_next_apply": 1000,
                        }
                    ],
                ],
                "0.0002",
            ),
            (
                "bybit",
                [
                    {"retCode": 0, "result": {"list": [quote]}},
                    {
                        "retCode": 0,
                        "result": {
                            "list": [
                                {
                                    **quote,
                                    "fundingRate": "0.0003",
                                    "fundingIntervalHour": "8",
                                }
                            ]
                        },
                    },
                ],
                "0.0003",
            ),
            (
                "bitget",
                [
                    {"code": "00000", "data": [quote]},
                    {"code": "00000", "data": [quote]},
                    {
                        "code": "00000",
                        "data": [
                            {
                                "symbol": "BTCUSDT",
                                "fundingRate": "0.0004",
                                "fundingRateInterval": "8",
                            }
                        ],
                    },
                ],
                "0.0004",
            ),
            (
                "okx",
                [
                    {
                        "code": "0",
                        "data": [
                            {
                                "instId": "BTC-USDT",
                                "bidPx": "1",
                                "askPx": "2",
                                "volCcy24h": "10",
                            }
                        ],
                    },
                    {
                        "code": "0",
                        "data": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "bidPx": "1",
                                "askPx": "2",
                                "volCcy24h": "10",
                            }
                        ],
                    },
                    {
                        "code": "0",
                        "data": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "fundingRate": "0.0005",
                                "fundingTime": "3600000",
                                "nextFundingTime": "7200000",
                            }
                        ],
                    },
                ],
                "0.0005",
            ),
        ]
        for venue, responses, expected_rate in cases:
            with self.subTest(venue=venue):
                with patch.object(market_data, "_get", new_callable=AsyncMock) as get:
                    get.side_effect = responses
                    spot, perp, funding = await market_data.COLLECTORS[venue](None)
                self.assertEqual(spot["BTCUSDT"]["bid"], "1")
                self.assertEqual(perp["BTCUSDT"]["ask"], "2")
                self.assertEqual(funding["BTCUSDT"]["rate"], expected_rate)
                if venue == "gate":
                    self.assertEqual(
                        funding["BTCUSDT"]["next_funding_at_ms"], 1_000_000
                    )
                if venue == "binance":
                    self.assertEqual(funding["BTCUSDT"]["interval_hours"], "4")
                if venue == "okx":
                    self.assertEqual(funding["BTCUSDT"]["interval_hours"], "1")
                    self.assertIsNone(perp["BTCUSDT"]["quote_turnover_24h"])

    async def test_binance_uses_default_funding_interval_without_adjustment(
        self,
    ) -> None:
        responses = [
            [],
            [{"symbol": "BTCUSDT", "lastPrice": "1.5"}],
            [{"symbol": "BTCUSDT", "bidPrice": "1", "askPrice": "2"}],
            [{"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}],
            [],
        ]
        with patch.object(market_data, "_get", new_callable=AsyncMock) as get:
            get.side_effect = responses
            _, _, funding = await market_data._binance(None)
        self.assertEqual(funding["BTCUSDT"]["interval_hours"], "8")

    async def test_publishes_three_expiring_snapshots_per_venue(self) -> None:
        class Pipeline:
            def __init__(self) -> None:
                self.commands: list[tuple[str, str, int]] = []

            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            def set(self, key: str, value: str, ex: int) -> None:
                self.commands.append((key, value, ex))

            async def execute(self) -> None:
                return None

        class RedisStub:
            def __init__(self) -> None:
                self.pipelines: list[Pipeline] = []

            def pipeline(self, *, transaction: bool) -> Pipeline:
                assert transaction
                pipeline = Pipeline()
                self.pipelines.append(pipeline)
                return pipeline

        async def collector(_client: object) -> tuple[dict, dict, dict]:
            return {"BTCUSDT": {}}, {"BTCUSDT": {}}, {"BTCUSDT": {"rate": "0"}}

        redis = RedisStub()
        with patch.dict(
            market_data.COLLECTORS, dict.fromkeys(market_data.VENUES, collector)
        ):
            successful = await market_data.collect_once(None, redis, 60)
        self.assertEqual(successful, 5)
        self.assertEqual(len(redis.pipelines), 5)
        self.assertEqual(sum(len(p.commands) for p in redis.pipelines), 15)
        key, value, expiry = redis.pipelines[0].commands[0]
        self.assertEqual(key, "market:v1:binance:spot")
        self.assertEqual(expiry, 60)
        self.assertEqual(json.loads(value)["schema_version"], 1)
        self.assertEqual(
            [command[0] for command in redis.pipelines[-1].commands],
            [f"market:v1:okx:{market}" for market in ("spot", "perp", "funding")],
        )

    async def test_failed_collectors_report_exception_type_and_return_zero(
        self,
    ) -> None:
        async def collector(_client: object) -> tuple[dict, dict, dict]:
            raise TimeoutError

        with (
            patch.dict(
                market_data.COLLECTORS, dict.fromkeys(market_data.VENUES, collector)
            ),
            self.assertLogs(market_data.LOG, level="WARNING") as logs,
        ):
            successful = await market_data.collect_once(None, None, 60)

        self.assertEqual(successful, 0)
        self.assertIn("TimeoutError: TimeoutError()", logs.output[0])


if __name__ == "__main__":
    unittest.main()
