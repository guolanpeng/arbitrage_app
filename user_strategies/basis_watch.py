"""Buy spot as maker and hedge fills with perpetual taker orders."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from queue import Empty
from typing import Any

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import (
    BookType,
    ClientId,
    ClientOrderId,
    FundingRateUpdate,
    InstrumentId,
    OrderBookDeltas,
    OrderSide,
    PositionSide,
    Quantity,
    StrategyId,
)
from nautilus_trader.trading import Strategy

from user_strategies.basis_exit_control import (
    ExitControlReader,
    condition_met,
    exit_settings,
)


def gross_basis_percent(spot_ask: Decimal, perp_bid: Decimal) -> Decimal:
    """Return the gross basis for buying spot and selling the perpetual."""
    return (perp_bid / spot_ask - Decimal(1)) * Decimal(100)


class BasisWatchConfig(StrategyConfig):
    """Configure a USDT spot purchase target and perpetual hedge threshold."""

    def __init__(
        self,
        *,
        spot_id: InstrumentId | str,
        perp_id: InstrumentId | str,
        spot_client_id: ClientId | str,
        perp_client_id: ClientId | str,
        alert_basis_percent: Decimal | str,
        target_notional: Decimal | str,
        strategy_id: StrategyId,
        order_id_tag: str | None = None,
        book_depth: int = 20,
        max_book_age_secs: int = 5,
        exit_basis_percent: Decimal | str = Decimal("-0.2"),
        persistent_exit_control: bool = False,
        recovery: dict[str, Any] | None = None,
        legs: dict[str, dict[str, str]] | None = None,
        use_uuid_client_order_ids: bool = True,
    ) -> None:
        if book_depth <= 0 or max_book_age_secs <= 0:
            raise ValueError("Book depth and book age must be positive")
        # StrategyConfig initializes its PyO3 base in __new__, including strategy_id.
        super().__init__()
        self.spot_id = (
            InstrumentId.from_str(spot_id) if isinstance(spot_id, str) else spot_id
        )
        self.perp_id = (
            InstrumentId.from_str(perp_id) if isinstance(perp_id, str) else perp_id
        )
        self.spot_client_id = (
            ClientId.from_str(spot_client_id)
            if isinstance(spot_client_id, str)
            else spot_client_id
        )
        self.perp_client_id = (
            ClientId.from_str(perp_client_id)
            if isinstance(perp_client_id, str)
            else perp_client_id
        )
        if target_notional is None:
            raise ValueError("Target USDT notional is required")
        if alert_basis_percent is None:
            raise ValueError("Trading requires a finite basis threshold")
        self.target_notional = Decimal(target_notional)
        self.alert_basis_percent = Decimal(alert_basis_percent)
        self.exit_basis_percent = Decimal(exit_basis_percent)
        if not self.target_notional.is_finite() or self.target_notional <= 0:
            raise ValueError("Target USDT notional must be finite and positive")
        if not self.alert_basis_percent.is_finite():
            raise ValueError("Trading requires a finite basis threshold")
        if not self.exit_basis_percent.is_finite():
            raise ValueError("Trading requires a finite exit basis threshold")
        self.book_depth = book_depth
        self.max_book_age_ns = max_book_age_secs * 1_000_000_000
        self.persistent_exit_control = persistent_exit_control
        self.recovery = recovery
        self.legs = legs
        if legs is not None:
            from user_strategies.basis_adapters import instance_legs

            instance_legs(
                {
                    "legs": legs,
                    "spot_id": str(self.spot_id),
                    "perp_id": str(self.perp_id),
                }
            )
            if legs["spot"]["client_id"] != str(self.spot_client_id) or legs["perp"][
                "client_id"
            ] != str(self.perp_client_id):
                raise ValueError("Strategy client IDs differ from persisted routes")


class BasisWatchStrategy(Strategy):
    """Buy spot as maker and immediately hedge fills with perpetual taker orders."""

    def __init__(self, config: BasisWatchConfig) -> None:
        super().__init__(config)
        self._config = config
        self._spot_book_update_ns: int | None = None
        self._perp_book_update_ns: int | None = None
        self._spot_order: Any = None
        self._spot_orders: dict[Any, Any] = {}
        self._hedge_orders: dict[Any, Any] = {}
        self._seen_fills: set[tuple[Any, Any]] = set()
        self._spot_filled = Decimal(0)
        self._spot_filled_notional = Decimal(0)
        self._perp_filled = Decimal(0)
        self._cancel_pending = False
        self._cancel_retry_after_ns = 0
        self._entry_paused = False
        self._order_check_started = False
        self._funding_rate: Decimal | None = None
        self._funding_update_ns: int | None = None
        self._exiting = False
        self._exit_spot_orders: set[Any] = set()
        self._exit_hedge_orders: set[Any] = set()
        self._spot_closed = Decimal(0)
        self._perp_closed = Decimal(0)
        self._monitor_started_ns = 0
        self._monitor_stopped_ns: int | None = None
        self._monitor_completed = False
        self._monitor_reason: str | None = None
        self._monitor_baseline: dict[str, str | None] = {"spot": None, "perp": None}
        self._monitor_last_state: tuple[str, str | None] | None = None
        self._monitor_sequence = 0
        self._exit_settings = (
            exit_settings({}) if config.persistent_exit_control else None
        )
        self._exit_reader = (
            ExitControlReader(str(config.strategy_id))
            if config.persistent_exit_control
            else None
        )
        self._control_ready = not config.persistent_exit_control
        self._recovery_pending = config.recovery is not None
        self._restored_spot_fills = Decimal(0)
        self._restored_spot_closes = Decimal(0)

    def _publish_monitor(self) -> None:
        """Queue public monitoring records through the existing asynchronous cache writer."""
        now = self.clock.timestamp_ns()
        state = (
            "closed"
            if self._monitor_completed
            else "stopped"
            if self._monitor_stopped_ns is not None
            else "reconciling"
            if self._recovery_pending or not self._control_ready
            else "paused"
            if self._entry_paused
            else "exiting"
            if self._exiting
            else "hedging"
            if self._spot_filled - self._spot_closed
            != self._perp_filled - self._perp_closed
            else "opening"
            if self._spot_order is not None
            else "holding"
            if self._spot_filled
            else "waiting"
        )
        snapshot = {
            "schema_version": 1,
            "trader_id": str(self.trader_id),
            "strategy_id": str(self.strategy_id),
            "spot_id": str(self._config.spot_id),
            "perp_id": str(self._config.perp_id),
            "spot_client_id": str(self._config.spot_client_id),
            "perp_client_id": str(self._config.perp_client_id),
            "legs": self._config.legs,
            "target_notional": str(self._config.target_notional),
            "entry_basis_percent": str(self._config.alert_basis_percent),
            "exit_basis_percent": str(self._config.exit_basis_percent),
            "started_at_ms": self._monitor_started_ns // 1_000_000,
            "updated_at_ms": now // 1_000_000,
            "stopped_at_ms": (
                self._monitor_stopped_ns // 1_000_000
                if self._monitor_stopped_ns is not None
                else None
            ),
            "state": state,
            "reason": self._monitor_reason,
            "baseline": self._monitor_baseline,
            "base_currency": self._monitor_baseline.get("base_currency"),
            "spot_remaining": str(self._spot_filled - self._spot_closed),
            "perp_remaining": str(self._perp_filled - self._perp_closed),
            "exit_control_supported": self._exit_reader is not None,
            "exit_settings": self._exit_settings,
            "control_ready": self._control_ready,
            "recovery_pending": self._recovery_pending,
        }
        value = json.dumps(snapshot, separators=(",", ":")).encode()
        try:
            self.cache.add(f"basis:instance:v1:{self.strategy_id}", value)
            transition = (state, self._monitor_reason)
            if transition != self._monitor_last_state:
                self._monitor_sequence += 1
                self.cache.add(
                    f"basis:state:v1:{self.strategy_id}:{self._monitor_sequence}",
                    value,
                )
                self._monitor_last_state = transition
        except (RuntimeError, ValueError) as exc:
            self.log.warning(
                f"Monitoring record could not be queued: {type(exc).__name__}"
            )

    def _capture_monitor_baseline(self, spot: Any) -> None:
        """Record existing inventory separately from this instance's fills."""
        try:
            self._monitor_baseline["base_currency"] = str(spot.base_currency)
            account = self.cache.account_for_venue(self._config.spot_id.venue)
            if (
                account is not None
                and account.last_event is not None
                and (
                    0
                    <= self.clock.timestamp_ns() - account.last_event.ts_init
                    <= 30_000_000_000
                )
            ):
                balance = account.balance_total(spot.base_currency)
                self._monitor_baseline["spot"] = (
                    str(balance.as_decimal()) if balance is not None else "0"
                )
            account = self.cache.account_for_venue(self._config.perp_id.venue)
            if (
                account is not None
                and account.last_event is not None
                and (
                    0
                    <= self.clock.timestamp_ns() - account.last_event.ts_init
                    <= 30_000_000_000
                )
            ):
                quantity = sum(
                    (
                        position.quantity.as_decimal()
                        * (-1 if position.side == PositionSide.SHORT else 1)
                        for position in self.cache.positions_open(
                            instrument_id=self._config.perp_id,
                        )
                    ),
                    Decimal(0),
                )
                self._monitor_baseline["perp"] = str(quantity)
        except (RuntimeError, ValueError) as exc:
            self.log.warning(f"Monitoring baseline unavailable: {type(exc).__name__}")

    def on_start(self) -> None:
        """Subscribe to managed L2 order books after both instruments are loaded."""
        self._monitor_started_ns = self.clock.timestamp_ns()
        if self.cache.instrument(self._config.spot_id) is None:
            self._monitor_reason = "Spot instrument unavailable"
            self._publish_monitor()
            self.log.error(f"Spot instrument unavailable: {self._config.spot_id}")
            self.stop()
            return
        if self.cache.instrument(self._config.perp_id) is None:
            self._monitor_reason = "Perpetual instrument unavailable"
            self._publish_monitor()
            self.log.error(f"Perpetual instrument unavailable: {self._config.perp_id}")
            self.stop()
            return

        spot = self.cache.instrument(self._config.spot_id)
        perp = self.cache.instrument(self._config.perp_id)
        spot_step = spot.size_increment.as_decimal()
        perp_step = perp.size_increment.as_decimal()
        if (
            spot.base_currency != perp.base_currency
            or spot.quote_currency != perp.quote_currency
            or perp.is_inverse
            or perp.multiplier.as_decimal() != 1
            or spot_step % perp_step != 0
            or str(spot.quote_currency) != "USDT"
        ):
            self._monitor_reason = "Incompatible instruments"
            self._publish_monitor()
            self.log.error(
                "Trading requires matching USDT linear assets and hedgeable quantity steps"
            )
            self.stop()
            return
        if self._config.recovery is not None:
            recovery = self._config.recovery
            self._monitor_started_ns = int(recovery["started_at_ms"]) * 1_000_000
            self._monitor_baseline = recovery["baseline"]
            self._monitor_sequence = recovery["monitor_sequence"]
            for name in (
                "spot_filled",
                "perp_filled",
                "spot_closed",
                "perp_closed",
                "spot_filled_notional",
            ):
                setattr(self, f"_{name}", Decimal(recovery[name]))
            self._restored_spot_fills = self._spot_filled
            self._restored_spot_closes = self._spot_closed
            self._exiting = recovery["exiting"]
            self._monitor_reason = "Restart awaiting account and order reconciliation"
        else:
            self._capture_monitor_baseline(spot)
        if self._exit_reader is not None:
            self._exit_reader.start()
        self._publish_monitor()
        self.clock.set_timer("basis-order-check", timedelta(seconds=1))
        self._order_check_started = True
        self.subscribe_funding_rates(
            self._config.perp_id, client_id=self._config.perp_client_id
        )

        self.subscribe_book_deltas(
            self._config.spot_id,
            BookType.L2_MBP,
            depth=self._config.book_depth,
            client_id=self._config.spot_client_id,
            managed=True,
        )
        self.subscribe_book_deltas(
            self._config.perp_id,
            BookType.L2_MBP,
            depth=self._config.book_depth,
            client_id=self._config.perp_client_id,
            managed=True,
        )

    def on_book_deltas(self, deltas: OrderBookDeltas) -> None:
        """Evaluate the spot maker price against the latest perpetual bid."""
        if deltas.instrument_id == self._config.spot_id:
            self._spot_book_update_ns = deltas.ts_init
        elif deltas.instrument_id == self._config.perp_id:
            self._perp_book_update_ns = deltas.ts_init
        else:
            return

        self._evaluate_entry()

    def on_time_event(self, event: Any) -> None:
        """Cancel resting entries when market data stops arriving."""
        if event.name == "basis-order-check":
            self._consume_exit_control()
            self._evaluate_entry()
            self._publish_monitor()

    def on_funding_rate(self, funding_rate: FundingRateUpdate) -> None:
        """Evaluate exits using the selected perpetual's latest funding rate."""
        if funding_rate.instrument_id != self._config.perp_id:
            return
        self._funding_rate = funding_rate.rate
        self._funding_update_ns = funding_rate.ts_init
        self._evaluate_entry()

    def _cancel_entry(self) -> None:
        # 如果当前有spot订单且没有取消挂起，则发起取消
        if self._spot_order is not None and not self._cancel_pending:
            if self.clock.timestamp_ns() < self._cancel_retry_after_ns:
                return
            self._cancel_pending = True
            self.log.info(
                f"Cancel remaining spot entry: {self._spot_order.client_order_id}"
            )
            self.cancel_order(
                self._spot_order.client_order_id,
                client_id=self._config.spot_client_id,
            )

    def _consume_exit_control(self) -> None:
        if self._exit_reader is None:
            return
        try:
            update = self._exit_reader.updates.get_nowait()
        except Empty:
            return
        if "error" in update:
            self._control_ready = False
            self._monitor_reason = "Exit configuration database unavailable"
            return
        settings = update["settings"]
        if (
            self._exit_settings is not None
            and settings["version"] < self._exit_settings["version"]
        ):
            self._control_ready = False
            self._monitor_reason = "Exit configuration version regressed"
            return
        self._exit_settings = settings
        self._control_ready = True
        if not self._recovery_pending and self._monitor_reason in (
            None,
            "Exit configuration database unavailable",
            "Exit configuration version regressed",
        ):
            self._monitor_reason = "Exit configuration loaded"
        if self._recovery_pending:
            try:
                self._reconcile_restart(update.get("account"))
            except (ValueError, KeyError, TypeError, ArithmeticError):
                self._monitor_reason = "Restart reconciliation data invalid"

    def _reconcile_restart(self, account: dict[str, Any] | None) -> None:
        if (
            account is None
            or not 0
            <= self.clock.timestamp_ns() // 1_000_000 - account["collected_at_ms"]
            <= 120_000
        ):
            return
        if not self._config.recovery["orders_terminal"]:
            return
        baseline = self._monitor_baseline
        if baseline.get("spot") is None or baseline.get("perp") is None:
            return
        legs = self._config.legs
        if legs is not None:
            if account.get("legs") != legs:
                return
            spot_account, perp_account = (
                account.get("spot_account"),
                account.get("perp_account"),
            )
            if spot_account is None or perp_account is None:
                return
            balances, positions = (
                spot_account.get("balances"),
                perp_account.get("positions"),
            )
            spot_orders, perp_orders = (
                spot_account.get("open_orders"),
                perp_account.get("open_orders"),
            )
            symbol = legs["perp"]["symbol"]
            if (
                spot_orders is None
                or perp_orders is None
                or any(
                    row.get("instrument_id") == str(self._config.spot_id)
                    for row in spot_orders
                )
                or any(
                    row.get("instrument_id") == str(self._config.perp_id)
                    for row in perp_orders
                )
            ):
                return
        else:
            # Legacy records were written only for the Gate/Binance pair
            balances, positions = (
                account.get("gate_balances"),
                account.get("binance_positions"),
            )
            spot_orders, perp_orders = (
                account.get("gate_open_orders"),
                account.get("binance_open_orders"),
            )
            if spot_orders is None or perp_orders is None:
                return
            symbol = str(self._config.perp_id).removesuffix("-PERP.BINANCE")
            if any(
                row.get("currency_pair") == f"{baseline['base_currency']}_USDT"
                for row in spot_orders
            ) or any(row.get("symbol") == symbol for row in perp_orders):
                return
        if balances is None or positions is None:
            return
        base = baseline["base_currency"]
        spot_quantity = sum(
            (
                Decimal(row["available"]) + Decimal(row["locked"])
                for row in balances
                if row["currency"] == base
            ),
            Decimal(0),
        )
        matched = [row for row in positions if row["symbol"] == symbol]
        if any(row.get("positionSide") != "BOTH" for row in matched):
            return
        perp_quantity = sum(
            (Decimal(row["positionAmt"]) for row in matched), Decimal(0)
        )
        cached_perp = sum(
            (
                position.quantity.as_decimal()
                * (-1 if position.side == PositionSide.SHORT else 1)
                for position in self.cache.positions_open(
                    instrument_id=self._config.perp_id
                )
            ),
            Decimal(0),
        )
        if cached_perp != perp_quantity:
            return
        remaining_spot = self._spot_filled - self._spot_closed
        remaining_perp = self._perp_filled - self._perp_closed
        if (
            spot_quantity != Decimal(baseline["spot"]) + remaining_spot
            or perp_quantity != Decimal(baseline["perp"]) - remaining_perp
            or remaining_spot != remaining_perp
        ):
            return
        self._recovery_pending = False
        self._monitor_reason = "Restart reconciled; execution resumed"

    def _exit_condition(self, basis: Decimal, now: int) -> bool:
        settings = self._exit_settings
        if settings is None:
            return False
        funding_fresh = (
            self._funding_update_ns is not None
            and 0 <= now - self._funding_update_ns <= self._config.max_book_age_ns
        )
        return condition_met(
            basis, settings["basis_percent"], settings["basis_operator"]
        ) or (
            funding_fresh
            and self._funding_rate is not None
            and condition_met(
                self._funding_rate * 100,
                settings["funding_percent"],
                settings["funding_operator"],
            )
        )

    def _evaluate_entry(self) -> None:
        if not self._control_ready or self._recovery_pending:
            self._cancel_entry()
            return
        if self._entry_paused:
            self._cancel_entry()
            return

        if (
            self._exiting
            and self._spot_order is None
            and self._spot_closed == self._spot_filled
            and self._perp_closed == self._perp_filled
            and all(order.is_closed for order in self._hedge_orders.values())
            and sum(
                (order.filled_qty.as_decimal() for order in self._spot_orders.values()),
                Decimal(0),
            )
            + self._restored_spot_fills
            + self._restored_spot_closes
            == self._spot_filled + self._spot_closed
        ):
            self._entry_paused = True
            self._monitor_completed = True
            self._monitor_reason = "Both legs closed"
            self.log.info("Spot and perpetual exit completed")
            self.stop()
            return

        # 如果两个交易所的订单簿更新都没有超过最大允许的时间间隔，则取消挂单
        now = self.clock.timestamp_ns()
        for updated_at in (self._spot_book_update_ns, self._perp_book_update_ns):
            if (
                updated_at is None
                or not 0 <= now - updated_at <= self._config.max_book_age_ns
            ):
                self._cancel_entry()
                return
        spot_top = self.cache.top_of_book(self._config.spot_id)
        perp_top = self.cache.top_of_book(self._config.perp_id)
        if spot_top is None or perp_top is None:
            self._cancel_entry()
            return
        if any(
            top[0].as_decimal() <= 0
            or top[1].as_decimal() <= 0
            or top[2].as_decimal() <= 0
            or top[3].as_decimal() <= 0
            for top in (spot_top, perp_top)
        ):
            self._cancel_entry()
            return
        negative_funding = (
            self._funding_rate is not None
            and self._funding_rate.is_finite()
            and self._funding_rate < 0
            and self._funding_update_ns is not None
            and 0 <= now - self._funding_update_ns <= self._config.max_book_age_ns
        )
        exit_basis = gross_basis_percent(
            spot_top[0].as_decimal(), perp_top[2].as_decimal()
        )
        trigger = (
            self._exit_condition(exit_basis, now)
            if self._exit_settings is not None
            else negative_funding and exit_basis <= self._config.exit_basis_percent
        )
        if not self._exiting and self._spot_filled > 0 and trigger:
            self._exiting = True
            self._monitor_reason = "Exit condition reached"
            self._publish_monitor()
            self.log.info(
                f"Begin maker exit: funding={self._funding_rate}, basis={exit_basis}%"
            )
        if (
            self._exiting
            and self._spot_order is not None
            and self._spot_order.client_order_id not in self._exit_spot_orders
        ):
            self._cancel_entry()
            return
        price = (
            self._spot_order.price
            if self._spot_order is not None
            and getattr(self._spot_order, "price", None) is not None
            else (
                spot_top[0]
                if self._exiting
                and self._exit_settings is not None
                and self._exit_settings["spot_mode"] == "TAKER"
                else spot_top[2]
                if self._exiting
                else spot_top[0]
            )
        )
        basis = gross_basis_percent(
            price.as_decimal(),
            (perp_top[2] if self._exiting else perp_top[0]).as_decimal(),
        )
        exit_allowed = (
            self._exit_condition(exit_basis, now)
            if self._exit_settings is not None
            else negative_funding and basis <= self._config.exit_basis_percent
        )
        if (self._exiting and not exit_allowed) or (
            not self._exiting and basis < self._config.alert_basis_percent
        ):
            self._cancel_entry()
            return

        # 如果当前有spot订单或者取消挂单正在进行，则不再发起新的挂单
        if self._spot_order is not None or self._cancel_pending:
            return
        # 如果当前有未完成的hedge订单，则不再发起新的挂单
        if any(not order.is_closed for order in self._hedge_orders.values()):
            return

        # 如果spot和perp的已成交数量不相等，则不再发起新的挂单
        if (
            self._spot_filled - self._spot_closed
            != self._perp_filled - self._perp_closed
        ):
            return

        # A preceding order callback may expose fills whose callbacks are still queued
        filled = sum(
            (
                order.filled_qty.as_decimal()
                for order_id, order in self._spot_orders.items()
                if order_id not in self._exit_spot_orders
            ),
            Decimal(0),
        )
        if filled + self._restored_spot_fills > self._spot_filled:
            return
        closed = sum(
            (
                self._spot_orders[order_id].filled_qty.as_decimal()
                for order_id in self._exit_spot_orders
            ),
            Decimal(0),
        )
        if closed + self._restored_spot_closes > self._spot_closed:
            return
        remaining_notional = self._config.target_notional - self._spot_filled_notional
        if not self._exiting and remaining_notional <= 0:
            return
        spot = self.cache.instrument(self._config.spot_id)
        step = spot.size_increment.as_decimal()
        remaining = (
            self._spot_filled - self._spot_closed
            if self._exiting
            else (remaining_notional // (price.as_decimal() * step)) * step
        )
        if remaining <= 0:
            self.log.warning(
                f"Remaining USDT notional {remaining_notional} is below one quantity step"
            )
            self._entry_paused = True
            self._monitor_reason = "Remaining budget below quantity step"
            self._publish_monitor()
            return
        if spot.min_quantity is not None and remaining < spot.min_quantity.as_decimal():
            self.log.warning(
                f"Remaining spot quantity {remaining} is below the order minimum"
            )
            self._entry_paused = True
            self._monitor_reason = "Remaining quantity below order minimum"
            self._publish_monitor()
            return
        if (
            spot.min_notional is not None
            and remaining * price.as_decimal() < spot.min_notional.as_decimal()
        ):
            self.log.warning(
                f"Remaining spot quantity {remaining} is below the notional minimum"
            )
            self._entry_paused = True
            self._monitor_reason = "Remaining notional below order minimum"
            self._publish_monitor()
            return
        if (
            self._exiting
            and self._exit_settings is not None
            and self._exit_settings["spot_mode"] == "TAKER"
        ):
            order = self.order_factory.market(
                self._config.spot_id,
                OrderSide.SELL,
                Quantity.from_str(f"{remaining:.{spot.size_precision}f}"),
            )
        else:
            order = self.order_factory.limit(
                self._config.spot_id,
                OrderSide.SELL if self._exiting else OrderSide.BUY,
                Quantity.from_str(f"{remaining:.{spot.size_precision}f}"),
                price,
                post_only=True,
            )
        self._spot_order = order
        self._spot_orders[order.client_order_id] = order
        if self._exiting:
            self._exit_spot_orders.add(order.client_order_id)
        self.log.info(
            f"Spot maker {'SELL' if self._exiting else 'BUY'} "
            f"quantity={remaining} price={price}, basis={basis:.4f}%"
        )
        self.submit_order(order, client_id=self._config.spot_client_id)

    def _refresh_order(self, order_id: ClientOrderId) -> None:
        # 如果订单ID不在spot订单或hedge订单中，则不刷新
        if order_id not in self._spot_orders and order_id not in self._hedge_orders:
            return
        cached = self.cache.order(order_id)
        if cached is None:
            return

        # Python order objects are snapshots; refresh only the order receiving an event
        if order_id in self._spot_orders:
            self._spot_orders[order_id] = cached
            if (
                self._spot_order is not None
                and self._spot_order.client_order_id == order_id
            ):
                self._spot_order = cached
        else:
            self._hedge_orders[order_id] = cached

    def on_order_submitted(self, event: Any) -> None:
        """Update the submitted order snapshot."""
        self._refresh_order(event.client_order_id)

    def on_order_accepted(self, event: Any) -> None:
        """Check whether the accepted entry still meets the basis requirement."""
        self._refresh_order(event.client_order_id)
        self._evaluate_entry()

    def on_order_updated(self, event: Any) -> None:
        """Reevaluate the entry after an order update."""
        self._refresh_order(event.client_order_id)
        self._evaluate_entry()

    def on_order_pending_cancel(self, event: Any) -> None:
        """Update the snapshot while cancellation is pending."""
        self._refresh_order(event.client_order_id)

    def on_order_filled(self, event: Any) -> None:
        """Hedge each spot fill immediately, including fills during cancellation."""
        order_id = event.client_order_id
        if order_id not in self._spot_orders and order_id not in self._hedge_orders:
            return
        self._refresh_order(order_id)
        fill_id = (order_id, event.trade_id)
        # 订单号去重，避免重复处理同一笔成交
        if fill_id in self._seen_fills:
            return
        self._seen_fills.add(fill_id)
        quantity = event.last_qty.as_decimal()
        if order_id in self._spot_orders:
            exiting = order_id in self._exit_spot_orders
            if exiting:
                self._spot_closed += quantity
            else:
                self._spot_filled += quantity
                self._spot_filled_notional += quantity * event.last_px.as_decimal()
            perp = self.cache.instrument(self._config.perp_id)
            if (
                exiting
                and self._exit_settings is not None
                and self._exit_settings["perp_mode"] == "MAKER"
            ):
                top = self.cache.top_of_book(self._config.perp_id)
                updated = self._perp_book_update_ns
                if (
                    top is None
                    or updated is None
                    or not 0
                    <= self.clock.timestamp_ns() - updated
                    <= self._config.max_book_age_ns
                ):
                    self._entry_paused = True
                    self._monitor_reason = "Exit maker hedge missing fresh book; exposure requires reconciliation"
                    self._publish_monitor()
                    return
                order = self.order_factory.limit(
                    self._config.perp_id,
                    OrderSide.BUY,
                    Quantity.from_str(f"{quantity:.{perp.size_precision}f}"),
                    top[0],
                    post_only=True,
                    reduce_only=True,
                )
            else:
                order = self.order_factory.market(
                    self._config.perp_id,
                    OrderSide.BUY if exiting else OrderSide.SELL,
                    Quantity.from_str(f"{quantity:.{perp.size_precision}f}"),
                    **({"reduce_only": True} if exiting else {}),
                )
            self._hedge_orders[order.client_order_id] = order
            if exiting:
                self._exit_hedge_orders.add(order.client_order_id)
            self.submit_order(order, client_id=self._config.perp_client_id)
            if (
                self._spot_order is not None
                and self._spot_order.client_order_id == order_id
                and self._spot_order.is_closed
            ):
                self._spot_order = None
                self._cancel_pending = False
                self._cancel_retry_after_ns = 0
        else:
            if order_id in self._exit_hedge_orders:
                self._perp_closed += quantity
            else:
                self._perp_filled += quantity
        self._evaluate_entry()
        self._publish_monitor()

    def on_order_canceled(self, event: Any) -> None:
        """Resume the remaining spot entry on cancellation, or pause on a canceled hedge."""
        order_id = event.client_order_id
        self._refresh_order(order_id)
        if order_id in self._hedge_orders:
            self._pause_entry_on_failure(event)
            return
        if (
            self._spot_order is not None
            and self._spot_order.client_order_id == order_id
        ):
            self._spot_order = None
            self._cancel_pending = False
            self._cancel_retry_after_ns = 0
            self._evaluate_entry()

    def on_order_expired(self, event: Any) -> None:
        """Handle an expired entry or incomplete hedge."""
        order_id = event.client_order_id
        self._refresh_order(order_id)
        if order_id in self._hedge_orders:
            self._pause_entry_on_failure(event)
            return
        if (
            self._spot_order is not None
            and self._spot_order.client_order_id == order_id
        ):
            self._spot_order = None
            self._cancel_pending = False
            self._cancel_retry_after_ns = 0
            self._evaluate_entry()

    def on_order_rejected(self, event: Any) -> None:
        """Pause new entries after a rejected order."""
        self._refresh_order(event.client_order_id)
        self._pause_entry_on_failure(event)

    def on_order_denied(self, event: Any) -> None:
        """Pause new entries after a locally denied order."""
        self._refresh_order(event.client_order_id)
        self._pause_entry_on_failure(event)

    def _pause_entry_on_failure(self, event: Any) -> None:
        order_id = event.client_order_id
        if order_id not in self._spot_orders and order_id not in self._hedge_orders:
            return
        self._entry_paused = True
        self._monitor_reason = f"Order {order_id} failed or terminated"
        self._publish_monitor()
        self.log.error(
            f"Order {order_id} failed or terminated; new entries paused, "
            f"unhedged base quantity="
            f"{self._spot_filled - self._spot_closed - self._perp_filled + self._perp_closed}"
        )
        if (
            self._spot_order is not None
            and self._spot_order.client_order_id == order_id
        ):
            self._spot_order = None
            self._cancel_pending = False
        self._cancel_entry()

    def on_order_cancel_rejected(self, event: Any) -> None:
        """Allow another cancellation attempt without releasing the resting order."""
        self._refresh_order(event.client_order_id)
        if (
            self._spot_order is not None
            and event.client_order_id == self._spot_order.client_order_id
        ):
            self._cancel_pending = False
            # Avoid a rejection callback causing an immediate cancellation retry loop
            self._cancel_retry_after_ns = self.clock.timestamp_ns() + 1_000_000_000
            self.log.warning(f"Spot cancellation rejected: {event.client_order_id}")
            self._evaluate_entry()

    def on_stop(self) -> None:
        """Cancel the spot entry and release the order book subscriptions."""
        self._entry_paused = True
        if self._exit_reader is not None:
            self._exit_reader.stop()
        self._monitor_stopped_ns = self.clock.timestamp_ns()
        if self._monitor_reason is None:
            self._monitor_reason = "Strategy stopped"
        self._publish_monitor()
        self._cancel_entry()
        if self._order_check_started:
            self.clock.cancel_timer("basis-order-check")
        self.unsubscribe_book_deltas(
            self._config.spot_id, client_id=self._config.spot_client_id
        )
        self.unsubscribe_book_deltas(
            self._config.perp_id, client_id=self._config.perp_client_id
        )
        self.unsubscribe_funding_rates(
            self._config.perp_id, client_id=self._config.perp_client_id
        )
