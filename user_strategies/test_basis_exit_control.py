import json
from decimal import Decimal
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest
from nautilus_trader.model import OrderSide, PositionSide, Price, Quantity

from user_strategies.basis_exit_control import (
    allocate_strategy_id,
    condition_met,
    execution_lease,
    exit_settings,
    recovery_candidate,
)
from user_strategies.test_basis_watch import (
    opened_strategy,
    spot_fill,
    trading_strategy,
    update_funding,
)


def test_strategy_id_allocation_commits_before_returning_and_uses_shared_counter() -> (
    None
):
    db = Mock()
    db.execute.return_value.fetchone.side_effect = [{"number": 42}, {"number": 43}]
    with patch("user_strategies.basis_exit_control.connection") as connect:
        connect.return_value.__enter__.return_value = db
        assert allocate_strategy_id("gate", "binance", "BTCUSDT") == (
            "BASIS-GATE-BINANCE-BTCUSDT-42"
        )
        assert allocate_strategy_id("kraken", "bybit", "ETHUSDT") == (
            "BASIS-KRAKEN-BYBIT-ETHUSDT-43"
        )
        assert connect.call_args_list == [
            call(read_only=False),
            call(read_only=False),
        ]
        assert connect.return_value.__exit__.call_count == 2
    for invocation in db.execute.call_args_list:
        assert "ON CONFLICT (id) DO UPDATE" in invocation.args[0]
        assert invocation.args[1] == ("basis:strategy-sequence:v1",)


def test_strategy_id_is_not_returned_when_counter_commit_fails() -> None:
    with patch("user_strategies.basis_exit_control.connection") as connect:
        connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {
            "number": 42
        }
        connect.return_value.__exit__.side_effect = RuntimeError("commit failed")
        with pytest.raises(RuntimeError, match="commit failed"):
            allocate_strategy_id("gate", "binance", "BTCUSDT")


def test_default_percentage_means_negative_two_percent() -> None:
    settings = exit_settings({})
    assert settings["funding_percent"] == "-2"
    assert not condition_met(Decimal("-0.019") * 100, "-2", "le")
    assert condition_met(Decimal("-0.02") * 100, "-2", "le")


@pytest.mark.parametrize(
    "value",
    [
        {"funding_percent": "NaN"},
        {"funding_percent": 2.0},
        {"funding_percent": None, "basis_percent": None},
        {"perp_mode": "LIMIT"},
        {"version": True},
    ],
)
def test_invalid_exit_settings_are_rejected(value: dict) -> None:
    with pytest.raises((ValueError, TypeError)):
        exit_settings(value)


@pytest.mark.parametrize(
    "funding,basis_threshold,expected",
    [("-0.02", None, True), ("-0.019", None, False), ("0.001", "1", True)],
)
def test_funding_or_basis_triggers_exit(
    funding: str, basis_threshold: str | None, expected: bool
) -> None:
    strategy = opened_strategy()
    strategy._exit_settings = exit_settings(
        {"basis_percent": basis_threshold, "basis_operator": "ge"}
    )
    update_funding(strategy, funding)
    assert strategy._exiting is expected


def test_exit_maker_hedges_only_actual_fill_and_is_reduce_only() -> None:
    strategy = opened_strategy()
    strategy._exit_settings = exit_settings({"perp_mode": "MAKER"})
    update_funding(strategy, "-0.02")
    strategy.order_factory.limit.reset_mock()
    event = spot_fill(strategy, "0.5", "one", "101")
    strategy.on_order_filled(event)
    strategy.order_factory.limit.assert_called_once_with(
        "perp",
        OrderSide.BUY,
        Quantity.from_str("0.500"),
        Price.from_str("100.5"),
        post_only=True,
        reduce_only=True,
    )
    strategy.order_factory.market.assert_not_called()


def test_stale_maker_book_does_not_create_an_unpriced_hedge() -> None:
    strategy = opened_strategy()
    strategy._exit_settings = exit_settings({"perp_mode": "MAKER"})
    update_funding(strategy, "-0.02")
    strategy._perp_book_update_ns = 80
    spot_fill(strategy, "0.5", "one", "101")
    assert strategy._entry_paused
    assert strategy._spot_closed == Decimal("0.5")
    assert strategy._perp_closed == 0
    strategy.order_factory.market.assert_not_called()


def test_taker_spot_exit_remains_valid_during_order_callbacks() -> None:
    strategy = opened_strategy()
    strategy._exit_settings = exit_settings({"spot_mode": "TAKER"})
    update_funding(strategy, "-0.02")
    strategy.order_factory.market.assert_called_once_with(
        "spot", OrderSide.SELL, Quantity.from_str("2.000")
    )
    strategy._evaluate_entry()
    assert strategy.order_factory.market.call_count == 1


def test_database_failure_blocks_new_orders_and_cancels_resting_spot() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    strategy._exit_reader = SimpleNamespace(updates=Queue())
    strategy._exit_reader.updates.put({"error": "OperationalError"})
    strategy.on_time_event(SimpleNamespace(name="basis-order-check"))
    assert not strategy._control_ready
    strategy.cancel_order.assert_called_once()
    assert strategy.submit_order.call_count == 1


def test_setting_update_is_acknowledged_and_applies_to_only_one_instance() -> None:
    strategy, other = opened_strategy(), opened_strategy()
    strategy._exit_reader = SimpleNamespace(updates=Queue())
    strategy._exit_reader.updates.put(
        {"settings": exit_settings({"version": 3, "funding_percent": "-1.9"})}
    )
    strategy._consume_exit_control()
    strategy._publish_monitor()
    snapshot = json.loads(strategy.cache.add.call_args_list[-1].args[1])
    assert snapshot["exit_settings"]["version"] == 3
    assert other._exit_settings is None


@pytest.mark.parametrize(
    "problem",
    [
        None,
        "quantity",
        "open_order",
        "missing_orders",
        "partial_hedge",
        "stale",
        "nonterminal",
    ],
)
def test_restart_only_resumes_after_full_reconciliation(problem: str | None) -> None:
    strategy = opened_strategy()
    strategy._config.perp_id = "BTCUSDT-PERP.BINANCE"
    strategy._config.recovery = {"orders_terminal": problem != "nonterminal"}
    strategy._monitor_baseline = {"base_currency": "BTC", "spot": "0", "perp": "0"}
    strategy._recovery_pending = True
    strategy.cache.positions_open.return_value = [
        SimpleNamespace(quantity=Quantity.from_str("2"), side=PositionSide.SHORT)
    ]
    account = {
        "collected_at_ms": 0,
        "gate_balances": [{"currency": "BTC", "available": "2", "locked": "0"}],
        "binance_positions": [
            {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-2"}
        ],
        "gate_open_orders": [],
        "binance_open_orders": [],
    }
    if problem == "quantity":
        account["gate_balances"][0]["available"] = "1"
    if problem == "open_order":
        account["binance_open_orders"] = [{"symbol": "BTCUSDT"}]
    if problem == "missing_orders":
        account.pop("gate_open_orders")
    if problem == "partial_hedge":
        strategy._perp_filled = Decimal(1)
    if problem == "stale":
        account["collected_at_ms"] = -200000
    strategy._reconcile_restart(account)
    assert strategy._recovery_pending is (problem is not None)


def test_recovery_ledger_deduplicates_fills_and_checks_full_order_quantities() -> None:
    instance = {
        "trader_id": "DYNAMIC-BASIS-001",
        "strategy_id": "one",
        "spot_id": "spot",
        "perp_id": "perp",
        "state": "holding",
        "spot_remaining": "2",
        "perp_remaining": "2",
        "baseline": {"spot": "0", "perp": "0"},
        "started_at_ms": 10,
    }
    events = [
        {
            "client_order_id": "s",
            "kind": "OrderInitialized",
            "instrument_id": "spot",
            "order_side": "BUY",
            "last_qty": None,
        },
        {
            "client_order_id": "s",
            "kind": "OrderFilled",
            "instrument_id": "spot",
            "order_side": "BUY",
            "last_qty": "2",
            "last_px": "100",
            "trade_id": "a",
        },
        {
            "client_order_id": "p",
            "kind": "OrderFilled",
            "instrument_id": "perp",
            "order_side": "SELL",
            "last_qty": "2",
            "last_px": "101",
            "trade_id": "b",
        },
    ]
    events.append(events[1])
    database = Mock()
    database.execute.return_value.fetchall.side_effect = [
        [{"snapshot": instance}],
        events,
        [],
        [
            {"client_order_id": "s", "quantity": "2"},
            {"client_order_id": "p", "quantity": "2"},
        ],
    ]
    with patch("user_strategies.basis_exit_control.connection") as connect:
        connect.return_value.__enter__.return_value = database
        recovered = recovery_candidate()["recovery"]
    query = database.execute.call_args_list[0]
    assert "FROM basis_strategy WHERE trader_id = %s" in query.args[0]
    assert "spot_remaining <> 0" in query.args[0]
    assert query.args[1] == ("DYNAMIC-BASIS-001",)
    assert recovered["spot_filled"] == "2"
    assert recovered["spot_filled_notional"] == "200"
    assert recovered["orders_terminal"]


def test_no_recovery_candidate_does_not_read_order_history() -> None:
    with patch("user_strategies.basis_exit_control.connection") as connect:
        db = connect.return_value.__enter__.return_value
        db.execute.return_value.fetchall.return_value = []
        assert recovery_candidate() is None
    db.execute.assert_called_once()


def test_multiple_recovery_candidates_do_not_silently_pick_one() -> None:
    with patch("user_strategies.basis_exit_control.connection") as connect:
        db = connect.return_value.__enter__.return_value
        db.execute.return_value.fetchall.return_value = [
            {"snapshot": {}},
            {"snapshot": {}},
        ]
        with pytest.raises(ValueError, match="Multiple retained"):
            recovery_candidate()
    db.execute.assert_called_once()


@pytest.mark.parametrize("acquired", [True, False])
def test_execution_lease_rejects_duplicate_runner_and_releases_lock(
    acquired: bool,
) -> None:
    db = Mock()
    db.execute.return_value.fetchone.return_value = {"acquired": acquired}
    db.__enter__ = Mock(return_value=db)
    db.__exit__ = Mock(return_value=False)
    with patch("user_strategies.basis_exit_control.connection", return_value=db):
        if acquired:
            with execution_lease():
                db.commit.assert_called_once()
            assert "pg_advisory_unlock" in db.execute.call_args.args[0]
        else:
            with (
                pytest.raises(ValueError, match="Another basis execution"),
                execution_lease(),
            ):
                pytest.fail("Duplicate runner entered execution")
            assert db.execute.call_count == 1


def test_new_condition_cancels_unfilled_exit_but_filled_spot_still_hedges() -> None:
    strategy = opened_strategy()
    strategy._exit_settings = exit_settings({})
    update_funding(strategy, "-0.02")
    strategy._exit_reader = SimpleNamespace(updates=Queue())
    strategy._exit_reader.updates.put(
        {"settings": exit_settings({"version": 1, "funding_percent": "-3"})}
    )
    strategy.on_time_event(SimpleNamespace(name="basis-order-check"))
    strategy.cancel_order.assert_called_once()
    spot_fill(strategy, "0.5", "late-fill", "101")
    strategy.order_factory.market.assert_called_once_with(
        "perp",
        OrderSide.BUY,
        Quantity.from_str("0.500"),
        reduce_only=True,
    )


def test_regressed_version_blocks_orders_without_replacing_applied_settings() -> None:
    strategy = opened_strategy()
    strategy._exit_settings = exit_settings({"version": 3})
    strategy._exit_reader = SimpleNamespace(updates=Queue())
    strategy._exit_reader.updates.put({"settings": exit_settings({"version": 2})})
    strategy.on_time_event(SimpleNamespace(name="basis-order-check"))
    assert not strategy._control_ready
    assert strategy._exit_settings["version"] == 3
    strategy.order_factory.market.assert_not_called()


def test_stale_funding_cannot_trigger_but_fresh_basis_can() -> None:
    strategy = opened_strategy()
    strategy._exit_settings = exit_settings({})
    strategy._funding_rate = Decimal("-0.03")
    strategy._funding_update_ns = 80
    assert not strategy._exit_condition(Decimal(2), 100)
    strategy._exit_settings = exit_settings(
        {"basis_percent": "1", "basis_operator": "ge"}
    )
    assert strategy._exit_condition(Decimal(2), 100)


def test_negative_exit_basis_triggers_only_at_or_below_threshold() -> None:
    settings = exit_settings({"funding_percent": None, "basis_percent": "-0.2"})
    strategy = opened_strategy()
    strategy._exit_settings = settings
    assert settings["basis_operator"] == "le"
    assert not strategy._exit_condition(Decimal("0.2"), 100)
    assert not strategy._exit_condition(Decimal("-0.1"), 100)
    assert strategy._exit_condition(Decimal("-0.2"), 100)
    assert strategy._exit_condition(Decimal("-0.3"), 100)
