import json
from contextlib import nullcontext
from decimal import Decimal
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from nautilus_trader.model import StrategyId

from user_strategies.basis_watch import BasisWatchConfig, BasisWatchStrategy
from user_strategies.dynamic_basis_watch import (
    CONTROLLER_SNAPSHOT_KEY,
    BasisCandidateController,
    FundingCandidate,
    _rank_candidates,
    controller_snapshot,
    find_candidates,
)


@pytest.fixture(autouse=True)
def no_database_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("BASIS_ADAPTERS", "BASIS_SPOT_PROFILE", "BASIS_PERP_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "user_strategies.basis_exit_control.recovery_candidate", Mock(return_value=None)
    )
    monkeypatch.setattr(
        "user_strategies.basis_exit_control.allocate_strategy_id",
        Mock(return_value="BASIS-GATE-BINANCE-BTCUSDT-42"),
    )
    monkeypatch.setattr(
        "user_strategies.basis_exit_control.execution_lease",
        Mock(return_value=nullcontext()),
    )
    monkeypatch.setattr(
        "user_strategies.basis_exit_control.initialize_strategy_schema", Mock()
    )


@pytest.mark.parametrize("target_notional", [None, Decimal(100)])
def test_dynamic_node_configures_postgres_without_loading_old_orders(
    monkeypatch: pytest.MonkeyPatch,
    target_notional: Decimal | None,
) -> None:
    from user_strategies import dynamic_basis_watch

    builder = Mock()
    builder.with_cache_database_factory.return_value = builder
    builder.with_exec_engine_config.return_value = builder
    builder.add_data_client.return_value = builder
    builder.add_exec_client.return_value = builder
    builder.with_controller.return_value = builder
    monkeypatch.setattr(
        dynamic_basis_watch,
        "LiveNode",
        SimpleNamespace(builder=Mock(return_value=builder)),
    )

    monkeypatch.setenv("GATE_API_KEY", "test-key")
    monkeypatch.setenv("GATE_API_SECRET", "test-secret")
    monkeypatch.setattr(
        dynamic_basis_watch,
        "_read_candidates",
        Mock(
            return_value=[
                FundingCandidate(
                    "gate",
                    "binance",
                    "BTCUSDT",
                    Decimal("0.001"),
                    Decimal(8),
                    Decimal(0),
                )
            ]
        ),
    )
    with patch("redis.Redis.from_url"):
        if target_notional is None:
            dynamic_basis_watch.main()
        else:
            dynamic_basis_watch.main(target_notional)

    builder.with_cache_database_factory.assert_called_once()
    assert isinstance(
        builder.with_cache_database_factory.call_args.args[0],
        dynamic_basis_watch.PostgresCacheConfig,
    )
    builder.with_exec_engine_config.assert_called_once()
    assert builder.with_exec_engine_config.call_args.args[0].load_cache is False
    assert [call.args[0] for call in builder.add_data_client.call_args_list] == [
        "BINANCE_FUTURES",
        "GATE_SPOT",
    ]
    assert [call.args[0] for call in builder.add_exec_client.call_args_list] == [
        "GATE_SPOT",
        "BINANCE_FUTURES",
    ]
    binance_config = builder.add_exec_client.call_args_list[1].args[2]
    assert binance_config.unified_account is True
    assert binance_config.use_ws_trading is False
    assert binance_config.futures_leverages == {"BTCUSDT": 2}
    assert binance_config.instrument_provider.load_ids == ["BTCUSDT-PERP.BINANCE"]
    controller_config = builder.with_controller.call_args.args[0].config
    assert controller_config["target_notional"] == (
        "250" if target_notional is None else str(target_notional)
    )
    assert controller_config["symbol"] == "BTCUSDT"
    assert controller_config["new_strategy_id"] == "BASIS-GATE-BINANCE-BTCUSDT-42"
    builder.build.return_value.run.assert_called_once()
    builder.build.return_value.dispose.assert_called_once()


@pytest.mark.parametrize("target", ["0", "-1", "NaN", "Infinity"])
def test_invalid_target_does_not_connect_to_redis_or_build_live_node(
    target: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from user_strategies import dynamic_basis_watch

    builder = Mock()
    monkeypatch.setattr(dynamic_basis_watch, "LiveNode", builder)
    with patch("redis.Redis.from_url") as connect:
        with pytest.raises(ValueError, match="Target USDT"):
            dynamic_basis_watch.main(Decimal(target))
        connect.assert_not_called()
    builder.builder.assert_not_called()


def test_no_fresh_candidate_does_not_build_live_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from user_strategies import dynamic_basis_watch

    builder = Mock()
    monkeypatch.setattr(dynamic_basis_watch, "LiveNode", builder)
    monkeypatch.setattr(dynamic_basis_watch, "_read_candidates", Mock(return_value=[]))
    with (
        patch("redis.Redis.from_url"),
        pytest.raises(SystemExit, match="No fresh funding candidate"),
    ):
        dynamic_basis_watch.main(Decimal(100))
    builder.builder.assert_not_called()


def _snapshots(now_ms: int, venue: str = "binance") -> tuple[dict, dict, dict]:
    spot = {
        "schema_version": 1,
        "venue": venue,
        "market": "spot",
        "collected_at_ms": now_ms,
        "instruments": {
            "BTCUSDT": {"base": "BTC", "quote": "USDT", "ask": "100.10"},
            "EDGEUSDT": {"base": "EDGE", "quote": "USDT", "ask": "0.10"},
        },
    }
    perp = {
        "schema_version": 1,
        "venue": venue,
        "market": "perp",
        "collected_at_ms": now_ms,
        "instruments": {
            "BTCUSDT": {"base": "BTC", "quote": "USDT", "bid": "101.10"},
            "EDGEUSDT": {"base": "OTHER", "quote": "USDT", "bid": "0.50"},
        },
    }
    funding = {
        "schema_version": 1,
        "venue": venue,
        "market": "funding",
        "collected_at_ms": now_ms,
        "instruments": {
            symbol: {"rate": "0.0005", "interval_hours": "8"}
            for symbol in spot["instruments"]
        },
    }
    return spot, perp, funding


@pytest.mark.parametrize("venue", ["binance", "gate"])
def test_candidate_filter_uses_fresh_executable_prices_and_matching_assets(
    venue: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("user_strategies.dynamic_basis_watch.VENUE_PAIR_BLACKLIST", [])
    spot, perp, funding = _snapshots(1_000_000, venue)

    result = find_candidates(spot, perp, funding, 1_000_500, venue, venue)

    assert [candidate.symbol for candidate in result] == ["BTCUSDT"]
    assert result[0].basis_percent > Decimal("0.9")


@pytest.mark.parametrize("spot_venue", ["binance", "gate"])
@pytest.mark.parametrize("perp_venue", ["binance", "gate"])
def test_candidate_filter_excludes_default_blacklisted_pair(
    spot_venue: str,
    perp_venue: str,
) -> None:
    spot, _, _ = _snapshots(1_000_000, spot_venue)
    _, perp, funding = _snapshots(1_000_000, perp_venue)

    result = find_candidates(spot, perp, funding, 1_000_500, spot_venue, perp_venue)

    assert [candidate.symbol for candidate in result] == (
        [] if perp_venue == "gate" else ["BTCUSDT"]
    )


@pytest.mark.parametrize(
    ("blacklist", "expected_count"),
    [([], 1), ([("gate", "binance")], 0), ([("binance", "gate")], 1)],
)
def test_candidate_filter_uses_configured_blacklist_direction(
    blacklist: list[tuple[str, str]],
    expected_count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "user_strategies.dynamic_basis_watch.VENUE_PAIR_BLACKLIST", blacklist
    )
    spot, _, _ = _snapshots(1_000_000, "gate")
    _, perp, funding = _snapshots(1_000_000, "binance")

    result = find_candidates(spot, perp, funding, 1_000_500, "gate", "binance")

    assert len(result) == expected_count


@pytest.mark.parametrize("spot_venue", ["binance", "gate"])
@pytest.mark.parametrize("perp_venue", ["binance", "gate"])
def test_candidate_filter_preserves_chinese_symbols(
    spot_venue: str,
    perp_venue: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("user_strategies.dynamic_basis_watch.VENUE_PAIR_BLACKLIST", [])
    spot, _, _ = _snapshots(1_000_000, spot_venue)
    _, perp, funding = _snapshots(1_000_000, perp_venue)
    for snapshot in (spot, perp, funding):
        instrument = snapshot["instruments"].pop("BTCUSDT")
        if snapshot["market"] != "funding":
            instrument["base"] = "龙虾"
        snapshot["instruments"]["龙虾USDT"] = instrument

    result = find_candidates(spot, perp, funding, 1_000_500, spot_venue, perp_venue)

    assert [candidate.symbol for candidate in result] == ["龙虾USDT"]


@pytest.mark.parametrize(
    ("turnover", "expected"),
    [
        ("123456.78", Decimal("123456.78")),
        ("NaN", Decimal(0)),
        ("-1", Decimal(0)),
        (None, Decimal(0)),
    ],
)
def test_candidate_filter_reads_perpetual_quote_turnover(
    turnover: str | None,
    expected: Decimal,
) -> None:
    spot, perp, funding = _snapshots(1_000_000)
    perp["instruments"]["BTCUSDT"]["quote_turnover_24h"] = turnover

    result = find_candidates(spot, perp, funding, 1_000_500, "binance", "binance")

    assert result[0].perp_quote_turnover_24h == expected


def test_candidate_ranking_combines_funding_and_perpetual_turnover() -> None:
    candidates = [
        FundingCandidate(
            "binance",
            "gate",
            symbol,
            Decimal(rate),
            Decimal(8),
            Decimal(0),
            Decimal(turnover),
        )
        for symbol, rate, turnover in (
            ("AUSDT", "0.0008", "100"),
            ("BUSDT", "0.0007", "400"),
            ("CUSDT", "0.0006", "300"),
            ("DUSDT", "0.0005", "200"),
        )
    ]

    _rank_candidates(candidates)

    assert [candidate.symbol for candidate in candidates] == [
        "BUSDT",
        "CUSDT",
        "AUSDT",
        "DUSDT",
    ]


def test_candidate_ranking_puts_missing_turnover_last_at_equal_funding() -> None:
    candidates = [
        FundingCandidate(
            "binance", "gate", "AUSDT", Decimal("0.0005"), Decimal(8), Decimal(0)
        ),
        FundingCandidate(
            "binance",
            "gate",
            "BUSDT",
            Decimal("0.0005"),
            Decimal(8),
            Decimal(0),
            Decimal(100),
        ),
    ]

    _rank_candidates(candidates)

    assert [candidate.symbol for candidate in candidates] == ["BUSDT", "AUSDT"]


@pytest.mark.parametrize("venue", ["binance", "gate"])
def test_candidate_filter_rejects_stale_snapshot(venue: str) -> None:
    spot, perp, funding = _snapshots(1_000_000, venue)

    assert find_candidates(spot, perp, funding, 1_060_001, venue, venue) == []


def test_candidate_filter_rejects_mixed_venues() -> None:
    spot, _, funding = _snapshots(1_000_000, "binance")
    _, perp, _ = _snapshots(1_000_000, "gate")

    for venue in ("binance", "gate"):
        assert find_candidates(spot, perp, funding, 1_000_500, venue, venue) == []


@pytest.mark.parametrize("rate", ["0", "-0.0001", "NaN", "Infinity", "invalid", None])
def test_candidate_filter_rejects_nonpositive_or_invalid_funding(
    rate: str | None,
) -> None:
    spot, perp, funding = _snapshots(1_000_000)
    funding["instruments"]["BTCUSDT"]["rate"] = rate

    assert find_candidates(spot, perp, funding, 1_000_500, "binance", "binance") == []


@pytest.mark.parametrize("interval", [None, "0", "-1", "NaN", "Infinity", "invalid"])
def test_candidate_filter_rejects_unknown_or_invalid_interval(
    interval: str | None,
) -> None:
    spot, perp, funding = _snapshots(1_000_000)
    funding["instruments"]["BTCUSDT"]["interval_hours"] = interval

    assert find_candidates(spot, perp, funding, 1_000_500, "binance", "binance") == []


def test_positive_funding_qualifies_with_negative_basis() -> None:
    spot, perp, funding = _snapshots(1_000_000)
    spot["instruments"]["BTCUSDT"]["ask"] = "100"
    perp["instruments"]["BTCUSDT"]["bid"] = "99.01"

    result = find_candidates(spot, perp, funding, 1_000_500, "binance", "binance")

    assert len(result) == 1
    assert result[0].basis_percent < 0
    assert result[0].rate_8h == Decimal("0.0005")


@pytest.mark.parametrize(
    ("rate", "expected_count"),
    [("0.4999", 0), ("0.5", 1)],
)
def test_candidate_filter_applies_annualized_funding_threshold(
    rate: str,
    expected_count: int,
) -> None:
    spot, perp, funding = _snapshots(1_000_000)
    funding["instruments"]["BTCUSDT"] = {
        "rate": rate,
        "interval_hours": "8760",
    }

    result = find_candidates(spot, perp, funding, 1_000_500, "binance", "binance")

    assert len(result) == expected_count


@pytest.mark.parametrize(
    ("perp_bid", "expected_count"),
    [("98.99", 0), ("99", 0), ("99.01", 1), ("101", 1), ("101.01", 0)],
)
def test_candidate_filter_requires_basis_within_range(
    perp_bid: str,
    expected_count: int,
) -> None:
    spot, perp, funding = _snapshots(1_000_000)
    spot["instruments"]["BTCUSDT"]["ask"] = "100"
    perp["instruments"]["BTCUSDT"]["bid"] = perp_bid

    result = find_candidates(spot, perp, funding, 1_000_500, "binance", "binance")

    assert len(result) == expected_count


def test_candidate_filter_rejects_stale_funding_with_fresh_prices() -> None:
    spot, perp, funding = _snapshots(1_000_000)
    funding["collected_at_ms"] = 939_999

    assert find_candidates(spot, perp, funding, 1_000_000, "binance", "binance") == []


@pytest.mark.parametrize(
    "spot_venue,perp_venue", [("binance", "gate"), ("gate", "binance")]
)
def test_cross_venue_pair_uses_perpetual_funding_and_selected_spot_ask(
    spot_venue: str,
    perp_venue: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("user_strategies.dynamic_basis_watch.VENUE_PAIR_BLACKLIST", [])
    spot, _, _ = _snapshots(1_000_000, spot_venue)
    _, perp, funding = _snapshots(1_000_000, perp_venue)
    spot["instruments"]["BTCUSDT"]["ask"] = "100"
    perp["instruments"]["BTCUSDT"]["bid"] = "99.01"
    funding["instruments"]["BTCUSDT"] = {"rate": "0.0003", "interval_hours": "4"}

    result = find_candidates(spot, perp, funding, 1_000_500, spot_venue, perp_venue)

    assert result == [
        FundingCandidate(
            spot_venue,
            perp_venue,
            "BTCUSDT",
            Decimal("0.0003"),
            Decimal(4),
            Decimal("-0.99"),
        ),
    ]
    assert result[0].rate_8h == Decimal("0.0006")

    funding["venue"] = spot_venue
    assert find_candidates(spot, perp, funding, 1_000_500, spot_venue, perp_venue) == []


def test_controller_creates_only_one_gate_spot_binance_perpetual_strategy() -> None:
    controller = SimpleNamespace(
        _updates=Queue(),
        log=Mock(),
        create_strategy_from_config=Mock(),
        _strategy_id=None,
        _config=SimpleNamespace(
            symbol="BTCUSDT",
            target_notional=Decimal(100),
            new_strategy_id="BASIS-GATE-BINANCE-BTCUSDT-42",
        ),
    )
    candidates = [
        FundingCandidate(
            spot, perp, "BTCUSDT", Decimal("0.0001"), Decimal(8), Decimal(-1)
        )
        for spot in ("binance", "gate")
        for perp in ("binance", "gate")
    ]
    controller._updates.put(candidates)
    BasisCandidateController.on_time_event(controller, None)
    assert controller.log.info.call_count == 4
    assert all("FUNDING" in call.args[0] for call in controller.log.info.call_args_list)
    controller.create_strategy_from_config.assert_called_once()
    config = controller.create_strategy_from_config.call_args.args[0].config
    assert config["spot_id"] == "BTCUSDT.GATE"
    assert config["perp_id"] == "BTCUSDT-PERP.BINANCE"
    assert config["spot_client_id"] == "GATE_SPOT"
    assert config["perp_client_id"] == "BINANCE_FUTURES"
    assert config["target_notional"] == "100"
    assert config["alert_basis_percent"] == "0.5"
    assert config["exit_basis_percent"] == "-0.2"
    assert config["strategy_id"] == "BASIS-GATE-BINANCE-BTCUSDT-42"
    assert "instance_id" not in config
    assert str(StrategyId.from_str(config["strategy_id"])) == config["strategy_id"]
    controller._updates.put(candidates)
    BasisCandidateController.on_time_event(controller, None)
    controller.create_strategy_from_config.assert_called_once()


def test_controller_does_not_create_a_different_symbol() -> None:
    controller = SimpleNamespace(
        _updates=Queue(),
        _strategy_id=None,
        _config=SimpleNamespace(symbol="BTCUSDT", target_notional=Decimal(100)),
        log=Mock(),
        create_strategy_from_config=Mock(),
    )
    controller._updates.put(
        [
            FundingCandidate(
                "gate", "binance", "ETHUSDT", Decimal("0.001"), Decimal(8), Decimal(0)
            )
        ],
    )

    BasisCandidateController.on_time_event(controller, None)

    controller.create_strategy_from_config.assert_not_called()


@pytest.mark.parametrize("missing_binance", [False, True])
@pytest.mark.parametrize("snapshot_write_fails", [False, True])
def test_redis_poll_reads_only_gate_spot_and_binance_perpetual_candidates(
    missing_binance: bool,
    snapshot_write_fails: bool,
) -> None:
    from redis import RedisError

    binance = _snapshots(1_000_000)
    gate = _snapshots(1_000_000, "gate")
    gate[2]["instruments"]["BTCUSDT"]["interval_hours"] = "1"
    gate[1]["instruments"]["BTCUSDT"]["bid"] = "99.11"
    snapshots = [json.dumps(snapshot) for snapshot in (gate[0], binance[1], binance[2])]
    if missing_binance:
        snapshots[1:] = [None, None]
    client = Mock()
    client.mget.return_value = snapshots
    if snapshot_write_fails:
        client.set.side_effect = RedisError("unavailable")
    controller = SimpleNamespace(
        _config=SimpleNamespace(
            redis_url="redis://localhost:6379/0",
            poll_seconds=5,
            symbol="BTCUSDT",
            target_notional=Decimal("250.123456789"),
        ),
        _strategy_id=None,
        _stop_event=Mock(),
        _updates=Queue(maxsize=1),
    )
    controller._stop_event.is_set.side_effect = [False, True]

    with (
        patch("redis.Redis.from_url", return_value=client),
        patch(
            "user_strategies.dynamic_basis_watch.time.time_ns",
            return_value=1_000_500_000_000,
        ),
    ):
        BasisCandidateController._poll_redis(controller)

    client.mget.assert_called_once_with(
        [
            "market:v1:gate:spot",
            "market:v1:binance:perp",
            "market:v1:binance:funding",
        ],
    )
    result = controller._updates.get_nowait()
    assert [(c.spot_venue, c.perp_venue, c.symbol) for c in result] == (
        []
        if missing_binance
        else [
            ("gate", "binance", "BTCUSDT"),
        ]
    )
    client.close.assert_called_once()
    assert client.set.call_args.args[0] == CONTROLLER_SNAPSHOT_KEY
    stored = json.loads(client.set.call_args.args[1])
    assert stored["target_notional"] == "250.123456789"
    assert stored["strategy_id"] is None
    assert stored["max_strategies"] == 1


def test_controller_snapshot_reports_creation_not_strategy_liveness() -> None:
    snapshot = controller_snapshot(
        SimpleNamespace(symbol="BTCUSDT", target_notional=Decimal(100), poll_seconds=5),
        StrategyId.from_str("BASIS-GATE-BINANCE-001"),
        "scan failed",
    )
    assert snapshot["strategy_id"] == "BASIS-GATE-BINANCE-001"
    assert snapshot["scan_failed"] is True
    assert "is_running" not in snapshot
    assert "redis_url" not in snapshot


def test_importable_strategy_parameters_become_domain_types() -> None:
    config = BasisWatchConfig(
        spot_id="BTCUSDT.BINANCE",
        perp_id="BTCUSDT-PERP.BINANCE",
        spot_client_id="BINANCE_SPOT",
        perp_client_id="BINANCE_FUTURES",
        alert_basis_percent="0.5",
        target_notional="100",
        strategy_id=StrategyId.from_str("BASIS-BTCUSDT-001"),
    )

    assert str(config.spot_id) == "BTCUSDT.BINANCE"
    assert config.alert_basis_percent == Decimal("0.5")
    assert config.strategy_id == StrategyId.from_str("BASIS-BTCUSDT-001")
    assert not hasattr(config, "instance_id")


def test_strategy_config_preserves_readable_identity() -> None:
    config = BasisWatchConfig(
        spot_id="BTCUSDT.GATE",
        perp_id="BTCUSDT-PERP.BINANCE",
        spot_client_id="GATE_SPOT",
        perp_client_id="BINANCE_FUTURES",
        alert_basis_percent="0.5",
        target_notional="100",
        strategy_id=StrategyId.from_str("BASIS-GATE-BINANCE-BTCUSDT-42"),
    )
    assert str(config.strategy_id) == "BASIS-GATE-BINANCE-BTCUSDT-42"
    assert (
        str(BasisWatchStrategy(config).strategy_id) == "BASIS-GATE-BINANCE-BTCUSDT-42"
    )
    assert not hasattr(config, "instance_id")
