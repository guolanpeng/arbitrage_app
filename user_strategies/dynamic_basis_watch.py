"""Trade a funding candidate using locally configured venue adapters."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Any

from nautilus_trader.common import DataActorConfig, Environment
from nautilus_trader.infrastructure import PostgresCacheConfig
from nautilus_trader.live import LiveExecutionEngineConfig, LiveNode
from nautilus_trader.model import ActorId, StrategyId, TraderId
from nautilus_trader.trading import (
    Controller,
    ImportableControllerConfig,
    ImportableStrategyConfig,
)

from user_strategies.basis_adapters import (
    adapter,
    instance_legs,
    selected_legs,
    validate_leg,
)
from user_strategies.basis_watch import gross_basis_percent

# Each pair is (spot venue, perpetual venue)
VENUE_PAIR_BLACKLIST = [
    ("binance", "gate"),
    ("gate", "gate"),
]
MAX_SNAPSHOT_AGE_MS = 60_000
HOURS_PER_YEAR = Decimal(8_760)
MIN_ANNUALIZED_FUNDING_RATE = Decimal("0.5")
MIN_BASIS_PERCENT = Decimal(-1)
MAX_BASIS_PERCENT = Decimal(1)
FUNDING_RANK_WEIGHT = Decimal("0.3")
TURNOVER_RANK_WEIGHT = Decimal("0.7")
DEFAULT_TARGET_NOTIONAL = Decimal(250)
CONTROLLER_SNAPSHOT_KEY = "strategy:v1:dynamic-basis:controller"
ENTRY_BASIS_PERCENT = "0.5"
EXIT_BASIS_PERCENT = "-0.2"


def controller_snapshot(
    config: BasisCandidateControllerConfig,
    strategy_id: StrategyId | None,
    scan_error: str | None,
) -> dict[str, Any]:
    """Describe controller configuration without exposing account credentials."""
    legs = getattr(config, "legs", None) or selected_legs(config.symbol)
    return {
        "schema_version": 1,
        "collected_at_ms": time.time_ns() // 1_000_000,
        "trader_id": "DYNAMIC-BASIS-001",
        "strategy_id": str(strategy_id) if strategy_id is not None else None,
        "symbol": config.symbol,
        "spot_id": legs["spot"]["instrument_id"],
        "perp_id": legs["perp"]["instrument_id"],
        "legs": legs,
        "target_notional": str(config.target_notional),
        "entry_basis_percent": ENTRY_BASIS_PERCENT,
        "exit_basis_percent": EXIT_BASIS_PERCENT,
        "min_annualized_funding_rate": str(MIN_ANNUALIZED_FUNDING_RATE),
        "min_candidate_basis_percent": str(MIN_BASIS_PERCENT),
        "max_candidate_basis_percent": str(MAX_BASIS_PERCENT),
        "max_strategies": 1,
        "poll_seconds": config.poll_seconds,
        "scan_failed": scan_error is not None,
    }


@dataclass(frozen=True)
class FundingCandidate:
    spot_venue: str
    perp_venue: str
    symbol: str
    rate: Decimal
    interval_hours: Decimal
    basis_percent: Decimal
    perp_quote_turnover_24h: Decimal = Decimal(0)

    @property
    def rate_8h(self) -> Decimal:
        return self.rate * 8 / self.interval_hours


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _rank_candidates(candidates: list[FundingCandidate]) -> None:
    rates = sorted({candidate.rate_8h for candidate in candidates})
    turnovers = sorted({candidate.perp_quote_turnover_24h for candidate in candidates})
    rate_ranks = {
        rate: Decimal(index) / (len(rates) - 1) if len(rates) > 1 else Decimal(1)
        for index, rate in enumerate(rates)
    }
    turnover_ranks = {
        turnover: Decimal(index) / (len(turnovers) - 1)
        if len(turnovers) > 1
        else Decimal(1)
        for index, turnover in enumerate(turnovers)
    }
    candidates.sort(
        key=lambda item: (
            FUNDING_RANK_WEIGHT * rate_ranks[item.rate_8h]
            + TURNOVER_RANK_WEIGHT * turnover_ranks[item.perp_quote_turnover_24h],
            item.rate_8h,
            item.perp_quote_turnover_24h,
        ),
        reverse=True,
    )


def find_candidates(
    spot_snapshot: dict[str, Any],
    perp_snapshot: dict[str, Any],
    funding_snapshot: dict[str, Any],
    now_ms: int,
    spot_venue: str,
    perp_venue: str,
) -> list[FundingCandidate]:
    """Find spot/perpetual pairs ranked by positive eight-hour funding."""
    if not spot_venue or not perp_venue:
        return []
    if (spot_venue, perp_venue) in VENUE_PAIR_BLACKLIST:
        return []
    for snapshot, market, venue in (
        (spot_snapshot, "spot", spot_venue),
        (perp_snapshot, "perp", perp_venue),
        (funding_snapshot, "funding", perp_venue),
    ):
        if not isinstance(snapshot, dict):
            return []
        collected_at = snapshot.get("collected_at_ms")
        if (
            snapshot.get("schema_version") != 1
            or snapshot.get("venue") != venue
            or snapshot.get("market") != market
            or not isinstance(collected_at, int)
            or not 0 <= now_ms - collected_at <= MAX_SNAPSHOT_AGE_MS
            or not isinstance(snapshot.get("instruments"), dict)
        ):
            return []

    spots = spot_snapshot["instruments"]
    perps = perp_snapshot["instruments"]
    funding = funding_snapshot["instruments"]
    candidates = []
    for symbol in spots.keys() & perps.keys() & funding.keys():
        if not isinstance(symbol, str) or not symbol.isalnum():
            continue
        spot = spots[symbol]
        perp = perps[symbol]
        funding_info = funding[symbol]
        if (
            not isinstance(spot, dict)
            or not isinstance(perp, dict)
            or not isinstance(funding_info, dict)
            or spot.get("base") != perp.get("base")
            or spot.get("quote") != perp.get("quote")
            or spot.get("quote") != "USDT"
            or not symbol.endswith("USDT")
        ):
            continue
        spot_ask = _positive_decimal(spot.get("ask"))
        perp_bid = _positive_decimal(perp.get("bid"))
        rate = _positive_decimal(funding_info.get("rate"))
        interval_hours = _positive_decimal(funding_info.get("interval_hours"))
        if (
            spot_ask is None
            or perp_bid is None
            or rate is None
            or interval_hours is None
        ):
            continue

        annualized_rate = rate * HOURS_PER_YEAR / interval_hours
        if annualized_rate < MIN_ANNUALIZED_FUNDING_RATE:
            continue

        basis = gross_basis_percent(spot_ask, perp_bid)
        if basis <= MIN_BASIS_PERCENT or basis > MAX_BASIS_PERCENT:
            continue
        candidates.append(
            FundingCandidate(
                spot_venue,
                perp_venue,
                symbol,
                rate,
                interval_hours,
                basis,
                _positive_decimal(perp.get("quote_turnover_24h")) or Decimal(0),
            ),
        )
    return sorted(candidates, key=lambda item: item.rate_8h, reverse=True)


class BasisCandidateControllerConfig(DataActorConfig):
    """Configure a funding candidate and its venue/account routes."""

    def __init__(
        self,
        *,
        actor_id: ActorId,
        redis_url: str,
        symbol: str,
        target_notional: Decimal | str = DEFAULT_TARGET_NOTIONAL,
        poll_seconds: int = 5,
        recovery_instance: dict[str, Any] | None = None,
        legs: dict[str, dict[str, str]] | None = None,
        new_strategy_id: str | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("Poll interval must be positive")
        super().__init__()
        self.redis_url = redis_url
        self.symbol = symbol
        self.target_notional = Decimal(target_notional)
        if not self.target_notional.is_finite() or self.target_notional <= 0:
            raise ValueError("Target USDT notional must be finite and positive")
        self.poll_seconds = poll_seconds
        self.recovery_instance = recovery_instance
        self.legs = legs or selected_legs(symbol)
        if recovery_instance is None and new_strategy_id is None:
            raise ValueError("New strategy requires a database-allocated ID")
        self.new_strategy_id = new_strategy_id


class BasisCandidateController(Controller):
    """Create at most one strategy for the symbol selected before connecting."""

    def __init__(self, config: BasisCandidateControllerConfig) -> None:
        super().__init__(config)
        self._config = config
        self._updates: Queue[list[FundingCandidate] | str] = Queue(maxsize=1)
        self._stop_event = Event()
        self._strategy_id: StrategyId | None = None

    def on_start(self) -> None:
        """Poll Redis off the live event loop and process results on its timer."""
        self.clock.set_timer("basis-candidate-check", timedelta(seconds=1))
        if self._config.recovery_instance is not None:
            instance = self._config.recovery_instance
            legs = instance_legs(instance)
            self._strategy_id = self.create_strategy_from_config(
                ImportableStrategyConfig(
                    strategy_path="user_strategies.basis_watch:BasisWatchStrategy",
                    config_path="user_strategies.basis_watch:BasisWatchConfig",
                    config={
                        "spot_id": instance["spot_id"],
                        "perp_id": instance["perp_id"],
                        "spot_client_id": legs["spot"]["client_id"],
                        "perp_client_id": legs["perp"]["client_id"],
                        "legs": legs,
                        "target_notional": instance["target_notional"],
                        "alert_basis_percent": instance["entry_basis_percent"],
                        "exit_basis_percent": instance.get(
                            "exit_basis_percent", EXIT_BASIS_PERCENT
                        ),
                        "strategy_id": instance["strategy_id"],
                        "persistent_exit_control": True,
                        "recovery": instance["recovery"],
                        "use_uuid_client_order_ids": True,
                    },
                ),
            )
        Thread(target=self._poll_redis, name="basis-redis-poller", daemon=True).start()

    def on_time_event(self, _event: Any) -> None:
        """Start the selected candidate once its Redis snapshots are fresh."""
        try:
            update = self._updates.get_nowait()
        except Empty:
            return
        if isinstance(update, str):
            self.log.warning(f"Redis candidate scan failed: {update}")
            return

        legs = getattr(self._config, "legs", None) or selected_legs(self._config.symbol)
        for candidate in update:
            spot_venue = candidate.spot_venue.upper()
            perp_venue = candidate.perp_venue.upper()
            symbol = candidate.symbol
            self.log.info(
                f"FUNDING {spot_venue} spot / {perp_venue} perpetual {symbol}: "
                f"rate={candidate.rate * 100:.6f}%/{candidate.interval_hours}h, "
                f"8h equivalent={candidate.rate_8h * 100:.6f}%, "
                f"perp 24h turnover={candidate.perp_quote_turnover_24h} USDT, "
                f"reference basis={candidate.basis_percent:.4f}%"
            )
            if (
                self._strategy_id is not None
                or (candidate.spot_venue, candidate.perp_venue)
                != (legs["spot"]["venue"], legs["perp"]["venue"])
                or candidate.symbol != self._config.symbol
            ):
                continue
            strategy_id = self._config.new_strategy_id
            self._strategy_id = self.create_strategy_from_config(
                ImportableStrategyConfig(
                    strategy_path="user_strategies.basis_watch:BasisWatchStrategy",
                    config_path="user_strategies.basis_watch:BasisWatchConfig",
                    config={
                        "spot_id": legs["spot"]["instrument_id"],
                        "perp_id": legs["perp"]["instrument_id"],
                        "spot_client_id": legs["spot"]["client_id"],
                        "perp_client_id": legs["perp"]["client_id"],
                        "legs": legs,
                        "target_notional": str(self._config.target_notional),
                        "alert_basis_percent": ENTRY_BASIS_PERCENT,
                        "exit_basis_percent": EXIT_BASIS_PERCENT,
                        "strategy_id": strategy_id,
                        "persistent_exit_control": True,
                        "use_uuid_client_order_ids": True,
                    },
                ),
            )

    def on_stop(self) -> None:
        """Stop the Redis poller without blocking the live event loop."""
        self._stop_event.set()
        self.clock.cancel_timer("basis-candidate-check")

    def _poll_redis(self) -> None:
        from redis import Redis, RedisError

        client = Redis.from_url(
            self._config.redis_url,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        try:
            while not self._stop_event.is_set():
                try:
                    legs = getattr(self._config, "legs", None)
                    update: list[FundingCandidate] | str = _read_candidates(
                        client,
                        legs["spot"]["venue"] if legs else None,
                        legs["perp"]["venue"] if legs else None,
                    )
                except (RedisError, json.JSONDecodeError, TypeError) as exc:
                    update = str(exc)
                try:
                    self._updates.put_nowait(update)
                except Full:
                    try:
                        self._updates.get_nowait()
                    except Empty:
                        pass
                    self._updates.put_nowait(update)
                try:
                    client.set(
                        CONTROLLER_SNAPSHOT_KEY,
                        json.dumps(
                            controller_snapshot(
                                self._config,
                                self._strategy_id,
                                update if isinstance(update, str) else None,
                            ),
                            separators=(",", ":"),
                        ),
                    )
                except RedisError:
                    # Display persistence must not discard candidate updates
                    pass
                self._stop_event.wait(self._config.poll_seconds)
        finally:
            client.close()


def _read_candidates(
    client: Any, spot_venue: str | None = None, perp_venue: str | None = None
) -> list[FundingCandidate]:
    spot_venue = (
        spot_venue
        or adapter(os.getenv("BASIS_SPOT_PROFILE", "gate_spot"), "spot").venue
    )
    perp_venue = (
        perp_venue
        or adapter(os.getenv("BASIS_PERP_PROFILE", "binance_perp"), "perp").venue
    )
    snapshots = client.mget(
        [
            f"market:v1:{spot_venue}:spot",
            f"market:v1:{perp_venue}:perp",
            f"market:v1:{perp_venue}:funding",
        ],
    )
    if any(snapshot is None for snapshot in snapshots):
        return []
    candidates = find_candidates(
        *(json.loads(snapshot) for snapshot in snapshots),
        time.time_ns() // 1_000_000,
        spot_venue,
        perp_venue,
    )
    _rank_candidates(candidates)
    return candidates


def main(target_notional: Decimal = DEFAULT_TARGET_NOTIONAL) -> None:
    """Select one candidate, configure 2x leverage, and run live execution."""

    if not target_notional.is_finite() or target_notional <= 0:
        raise ValueError("Target USDT notional must be finite and positive")
    from user_strategies.basis_exit_control import (
        execution_lease,
        initialize_strategy_schema,
    )

    with execution_lease():
        initialize_strategy_schema()
        _run_dynamic(target_notional)


def _run_dynamic(target_notional: Decimal) -> None:
    from redis import Redis

    from user_strategies.basis_exit_control import (
        allocate_strategy_id,
        recovery_candidate,
    )

    recovery = recovery_candidate()
    if (
        recovery
        and recovery["state"] != "stopped"
        and 0 <= time.time_ns() // 1_000_000 - recovery["updated_at_ms"] <= 10_000
    ):
        raise SystemExit(
            "Retained strategy still heartbeating; refusing a duplicate execution process"
        )
    redis_url = os.getenv("MARKET_REDIS_URL", "redis://127.0.0.1:6379/0")
    if recovery is not None:
        candidates = []
    else:
        with Redis.from_url(
            redis_url, socket_timeout=2, socket_connect_timeout=2
        ) as client:
            candidates = _read_candidates(client)
    if not candidates and recovery is None:
        raise SystemExit("No fresh funding candidate for configured adapters")
    symbol = (
        instance_legs(recovery)["spot"]["symbol"] if recovery else candidates[0].symbol
    )
    legs = instance_legs(recovery) if recovery else selected_legs(symbol)
    providers = {
        market: validate_leg(legs[market], market) for market in ("spot", "perp")
    }
    new_strategy_id = (
        None
        if recovery is not None
        else allocate_strategy_id(legs["spot"]["venue"], legs["perp"]["venue"], symbol)
    )
    builder = (
        LiveNode.builder(
            "MULTI-EXCHANGE-DYNAMIC-BASIS-001",
            TraderId.from_str("DYNAMIC-BASIS-001"),
            Environment.LIVE,
        )
        .with_cache_database_factory(PostgresCacheConfig())
        .with_exec_engine_config(LiveExecutionEngineConfig(load_cache=False))
    )
    for market in ("perp", "spot"):
        builder = providers[market].add_data(builder, legs[market])
    for market in ("spot", "perp"):
        builder = providers[market].add_exec(builder, legs[market])
    node = builder.with_controller(
        ImportableControllerConfig(
            controller_path="user_strategies.dynamic_basis_watch:BasisCandidateController",
            config_path="user_strategies.dynamic_basis_watch:BasisCandidateControllerConfig",
            config={
                "actor_id": "BASIS-CANDIDATES-001",
                "redis_url": redis_url,
                "symbol": symbol,
                "target_notional": str(target_notional),
                "poll_seconds": 5,
                "recovery_instance": recovery,
                "legs": legs,
                "new_strategy_id": new_strategy_id,
            },
        ),
    ).build()
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-notional",
        type=Decimal,
        default=DEFAULT_TARGET_NOTIONAL,
        help=(
            "Target spot purchase notional in USDT (default: 250), "
            "excluding fees and perpetual margin"
        ),
    )
    main(parser.parse_args().target_notional)
