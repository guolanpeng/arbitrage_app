"""Persist read-only dedicated-account snapshots and settled UM funding income."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
import psycopg
from psycopg.rows import dict_row
from redis import Redis, RedisError

from user_strategies.basis_adapters import instance_legs, validate_leg

DAY_MS = 86_400_000
INCOME_RETENTION_MS = 89 * DAY_MS


def database_connection() -> Any:
    """Connect to the same PostgreSQL database as the trading cache."""
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        user=os.environ["POSTGRES_USERNAME"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DATABASE"],
        connect_timeout=3,
        options="-c statement_timeout=5000",
        row_factory=dict_row,
    )


def write_record(connection: Any, key: str, value: dict[str, Any]) -> None:
    connection.execute(
        "INSERT INTO general (id, value) VALUES (%s, %s) "
        "ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value",
        (key, json.dumps(value, separators=(",", ":")).encode()),
    )


def load_records(connection: Any, prefix: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        "SELECT id, value FROM general WHERE starts_with(id, %s)",
        (prefix,),
    ).fetchall()
    return {row["id"]: json.loads(bytes(row["value"])) for row in rows}


class AccountReader:
    """Only issue authenticated GET requests; never submit or modify orders."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def gate_get(self, path: str) -> list[dict[str, Any]]:
        timestamp = str(int(time.time()))
        payload = f"GET\n{path}\n\n{hashlib.sha512(b'').hexdigest()}\n{timestamp}"
        signature = hmac.new(
            os.environ["GATE_API_SECRET"].encode(),
            payload.encode(),
            hashlib.sha512,
        ).hexdigest()
        response = self.client.get(
            f"https://api.gateio.ws{path}",
            headers={
                "KEY": os.environ["GATE_API_KEY"],
                "SIGN": signature,
                "Timestamp": timestamp,
            },
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise TypeError("Invalid Gate balance response")
        return rows

    def gate_balances(self) -> list[dict[str, Any]]:
        return self.gate_get("/api/v4/spot/accounts")

    def gate_open_orders(self) -> list[dict[str, Any]]:
        rows = self.gate_get("/api/v4/spot/open_orders")
        return [order for group in rows for order in group["orders"]]

    def binance_get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        query = urlencode(
            {**params, "timestamp": time.time_ns() // 1_000_000, "recvWindow": 5000}
        )
        signature = hmac.new(
            os.environ["BINANCE_API_SECRET"].encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        response = self.client.get(
            f"https://papi.binance.com{path}?{query}&signature={signature}",
            headers={"X-MBX-APIKEY": os.environ["BINANCE_API_KEY"]},
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise TypeError("Invalid Binance response")
        return rows

    def funding_history(
        self, symbol: str, start: int, end: int
    ) -> list[dict[str, Any]]:
        """Retrieve every page in bounded time windows, including both boundaries."""
        records = {}
        while start <= end:
            stop = min(end, start + 7 * DAY_MS - 1)
            page = 1
            seen_pages = set()
            while True:
                rows = self.binance_get(
                    "/papi/v1/um/income",
                    {
                        "symbol": symbol,
                        "incomeType": "FUNDING_FEE",
                        "startTime": start,
                        "endTime": stop,
                        "limit": 1000,
                        "page": page,
                    },
                )
                identifiers = tuple((str(row["tranId"]), row["asset"]) for row in rows)
                if rows and identifiers in seen_pages:
                    raise ValueError("Funding pagination did not advance")
                seen_pages.add(identifiers)
                for row in rows:
                    if (
                        row["symbol"] != symbol
                        or row["incomeType"] != "FUNDING_FEE"
                        or not start <= int(row["time"]) <= stop
                        or not Decimal(row["income"]).is_finite()
                    ):
                        raise ValueError("Invalid funding history row")
                    records[(str(row["tranId"]), row["asset"])] = row
                if len(rows) < 1000:
                    break
                page += 1
            start = stop + 1
        return list(records.values())


def collect_once(reader: AccountReader, redis: Redis) -> int:
    """Store per-instance funding and fresh account checks independently of trading."""
    now = time.time_ns() // 1_000_000
    with database_connection() as connection:
        instances = [
            row["snapshot"]
            for row in connection.execute(
                "SELECT snapshot FROM basis_strategy WHERE trader_id = %s AND "
                "(state NOT IN ('closed', 'stopped') OR spot_remaining <> 0 "
                "OR perp_remaining <> 0 OR COALESCE(stopped_at_ms, updated_at_ms) >= %s)",
                ("DYNAMIC-BASIS-001", now - DAY_MS),
            ).fetchall()
        ]
        previous = load_records(connection, "basis:account:v1:")
    instances = [
        instance
        for instance in instances
        if instance.get("trader_id") == "DYNAMIC-BASIS-001"
        and not previous.get(f"basis:account:v1:{instance['strategy_id']}", {}).get(
            "finalized"
        )
    ]
    if not instances:
        return 0
    account_cache = {}
    written = 0
    for instance in instances:
        if instance.get("trader_id") != "DYNAMIC-BASIS-001":
            continue
        strategy_id = instance["strategy_id"]
        account_key = f"basis:account:v1:{strategy_id}"
        old = previous.get(account_key, {})
        if old.get("finalized"):
            continue
        legs = instance_legs(instance)
        providers = {
            market: validate_leg(legs[market], market) for market in ("spot", "perp")
        }
        accounts = {}
        for name, leg in legs.items():
            key = (leg["profile"], leg["account_id"], leg["instrument_id"])
            if key not in account_cache:
                try:
                    account_cache[key] = providers[name].account(reader, leg)
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    account_cache[key] = None
            accounts[name] = account_cache[key]
        try:
            values = redis.mget(
                [
                    f"market:v1:{legs['spot']['venue']}:spot",
                    f"market:v1:{legs['perp']['venue']}:funding",
                ]
            )
            market = [json.loads(value) if value else {} for value in values]
            if any(not isinstance(snapshot, dict) for snapshot in market):
                raise ValueError("Invalid market snapshot")
        except (RedisError, ValueError, TypeError):
            market = [{}, {}]
        symbol = legs["perp"]["symbol"]
        end = int(instance["stopped_at_ms"]) if instance["state"] == "closed" else now
        first_start = int(instance["started_at_ms"])
        start = max(
            first_start, int(old.get("income_checked_at_ms") or first_start) - DAY_MS
        )
        income_complete = old.get("income_complete", False)
        history = []
        income_error = None
        try:
            if start < now - INCOME_RETENTION_MS:
                raise ValueError("Income history retention exceeded")
            history = providers["perp"].funding_history(
                reader, legs["perp"], start, end
            )
            income_complete = True
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            income_error = "资金费流水同步失败或超出交易所历史保留范围"
        snapshot = {
            "schema_version": 1,
            "strategy_id": strategy_id,
            "collected_at_ms": now,
            "legs": legs,
            "spot_account": accounts["spot"],
            "perp_account": accounts["perp"],
            "income_complete": income_complete,
            "income_checked_at_ms": (
                old.get("income_checked_at_ms") if income_error else end
            ),
            "income_error": income_error,
            # Wait for delayed funding postings after closure before finalizing
            "finalized": instance["state"] == "closed"
            and income_error is None
            and now - end >= DAY_MS,
            "quotes": {
                "spot": market[0].get("instruments", {}).get(symbol),
                "spot_at_ms": market[0].get("collected_at_ms"),
                "funding": market[1].get("instruments", {}).get(symbol),
                "funding_at_ms": market[1].get("collected_at_ms"),
            },
        }
        with database_connection() as connection:
            for row in history:
                write_record(
                    connection,
                    f"basis:income:v1:{strategy_id}:{row['tranId']}:{row['asset']}",
                    {
                        "strategy_id": strategy_id,
                        "symbol": symbol,
                        "transaction_id": str(row["tranId"]),
                        "asset": row["asset"],
                        "amount": row["income"],
                        "at_ms": int(row["time"]),
                    },
                )
            write_record(connection, account_key, snapshot)
            write_record(connection, f"basis:sample:v1:{strategy_id}:{now}", snapshot)
        written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if args.interval < 15:
        parser.error("Interval must be at least 15 seconds")
    with (
        httpx.Client(timeout=10) as client,
        Redis.from_url(
            os.getenv("MARKET_REDIS_URL", "redis://127.0.0.1:6379/0"),
            socket_timeout=3,
        ) as redis,
    ):
        reader = AccountReader(client)
        while True:
            try:
                count = collect_once(reader, redis)
                print(f"Basis monitoring: {count} instance(s) updated", flush=True)
            except (psycopg.Error, ValueError, KeyError, TypeError) as exc:
                # Exceptions can contain signed request URLs or credentials
                print(f"Basis monitoring unavailable: {type(exc).__name__}", flush=True)
            if args.once:
                return
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
