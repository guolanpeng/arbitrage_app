from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest

from user_strategies.arbitrage_budget import (
    ArbitrageBudget,
    depth_vwap,
    read_account_funds,
)


def _account(
    account_id: str, equity: str, available: str | None = None
) -> SimpleNamespace:
    available = equity if available is None else available
    return SimpleNamespace(
        id=account_id,
        last_event=SimpleNamespace(
            ts_init=1_000,
            info={
                "gate_unified": {
                    "mode": "multi_currency",
                    "unified_account_total_equity": equity,
                    "total_available_margin": available,
                    "balances": {
                        "USDT": {
                            "equity": equity,
                            "available": available,
                            "freeze": "0",
                        },
                    },
                },
            },
        ),
    )


def _reserve(budget: ArbitrageBudget, **changes):
    values = {
        "reservation_id": "order-1",
        "coin": "BTC",
        "accounts": [_account("spot", "2000"), _account("perp", "1000")],
        "spot_account": "spot",
        "perp_account": "perp",
        "now_ns": 1_001,
        "spot_asks": {Decimal(100): Decimal(10)},
        "maker_price": Decimal(100),
        "spot_step": Decimal("0.01"),
        "contract_step": Decimal(1),
        "contract_multiplier": Decimal("0.01"),
        "spot_min_quantity": Decimal(0),
        "spot_min_notional": Decimal(0),
        "contract_min_quantity": Decimal(0),
        "contract_min_notional": Decimal(0),
        "occupied_by_coin": {},
    }
    return budget.reserve(**(values | changes))


def test_coin_cap_is_ten_percent_of_equity_including_both_legs() -> None:
    result = _reserve(ArbitrageBudget())

    assert result.base_quantity == Decimal(2)
    assert result.contract_quantity == Decimal(200)
    assert result.spot_cost == Decimal(200)
    assert result.perp_margin == Decimal(100)
    assert result.capital == Decimal(300)


def test_half_depth_and_partial_last_level_determine_actual_vwap() -> None:
    result = _reserve(
        ArbitrageBudget(),
        accounts=[_account("spot", "20000"), _account("perp", "10000")],
        spot_asks={
            Decimal(102): Decimal(5),
            Decimal(100): Decimal(2),
            Decimal(101): Decimal(3),
        },
    )

    assert result.base_quantity == Decimal(5)
    assert result.spot_cost == Decimal(503)
    assert result.spot_vwap == Decimal("100.6")


@pytest.mark.parametrize(
    ("spot_free", "margin_free", "expected"),
    [("50", "1000", "0.5"), ("2000", "25", "0.5"), ("0", "1000", None)],
)
def test_each_account_available_funds_limit_quantity(
    spot_free, margin_free, expected
) -> None:
    result = _reserve(
        ArbitrageBudget(),
        accounts=[
            _account("spot", "2000", spot_free),
            _account("perp", "1000", margin_free),
        ],
    )

    assert (None if result is None else result.base_quantity) == (
        None if expected is None else Decimal(expected)
    )


def test_same_account_equity_is_counted_once_and_collateral_is_shared() -> None:
    account = _account("shared", "3000", "75")
    result = _reserve(
        ArbitrageBudget(),
        accounts=[account, account],
        spot_account="shared",
        perp_account="shared",
    )

    assert result.base_quantity == Decimal("0.5")
    assert result.capital == Decimal(75)


def test_existing_positions_and_cross_venue_reservations_share_coin_cap() -> None:
    budget = ArbitrageBudget()
    first = _reserve(budget, occupied_by_coin={"BTC": Decimal(150)})
    second = _reserve(
        budget,
        reservation_id="order-2",
        spot_account="perp",
        perp_account="spot",
        occupied_by_coin={"BTC": Decimal(150)},
    )

    assert first.capital == Decimal(150)
    assert second is None


def test_confirmed_partial_cancellation_keeps_filled_capital_reserved() -> None:
    budget = ArbitrageBudget()
    _reserve(budget)
    budget.release("order-1", Decimal(1))
    replacement = _reserve(budget, reservation_id="order-2")

    assert replacement.capital == Decimal(150)
    assert _reserve(budget, reservation_id="order-3") is None


def test_full_confirmed_cancellation_releases_capital() -> None:
    budget = ArbitrageBudget()
    _reserve(budget)
    budget.release("order-1", Decimal(2))

    assert _reserve(budget, reservation_id="order-2").capital == Decimal(300)


def test_concurrent_strategies_cannot_reserve_same_coin_allowance_twice() -> None:
    budget = ArbitrageBudget()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda index: _reserve(budget, reservation_id=str(index)), range(2)
            )
        )

    assert sum(result is not None for result in results) == 1


def test_rounding_matches_both_spot_and_contract_quantity_steps() -> None:
    result = _reserve(
        ArbitrageBudget(),
        spot_step=Decimal("0.03"),
        contract_multiplier=Decimal("0.02"),
    )

    assert result.base_quantity == Decimal("1.98")
    assert result.contract_quantity == Decimal(99)


@pytest.mark.parametrize(
    "minimum",
    [
        "spot_min_quantity",
        "spot_min_notional",
        "contract_min_quantity",
        "contract_min_notional",
    ],
)
def test_order_below_exchange_minimum_does_not_reserve_funds(minimum: str) -> None:
    budget = ArbitrageBudget()

    assert _reserve(budget, **{minimum: Decimal(10000)}) is None
    assert _reserve(budget).capital == Decimal(300)


def test_existing_total_capital_also_limits_new_coin() -> None:
    result = _reserve(
        ArbitrageBudget(),
        occupied_by_coin={"ETH": Decimal(2900)},
    )

    assert result.capital == Decimal(99)


def test_other_coins_cannot_reuse_pending_spot_cash() -> None:
    budget = ArbitrageBudget()
    accounts = [_account("spot", "2000", "100"), _account("perp", "1000")]

    first = _reserve(budget, accounts=accounts)
    second = _reserve(budget, accounts=accounts, coin="ETH", reservation_id="order-2")

    assert first.base_quantity == Decimal(1)
    assert second is None


def test_duplicate_id_cannot_replace_an_existing_reservation() -> None:
    budget = ArbitrageBudget()
    _reserve(budget)

    with pytest.raises(ValueError, match="already exists"):
        _reserve(budget)
    assert _reserve(budget, reservation_id="order-2") is None


def test_over_release_does_not_free_an_existing_reservation() -> None:
    budget = ArbitrageBudget()
    _reserve(budget)

    with pytest.raises(ValueError, match="within"):
        budget.release("order-1", Decimal(3))
    assert _reserve(budget, reservation_id="order-2") is None


def test_stale_account_does_not_create_reservation() -> None:
    budget = ArbitrageBudget()
    with pytest.raises(ValueError, match="stale"):
        _reserve(budget, now_ns=31_000_001_001)

    assert _reserve(budget).capital == Decimal(300)


def test_account_reader_uses_equity_instead_of_borrowed_available_balance() -> None:
    funds = read_account_funds(_account("spot", "100", "500"), 1_001)

    assert funds.equity == Decimal(100)
    assert funds.spot_free == Decimal(100)


def test_binance_reader_handles_empty_available_margin_and_separate_spot_cash() -> None:
    account = SimpleNamespace(
        id="binance",
        balance_total=lambda currency: SimpleNamespace(as_decimal=lambda: Decimal(500)),
        last_event=SimpleNamespace(
            ts_init=1_000,
            info={
                "binance_portfolio_margin": {
                    "accountStatus": "NORMAL",
                    "actualEquity": "3000",
                    "accountEquity": "2800",
                    "accountInitialMargin": "1000",
                    "totalAvailableBalance": "",
                },
                "binance_portfolio_balances": [
                    {"asset": "USDT", "crossMarginFree": "300"}
                ],
            },
        ),
    )

    funds = read_account_funds(account, 1_001)

    assert funds.equity == Decimal(3000)
    assert funds.spot_free == Decimal(300)
    assert funds.margin_free == Decimal(1800)


def test_depth_vwap_returns_weighted_price_and_total_quantity() -> None:
    assert depth_vwap(
        {
            Decimal(100): Decimal(2),
            Decimal(101): Decimal(3),
            Decimal(102): Decimal(5),
        },
    ) == (Decimal("101.3"), Decimal(10), Decimal(1013))


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (Decimal(1), (Decimal(100), Decimal(1), Decimal(100))),
        (Decimal(2), (Decimal(100), Decimal(2), Decimal(200))),
        (Decimal(4), (Decimal("100.5"), Decimal(4), Decimal(402))),
        (Decimal(5), (Decimal("100.6"), Decimal(5), Decimal(503))),
        (Decimal(10), (Decimal("101.3"), Decimal(10), Decimal(1013))),
        (Decimal(11), None),
        (Decimal(0), None),
        (Decimal(-1), None),
        (Decimal("NaN"), None),
    ],
)
def test_depth_vwap_consumes_only_target_quantity_in_price_order(
    target: Decimal,
    expected: tuple[Decimal, Decimal, Decimal] | None,
) -> None:
    levels = {
        Decimal(102): Decimal(5),
        Decimal(100): Decimal(2),
        Decimal(101): Decimal(3),
    }

    assert depth_vwap(levels, target) == expected


@pytest.mark.parametrize(
    "levels",
    [
        {},
        {Decimal(0): Decimal(1)},
        {Decimal(1): Decimal(0)},
    ],
)
def test_depth_vwap_rejects_empty_or_non_positive_levels(
    levels: dict[Decimal, Decimal],
) -> None:
    assert depth_vwap(levels) is None
