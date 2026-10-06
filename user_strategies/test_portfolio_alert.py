import json
from decimal import Decimal
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from user_strategies.portfolio_alert import (
    MUTE_DURATION_MS,
    STATE_KEY,
    Alert,
    FeishuClient,
    QQEmailClient,
    _route_card_frame_as_event,
    evaluate_alerts,
    handle_card_action,
    is_alert_muted,
    mute_alert,
    process_alerts,
)


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **_kwargs):
        self.values[key] = value


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


class FakeEmailNotifier:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def settings(**overrides):
    return {
        "warning_unimmr": "1.50",
        "urgent_unimmr": "1.20",
        "phone_unimmr": "1.10",
        "app_urgent_enabled": True,
        "phone_urgent_enabled": True,
        "funding_alert_enabled": True,
        "funding_urgent_enabled": True,
        "funding_negative_phone_enabled": True,
        "funding_rate_percent": "0",
        "phone_message_interval_seconds": 1,
        "repeat_minutes": 30,
        "recovery_enabled": True,
        **overrides,
    }


def account(unimmr="2", amount="-10", notional="-1000"):
    return {
        "schema_version": 1,
        "account": {
            "uniMMR": unimmr,
            "accountStatus": "NORMAL",
            "accountEquity": "1000.123456789",
            "totalAvailableBalance": "500.987654321",
        },
        "positions": [
            {
                "product": "UM",
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "positionAmt": amount,
                "notional": notional,
            }
        ],
    }


def funding(rate):
    return {
        "schema_version": 1,
        "instruments": {"BTCUSDT": {"rate": rate}},
    }


def test_unimmr_selects_phone_channel_at_lowest_threshold():
    alerts = evaluate_alerts(account("1.09"), None, settings())

    assert alerts == [
        Alert(
            key="account:unimmr",
            title="统一保证金风险",
            body=(
                "uniMMR=1.09，已低于 1.10。账户状态=NORMAL，"
                "账户权益=1000.123456789 USD，可用余额=500.987654321 USD。"
            ),
            channel="phone",
        )
    ]


def test_negative_funding_uses_phone_for_held_symbol():
    negative = evaluate_alerts(account(), funding("-0.0002"), settings())
    positive = evaluate_alerts(account(), funding("0.0002"), settings())

    assert len(negative) == 1
    assert negative[0].key == "funding-negative:BTCUSDT"
    assert negative[0].channel == "phone"
    assert "0.0200%" in negative[0].body
    assert Decimal(negative[0].body.split("本次支出=")[1].split(" USD")[0]) == Decimal(
        "0.2"
    )
    assert positive == []


def test_funding_threshold_is_expressed_as_percent():
    below = evaluate_alerts(
        account(amount="10", notional="1000"),
        funding("0.00009"),
        settings(funding_rate_percent="0.01"),
    )
    at_threshold = evaluate_alerts(
        account(amount="10", notional="1000"),
        funding("0.0001"),
        settings(funding_rate_percent="0.01"),
    )

    assert below == []
    assert len(at_threshold) == 1


def test_alerts_are_deduplicated_repeated_and_recovered():
    redis = FakeRedis()
    notifier = FakeNotifier()
    alert = Alert("account:unimmr", "统一保证金风险", "body", "app")

    process_alerts(redis, notifier, [alert], settings(), 1_000)
    process_alerts(redis, notifier, [alert], settings(), 2_000)
    process_alerts(redis, notifier, [alert], settings(), 1_801_000)
    process_alerts(redis, notifier, [], settings(), 1_802_000)

    assert [item.channel for item in notifier.sent] == ["app", "app", "normal"]
    state = json.loads(redis.values[STATE_KEY])
    assert state["alerts"]["account:unimmr"]["active"] is False


def test_critical_alert_messages_repeat_each_second_but_email_uses_repeat_interval():
    redis = FakeRedis()
    notifier = FakeNotifier()
    email_notifier = FakeEmailNotifier()
    alert = Alert("account:unimmr", "统一保证金风险", "body", "phone")
    current_settings = settings(repeat_minutes=1)

    process_alerts(redis, notifier, [alert], current_settings, 1_000, email_notifier)
    process_alerts(redis, notifier, [alert], current_settings, 1_500, email_notifier)
    process_alerts(redis, notifier, [alert], current_settings, 2_000, email_notifier)
    process_alerts(redis, notifier, [alert], current_settings, 61_000, email_notifier)

    assert [item.channel for item in notifier.sent] == ["phone", "phone", "phone"]
    assert email_notifier.sent == [alert, alert]


def test_muted_alert_stops_messages_and_email_until_one_hour_expires():
    redis = FakeRedis()
    notifier = FakeNotifier()
    email_notifier = FakeEmailNotifier()
    alert = Alert("account:unimmr", "统一保证金风险", "body", "phone")

    mute_alert(redis, alert.key, now_ms=1_000)
    process_alerts(
        redis, notifier, [alert], settings(repeat_minutes=1), 2_000, email_notifier
    )
    process_alerts(
        redis,
        notifier,
        [alert],
        settings(repeat_minutes=1),
        1_000 + MUTE_DURATION_MS,
        email_notifier,
    )

    assert notifier.sent == [alert]
    assert email_notifier.sent == [alert]
    assert not is_alert_muted(redis, alert.key, 1_000 + MUTE_DURATION_MS)


def test_card_action_mutes_only_the_selected_alert_for_one_hour():
    redis = FakeRedis()
    data = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="open-id"),
            action=SimpleNamespace(
                value={"action": "mute_alert", "alert_key": "account:unimmr"},
            ),
        ),
    )

    response = handle_card_action(redis, "open-id", data, now_ms=1_000)

    assert response == {
        "toast": {"type": "success", "content": "已暂停此项报警 1 小时"},
    }
    assert is_alert_muted(redis, "account:unimmr", 1_000 + MUTE_DURATION_MS - 1)
    assert not is_alert_muted(redis, "funding:BTCUSDT:SHORT", 2_000)


def test_card_action_rejects_another_operator():
    redis = FakeRedis()
    data = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="other-open-id"),
            action=SimpleNamespace(
                value={"action": "mute_alert", "alert_key": "account:unimmr"},
            ),
        ),
    )

    response = handle_card_action(redis, "open-id", data, now_ms=1_000)

    assert response["toast"]["type"] == "error"
    assert not is_alert_muted(redis, "account:unimmr", 2_000)


def test_card_frame_uses_the_sdk_event_dispatch_path():
    type_header = SimpleNamespace(key="type", value="card")
    frame = SimpleNamespace(
        headers=[SimpleNamespace(key="trace", value="1"), type_header],
    )

    _route_card_frame_as_event(frame, "type", "card", "event")

    assert type_header.value == "event"


def test_critical_alert_sends_message_and_app_urgent_without_phone_urgent():
    calls = []
    sent_message = None

    def handler(request):
        nonlocal sent_message
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "token",
                    "expire": 7200,
                },
            )
        if request.url.path == "/open-apis/im/v1/messages":
            sent_message = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"message_id": "message-id"},
                },
            )
        return httpx.Response(200, json={"code": 0})

    with httpx.Client(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as client:
        notifier = FeishuClient(client, "app-id", "secret", receiver_open_id="open-id")
        notifier.send(Alert("key", "title", "body", "phone"))

    assert calls == [
        ("POST", "/open-apis/auth/v3/tenant_access_token/internal"),
        ("POST", "/open-apis/im/v1/messages"),
        ("PATCH", "/open-apis/im/v1/messages/message-id/urgent_app"),
    ]
    assert sent_message["msg_type"] == "interactive"
    card = json.loads(sent_message["content"])
    button = card["elements"][-1]["actions"][0]
    assert button["value"] == {"action": "mute_alert", "alert_key": "key"}


def test_qq_email_uses_tls_authorization_code_and_recipient():
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    with patch("user_strategies.portfolio_alert.smtplib.SMTP_SSL", return_value=smtp):
        notifier = QQEmailClient("sender@qq.com", "auth-code", "receiver@qq.com")
        notifier.send(Alert("key", "title", "body", "phone"))

    smtp.login.assert_called_once_with("sender@qq.com", "auth-code")
    message = smtp.send_message.call_args.args[0]
    assert isinstance(message, EmailMessage)
    assert message["To"] == "receiver@qq.com"
    assert "title" in message["Subject"]
    assert "body" in message.get_content()
