import json
from decimal import Decimal
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, call

import pytest
from nautilus_trader.model import BookType, OrderSide, Price, Quantity, StrategyId

from user_strategies.basis_watch import (
    BasisWatchConfig,
    BasisWatchStrategy,
    gross_basis_percent,
)


def test_gross_basis_uses_spot_ask_and_perpetual_bid() -> None:
    assert gross_basis_percent(Decimal("100.10"), Decimal("100.30")) == Decimal(
        "0.1998001998001998001998002000",
    )


def test_gross_basis_can_be_negative() -> None:
    assert gross_basis_percent(Decimal(100), Decimal(99)) == Decimal(-1)


def test_monitoring_failure_does_not_change_entry_execution() -> None:
    strategy = trading_strategy()
    strategy.cache.add.side_effect = RuntimeError("writer unavailable")
    strategy.on_time_event(SimpleNamespace(name="basis-order-check"))
    strategy.submit_order.assert_called_once()
    assert not strategy._entry_paused
    strategy.log.warning.assert_called_once()


def test_monitor_records_pause_reason_and_deduplicates_state_transitions() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    strategy.on_order_rejected(SimpleNamespace(client_order_id="spot-0"))
    first = json.loads(strategy.cache.add.call_args_list[0].args[1])
    assert "instance_id" not in first
    assert first["strategy_id"] == "BASIS-TEST-001"
    assert (
        strategy.cache.add.call_args_list[0].args[0]
        == "basis:instance:v1:BASIS-TEST-001"
    )
    assert first["state"] == "paused"
    assert "spot-0" in first["reason"]
    strategy.cache.add.reset_mock()
    strategy._publish_monitor()
    assert strategy.cache.add.call_count == 1


def test_completed_monitor_remains_closed_after_stop_hook() -> None:
    strategy = trading_strategy()
    strategy._monitor_completed = True
    strategy._order_check_started = False
    strategy.unsubscribe_book_deltas = Mock()
    BasisWatchStrategy.on_stop(strategy)
    snapshot = json.loads(strategy.cache.add.call_args_list[0].args[1])
    assert snapshot["state"] == "closed"
    assert snapshot["stopped_at_ms"] is not None


def trading_strategy() -> SimpleNamespace:
    strategy = SimpleNamespace(
        _config=SimpleNamespace(
            spot_id="spot",
            perp_id="perp",
            spot_client_id="spot-client",
            perp_client_id="perp-client",
            target_notional=Decimal(200),
            alert_basis_percent=Decimal("0.5"),
            max_book_age_ns=10,
            exit_basis_percent=Decimal("-0.2"),
            recovery=None,
            legs=None,
        ),
        _spot_book_update_ns=100,
        _perp_book_update_ns=100,
        _spot_order=None,
        _spot_orders={},
        _hedge_orders={},
        _seen_fills=set(),
        _spot_filled=Decimal(0),
        _spot_filled_notional=Decimal(0),
        _perp_filled=Decimal(0),
        _cancel_pending=False,
        _cancel_retry_after_ns=0,
        _entry_paused=False,
        _funding_rate=None,
        _funding_update_ns=None,
        _exiting=False,
        _exit_settings=None,
        _exit_reader=None,
        _control_ready=True,
        _recovery_pending=False,
        _restored_spot_fills=Decimal(0),
        _restored_spot_closes=Decimal(0),
        _exit_spot_orders=set(),
        _exit_hedge_orders=set(),
        _spot_closed=Decimal(0),
        _perp_closed=Decimal(0),
        _monitor_started_ns=100,
        _monitor_stopped_ns=None,
        _monitor_completed=False,
        _monitor_reason=None,
        _monitor_baseline={"spot": "0", "perp": "0"},
        _monitor_last_state=None,
        _monitor_sequence=0,
        trader_id="DYNAMIC-BASIS-001",
        strategy_id="BASIS-TEST-001",
        _capture_monitor_baseline=Mock(),
        subscribe_funding_rates=Mock(),
        unsubscribe_funding_rates=Mock(),
        stop=Mock(),
        cache=Mock(),
        clock=Mock(),
        log=Mock(),
        order_factory=Mock(),
        submit_order=Mock(),
        cancel_order=Mock(),
    )
    for name in (
        "_evaluate_entry",
        "_consume_exit_control",
        "_reconcile_restart",
        "_exit_condition",
        "_publish_monitor",
        "_cancel_entry",
        "_pause_entry_on_failure",
        "_refresh_order",
        "on_order_submitted",
        "on_order_accepted",
        "on_order_updated",
        "on_order_pending_cancel",
        "on_order_filled",
        "on_order_canceled",
        "on_order_cancel_rejected",
        "on_order_rejected",
        "on_order_denied",
        "on_order_expired",
        "on_book_deltas",
        "on_time_event",
        "on_funding_rate",
    ):
        setattr(strategy, name, MethodType(getattr(BasisWatchStrategy, name), strategy))
    strategy.clock.timestamp_ns.return_value = 101
    strategy.spot_top = (
        Price.from_str("100"),
        Quantity.from_str("10"),
        Price.from_str("101"),
        Quantity.from_str("10"),
    )
    strategy.perp_top = (
        Price.from_str("100.5"),
        Quantity.from_str("10"),
        Price.from_str("102"),
        Quantity.from_str("10"),
    )
    strategy.cache.top_of_book.side_effect = lambda instrument_id: (
        strategy.spot_top if instrument_id == "spot" else strategy.perp_top
    )
    strategy.cache.order.side_effect = lambda order_id: (
        strategy._spot_orders.get(order_id) or strategy._hedge_orders.get(order_id)
    )
    strategy.cache.instrument.return_value = SimpleNamespace(
        size_precision=3,
        size_increment=Quantity.from_str("0.001"),
        min_quantity=Quantity.from_str("0.001"),
        min_notional=None,
    )
    strategy.order_factory.limit.side_effect = lambda *args, **kwargs: SimpleNamespace(
        client_order_id=f"spot-{len(strategy._spot_orders)}",
        price=args[3],
        quantity=args[2],
        filled_qty=Quantity.from_str("0"),
        is_closed=False,
    )
    strategy.order_factory.market.side_effect = lambda *args, **kwargs: SimpleNamespace(
        client_order_id=f"hedge-{len(strategy._hedge_orders)}",
        quantity=args[2],
        filled_qty=Quantity.from_str("0"),
        is_closed=False,
    )
    return strategy


def spot_fill(
    strategy: SimpleNamespace, quantity: str, trade_id: str, fill_price: str = "100"
) -> SimpleNamespace:
    order = strategy._spot_order
    order.filled_qty = Quantity.from_str(
        str(order.filled_qty.as_decimal() + Decimal(quantity))
    )
    event = SimpleNamespace(
        client_order_id=order.client_order_id,
        trade_id=trade_id,
        last_qty=Quantity.from_str(quantity),
        last_px=Price.from_str(fill_price),
    )
    strategy.on_order_filled(event)
    return event


def test_maker_entry_uses_spot_bid_perpetual_bid_and_creation_target() -> None:
    strategy = trading_strategy()
    strategy.on_book_deltas(SimpleNamespace(instrument_id="perp", ts_init=100))
    args = strategy.order_factory.limit.call_args
    assert args.args[1:] == (
        OrderSide.BUY,
        Quantity.from_str("2.000"),
        Price.from_str("100"),
    )
    assert args.kwargs == {"post_only": True}
    strategy.submit_order.assert_called_once_with(
        strategy._spot_order, client_id="spot-client"
    )


def test_partial_fill_cancel_race_and_resume_only_remaining_target() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    event = spot_fill(strategy, "0.5", "first")
    strategy.on_order_filled(event)
    assert strategy._spot_filled == Decimal("0.5")
    assert strategy._spot_filled_notional == Decimal(50)
    assert strategy.order_factory.market.call_count == 1
    assert strategy.order_factory.market.call_args.args[1:] == (
        OrderSide.SELL,
        Quantity.from_str("0.500"),
    )
    strategy.perp_top = (Price.from_str("100.4"), *strategy.perp_top[1:])
    strategy._evaluate_entry()
    strategy._evaluate_entry()
    strategy.cancel_order.assert_called_once_with("spot-0", client_id="spot-client")
    spot_fill(strategy, "0.25", "during-cancel")
    assert strategy.order_factory.market.call_count == 2
    strategy.perp_top = (Price.from_str("100.5"), *strategy.perp_top[1:])
    strategy._evaluate_entry()
    assert strategy.order_factory.limit.call_count == 1
    strategy._spot_order.is_closed = True
    strategy.on_order_canceled(SimpleNamespace(client_order_id="spot-0"))
    strategy._evaluate_entry()
    assert strategy.order_factory.limit.call_count == 1
    for hedge in strategy._hedge_orders.values():
        hedge.is_closed = True
        strategy.on_order_filled(
            SimpleNamespace(
                client_order_id=hedge.client_order_id,
                trade_id="hedged",
                last_qty=hedge.quantity,
                last_px=Price.from_str("100"),
            )
        )
    strategy._evaluate_entry()
    assert strategy.order_factory.limit.call_args.args[2] == Quantity.from_str("1.250")
    assert strategy._perp_filled == Decimal("0.750")


def test_resting_entry_basis_uses_its_price_instead_of_new_spot_bid() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    strategy.spot_top = (Price.from_str("99"), *strategy.spot_top[1:])
    strategy.perp_top = (Price.from_str("100.4"), *strategy.perp_top[1:])
    strategy._evaluate_entry()
    strategy.cancel_order.assert_called_once()


def test_stale_books_cancel_entry_even_without_new_quotes() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    strategy.clock.timestamp_ns.return_value = 111
    strategy.on_time_event(SimpleNamespace(name="basis-order-check"))
    strategy.cancel_order.assert_called_once()


def test_cancel_rejection_does_not_allow_duplicate_entry() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    strategy.perp_top = (Price.from_str("100.4"), *strategy.perp_top[1:])
    strategy._evaluate_entry()
    strategy.on_order_cancel_rejected(SimpleNamespace(client_order_id="spot-0"))
    strategy._evaluate_entry()
    assert strategy.cancel_order.call_count == 1
    strategy.clock.timestamp_ns.return_value = 1_000_000_101
    strategy._spot_book_update_ns = 1_000_000_100
    strategy._perp_book_update_ns = 1_000_000_100
    strategy.on_time_event(SimpleNamespace(name="basis-order-check"))
    assert strategy.cancel_order.call_count == 2
    assert strategy.order_factory.limit.call_count == 1


def test_hedge_rejection_pauses_entries_and_cancels_remaining_spot() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    spot_fill(strategy, "0.5", "first")
    strategy.on_order_rejected(SimpleNamespace(client_order_id="hedge-0"))
    assert strategy._entry_paused
    strategy.cancel_order.assert_called_once()
    assert "unhedged base quantity=0.5" in strategy.log.error.call_args.args[0]
    strategy.on_order_canceled(SimpleNamespace(client_order_id="spot-0"))
    strategy._evaluate_entry()
    assert strategy.order_factory.limit.call_count == 1


def test_full_target_fill_never_starts_another_entry() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    strategy._spot_order.is_closed = True
    spot_fill(strategy, "2", "full")
    assert strategy._spot_order is None
    strategy._hedge_orders["hedge-0"].is_closed = True
    strategy.on_order_filled(
        SimpleNamespace(
            client_order_id="hedge-0",
            trade_id="hedged",
            last_qty=Quantity.from_str("2"),
            last_px=Price.from_str("100.5"),
        )
    )
    strategy._evaluate_entry()
    assert strategy.order_factory.limit.call_count == 1


@pytest.mark.parametrize(
    "target, step, expected",
    [
        ("200", "0.001", "2"),
        ("200.99", "0.005", "2.005"),
        ("100.49", "0.005", "1"),
        ("100.50", "0.005", "1.005"),
    ],
)
def test_usdt_target_is_converted_at_bid_and_rounded_down_to_quantity_step(
    target: str,
    step: str,
    expected: str,
) -> None:
    strategy = trading_strategy()
    strategy._config.target_notional = Decimal(target)
    strategy.cache.instrument.return_value.size_increment = Quantity.from_str(step)

    strategy._evaluate_entry()

    order = strategy._spot_order
    assert order.quantity.as_decimal() == Decimal(expected)
    assert order.quantity.as_decimal() % Decimal(step) == 0
    assert order.quantity.as_decimal() * order.price.as_decimal() <= Decimal(target)


def test_reentry_converts_remaining_usdt_using_actual_fills_and_new_bid() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    event = spot_fill(strategy, "0.5", "first", fill_price="98")
    strategy.on_order_filled(event)
    assert strategy._spot_filled_notional == Decimal(49)
    strategy.perp_top = (Price.from_str("100.4"), *strategy.perp_top[1:])
    strategy._evaluate_entry()
    strategy._spot_order.is_closed = True
    strategy.on_order_canceled(SimpleNamespace(client_order_id="spot-0"))
    strategy._hedge_orders["hedge-0"].is_closed = True
    strategy.on_order_filled(
        SimpleNamespace(
            client_order_id="hedge-0",
            trade_id="hedged",
            last_qty=Quantity.from_str("0.5"),
            last_px=Price.from_str("101"),
        )
    )
    assert strategy._spot_filled_notional == Decimal(49)
    strategy.spot_top = (Price.from_str("80"), *strategy.spot_top[1:])
    strategy.perp_top = (Price.from_str("80.4"), *strategy.perp_top[1:])

    strategy._evaluate_entry()

    assert strategy._spot_order.quantity.as_decimal() == Decimal("1.887")
    assert strategy._spot_order.price.as_decimal() == Decimal(80)
    assert strategy._spot_filled_notional + Decimal("1.887") * 80 <= Decimal(200)


@pytest.mark.parametrize("minimum_notional", [None, "1"])
def test_remaining_usdt_below_order_minimum_never_submits(
    minimum_notional: str | None,
) -> None:
    strategy = trading_strategy()
    strategy._config.target_notional = Decimal(
        "0.09" if minimum_notional is None else "0.99"
    )
    instrument = strategy.cache.instrument.return_value
    if minimum_notional is not None:
        instrument.min_notional = SimpleNamespace(
            as_decimal=lambda: Decimal(minimum_notional)
        )

    strategy._evaluate_entry()

    strategy.submit_order.assert_not_called()
    assert strategy._entry_paused


@pytest.mark.parametrize(
    "target, threshold", [("0", "0.5"), ("NaN", "0.5"), ("1", None)]
)
def test_trading_config_rejects_invalid_target_or_missing_threshold(
    target: str,
    threshold: str | None,
) -> None:
    with pytest.raises(ValueError):
        BasisWatchConfig(
            spot_id="BTCUSDT.BINANCE",
            perp_id="BTCUSDT-PERP.BINANCE",
            spot_client_id="BINANCE_SPOT",
            perp_client_id="BINANCE_FUTURES",
            alert_basis_percent=threshold,
            target_notional=target,
            strategy_id=StrategyId.from_str("BASIS-001"),
        )


def test_hedge_fill_callback_refreshes_snapshot_before_resuming() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    spot_fill(strategy, "0.5", "first")
    original_hedge = strategy._hedge_orders["hedge-0"]
    cached_hedge = SimpleNamespace(**vars(original_hedge))
    cached_hedge.is_closed = True
    strategy.cache.order.side_effect = lambda order_id: (
        cached_hedge if order_id == "hedge-0" else strategy._spot_orders.get(order_id)
    )
    strategy.on_order_filled(
        SimpleNamespace(
            client_order_id="hedge-0",
            trade_id="done",
            last_qty=Quantity.from_str("0.5"),
            last_px=Price.from_str("100"),
        )
    )
    strategy.on_order_canceled(SimpleNamespace(client_order_id="spot-0"))
    strategy.cache.order.reset_mock()
    strategy._evaluate_entry()
    strategy.cache.order.assert_not_called()
    assert not original_hedge.is_closed
    assert strategy.order_factory.limit.call_count == 2
    assert strategy._spot_order.quantity == Quantity.from_str("1.500")


@pytest.mark.parametrize(
    "callback",
    [
        "on_order_submitted",
        "on_order_accepted",
        "on_order_updated",
        "on_order_pending_cancel",
    ],
)
@pytest.mark.parametrize("hedge", [False, True])
def test_order_callback_refreshes_only_its_order_snapshot(
    callback: str, hedge: bool
) -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    if hedge:
        spot_fill(strategy, "0.5", "first")
    orders = strategy._hedge_orders if hedge else strategy._spot_orders
    order_id = "hedge-0" if hedge else "spot-0"
    original = orders[order_id]
    cached = SimpleNamespace(**vars(original))
    cached.status = callback
    strategy.cache.order.return_value = cached
    strategy.cache.order.side_effect = None
    strategy.cache.order.reset_mock()

    getattr(strategy, callback)(SimpleNamespace(client_order_id=order_id))

    strategy.cache.order.assert_called_once_with(order_id)
    assert orders[order_id] is cached
    if not hedge:
        assert strategy._spot_order is cached
    strategy.cache.order.reset_mock()
    strategy._evaluate_entry()
    strategy.cache.order.assert_not_called()


def test_spot_fill_callback_refreshes_partial_and_full_fill_snapshots() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    original = strategy._spot_order
    cached = SimpleNamespace(**vars(original))
    cached.filled_qty = Quantity.from_str("0.5")
    strategy.cache.order.side_effect = lambda order_id: (
        cached if order_id == "spot-0" else strategy._hedge_orders.get(order_id)
    )
    strategy.on_order_filled(
        SimpleNamespace(
            client_order_id="spot-0",
            trade_id="partial",
            last_qty=Quantity.from_str("0.5"),
            last_px=Price.from_str("100"),
        )
    )
    assert original.filled_qty == Quantity.from_str("0")
    assert strategy._spot_order is cached
    assert strategy._spot_orders["spot-0"].filled_qty == Quantity.from_str("0.5")

    full = SimpleNamespace(**vars(cached))
    full.filled_qty = Quantity.from_str("2")
    full.is_closed = True
    cached = full
    strategy.on_order_filled(
        SimpleNamespace(
            client_order_id="spot-0",
            trade_id="full",
            last_qty=Quantity.from_str("1.5"),
            last_px=Price.from_str("100"),
        )
    )
    assert strategy._spot_orders["spot-0"] is full
    assert strategy._spot_order is None
    assert strategy._spot_filled == Decimal(2)
    assert strategy.order_factory.market.call_count == 2


@pytest.mark.parametrize(
    "callback, paused",
    [
        ("on_order_canceled", False),
        ("on_order_expired", False),
        ("on_order_rejected", True),
        ("on_order_denied", True),
    ],
)
def test_terminal_callback_refreshes_order_and_releases_current_entry(
    callback: str,
    paused: bool,
) -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    cached = SimpleNamespace(**vars(strategy._spot_order))
    cached.is_closed = True
    cached.filled_qty = Quantity.from_str("0.5")
    strategy.cache.order.side_effect = None
    strategy.cache.order.return_value = cached
    strategy._cancel_pending = True

    getattr(strategy, callback)(SimpleNamespace(client_order_id="spot-0"))

    assert strategy._spot_orders["spot-0"] is cached
    assert strategy._spot_order is None
    assert not strategy._cancel_pending
    assert strategy._entry_paused is paused
    strategy._evaluate_entry()
    assert strategy.order_factory.limit.call_count == 1


def test_cancel_rejection_refreshes_snapshot_and_retains_current_entry() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    cached = SimpleNamespace(**vars(strategy._spot_order))
    strategy.cache.order.side_effect = None
    strategy.cache.order.return_value = cached
    strategy._cancel_pending = True

    strategy.on_order_cancel_rejected(SimpleNamespace(client_order_id="spot-0"))

    assert strategy._spot_order is cached
    assert not strategy._cancel_pending
    strategy._evaluate_entry()
    assert strategy.order_factory.limit.call_count == 1


def test_cache_fill_ahead_of_callback_is_subtracted_from_remaining_target() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    strategy._spot_order.filled_qty = Quantity.from_str("0.5")
    strategy.on_order_canceled(SimpleNamespace(client_order_id="spot-0"))
    strategy._evaluate_entry()
    assert strategy.order_factory.limit.call_count == 1
    strategy.on_order_filled(
        SimpleNamespace(
            client_order_id="spot-0",
            trade_id="late",
            last_qty=Quantity.from_str("0.5"),
            last_px=Price.from_str("100"),
        )
    )
    strategy._hedge_orders["hedge-0"].is_closed = True
    strategy.on_order_filled(
        SimpleNamespace(
            client_order_id="hedge-0",
            trade_id="hedged",
            last_qty=Quantity.from_str("0.5"),
            last_px=Price.from_str("100"),
        )
    )
    strategy._evaluate_entry()
    assert strategy._spot_order.quantity == Quantity.from_str("1.500")


@pytest.mark.parametrize("bad_step", [False, True])
def test_trading_start_validates_quantity_steps_and_starts_staleness_timer(
    bad_step: bool,
) -> None:
    strategy = trading_strategy()
    strategy._config.book_depth = 20
    strategy._config.target_notional = Decimal(200)
    strategy.stop = Mock()
    strategy.subscribe_book_deltas = Mock()
    spot = SimpleNamespace(
        base_currency="BTC",
        quote_currency="USDT",
        size_increment=Quantity.from_str("0.001"),
    )
    perp = SimpleNamespace(
        base_currency="BTC",
        quote_currency="USDT",
        is_inverse=False,
        multiplier=Quantity.from_str("1"),
        size_increment=Quantity.from_str("0.01" if bad_step else "0.001"),
    )
    strategy.cache.instrument.side_effect = lambda instrument_id: (
        spot if instrument_id == "spot" else perp
    )
    BasisWatchStrategy.on_start(strategy)
    if bad_step:
        strategy.stop.assert_called_once()
        strategy.clock.set_timer.assert_not_called()
        strategy.subscribe_book_deltas.assert_not_called()
    else:
        strategy.stop.assert_not_called()
        strategy.subscribe_funding_rates.assert_called_once_with(
            "perp", client_id="perp-client"
        )
        assert strategy._order_check_started
        strategy.clock.set_timer.assert_called_once()
        assert strategy.subscribe_book_deltas.call_args_list == [
            call(
                "spot", BookType.L2_MBP, depth=20, client_id="spot-client", managed=True
            ),
            call(
                "perp", BookType.L2_MBP, depth=20, client_id="perp-client", managed=True
            ),
        ]


def test_stop_cancels_resting_entry_timer_and_book_subscriptions() -> None:
    strategy = trading_strategy()
    strategy._order_check_started = True
    strategy.unsubscribe_book_deltas = Mock()
    strategy._evaluate_entry()

    BasisWatchStrategy.on_stop(strategy)

    assert strategy._entry_paused
    strategy.cancel_order.assert_called_once_with("spot-0", client_id="spot-client")
    strategy.clock.cancel_timer.assert_called_once_with("basis-order-check")
    assert strategy.unsubscribe_book_deltas.call_args_list == [
        call("spot", client_id="spot-client"),
        call("perp", client_id="perp-client"),
    ]
    strategy.unsubscribe_funding_rates.assert_called_once_with(
        "perp", client_id="perp-client"
    )


def test_trading_below_threshold_uses_bid_and_never_enters() -> None:
    strategy = trading_strategy()
    strategy.perp_top = (Price.from_str("100.4"), *strategy.perp_top[1:])
    strategy._evaluate_entry()
    strategy.submit_order.assert_not_called()


@pytest.mark.parametrize("callback", ["on_order_canceled", "on_order_expired"])
def test_spot_terminal_event_resumes_entry_without_market_or_timer_event(
    callback: str,
) -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    strategy._spot_order.is_closed = True
    strategy._cancel_pending = True

    getattr(strategy, callback)(SimpleNamespace(client_order_id="spot-0"))

    assert strategy.order_factory.limit.call_count == 2
    assert strategy._spot_order.client_order_id == "spot-1"
    assert strategy._spot_order.quantity.as_decimal() == Decimal(2)


def test_final_hedge_fill_resumes_entry_without_market_or_timer_event() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    spot_fill(strategy, "0.5", "first")
    strategy._spot_order.is_closed = True
    strategy.on_order_canceled(SimpleNamespace(client_order_id="spot-0"))
    assert strategy.order_factory.limit.call_count == 1
    strategy._hedge_orders["hedge-0"].is_closed = True

    strategy.on_order_filled(
        SimpleNamespace(
            client_order_id="hedge-0",
            trade_id="done",
            last_qty=Quantity.from_str("0.5"),
            last_px=Price.from_str("101"),
        )
    )

    assert strategy.order_factory.limit.call_count == 2
    assert strategy._spot_order.quantity.as_decimal() == Decimal("1.5")


@pytest.mark.parametrize("stale", [False, True])
def test_spot_cancellation_event_does_not_resume_with_bad_basis_or_stale_books(
    stale: bool,
) -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    strategy._spot_order.is_closed = True
    if stale:
        strategy.clock.timestamp_ns.return_value = 111
    else:
        strategy.perp_top = (Price.from_str("100.4"), *strategy.perp_top[1:])

    strategy.on_order_canceled(SimpleNamespace(client_order_id="spot-0"))

    assert strategy._spot_order is None
    assert strategy.order_factory.limit.call_count == 1


@pytest.mark.parametrize(
    "callback", ["on_order_canceled", "on_order_expired", "on_order_denied"]
)
def test_hedge_failure_event_immediately_pauses_and_cancels_spot(callback: str) -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    spot_fill(strategy, "0.5", "first")

    getattr(strategy, callback)(SimpleNamespace(client_order_id="hedge-0"))

    assert strategy._entry_paused
    strategy.cancel_order.assert_called_once_with("spot-0", client_id="spot-client")


@pytest.mark.parametrize("callback", ["on_order_accepted", "on_order_updated"])
def test_order_acknowledgment_immediately_cancels_when_basis_has_dropped(
    callback: str,
) -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    strategy.perp_top = (Price.from_str("100.4"), *strategy.perp_top[1:])

    getattr(strategy, callback)(SimpleNamespace(client_order_id="spot-0"))

    strategy.cancel_order.assert_called_once_with("spot-0", client_id="spot-client")


def opened_strategy() -> SimpleNamespace:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    strategy._spot_order.is_closed = True
    spot_fill(strategy, "2", "open")
    complete_hedge(strategy, "hedge-0", "2")
    strategy.order_factory.limit.reset_mock()
    strategy.order_factory.market.reset_mock()
    return strategy


def complete_hedge(strategy: SimpleNamespace, order_id: str, quantity: str) -> None:
    strategy._hedge_orders[order_id].is_closed = True
    strategy.on_order_filled(
        SimpleNamespace(
            client_order_id=order_id,
            trade_id=f"filled-{order_id}",
            last_qty=Quantity.from_str(quantity),
            last_px=Price.from_str("100"),
        )
    )


def update_funding(
    strategy: SimpleNamespace, rate: str = "-0.0001", ts: int = 100
) -> None:
    strategy.on_funding_rate(
        SimpleNamespace(instrument_id="perp", rate=Decimal(rate), ts_init=ts)
    )


@pytest.mark.parametrize(
    "rate,perp_ask,ts,expected",
    [
        ("-0.0001", "99.8", 100, True),
        ("-0.0001", "99.7", 100, True),
        ("-0.0001", "99.81", 100, False),
        ("0", "99.8", 100, False),
        ("0.0001", "99.8", 100, False),
        ("NaN", "99.8", 100, False),
        ("-0.0001", "99.8", 90, False),
        ("-0.0001", "99.8", 102, False),
    ],
)
def test_exit_requires_negative_fresh_funding_and_exit_basis(
    rate: str, perp_ask: str, ts: int, expected: bool
) -> None:
    strategy = opened_strategy()
    strategy.perp_top = (
        Price.from_str("99.6"),
        strategy.perp_top[1],
        Price.from_str(perp_ask),
        strategy.perp_top[3],
    )
    update_funding(strategy, rate, ts)
    assert strategy._exiting is expected
    if expected:
        strategy.order_factory.limit.assert_called_once_with(
            "spot",
            OrderSide.SELL,
            Quantity.from_str("2.000"),
            Price.from_str("101"),
            post_only=True,
        )
    else:
        strategy.order_factory.limit.assert_not_called()


def test_exit_partial_fills_are_hedged_once_and_stop_only_after_final_hedge() -> None:
    strategy = opened_strategy()
    strategy.perp_top = (Price.from_str("99.6"), *strategy.perp_top[1:])
    strategy.perp_top = (
        *strategy.perp_top[:2],
        Price.from_str("99.8"),
        strategy.perp_top[3],
    )
    update_funding(strategy)
    event = spot_fill(strategy, "0.5", "close-part", "101")
    strategy.on_order_filled(event)
    strategy.order_factory.market.assert_called_once_with(
        "perp",
        OrderSide.BUY,
        Quantity.from_str("0.500"),
        reduce_only=True,
    )
    assert strategy._spot_filled_notional == Decimal(200)
    assert strategy._spot_closed == Decimal("0.5")
    complete_hedge(strategy, "hedge-1", "0.5")
    strategy._spot_order.is_closed = True
    spot_fill(strategy, "1.5", "close-rest", "101")
    strategy.stop.assert_not_called()
    complete_hedge(strategy, "hedge-2", "1.5")
    strategy.stop.assert_called_once()
    assert strategy._spot_closed == strategy._perp_closed == Decimal(2)
    strategy._evaluate_entry()
    strategy.order_factory.limit.assert_called_once()


@pytest.mark.parametrize("reason", ["funding", "basis", "stale_book", "stale_funding"])
def test_exit_cancels_and_resumes_only_remaining_position(reason: str) -> None:
    strategy = opened_strategy()
    strategy.perp_top = (
        *strategy.perp_top[:2],
        Price.from_str("99.8"),
        strategy.perp_top[3],
    )
    update_funding(strategy)
    spot_fill(strategy, "0.5", "close-part", "101")
    complete_hedge(strategy, "hedge-1", "0.5")
    if reason == "funding":
        update_funding(strategy, "0")
    elif reason == "basis":
        strategy.perp_top = (
            *strategy.perp_top[:2],
            Price.from_str("102"),
            strategy.perp_top[3],
        )
    elif reason == "stale_book":
        strategy._spot_book_update_ns = 90
    else:
        strategy._funding_update_ns = 90
    strategy._evaluate_entry()
    strategy.cancel_order.assert_called_once_with("spot-1", client_id="spot-client")
    strategy._spot_order.is_closed = True
    strategy.on_order_canceled(SimpleNamespace(client_order_id="spot-1"))
    assert strategy._spot_order is None
    strategy._spot_book_update_ns = 100
    strategy.perp_top = (
        *strategy.perp_top[:2],
        Price.from_str("99.8"),
        strategy.perp_top[3],
    )
    update_funding(strategy)
    assert strategy._spot_order.quantity == Quantity.from_str("1.500")
    assert strategy.order_factory.limit.call_args.args[1] == OrderSide.SELL


def test_exit_waits_for_entry_cancel_and_hedges_late_entry_fills_as_sells() -> None:
    strategy = trading_strategy()
    strategy._evaluate_entry()
    spot_fill(strategy, "0.5", "first")
    complete_hedge(strategy, "hedge-0", "0.5")
    strategy.perp_top = (
        *strategy.perp_top[:2],
        Price.from_str("99.8"),
        strategy.perp_top[3],
    )
    update_funding(strategy)
    assert strategy._exiting
    strategy.cancel_order.assert_called_once()
    spot_fill(strategy, "0.25", "cancel-race")
    assert strategy.order_factory.market.call_args.args[1] == OrderSide.SELL
    strategy._spot_order.is_closed = True
    strategy.on_order_canceled(SimpleNamespace(client_order_id="spot-0"))
    strategy.order_factory.limit.assert_called_once()
    complete_hedge(strategy, "hedge-1", "0.25")
    assert strategy.order_factory.limit.call_args.args[1:] == (
        OrderSide.SELL,
        Quantity.from_str("0.750"),
        Price.from_str("101"),
    )


def test_exit_hedge_failure_pauses_remaining_spot_sales() -> None:
    strategy = opened_strategy()
    strategy.perp_top = (
        *strategy.perp_top[:2],
        Price.from_str("99.8"),
        strategy.perp_top[3],
    )
    update_funding(strategy)
    spot_fill(strategy, "0.5", "close-part", "101")
    strategy.on_order_rejected(SimpleNamespace(client_order_id="hedge-1"))
    assert strategy._entry_paused
    strategy.cancel_order.assert_called_once_with("spot-1", client_id="spot-client")
    strategy.stop.assert_not_called()


def test_exit_waits_for_queued_sell_fill_after_cancellation() -> None:
    strategy = opened_strategy()
    strategy.perp_top = (
        *strategy.perp_top[:2],
        Price.from_str("99.8"),
        strategy.perp_top[3],
    )
    update_funding(strategy)
    order = strategy._spot_order
    order.is_closed = True
    order.filled_qty = Quantity.from_str("0.5")
    strategy.on_order_canceled(SimpleNamespace(client_order_id="spot-1"))
    strategy.order_factory.limit.assert_called_once()
    strategy.on_order_filled(
        SimpleNamespace(
            client_order_id="spot-1",
            trade_id="late-sell",
            last_qty=Quantity.from_str("0.5"),
            last_px=Price.from_str("101"),
        )
    )
    strategy.order_factory.market.assert_called_once_with(
        "perp",
        OrderSide.BUY,
        Quantity.from_str("0.500"),
        reduce_only=True,
    )
    strategy.order_factory.limit.assert_called_once()
    complete_hedge(strategy, "hedge-1", "0.5")
    assert strategy._spot_order.quantity == Quantity.from_str("1.500")


def test_exit_completes_even_if_funding_recovers_before_final_hedge_fill() -> None:
    strategy = opened_strategy()
    strategy.perp_top = (
        *strategy.perp_top[:2],
        Price.from_str("99.8"),
        strategy.perp_top[3],
    )
    update_funding(strategy)
    strategy._spot_order.is_closed = True
    spot_fill(strategy, "2", "close-all", "101")
    update_funding(strategy, "0.0001")
    strategy.stop.assert_not_called()
    strategy._spot_book_update_ns = 90
    complete_hedge(strategy, "hedge-1", "2")
    strategy.stop.assert_called_once()
