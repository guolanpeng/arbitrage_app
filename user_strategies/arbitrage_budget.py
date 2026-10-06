"""Reserve capital for spot buys hedging two-times-leveraged perpetual shorts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import lcm
from threading import RLock
from typing import Any

from nautilus_trader.model import Currency

SPOT_DEPTH_USAGE_RATIO = Decimal("0.5")
COIN_CAPITAL_RATIO = Decimal("0.1")
PERP_LEVERAGE = Decimal(2)
MAX_ACCOUNT_AGE_NS = 30_000_000_000


def depth_vwap(
    levels: Mapping[Decimal, Decimal],
    target_quantity: Decimal | None = None,
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Return the buy-side VWAP, quantity, and notional up to a target quantity."""
    if target_quantity is not None and (
        not target_quantity.is_finite() or target_quantity <= 0
    ):
        return None
    total_quantity = Decimal()
    total_notional = Decimal()
    for price, quantity in sorted(levels.items()):
        if (
            not price.is_finite()
            or not quantity.is_finite()
            or price <= 0
            or quantity <= 0
        ):
            return None
        if target_quantity is not None:
            quantity = min(quantity, target_quantity - total_quantity)
        total_quantity += quantity
        total_notional += price * quantity
        if target_quantity is not None and total_quantity == target_quantity:
            break

    if total_quantity == 0 or (
        target_quantity is not None and total_quantity < target_quantity
    ):
        return None
    return total_notional / total_quantity, total_quantity, total_notional


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise TypeError("Account amounts must be exact decimal values")
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError("Account amounts must be finite")
    return number


@dataclass(frozen=True)
class AccountFunds:
    account_id: str
    equity: Decimal
    spot_free: Decimal
    margin_free: Decimal
    ts_init: int


def read_account_funds(account: Any, now_ns: int) -> AccountFunds:
    """Read fresh unified-account metadata without treating borrowing as equity."""
    event = account.last_event
    if event is None or not 0 <= now_ns - event.ts_init <= MAX_ACCOUNT_AGE_NS:
        raise ValueError("Unified account snapshot is missing or stale")
    info = event.info
    if "gate_unified" in info:
        row = info["gate_unified"]
        asset = row["balances"]["USDT"]
        equity = _decimal(row["unified_account_total_equity"])
        if row.get("locked", False):
            raise ValueError("Gate unified account is locked")
        spot_free = min(
            _decimal(asset["available"]),
            _decimal(asset["equity"]) - _decimal(asset["freeze"]),
        )
        margin_free = _decimal(
            asset["available_margin"]
            if row["mode"] == "single_currency"
            else row["total_available_margin"]
        )
    elif "binance_portfolio_margin" in info:
        row = info["binance_portfolio_margin"]
        if row["accountStatus"] != "NORMAL":
            raise ValueError("Binance portfolio account is not in normal status")
        equity = _decimal(row["actualEquity"])
        asset = next(
            asset
            for asset in info["binance_portfolio_balances"]
            if asset["asset"] == "USDT"
        )
        balance = account.balance_total(Currency.from_str("USDT"))
        if balance is None:
            raise ValueError("USDT balance is unavailable")
        spot_free = min(_decimal(asset["crossMarginFree"]), balance.as_decimal())
        available = row.get("totalAvailableBalance")
        margin_free = (
            _decimal(available)
            if available not in (None, "")
            else _decimal(row["accountEquity"]) - _decimal(row["accountInitialMargin"])
        )
    else:
        raise ValueError("Unified account metadata is unavailable")
    return AccountFunds(
        str(account.id),
        equity,
        max(spot_free, Decimal(0)),
        max(margin_free, Decimal(0)),
        event.ts_init,
    )


@dataclass(frozen=True)
class CapitalReservation:
    coin: str
    spot_account: str
    perp_account: str
    base_quantity: Decimal
    contract_quantity: Decimal
    spot_vwap: Decimal
    spot_cost: Decimal
    perp_margin: Decimal

    @property
    def capital(self) -> Decimal:
        return self.spot_cost + self.perp_margin


class ArbitrageBudget:
    """Share one capital ledger across all combinations in a single trading process."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._reservations: dict[str, CapitalReservation] = {}

    def reserve(
        self,
        *,
        reservation_id: str,
        coin: str,
        accounts: Sequence[Any],
        spot_account: str,
        perp_account: str,
        now_ns: int,
        spot_asks: Mapping[Decimal, Decimal],
        maker_price: Decimal,
        spot_step: Decimal,
        contract_step: Decimal,
        contract_multiplier: Decimal,
        spot_min_quantity: Decimal,
        spot_min_notional: Decimal,
        contract_min_quantity: Decimal,
        contract_min_notional: Decimal,
        occupied_by_coin: Mapping[str, Decimal],
    ) -> CapitalReservation | None:
        """Atomically read balances, size a paired entry, and reserve its capital.

        Pass existing position capital separately from this ledger's reservations.
        Keep filled portions reserved until reconciled positions and balances include them.
        """
        with self._lock:
            if reservation_id in self._reservations:
                raise ValueError("Reservation ID already exists")
            for amount in (maker_price, spot_step, contract_step, contract_multiplier):
                if (
                    not isinstance(amount, Decimal)
                    or not amount.is_finite()
                    or amount <= 0
                ):
                    raise ValueError(
                        "Prices, quantity steps, and multiplier must be positive"
                    )
            for amount in (
                spot_min_quantity,
                spot_min_notional,
                contract_min_quantity,
                contract_min_notional,
            ):
                if (
                    not isinstance(amount, Decimal)
                    or not amount.is_finite()
                    or amount < 0
                ):
                    raise ValueError("Order minimums must be finite and nonnegative")
            if any(
                not amount.is_finite() or amount < 0
                for amount in occupied_by_coin.values()
            ):
                raise ValueError(
                    "Existing position capital must be finite and nonnegative"
                )
            funds = {}
            for account in accounts:
                snapshot = read_account_funds(account, now_ns)
                previous = funds.get(snapshot.account_id)
                if previous is not None and previous != snapshot:
                    raise ValueError("Conflicting snapshots for one unified account")
                funds[snapshot.account_id] = snapshot
            spot = funds[spot_account]
            perp = funds[perp_account]
            equity = sum((account.equity for account in funds.values()), Decimal(0))
            held = list(self._reservations.values())
            total_left = equity - sum(occupied_by_coin.values(), Decimal(0))
            total_left -= sum((entry.capital for entry in held), Decimal(0))
            coin_left = equity * COIN_CAPITAL_RATIO - occupied_by_coin.get(
                coin, Decimal(0)
            )
            coin_left -= sum(
                (entry.capital for entry in held if entry.coin == coin), Decimal(0)
            )
            spot_left = spot.spot_free - sum(
                (
                    entry.spot_cost
                    for entry in held
                    if entry.spot_account == spot_account
                ),
                Decimal(0),
            )
            margin_left = perp.margin_free - sum(
                (
                    entry.perp_margin
                    for entry in held
                    if entry.perp_account == perp_account
                ),
                Decimal(0),
            )
            # A spot purchase can consume collateral even when both legs share one account
            for entry in held:
                if entry.perp_account == spot_account:
                    spot_left -= entry.perp_margin
                if entry.spot_account == perp_account:
                    margin_left -= entry.spot_cost
            capital_left = min(total_left, coin_left)
            if spot_account == perp_account:
                capital_left = min(capital_left, margin_left)
            if min(capital_left, spot_left, margin_left) <= 0:
                return None
            levels = dict(sorted(spot_asks.items())[:3])
            depth = depth_vwap(levels)
            if depth is None:
                return None
            quantity_left = depth[1] * SPOT_DEPTH_USAGE_RATIO
            planned = Decimal(0)
            margin_per_coin = maker_price / PERP_LEVERAGE
            for price, available_quantity in levels.items():
                quantity = min(
                    available_quantity,
                    quantity_left,
                    spot_left / price,
                    margin_left / margin_per_coin,
                    capital_left / (price + margin_per_coin),
                )
                planned += quantity
                quantity_left -= quantity
                spot_left -= quantity * price
                margin_left -= quantity * margin_per_coin
                capital_left -= quantity * (price + margin_per_coin)
                if min(quantity_left, spot_left, margin_left, capital_left) <= 0:
                    break
            base_contract_step = contract_step * contract_multiplier
            scale = Decimal(10) ** max(
                0,
                -spot_step.as_tuple().exponent,
                -base_contract_step.as_tuple().exponent,
            )
            step = (
                Decimal(lcm(int(spot_step * scale), int(base_contract_step * scale)))
                / scale
            )
            planned = (planned // step) * step
            if planned <= 0:
                return None
            execution = depth_vwap(levels, planned)
            if execution is None:
                return None
            vwap, _, spot_cost = execution
            if (
                planned < spot_min_quantity
                or spot_cost < spot_min_notional
                or planned / contract_multiplier < contract_min_quantity
                or planned * maker_price < contract_min_notional
            ):
                return None
            entry = CapitalReservation(
                coin,
                spot_account,
                perp_account,
                planned,
                planned / contract_multiplier,
                vwap,
                spot_cost,
                planned * margin_per_coin,
            )
            self._reservations[reservation_id] = entry
            return entry

    def release(self, reservation_id: str, unfilled_quantity: Decimal) -> None:
        """Release only a confirmed unfilled quantity, retaining filled capital."""
        with self._lock:
            entry = self._reservations[reservation_id]
            if (
                not unfilled_quantity.is_finite()
                or not 0 <= unfilled_quantity <= entry.base_quantity
            ):
                raise ValueError("Released quantity must be within the reservation")
            remaining = entry.base_quantity - unfilled_quantity
            if remaining == 0:
                del self._reservations[reservation_id]
                return
            ratio = remaining / entry.base_quantity
            self._reservations[reservation_id] = CapitalReservation(
                entry.coin,
                entry.spot_account,
                entry.perp_account,
                remaining,
                entry.contract_quantity * ratio,
                entry.spot_vwap,
                entry.spot_cost * ratio,
                entry.perp_margin * ratio,
            )
