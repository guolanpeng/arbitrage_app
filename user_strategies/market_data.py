"""Collect public USDT spot and perpetual market snapshots for a separate UI."""

import argparse
import asyncio
import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from redis.asyncio import Redis

LOG = logging.getLogger(__name__)
VENUES = ("binance", "gate", "bybit", "bitget", "okx")


def _millis(value: Any, *, seconds: bool = False) -> int | None:
    if value in (None, "", "null", 0, "0"):
        return None
    timestamp = int(value)
    return timestamp * 1000 if seconds else timestamp


def _instrument(symbol: str) -> tuple[str, str] | None:
    for suffix in ("_USDT", "-USDT-SWAP", "-USDT", "USDT"):
        if symbol.endswith(suffix):
            base = symbol[: -len(suffix)]
            if base:
                return f"{base}USDT", base
    return None


def _quote(
    symbol: str,
    bid: Any,
    ask: Any,
    bid_size: Any = None,
    ask_size: Any = None,
    last: Any = None,
    quote_turnover_24h: Any = None,
    source_at_ms: Any = None,
) -> tuple[str, dict[str, Any]] | None:
    instrument = _instrument(symbol)
    if instrument is None or bid in (None, "") or ask in (None, ""):
        return None
    try:
        if Decimal(str(bid)) <= 0 or Decimal(str(ask)) <= 0:
            return None
    except InvalidOperation:
        return None
    key, base = instrument
    return key, {
        "symbol": symbol,
        "base": base,
        "quote": "USDT",
        "bid": str(bid),
        "ask": str(ask),
        "bid_size": str(bid_size) if bid_size not in (None, "") else None,
        "ask_size": str(ask_size) if ask_size not in (None, "") else None,
        "last": str(last) if last not in (None, "") else None,
        "quote_turnover_24h": (
            str(quote_turnover_24h) if quote_turnover_24h not in (None, "") else None
        ),
        "source_at_ms": _millis(source_at_ms),
    }


def _funding(
    symbol: str,
    rate: Any,
    next_at_ms: Any = None,
    interval_hours: Any = None,
    source_at_ms: Any = None,
) -> tuple[str, dict[str, Any]] | None:
    instrument = _instrument(symbol)
    if instrument is None or rate in (None, ""):
        return None
    try:
        Decimal(str(rate))
    except InvalidOperation:
        return None
    return instrument[0], {
        "symbol": symbol,
        "rate": str(rate),
        "next_funding_at_ms": _millis(next_at_ms),
        "interval_hours": str(interval_hours)
        if interval_hours not in (None, "")
        else None,
        "source_at_ms": _millis(source_at_ms),
    }


async def _get(
    client: httpx.AsyncClient, url: str, params: dict[str, str] | None = None
) -> Any:
    response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()


def _unwrap(data: dict[str, Any], success: str) -> list[dict[str, Any]]:
    code = data.get("code", data.get("retCode"))
    if str(code) != success:
        raise ValueError(
            f"Exchange API returned {code}: {data.get('msg', data.get('retMsg'))}"
        )
    rows = data.get("data", data.get("result"))
    if isinstance(rows, dict):
        rows = rows.get("list")
    if not isinstance(rows, list):
        raise TypeError("Exchange API response has no instrument list")
    return rows


def _add(target: dict[str, Any], item: tuple[str, dict[str, Any]] | None) -> None:
    if item is not None:
        target[item[0]] = item[1]


async def _binance(client: httpx.AsyncClient) -> tuple[dict, dict, dict]:
    spot_rows, perp_rows, perp_books, funding_rows, funding_info = await asyncio.gather(
        _get(client, "https://api.binance.com/api/v3/ticker/24hr"),
        _get(client, "https://fapi.binance.com/fapi/v1/ticker/24hr"),
        _get(client, "https://fapi.binance.com/fapi/v1/ticker/bookTicker"),
        _get(client, "https://fapi.binance.com/fapi/v1/premiumIndex"),
        _get(client, "https://fapi.binance.com/fapi/v1/fundingInfo"),
    )
    spot: dict[str, Any] = {}
    perp: dict[str, Any] = {}
    funding: dict[str, Any] = {}
    books = {row["symbol"]: row for row in perp_books}
    funding_intervals = {
        row["symbol"]: row["fundingIntervalHours"]
        for row in funding_info
        if row.get("symbol") and row.get("fundingIntervalHours")
    }
    for row in spot_rows:
        _add(
            spot,
            _quote(
                row["symbol"],
                row.get("bidPrice"),
                row.get("askPrice"),
                row.get("bidQty"),
                row.get("askQty"),
                row.get("lastPrice"),
                row.get("quoteVolume"),
            ),
        )
    for row in perp_rows:
        symbol = row["symbol"]
        book = books.get(symbol, {})
        _add(
            perp,
            _quote(
                symbol,
                book.get("bidPrice"),
                book.get("askPrice"),
                book.get("bidQty"),
                book.get("askQty"),
                row.get("lastPrice"),
                row.get("quoteVolume"),
                book.get("time"),
            ),
        )
    for row in funding_rows:
        _add(
            funding,
            _funding(
                row["symbol"],
                row.get("lastFundingRate"),
                row.get("nextFundingTime"),
                interval_hours=funding_intervals.get(row["symbol"], 8),
                source_at_ms=row.get("time"),
            ),
        )
    return (
        spot,
        perp,
        {symbol: value for symbol, value in funding.items() if symbol in perp},
    )


async def _gate(client: httpx.AsyncClient) -> tuple[dict, dict, dict]:
    spot_rows, perp_rows, contracts = await asyncio.gather(
        _get(client, "https://api.gateio.ws/api/v4/spot/tickers"),
        _get(client, "https://api.gateio.ws/api/v4/futures/usdt/tickers"),
        _get(client, "https://api.gateio.ws/api/v4/futures/usdt/contracts"),
    )
    spot: dict[str, Any] = {}
    perp: dict[str, Any] = {}
    funding: dict[str, Any] = {}
    contract_info = {
        row["name"]: row for row in contracts if row.get("status") == "trading"
    }
    for row in spot_rows:
        _add(
            spot,
            _quote(
                row["currency_pair"],
                row.get("highest_bid"),
                row.get("lowest_ask"),
                row.get("highest_size"),
                row.get("lowest_size"),
                row.get("last"),
                row.get("quote_volume"),
            ),
        )
    for row in perp_rows:
        symbol = row["contract"]
        contract = contract_info.get(symbol)
        if contract is None:
            continue
        _add(
            perp,
            _quote(
                symbol,
                row.get("highest_bid"),
                row.get("lowest_ask"),
                row.get("highest_size"),
                row.get("lowest_size"),
                row.get("last"),
                row.get("volume_24h_quote"),
            ),
        )
        interval = contract.get("funding_interval")
        _add(
            funding,
            _funding(
                symbol,
                row.get("funding_rate"),
                _millis(contract.get("funding_next_apply"), seconds=True),
                Decimal(str(interval)) / 3600 if interval else None,
            ),
        )
    return (
        spot,
        perp,
        {symbol: value for symbol, value in funding.items() if symbol in perp},
    )


async def _bybit(client: httpx.AsyncClient) -> tuple[dict, dict, dict]:
    spot_data, perp_data = await asyncio.gather(
        _get(client, "https://api.bybit.com/v5/market/tickers", {"category": "spot"}),
        _get(client, "https://api.bybit.com/v5/market/tickers", {"category": "linear"}),
    )
    spot: dict[str, Any] = {}
    perp: dict[str, Any] = {}
    funding: dict[str, Any] = {}
    for row in _unwrap(spot_data, "0"):
        _add(
            spot,
            _quote(
                row["symbol"],
                row.get("bid1Price"),
                row.get("ask1Price"),
                row.get("bid1Size"),
                row.get("ask1Size"),
                row.get("lastPrice"),
                row.get("turnover24h"),
                spot_data.get("time"),
            ),
        )
    for row in _unwrap(perp_data, "0"):
        symbol = row["symbol"]
        _add(
            perp,
            _quote(
                symbol,
                row.get("bid1Price"),
                row.get("ask1Price"),
                row.get("bid1Size"),
                row.get("ask1Size"),
                row.get("lastPrice"),
                row.get("turnover24h"),
                perp_data.get("time"),
            ),
        )
        _add(
            funding,
            _funding(
                symbol,
                row.get("fundingRate"),
                row.get("nextFundingTime"),
                row.get("fundingIntervalHour"),
                perp_data.get("time"),
            ),
        )
    return (
        spot,
        perp,
        {symbol: value for symbol, value in funding.items() if symbol in perp},
    )


async def _bitget(client: httpx.AsyncClient) -> tuple[dict, dict, dict]:
    spot_data, perp_data, funding_data = await asyncio.gather(
        _get(
            client, "https://api.bitget.com/api/v3/market/tickers", {"category": "SPOT"}
        ),
        _get(
            client,
            "https://api.bitget.com/api/v3/market/tickers",
            {"category": "USDT-FUTURES"},
        ),
        _get(
            client,
            "https://api.bitget.com/api/v3/market/current-fund-rate",
            {"category": "USDT-FUTURES"},
        ),
    )
    spot: dict[str, Any] = {}
    perp: dict[str, Any] = {}
    funding: dict[str, Any] = {}
    for rows, target in (
        (_unwrap(spot_data, "00000"), spot),
        (_unwrap(perp_data, "00000"), perp),
    ):
        for row in rows:
            _add(
                target,
                _quote(
                    row["symbol"],
                    row.get("bid1Price"),
                    row.get("ask1Price"),
                    row.get("bid1Size"),
                    row.get("ask1Size"),
                    row.get("lastPrice"),
                    row.get("turnover24h"),
                    row.get("ts"),
                ),
            )
    for row in _unwrap(funding_data, "00000"):
        _add(
            funding,
            _funding(
                row["symbol"],
                row.get("fundingRate"),
                row.get("nextUpdate"),
                row.get("fundingRateInterval"),
                funding_data.get("requestTime"),
            ),
        )
    return (
        spot,
        perp,
        {symbol: value for symbol, value in funding.items() if symbol in perp},
    )


async def _okx(client: httpx.AsyncClient) -> tuple[dict, dict, dict]:
    spot_data, perp_data, funding_data = await asyncio.gather(
        _get(client, "https://www.okx.com/api/v5/market/tickers", {"instType": "SPOT"}),
        _get(client, "https://www.okx.com/api/v5/market/tickers", {"instType": "SWAP"}),
        _get(
            client, "https://www.okx.com/api/v5/public/funding-rate", {"instId": "ANY"}
        ),
    )
    spot: dict[str, Any] = {}
    perp: dict[str, Any] = {}
    funding: dict[str, Any] = {}
    for row in _unwrap(spot_data, "0"):
        _add(
            spot,
            _quote(
                row["instId"],
                row.get("bidPx"),
                row.get("askPx"),
                row.get("bidSz"),
                row.get("askSz"),
                row.get("last"),
                row.get("volCcy24h"),
                row.get("ts"),
            ),
        )
    for row in _unwrap(perp_data, "0"):
        symbol = row["instId"]
        _add(
            perp,
            _quote(
                symbol,
                row.get("bidPx"),
                row.get("askPx"),
                row.get("bidSz"),
                row.get("askSz"),
                row.get("last"),
                source_at_ms=row.get("ts"),
            ),
        )
    for row in _unwrap(funding_data, "0"):
        symbol = row["instId"]
        if not symbol.endswith("-USDT-SWAP"):
            continue
        next_at = _millis(row.get("nextFundingTime"))
        funding_at = _millis(row.get("fundingTime"))
        interval = (
            Decimal(next_at - funding_at) / 3_600_000
            if next_at is not None and funding_at is not None and next_at > funding_at
            else None
        )
        _add(
            funding,
            _funding(
                symbol,
                row.get("fundingRate"),
                funding_at,
                interval,
                row.get("ts"),
            ),
        )
    return (
        spot,
        perp,
        {symbol: value for symbol, value in funding.items() if symbol in perp},
    )


COLLECTORS = {
    "binance": _binance,
    "gate": _gate,
    "bybit": _bybit,
    "bitget": _bitget,
    "okx": _okx,
}


async def collect_once(
    client: httpx.AsyncClient, redis: Redis, ttl_seconds: int
) -> int:
    results = await asyncio.gather(
        *(collector(client) for collector in COLLECTORS.values()),
        return_exceptions=True,
    )
    successful = 0
    for venue, result in zip(VENUES, results, strict=True):
        if isinstance(result, BaseException):
            LOG.warning(
                "%s market collection failed: %s: %r",
                venue,
                type(result).__name__,
                result,
            )
            continue
        spot, perp, funding = result
        if not spot or not perp or not funding:
            LOG.warning("%s returned an incomplete market snapshot", venue)
            continue
        collected_at_ms = int(time.time() * 1000)
        async with redis.pipeline(transaction=True) as pipe:
            for market, instruments in (
                ("spot", spot),
                ("perp", perp),
                ("funding", funding),
            ):
                payload = {
                    "schema_version": 1,
                    "venue": venue,
                    "market": market,
                    "collected_at_ms": collected_at_ms,
                    "instruments": instruments,
                }
                pipe.set(
                    f"market:v1:{venue}:{market}",
                    json.dumps(payload, separators=(",", ":")),
                    ex=ttl_seconds,
                )
            await pipe.execute()
        LOG.info(
            "%s: %d spot, %d perpetual, %d funding",
            venue,
            len(spot),
            len(perp),
            len(funding),
        )
        successful += 1
    return successful


async def _run(args: argparse.Namespace) -> None:
    redis = Redis.from_url(args.redis_url)
    client = httpx.AsyncClient(
        timeout=12.0, headers={"User-Agent": "market-collector/1"}
    )
    async with redis:
        await redis.ping()
        try:
            while True:
                started = time.monotonic()
                successful = await collect_once(client, redis, args.ttl)
                if args.once:
                    return
                if successful == 0:
                    LOG.warning("All market collections failed; recreating HTTP client")
                    await client.aclose()
                    client = httpx.AsyncClient(
                        timeout=12.0, headers={"User-Agent": "market-collector/1"}
                    )
                await asyncio.sleep(
                    max(0, args.interval - (time.monotonic() - started))
                )
        finally:
            await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("MARKET_REDIS_URL", "redis://localhost:6379/0"),
    )
    parser.add_argument(
        "--interval", type=int, default=15, help="Seconds between snapshots"
    )
    parser.add_argument(
        "--ttl", type=int, default=60, help="Redis snapshot expiry in seconds"
    )
    parser.add_argument(
        "--once", action="store_true", help="Collect one snapshot then exit"
    )
    args = parser.parse_args()
    if args.interval < 1 or args.ttl < 1:
        parser.error("--interval and --ttl must be positive")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
