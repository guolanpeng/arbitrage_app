"""Test monitoring without using real credentials, accounts or database writes."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

import httpx
import pytest

from user_strategies.basis_account_monitor import DAY_MS, AccountReader, collect_once


@pytest.fixture(autouse=True)
def local_adapter_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("BASIS_ADAPTERS", "BASIS_SPOT_PROFILE", "BASIS_PERP_PROFILE"):
        monkeypatch.delenv(name, raising=False)


def income(transaction_id: str, at: int = 1000) -> dict[str, object]:
    return {
        "tranId": transaction_id,
        "asset": "USDT",
        "symbol": "BTCUSDT",
        "incomeType": "FUNDING_FEE",
        "income": "0.123456789",
        "time": at,
    }


def test_signed_account_requests_are_get_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GATE_API_KEY", "BINANCE_API_KEY"):
        monkeypatch.setenv(name, "test-key")
    for name in ("GATE_API_SECRET", "BINANCE_API_SECRET"):
        monkeypatch.setenv(name, "test-secret")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        if request.url.host == "api.gateio.ws":
            expected = hmac.new(
                b"test-secret",
                f"GET\n/api/v4/spot/accounts\n\n{hashlib.sha512(b'').hexdigest()}\n123".encode(),
                hashlib.sha512,
            ).hexdigest()
            assert request.headers["SIGN"] == expected
        else:
            query, signature = str(request.url).split("?", 1)[1].split("&signature=")
            assert (
                signature
                == hmac.new(b"test-secret", query.encode(), hashlib.sha256).hexdigest()
            )
            assert parse_qs(query)["recvWindow"] == ["5000"]
        return httpx.Response(200, json=[])

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        patch("user_strategies.basis_account_monitor.time.time", return_value=123),
        patch(
            "user_strategies.basis_account_monitor.time.time_ns",
            return_value=123000000000,
        ),
    ):
        reader = AccountReader(client)
        assert reader.gate_balances() == []
        assert reader.binance_get("/papi/v1/um/positionRisk", {}) == []
    assert len(requests) == 2


def test_funding_history_fetches_all_pages_and_deduplicates_overlap() -> None:
    reader = AccountReader(Mock())
    reader.binance_get = Mock(
        side_effect=[
            [income(str(index)) for index in range(1000)],
            [income("999"), income("1000")],
        ]
    )
    result = reader.funding_history("BTCUSDT", 0, 2000)
    assert len(result) == 1001
    assert reader.binance_get.call_args_list[1].args[1]["page"] == 2


def test_funding_history_rejects_nonadvancing_pagination() -> None:
    reader = AccountReader(Mock())
    reader.binance_get = Mock(
        return_value=[income(str(index)) for index in range(1000)]
    )
    with pytest.raises(ValueError, match="did not advance"):
        reader.funding_history("BTCUSDT", 0, 2000)


def test_funding_windows_cover_boundaries_without_gaps() -> None:
    reader = AccountReader(Mock())
    reader.binance_get = Mock(return_value=[])
    reader.funding_history("BTCUSDT", 0, 8 * DAY_MS)
    first, second = [call.args[1] for call in reader.binance_get.call_args_list]
    assert second["startTime"] == first["endTime"] + 1
    assert second["endTime"] == 8 * DAY_MS


def test_no_instances_does_not_contact_exchange_accounts() -> None:
    reader, redis = Mock(), Mock()
    with (
        patch("user_strategies.basis_account_monitor.database_connection") as connect,
        patch(
            "user_strategies.basis_account_monitor.load_records",
            return_value={},
        ),
    ):
        connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
        assert collect_once(reader, redis) == 0
    reader.gate_balances.assert_not_called()
    reader.binance_get.assert_not_called()
    redis.mget.assert_not_called()


def test_first_income_failure_can_retry_without_claiming_zero_income() -> None:
    now = 2000
    instance = {
        "strategy_id": "one",
        "trader_id": "DYNAMIC-BASIS-001",
        "perp_id": "BTCUSDT-PERP.BINANCE",
        "spot_id": "BTCUSDT.GATE",
        "state": "holding",
        "started_at_ms": 1000,
    }
    previous = {
        "basis:account:v1:one": {"income_checked_at_ms": None, "income_complete": False}
    }
    reader = SimpleNamespace(
        gate_balances=Mock(return_value=[]),
        gate_open_orders=Mock(return_value=[]),
        binance_get=Mock(return_value=[]),
        funding_history=Mock(side_effect=ValueError("offline")),
    )
    redis = Mock()
    redis.mget.return_value = [None, None]
    with (
        patch("user_strategies.basis_account_monitor.database_connection") as connect,
        patch(
            "user_strategies.basis_account_monitor.load_records",
            return_value=previous,
        ),
        patch("user_strategies.basis_account_monitor.write_record") as write,
        patch(
            "user_strategies.basis_account_monitor.time.time_ns",
            return_value=now * 1000000,
        ),
    ):
        connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [
            {"snapshot": instance}
        ]
        assert collect_once(reader, redis) == 1
    snapshot = write.call_args_list[0].args[2]
    assert snapshot["income_complete"] is False
    assert snapshot["income_checked_at_ms"] is None
    assert snapshot["income_error"]
    assert reader.funding_history.call_args.args == ("BTCUSDT", 1000, 2000)
    assert json.dumps(snapshot)
