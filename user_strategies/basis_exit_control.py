"""Persistent per-instance exit settings and off-loop database reads."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from decimal import Decimal, localcontext
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Any


def exit_settings(value: dict[str, Any]) -> dict[str, Any]:
    """Validate percentages without converting money or rates through floats."""
    if not isinstance(value, dict):
        raise TypeError("Exit settings must be an object")
    result: dict[str, Any] = {}
    for name, default in (("funding_percent", "-2"), ("basis_percent", None)):
        raw = value.get(name, default)
        if raw is None:
            result[name] = None
            continue
        if not isinstance(raw, str) or len(raw) > 32:
            raise ValueError("Thresholds must be decimal strings")
        number = Decimal(raw)
        if not number.is_finite() or not -100 <= number <= 100:
            raise ValueError("Threshold percentage outside [-100, 100]")
        result[name] = str(number)
    if all(result[name] is None for name in ("funding_percent", "basis_percent")):
        raise ValueError("At least one exit condition is required")
    for name, default, allowed in (
        ("funding_operator", "le", ("le", "ge")),
        ("basis_operator", "le", ("le", "ge")),
        ("spot_mode", "MAKER", ("MAKER", "TAKER")),
        ("perp_mode", "TAKER", ("MAKER", "TAKER")),
    ):
        result[name] = value.get(name, default)
        if result[name] not in allowed:
            raise ValueError(f"Invalid {name}")
    result["version"] = value.get("version", 0)
    if type(result["version"]) is not int or result["version"] < 0:
        raise ValueError("Invalid version")
    return result


def condition_met(value: Decimal, threshold: str | None, operator: str) -> bool:
    if threshold is None or not value.is_finite():
        return False
    target = Decimal(threshold)
    return value <= target if operator == "le" else value >= target


def connection(*, read_only: bool = True) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        user=os.environ["POSTGRES_USERNAME"],
        password=os.getenv("POSTGRES_PASSWORD", ""),
        dbname=os.environ["POSTGRES_DATABASE"],
        connect_timeout=3,
        options=(
            f"-c default_transaction_read_only={'on' if read_only else 'off'} "
            "-c statement_timeout=5000"
        ),
        row_factory=dict_row,
    )


def allocate_strategy_id(spot_venue: str, perp_venue: str, symbol: str) -> str:
    """Commit a unique database counter before returning a new strategy ID."""
    with connection(read_only=False) as db:
        row = db.execute(
            "INSERT INTO general (id, value) VALUES (%s, convert_to('1', 'UTF8')) "
            "ON CONFLICT (id) DO UPDATE SET value = "
            "convert_to((convert_from(general.value, 'UTF8')::bigint + 1)::text, 'UTF8') "
            "RETURNING convert_from(value, 'UTF8')::bigint AS number",
            ("basis:strategy-sequence:v1",),
        ).fetchone()
    return f"BASIS-{spot_venue.upper()}-{perp_venue.upper()}-{symbol.upper()}-{row['number']}"


def initialize_strategy_schema() -> None:
    """Install the strategy table and asynchronous cache-write routing at startup."""
    with connection(read_only=False) as db:
        db.execute(
            Path(__file__).with_name("basis_schema.sql").read_text(encoding="utf-8")
        )


@contextmanager
def execution_lease() -> Any:
    """Hold a session advisory lock so two runners cannot execute the same trader."""
    with connection() as db:
        row = db.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (0x424153495301,)
        ).fetchone()
        db.commit()  # Session lock survives commit without a long-lived transaction.
        if not row["acquired"]:
            raise ValueError("Another basis execution process owns this trader")
        try:
            yield
        finally:
            db.execute("SELECT pg_advisory_unlock(%s)", (0x424153495301,))


def read_control(strategy_id: str) -> dict[str, Any]:
    with connection() as db:
        rows = db.execute(
            "SELECT id, value FROM general WHERE id = ANY(%s)",
            ([f"basis:exit:v1:{strategy_id}", f"basis:account:v1:{strategy_id}"],),
        ).fetchall()
    records = {row["id"]: json.loads(bytes(row["value"])) for row in rows}
    requested = records.get(f"basis:exit:v1:{strategy_id}")
    if requested is not None and requested.get("strategy_id") != strategy_id:
        raise ValueError("Exit settings identity mismatch")
    account = records.get(f"basis:account:v1:{strategy_id}")
    if account is not None and account.get("strategy_id") != strategy_id:
        raise ValueError("Account snapshot identity mismatch")
    return {"settings": exit_settings(requested or {}), "account": account}


def recovery_candidate() -> dict[str, Any] | None:
    with localcontext() as context:
        context.prec = 80
        return _recovery_candidate()


def _recovery_candidate() -> dict[str, Any] | None:
    """Recover one retained instance, never create a replacement over an old position."""
    with connection() as db:
        rows = db.execute(
            "SELECT snapshot FROM basis_strategy WHERE trader_id = %s AND "
            "(state NOT IN ('closed', 'stopped') OR spot_remaining <> 0 OR perp_remaining <> 0)",
            ("DYNAMIC-BASIS-001",),
        ).fetchall()
        active = [row["snapshot"] for row in rows]
        if not active:
            return None
        if len(active) != 1:
            raise ValueError(
                "Multiple retained instances require manual reconciliation"
            )
        instance = active[0]
        events = db.execute(
            "SELECT client_order_id, kind, instrument_id, order_side, last_qty, last_px, trade_id, ts_init FROM order_event WHERE trader_id = %s AND strategy_id = %s ORDER BY ts_init::numeric, id",
            (instance["trader_id"], instance["strategy_id"]),
        ).fetchall()
        history = db.execute(
            "SELECT id FROM general WHERE starts_with(id, %s)",
            (f"basis:state:v1:{instance['strategy_id']}:",),
        ).fetchall()
    state = {
        name: Decimal(0)
        for name in (
            "spot_filled",
            "spot_closed",
            "perp_filled",
            "perp_closed",
            "spot_filled_notional",
        )
    }
    latest, sides, seen = {}, {}, set()
    for row in events:
        order_id = row["client_order_id"]
        latest[order_id] = row["kind"]
        if row["order_side"]:
            sides[order_id] = row["order_side"]
        if row["kind"] != "OrderFilled":
            continue
        identity = (order_id, row["trade_id"])
        if identity in seen:
            continue
        seen.add(identity)
        quantity, price = Decimal(str(row["last_qty"])), Decimal(str(row["last_px"]))
        if (
            not quantity.is_finite()
            or not price.is_finite()
            or quantity <= 0
            or price <= 0
        ):
            raise ValueError("Invalid persisted fill")
        side = sides[order_id]
        if row["instrument_id"] == instance["spot_id"]:
            key = "spot_filled" if side == "BUY" else "spot_closed"
            if side == "BUY":
                state["spot_filled_notional"] += quantity * price
        elif row["instrument_id"] == instance["perp_id"]:
            key = "perp_filled" if side == "SELL" else "perp_closed"
        else:
            raise ValueError("Unexpected recovery instrument")
        state[key] += quantity
    # Filled callbacks alone do not prove full completion: verify initialized quantity too.
    terminal = {"OrderCanceled", "OrderExpired", "OrderRejected", "OrderDenied"}
    with connection() as db:
        totals = db.execute(
            "SELECT client_order_id, quantity FROM order_event WHERE trader_id = %s AND strategy_id = %s AND kind = 'OrderInitialized'",
            (instance["trader_id"], instance["strategy_id"]),
        ).fetchall()
    amounts = {row["client_order_id"]: Decimal(str(row["quantity"])) for row in totals}
    filled = {order_id: Decimal(0) for order_id in latest}
    for row in events:
        if (
            row["kind"] == "OrderFilled"
            and (row["client_order_id"], row["trade_id"]) in seen
        ):
            identity = (row["client_order_id"], row["trade_id"])
            filled[row["client_order_id"]] += Decimal(str(row["last_qty"]))
            seen.remove(identity)
    instance["recovery"] = {
        **{key: str(value) for key, value in state.items()},
        "baseline": instance["baseline"],
        "started_at_ms": instance["started_at_ms"],
        "exiting": bool(
            state["spot_closed"]
            or state["perp_closed"]
            or instance["state"] == "exiting"
        ),
        "orders_terminal": all(
            kind in terminal
            or (order_id in amounts and filled[order_id] == amounts[order_id])
            for order_id, kind in latest.items()
        ),
        "monitor_sequence": max(
            (int(row["id"].rsplit(":", 1)[1]) for row in history), default=0
        ),
    }
    return instance


class ExitControlReader:
    """Read PostgreSQL in a background thread; execution only consumes queued snapshots."""

    def __init__(self, strategy_id: str) -> None:
        self.strategy_id = strategy_id
        self.updates: Queue[dict[str, Any]] = Queue(maxsize=1)
        self.stopped = Event()

    def start(self) -> None:
        Thread(target=self._run, name="basis-exit-reader", daemon=True).start()

    def stop(self) -> None:
        self.stopped.set()

    def _run(self) -> None:
        import psycopg

        while not self.stopped.is_set():
            try:
                update = read_control(self.strategy_id)
            except (
                psycopg.Error,
                ValueError,
                TypeError,
                KeyError,
                ArithmeticError,
            ) as exc:
                update = {"error": type(exc).__name__}
            try:
                self.updates.put_nowait(update)
            except Full:
                try:
                    self.updates.get_nowait()
                except Empty:
                    pass
                self.updates.put_nowait(update)
            self.stopped.wait(5)
