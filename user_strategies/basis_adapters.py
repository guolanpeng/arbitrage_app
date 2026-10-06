"""Local adapter registry for venue-independent basis legs and account monitoring."""

from __future__ import annotations

import json
import os
from typing import Any

from nautilus_trader.live.config import resolve_path
from nautilus_trader.model import AccountId, InstrumentId


class GateSpotAdapter:
    venue = "gate"
    market = "spot"
    client_id = "GATE_SPOT"
    account_id = "GATE-SPOT-001"

    def leg(self, symbol: str) -> dict[str, str]:
        return {
            "instrument_id": f"{symbol}.GATE",
            "native_symbol": symbol.removesuffix("USDT") + "_USDT",
        }

    def add_data(self, builder: Any, leg: dict[str, str]) -> Any:
        from nautilus_trader.adapters.gate import (
            GateDataClientConfig,
            GateDataClientFactory,
            GateProduct,
        )

        return builder.add_data_client(
            leg["client_id"],
            GateDataClientFactory(),
            GateDataClientConfig(
                product=GateProduct.Spot,
                load_ids=[InstrumentId.from_str(leg["instrument_id"])],
            ),
        )

    def add_exec(self, builder: Any, leg: dict[str, str]) -> Any:
        from nautilus_trader.adapters.gate import (
            GateExecutionClientConfig,
            GateExecutionClientFactory,
            GateProduct,
        )

        return builder.add_exec_client(
            leg["client_id"],
            GateExecutionClientFactory(),
            GateExecutionClientConfig(
                product=GateProduct.Spot,
                account_id=AccountId.from_str(leg["account_id"]),
                api_key=os.environ["GATE_API_KEY"],
                api_secret=os.environ["GATE_API_SECRET"],
                user_id=os.getenv("GATE_USER_ID"),
            ),
        )

    def account(self, reader: Any, leg: dict[str, str]) -> dict[str, Any]:
        return {
            "balances": reader.gate_balances(),
            "open_orders": [
                {**row, "instrument_id": leg["instrument_id"]}
                for row in reader.gate_open_orders()
                if row["currency_pair"] == leg["native_symbol"]
            ],
        }


class BinancePerpAdapter:
    venue = "binance"
    market = "perp"
    client_id = "BINANCE_FUTURES"
    account_id = "BINANCE-FUTURES-001"

    def leg(self, symbol: str) -> dict[str, str]:
        return {"instrument_id": f"{symbol}-PERP.BINANCE", "native_symbol": symbol}

    def provider(self, leg: dict[str, str]) -> Any:
        from nautilus_trader.adapters.binance import BinanceInstrumentProviderConfig

        return BinanceInstrumentProviderConfig(
            load_all=False, load_ids=[leg["instrument_id"]]
        )

    def add_data(self, builder: Any, leg: dict[str, str]) -> Any:
        from nautilus_trader.adapters.binance import (
            BinanceDataClientConfig,
            BinanceDataClientFactory,
            BinanceEnvironment,
            BinanceProductType,
        )

        return builder.add_data_client(
            leg["client_id"],
            BinanceDataClientFactory(),
            BinanceDataClientConfig(
                product_type=BinanceProductType.USD_M,
                environment=BinanceEnvironment.LIVE,
                instrument_provider=self.provider(leg),
            ),
        )

    def add_exec(self, builder: Any, leg: dict[str, str]) -> Any:
        from nautilus_trader.adapters.binance import (
            BinanceEnvironment,
            BinanceExecutionClientConfig,
            BinanceExecutionClientFactory,
            BinanceProductType,
        )

        return builder.add_exec_client(
            leg["client_id"],
            BinanceExecutionClientFactory(),
            BinanceExecutionClientConfig(
                account_id=AccountId.from_str(leg["account_id"]),
                unified_account=True,
                product_type=BinanceProductType.USD_M,
                environment=BinanceEnvironment.LIVE,
                instrument_provider=self.provider(leg),
                futures_leverages={leg["native_symbol"]: 2},
                use_ws_trading=False,
            ),
        )

    def account(self, reader: Any, leg: dict[str, str]) -> dict[str, Any]:
        positions = reader.binance_get("/papi/v1/um/positionRisk", {})
        orders = reader.binance_get("/papi/v1/um/openOrders", {})
        return {
            "positions": [
                {**row, "symbol": leg["symbol"]}
                for row in positions
                if row["symbol"] == leg["native_symbol"]
            ],
            "open_orders": [
                {**row, "instrument_id": leg["instrument_id"]}
                for row in orders
                if row["symbol"] == leg["native_symbol"]
            ],
        }

    def funding_history(
        self, reader: Any, leg: dict[str, str], start: int, end: int
    ) -> list[dict[str, Any]]:
        return reader.funding_history(leg["native_symbol"], start, end)


def adapter(profile: str, market: str) -> Any:
    """Resolve adapter code only from local configuration, never from database paths."""
    profiles = {
        "gate_spot": "user_strategies.basis_adapters:GateSpotAdapter",
        "binance_perp": "user_strategies.basis_adapters:BinancePerpAdapter",
    }
    extra = json.loads(os.getenv("BASIS_ADAPTERS", "{}"))
    if not isinstance(extra, dict) or set(extra) & set(profiles):
        raise ValueError("Custom adapters must use distinct profile names")
    profiles.update(extra)
    if profile not in profiles:
        raise ValueError(f"Basis adapter profile not configured: {profile}")
    provider = resolve_path(profiles[profile])()
    required = ("leg", "add_data", "add_exec", "account") + (
        ("funding_history",) if market == "perp" else ()
    )
    if provider.market != market or not all(
        callable(getattr(provider, name, None)) for name in required
    ):
        raise ValueError(f"Basis adapter does not support {market}: {profile}")
    return provider


def make_leg(profile: str, market: str, symbol: str) -> dict[str, str]:
    provider = adapter(profile, market)
    route = provider.leg(symbol)
    leg = {
        "instrument_id": route["instrument_id"],
        "native_symbol": route["native_symbol"],
        "profile": profile,
        "venue": provider.venue,
        "market": market,
        "client_id": provider.client_id,
        "account_id": provider.account_id,
        "symbol": symbol,
    }
    validate_leg(leg, market)
    return leg


def validate_leg(leg: dict[str, str], market: str) -> Any:
    provider = adapter(leg["profile"], market)
    for key in ("venue", "market", "client_id", "account_id"):
        if leg[key] != getattr(provider, key):
            raise ValueError(f"Retained leg {key} does not match configured adapter")
    if (
        InstrumentId.from_str(leg["instrument_id"]).venue.value.casefold()
        != provider.venue.casefold()
    ):
        raise ValueError("Retained instrument venue does not match adapter")
    if not leg["symbol"] or not leg["native_symbol"]:
        raise ValueError("Leg symbols are required")
    expected = provider.leg(leg["symbol"])
    if any(leg[key] != expected[key] for key in ("instrument_id", "native_symbol")):
        raise ValueError("Retained symbols differ from configured adapter mapping")
    return provider


def instance_legs(instance: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Keep complete instrument IDs; migrate only the known legacy Gate/Binance pair."""
    legs = instance.get("legs")
    if legs is None:
        spot_id, perp_id = instance["spot_id"], instance["perp_id"]
        if not spot_id.endswith(".GATE") or not perp_id.endswith("-PERP.BINANCE"):
            raise ValueError(
                "Legacy instance lacks venue/account metadata; cannot guess routes"
            )
        symbol = spot_id.removesuffix(".GATE")
        legs = {
            "spot": make_leg("gate_spot", "spot", symbol),
            "perp": make_leg("binance_perp", "perp", symbol),
        }
    for market in ("spot", "perp"):
        validate_leg(legs[market], market)
        if legs[market]["instrument_id"] != instance[f"{market}_id"]:
            raise ValueError("Retained instrument ID differs from leg metadata")
    if legs["spot"]["symbol"] != legs["perp"]["symbol"]:
        raise ValueError("Basis legs must refer to the same canonical symbol")
    return legs


def selected_legs(symbol: str) -> dict[str, dict[str, str]]:
    return {
        "spot": make_leg(os.getenv("BASIS_SPOT_PROFILE", "gate_spot"), "spot", symbol),
        "perp": make_leg(
            os.getenv("BASIS_PERP_PROFILE", "binance_perp"), "perp", symbol
        ),
    }
