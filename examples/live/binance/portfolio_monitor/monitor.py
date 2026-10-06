#!/usr/bin/env python3
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
"""Monitor an existing Binance Portfolio Margin account with NautilusTrader."""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from nautilus_trader.adapters.binance import (
    BinanceExecutionClientConfig,
    BinanceExecutionClientFactory,
)
from nautilus_trader.common import Environment, LoggerConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import AccountId, TraderId
from nautilus_trader.trading import Strategy
from redis import Redis

ACCOUNT_ID = AccountId.from_str("BINANCE-001")
REDIS_KEY = "account:v1:binance:portfolio"


@dataclass(frozen=True)
class BinancePortfolioPositionRisk:
    """Exact risk values returned by a Portfolio Margin positionRisk query."""

    product: str
    symbol: str
    position_side: str
    position_amount: Decimal
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Decimal
    unrealized_pnl: Decimal
    leverage: Decimal
    notional: Decimal
    update_time: int


def show(value: object) -> str:
    """Show a missing exchange field without changing numeric precision."""
    return str(value) if value is not None and value != "" else "-"


def position_risks(state: object) -> list[BinancePortfolioPositionRisk]:
    """Build exact position-risk snapshots from a NautilusTrader account event."""
    info = state.info or {}
    risks = []
    for product, key in (
        ("UM", "binance_portfolio_um_position_risks"),
        ("CM", "binance_portfolio_cm_position_risks"),
    ):
        for row in info.get(key, []):
            amount = Decimal(row["positionAmt"])
            if amount.is_zero():
                continue
            risks.append(
                BinancePortfolioPositionRisk(
                    product=product,
                    symbol=row["symbol"],
                    position_side=row["positionSide"],
                    position_amount=amount,
                    entry_price=Decimal(row["entryPrice"]),
                    mark_price=Decimal(row["markPrice"]),
                    liquidation_price=Decimal(row["liquidationPrice"]),
                    unrealized_pnl=Decimal(row["unRealizedProfit"]),
                    leverage=Decimal(row["leverage"]),
                    notional=Decimal(
                        row.get("notional", row.get("notionalValue", "0"))
                    ),
                    update_time=int(row["updateTime"]),
                )
            )
    return risks


def redis_snapshot(state: object) -> dict[str, object]:
    """Build the versioned JSON snapshot stored for external consumers."""
    info = state.info or {}
    positions = []
    for product, key in (
        ("UM", "binance_portfolio_um_position_risks"),
        ("CM", "binance_portfolio_cm_position_risks"),
    ):
        for row in info.get(key, []):
            if Decimal(row["positionAmt"]).is_zero():
                continue
            positions.append({"product": product, **row})
    return {
        "schema_version": 1,
        "venue": "binance",
        "account_type": "portfolio_margin",
        "collected_at_ms": int(time.time() * 1000),
        "account": info.get("binance_portfolio_margin", {}),
        "assets": info.get("binance_portfolio_balances", []),
        "positions": positions,
    }


def write_redis_snapshot(redis: Redis, state: object, ttl_seconds: int) -> None:
    """Replace the current Portfolio Margin snapshot in Redis."""
    redis.set(
        REDIS_KEY,
        json.dumps(redis_snapshot(state), ensure_ascii=False, separators=(",", ":")),
        ex=ttl_seconds,
    )


def render_snapshot(state: object, positions: list[object]) -> str:
    """Format the latest NautilusTrader account state and reconciled positions."""
    info = state.info or {}
    summary = info.get("binance_portfolio_margin", {})
    assets = info.get("binance_portfolio_balances", [])
    lines = [
        f"Portfolio Margin  {datetime.now(UTC).isoformat(timespec='seconds')}",
        "Account",
        (
            f"  status={show(summary.get('accountStatus'))} "
            f"uniMMR={show(summary.get('uniMMR'))} "
            f"equityUSD={show(summary.get('accountEquity'))} "
            f"actualEquityUSD={show(summary.get('actualEquity'))}"
        ),
        (
            f"  initialMarginUSD={show(summary.get('accountInitialMargin'))} "
            f"maintMarginUSD={show(summary.get('accountMaintMargin'))} "
            f"availableUSD={show(summary.get('totalAvailableBalance'))}"
        ),
        "Assets (cross-margin liabilities are shared across pairs)",
    ]
    lines.extend(
        (
            f"  {show(asset.get('asset'))}: "
            f"wallet={show(asset.get('totalWalletBalance'))} "
            f"borrowed={show(asset.get('crossMarginBorrowed'))} "
            f"interest={show(asset.get('crossMarginInterest'))} "
            f"locked={show(asset.get('crossMarginLocked'))}"
        )
        for asset in sorted(assets, key=lambda row: row.get("asset", ""))
    )
    if not assets:
        lines.append("  none")
    risks = position_risks(state)
    lines.append("Open futures positions (Portfolio Margin positionRisk)")
    for risk in sorted(
        risks, key=lambda item: (item.product, item.symbol, item.position_side)
    ):
        liquidation = (
            "-" if risk.liquidation_price.is_zero() else str(risk.liquidation_price)
        )
        lines.extend(
            [
                (
                    f"  {risk.product} {risk.symbol} side={risk.position_side} "
                    f"qty={risk.position_amount} leverage={risk.leverage}x"
                ),
                (
                    f"    entry={risk.entry_price} mark={risk.mark_price} "
                    f"liquidation={liquidation} unrealizedPnl={risk.unrealized_pnl} "
                    f"notional={risk.notional} updateTime={risk.update_time}"
                ),
            ]
        )
    if not risks:
        lines.extend(
            (
                f"  {position.instrument_id} "
                f"side={position.side} qty={position.quantity} id={position.id}"
            )
            for position in sorted(positions, key=lambda item: str(item.instrument_id))
        )
        if not positions:
            lines.append("  none")
    return "\n".join(lines)


class PortfolioMonitor(Strategy):
    """Read account and position state from the unified execution client."""

    def __init__(self) -> None:
        """Initialize the monitor before attaching it to a node."""
        super().__init__()

    def configure(
        self,
        interval_seconds: int,
        once: bool,
        redis_url: str,
        redis_ttl_seconds: int,
    ) -> None:
        """Configure the display interval and optional single snapshot."""
        self._interval_seconds = interval_seconds
        self._once = once
        self._redis = Redis.from_url(redis_url)
        self._redis_ttl_seconds = redis_ttl_seconds
        self._previous_account_event_id = None
        self._refresh_pending = False

    def on_start(self) -> None:
        """Query immediately, then refresh over HTTP at the configured interval."""
        self._redis.ping()
        self._request_refresh(None)
        self.clock.set_timer(
            "portfolio-monitor-check",
            timedelta(milliseconds=200),
            callback=self._on_refresh_check,
        )
        if not self._once:
            self.clock.set_timer(
                "portfolio-monitor-refresh",
                timedelta(seconds=self._interval_seconds),
                callback=self._request_refresh,
            )

    def _request_refresh(self, _event: object | None) -> None:
        account = self.cache.account(ACCOUNT_ID)
        state = account.last_event if account is not None else None
        self._previous_account_event_id = state.event_id if state is not None else None
        self._refresh_pending = True
        self.query_account(ACCOUNT_ID)

    def _on_refresh_check(self, _event: object) -> None:
        if not self._refresh_pending:
            return
        account = self.cache.account(ACCOUNT_ID)
        state = account.last_event if account is not None else None
        if state is None or "binance_portfolio_margin" not in (state.info or {}):
            return
        if state.event_id == self._previous_account_event_id:
            return
        self._refresh_pending = False
        write_redis_snapshot(self._redis, state, self._redis_ttl_seconds)
        print(
            render_snapshot(state, self.cache.positions_open(account_id=ACCOUNT_ID)),
            flush=True,
        )
        if self._once:
            self.clock.cancel_timer("portfolio-monitor-check")
            self.shutdown_system("Portfolio Margin snapshot complete")

    def on_stop(self) -> None:
        """Close the Redis connection owned by the monitor."""
        self._redis.close()


def build_node(
    interval_seconds: int,
    once: bool,
    redis_url: str,
    redis_ttl_seconds: int,
) -> LiveNode:
    """Build one unified execution client and a read-only monitoring strategy."""
    node = (
        LiveNode.builder(
            "BINANCE-PORTFOLIO-MONITOR",
            TraderId.from_str("MONITOR-001"),
            Environment.LIVE,
        )
        .with_logging(LoggerConfig(is_colored=sys.stdout.isatty()))
        .with_reconciliation(reconciliation=True)
        .with_timeout_connection(60)
        .add_exec_client(
            "BINANCE",
            BinanceExecutionClientFactory(),
            BinanceExecutionClientConfig(
                account_id=ACCOUNT_ID,
                unified_account=True,
                use_ws_trading=False,
            ),
        )
        .build()
    )
    monitor = PortfolioMonitor()
    monitor.configure(interval_seconds, once, redis_url, redis_ttl_seconds)
    node.add_strategy(monitor)
    return node


def main() -> None:
    """Run the monitor until interrupted or after one snapshot."""
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="Print one snapshot and exit"
    )
    parser.add_argument(
        "--interval", type=int, default=30, help="Display interval in seconds (min 30)"
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("MARKET_REDIS_URL", "redis://localhost:6379/0"),
    )
    parser.add_argument(
        "--redis-ttl", type=int, default=60, help="Redis snapshot expiry in seconds"
    )
    args = parser.parse_args()
    if args.interval < 30:
        parser.error("--interval must be at least 30 seconds")
    if args.redis_ttl < 1:
        parser.error("--redis-ttl must be positive")
    if not os.environ.get("BINANCE_API_KEY") or not os.environ.get(
        "BINANCE_API_SECRET"
    ):
        parser.error("BINANCE_API_KEY and BINANCE_API_SECRET are required")
    build_node(args.interval, args.once, args.redis_url, args.redis_ttl).run()


if __name__ == "__main__":
    main()
