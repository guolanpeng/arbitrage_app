"""Verify configurable routes without contacting exchanges or placing orders."""

import json
from contextlib import nullcontext
from copy import deepcopy
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from nautilus_trader.model import PositionSide, Quantity

from user_strategies.basis_account_monitor import collect_once
from user_strategies.basis_adapters import adapter, instance_legs, selected_legs
from user_strategies.dynamic_basis_watch import BasisCandidateController
from user_strategies.test_basis_watch import opened_strategy


class OtherSpotAdapter:
    venue = "kraken"
    market = "spot"
    client_id = "KRAKEN_SPOT"
    account_id = "KRAKEN-001"

    def leg(self, symbol):
        return {
            "instrument_id": "BTC_USDT.KRAKEN",
            "native_symbol": "BTC/USDT",
            "api_secret": "must-not-persist",
        }

    def add_data(self, builder, leg):
        return builder.add_data_client(leg["client_id"], "data-factory", leg)

    def add_exec(self, builder, leg):
        return builder.add_exec_client(leg["client_id"], "exec-factory", leg)

    def account(self, reader, leg):
        return {
            "balances": [{"currency": "BTC", "available": "2", "locked": "0"}],
            "open_orders": [],
        }


class OtherPerpAdapter(OtherSpotAdapter):
    venue = "bybit"
    market = "perp"
    client_id = "BYBIT_PERP"
    account_id = "BYBIT-001"

    def leg(self, symbol):
        return {"instrument_id": "BTC_USDT-LINEAR.BYBIT", "native_symbol": "BTCUSDT"}

    def account(self, reader, leg):
        return {
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "-2",
                    "markPrice": "100",
                }
            ],
            "open_orders": [],
        }

    def funding_history(self, reader, leg, start, end):
        return []


@pytest.fixture
def other_legs(monkeypatch):
    monkeypatch.setenv(
        "BASIS_ADAPTERS",
        json.dumps(
            {
                "other_spot": "user_strategies.test_basis_adapters:OtherSpotAdapter",
                "other_perp": "user_strategies.test_basis_adapters:OtherPerpAdapter",
            }
        ),
    )
    monkeypatch.setenv("BASIS_SPOT_PROFILE", "other_spot")
    monkeypatch.setenv("BASIS_PERP_PROFILE", "other_perp")
    return selected_legs("BTCUSDT")


def retained(legs):
    return {
        "trader_id": "DYNAMIC-BASIS-001",
        "strategy_id": "BASIS-original-001",
        "spot_id": legs["spot"]["instrument_id"],
        "perp_id": legs["perp"]["instrument_id"],
        "legs": legs,
        "state": "stopped",
        "started_at_ms": 1000,
        "target_notional": "200",
        "entry_basis_percent": "0.5",
        "recovery": {"orders_terminal": True},
    }


def test_recovery_preserves_full_foreign_instrument_ids_and_ignores_new_selection(
    other_legs, monkeypatch
):
    instance = retained(other_legs)
    monkeypatch.setenv("BASIS_SPOT_PROFILE", "gate_spot")
    monkeypatch.setenv("BASIS_PERP_PROFILE", "binance_perp")
    assert instance_legs(instance) == other_legs
    assert "api_secret" not in other_legs["spot"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "OTHER-001"),
        ("client_id", "OTHER"),
        ("instrument_id", "BTCUSDT.GATE"),
        ("profile", "unknown"),
    ],
)
def test_changed_account_or_unknown_route_is_rejected(other_legs, field, value):
    instance = retained(deepcopy(other_legs))
    instance["legs"]["spot"][field] = value
    with pytest.raises(ValueError):
        instance_legs(instance)


def test_legacy_foreign_instance_is_not_guessed():
    with pytest.raises(ValueError, match="cannot guess"):
        instance_legs({"spot_id": "BTCUSDT.KRAKEN", "perp_id": "BTCUSDT.BYBIT"})


def test_wrong_market_adapter_is_rejected(other_legs):
    with pytest.raises(ValueError, match="does not support"):
        adapter("other_spot", "perp")


def test_controller_resumes_original_clients_and_id(other_legs):
    controller = SimpleNamespace(
        _config=SimpleNamespace(recovery_instance=retained(other_legs)),
        clock=Mock(),
        _poll_redis=Mock(),
        create_strategy_from_config=Mock(),
    )
    with patch("user_strategies.dynamic_basis_watch.Thread"):
        BasisCandidateController.on_start(controller)
    config = controller.create_strategy_from_config.call_args.args[0].config
    assert config["spot_client_id"] == "KRAKEN_SPOT"
    assert config["perp_client_id"] == "BYBIT_PERP"
    assert config["spot_id"] == "BTC_USDT.KRAKEN"
    assert config["strategy_id"] == "BASIS-original-001"


def test_node_uses_retained_adapters_when_candidates_empty(other_legs, monkeypatch):
    from user_strategies import dynamic_basis_watch

    builder = Mock()
    for name in (
        "with_cache_database_factory",
        "with_exec_engine_config",
        "add_data_client",
        "add_exec_client",
        "with_controller",
    ):
        getattr(builder, name).return_value = builder
    monkeypatch.setattr(
        dynamic_basis_watch,
        "LiveNode",
        SimpleNamespace(builder=Mock(return_value=builder)),
    )
    monkeypatch.setattr(dynamic_basis_watch, "_read_candidates", Mock(return_value=[]))
    with (
        patch(
            "user_strategies.basis_exit_control.recovery_candidate",
            return_value=retained(other_legs),
        ),
        patch(
            "user_strategies.basis_exit_control.execution_lease",
            return_value=nullcontext(),
        ),
        patch("redis.Redis.from_url"),
        patch("user_strategies.basis_exit_control.allocate_strategy_id") as allocate,
        patch("user_strategies.basis_exit_control.initialize_strategy_schema"),
    ):
        dynamic_basis_watch.main()
    allocate.assert_not_called()
    assert [call.args[0] for call in builder.add_exec_client.call_args_list] == [
        "KRAKEN_SPOT",
        "BYBIT_PERP",
    ]
    assert builder.with_controller.call_args.args[0].config["legs"] == other_legs
    dynamic_basis_watch._read_candidates.assert_not_called()


@pytest.mark.parametrize("problem", [None, "identity", "open_order"])
def test_foreign_account_snapshot_controls_restart(other_legs, problem):
    strategy = opened_strategy()
    strategy._config.legs = other_legs
    strategy._config.spot_id = other_legs["spot"]["instrument_id"]
    strategy._config.perp_id = other_legs["perp"]["instrument_id"]
    strategy._config.recovery = {"orders_terminal": True}
    strategy._monitor_baseline = {"base_currency": "BTC", "spot": "0", "perp": "0"}
    strategy._recovery_pending = True
    strategy.cache.positions_open.return_value = [
        SimpleNamespace(quantity=Quantity.from_str("2"), side=PositionSide.SHORT)
    ]
    account = {
        "legs": deepcopy(other_legs),
        "collected_at_ms": 0,
        "spot_account": OtherSpotAdapter().account(None, None),
        "perp_account": OtherPerpAdapter().account(None, None),
    }
    if problem == "identity":
        account["legs"]["spot"]["account_id"] = "WRONG-001"
    if problem == "open_order":
        account["spot_account"]["open_orders"] = [
            {"instrument_id": strategy._config.spot_id}
        ]
    strategy._reconcile_restart(account)
    assert strategy._recovery_pending is (problem is not None)


def test_collector_routes_foreign_legs_without_gate_or_binance_calls(other_legs):
    reader, redis = Mock(), Mock()
    redis.mget.return_value = [None, None]
    with (
        patch("user_strategies.basis_account_monitor.database_connection") as connect,
        patch(
            "user_strategies.basis_account_monitor.load_records",
            return_value={},
        ),
        patch("user_strategies.basis_account_monitor.write_record") as write,
        patch(
            "user_strategies.basis_account_monitor.time.time_ns",
            return_value=2000000000,
        ),
    ):
        connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [
            {"snapshot": retained(other_legs)}
        ]
        assert collect_once(reader, redis) == 1
    reader.gate_balances.assert_not_called()
    reader.binance_get.assert_not_called()
    redis.mget.assert_called_once_with(
        ["market:v1:kraken:spot", "market:v1:bybit:funding"]
    )
    snapshot = write.call_args_list[0].args[2]
    assert snapshot["legs"] == other_legs
    assert snapshot["spot_account"]["balances"][0]["available"] == "2"
    assert snapshot["income_complete"]


def test_new_candidate_uses_configured_nondefault_pair(other_legs):
    from decimal import Decimal

    from user_strategies.dynamic_basis_watch import FundingCandidate

    controller = SimpleNamespace(
        _config=SimpleNamespace(
            legs=other_legs,
            symbol="BTCUSDT",
            target_notional=Decimal(200),
            new_strategy_id="BASIS-KRAKEN-BYBIT-BTCUSDT-43",
        ),
        _updates=Queue(),
        _strategy_id=None,
        log=Mock(),
        create_strategy_from_config=Mock(),
    )
    controller._updates.put(
        [
            FundingCandidate(
                "kraken", "bybit", "BTCUSDT", Decimal("0.001"), Decimal(8), Decimal(0)
            )
        ]
    )
    BasisCandidateController.on_time_event(controller, None)
    config = controller.create_strategy_from_config.call_args.args[0].config
    assert config["legs"] == other_legs
    assert config["spot_id"] == "BTC_USDT.KRAKEN"
    assert config["perp_id"] == "BTC_USDT-LINEAR.BYBIT"
    assert config["strategy_id"] == "BASIS-KRAKEN-BYBIT-BTCUSDT-43"
