"""Send deduplicated Feishu alerts for Binance Portfolio Margin snapshots."""

import argparse
import json
import logging
import os
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from typing import Any

import httpx
from redis import Redis

LOG = logging.getLogger(__name__)
ACCOUNT_KEY = "account:v1:binance:portfolio"
FUNDING_KEY = "market:v1:binance:funding"
SETTINGS_KEY = "alert:v1:settings:binance:portfolio"
STATE_KEY = "alert:v1:state:binance:portfolio"
MUTE_KEY_PREFIX = "alert:v1:mute:binance:portfolio"
MUTE_DURATION_MS = 60 * 60 * 1_000
DEFAULT_SETTINGS = {
    "schema_version": 1,
    "enabled": True,
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
}


@dataclass(frozen=True)
class Alert:
    """One currently active alert condition."""

    key: str
    title: str
    body: str
    channel: str


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _json_object(value: bytes | str | None) -> dict[str, Any] | None:
    try:
        result = json.loads(value) if value is not None else None
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    return result if isinstance(result, dict) else None


def load_settings(redis: Redis) -> dict[str, Any]:
    """Load version-one alert settings, falling back per field."""
    stored = _json_object(redis.get(SETTINGS_KEY)) or {}
    if stored.get("schema_version", 1) != 1:
        stored = {}
    return {
        **DEFAULT_SETTINGS,
        **{key: stored[key] for key in DEFAULT_SETTINGS if key in stored},
    }


def _mute_key(alert_key: str) -> str:
    return f"{MUTE_KEY_PREFIX}:{alert_key}"


def mute_alert(
    redis: Redis,
    alert_key: str,
    now_ms: int | None = None,
) -> int:
    """Mute one alert condition for one hour and return its expiry time."""
    if alert_key != "account:unimmr" and not alert_key.startswith(
        ("funding:", "funding-negative:"),
    ):
        raise ValueError("未知的报警项目")
    muted_at_ms = now_ms if now_ms is not None else int(time.time() * 1_000)
    muted_until_ms = muted_at_ms + MUTE_DURATION_MS
    redis.set(
        _mute_key(alert_key),
        json.dumps(
            {
                "schema_version": 1,
                "alert_key": alert_key,
                "muted_at_ms": muted_at_ms,
                "muted_until_ms": muted_until_ms,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        px=MUTE_DURATION_MS,
    )
    return muted_until_ms


def is_alert_muted(redis: Redis, alert_key: str, now_ms: int) -> bool:
    """Return whether one alert condition is still muted."""
    state = _json_object(redis.get(_mute_key(alert_key)))
    try:
        return bool(state and int(state.get("muted_until_ms", 0)) > now_ms)
    except (TypeError, ValueError):
        return False


def handle_card_action(
    redis: Redis,
    expected_open_id: str,
    data: Any,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Handle a Feishu card acknowledgement and build its toast response."""
    try:
        event = data.event
        operator = event.operator
        action = event.action
        if not operator or operator.open_id != expected_open_id:
            raise ValueError("只有报警接收人可以暂停提醒")
        value = action.value if action else None
        if not isinstance(value, dict) or value.get("action") != "mute_alert":
            raise ValueError("无法识别这个操作")
        alert_key = value.get("alert_key")
        if not isinstance(alert_key, str):
            raise TypeError("报警项目无效")
        mute_alert(redis, alert_key, now_ms)
        LOG.info("Muted alert %s for one hour by %s", alert_key, operator.open_id)
        return {"toast": {"type": "success", "content": "已暂停此项报警 1 小时"}}
    except (AttributeError, TypeError, ValueError) as e:
        LOG.warning("Rejected Feishu card action: %s", e)
        return {"toast": {"type": "error", "content": str(e)}}


def _route_card_frame_as_event(
    frame: Any,
    header_type: str,
    card_type: str,
    event_type: str,
) -> None:
    """Route an SDK card frame through its existing event dispatcher."""
    for header in frame.headers:
        if header.key == header_type and header.value == card_type:
            header.value = event_type
            return


def _position_side(position: dict[str, Any], amount: Decimal) -> str | None:
    side = str(position.get("positionSide", "")).upper()
    if side in ("LONG", "SHORT"):
        return side
    if amount > 0:
        return "LONG"
    if amount < 0:
        return "SHORT"
    return None


def evaluate_alerts(
    account_snapshot: dict[str, Any],
    funding_snapshot: dict[str, Any] | None,
    settings: dict[str, Any],
) -> list[Alert]:
    """Evaluate account and funding rules with exact decimal arithmetic."""
    alerts = []
    account = account_snapshot.get("account", {})
    unimmr = _decimal(account.get("uniMMR"))
    warning = _decimal(settings.get("warning_unimmr"))
    urgent = _decimal(settings.get("urgent_unimmr"))
    phone = _decimal(settings.get("phone_unimmr"))
    if (
        unimmr is not None
        and warning is not None
        and urgent is not None
        and phone is not None
        and unimmr <= warning
    ):
        channel = "normal"
        threshold = warning
        if settings.get("phone_urgent_enabled") and unimmr <= phone:
            channel = "phone"
            threshold = phone
        elif settings.get("app_urgent_enabled") and unimmr <= urgent:
            channel = "app"
            threshold = urgent
        alerts.append(
            Alert(
                key="account:unimmr",
                title="统一保证金风险",
                body=(
                    f"uniMMR={unimmr}，已低于 {threshold}。"
                    f"账户状态={account.get('accountStatus', '-')}，"
                    f"账户权益={account.get('accountEquity', '-')} USD，"
                    f"可用余额={account.get('totalAvailableBalance', '-')} USD。"
                ),
                channel=channel,
            )
        )

    if not settings.get("funding_alert_enabled") or not funding_snapshot:
        return alerts
    instruments = funding_snapshot.get("instruments")
    if not isinstance(instruments, dict):
        return alerts
    minimum_percent = _decimal(settings.get("funding_rate_percent")) or Decimal(0)
    minimum_rate = minimum_percent / Decimal(100)
    for position in account_snapshot.get("positions", []):
        if not isinstance(position, dict) or position.get("product") != "UM":
            continue
        symbol = str(position.get("symbol", ""))
        funding = instruments.get(symbol)
        if not isinstance(funding, dict):
            continue
        amount = _decimal(position.get("positionAmt"))
        rate = _decimal(funding.get("rate"))
        if amount is None or rate is None or amount.is_zero() or rate.is_zero():
            continue
        side = _position_side(position, amount)
        notional = _decimal(position.get("notional", position.get("notionalValue")))
        if notional is None:
            mark_price = _decimal(position.get("markPrice")) or Decimal(0)
            notional = amount * mark_price
        rate_percent = rate * Decimal(100)
        estimated_payment = abs(notional) * abs(rate)
        if rate < 0 and settings.get("funding_negative_phone_enabled"):
            effect = "支出" if side == "SHORT" else "收入"
            alerts.append(
                Alert(
                    key=f"funding-negative:{symbol}",
                    title="持仓币种资金费率转负",
                    body=(
                        f"{symbol} 当前资金费率={rate_percent}% ，持仓方向={side}，"
                        f"名义价值={abs(notional)} USD，按当前费率估算本次{effect}="
                        f"{estimated_payment} USD。"
                    ),
                    channel="phone",
                )
            )
            continue
        pays = (side == "LONG" and rate > 0) or (side == "SHORT" and rate < 0)
        if not pays or abs(rate) < minimum_rate:
            continue
        channel = "app" if settings.get("funding_urgent_enabled") else "normal"
        alerts.append(
            Alert(
                key=f"funding:{symbol}:{side}",
                title="资金费率变为仓位支出",
                body=(
                    f"{symbol} {side} 仓位将在当前费率方向支付资金费。"
                    f"费率={rate_percent}% ，名义价值={abs(notional)} USD，"
                    f"按当前费率估算本次支出={estimated_payment} USD。"
                ),
                channel=channel,
            )
        )
    return alerts


class FeishuClient:
    """Minimal Feishu API client for messages and in-app urgency."""

    def __init__(
        self,
        client: httpx.Client,
        app_id: str,
        app_secret: str,
        receiver_open_id: str | None = None,
        receiver_mobile: str | None = None,
        receiver_email: str | None = None,
    ) -> None:
        self._client = client
        self._app_id = app_id
        self._app_secret = app_secret
        self._receiver_open_id = receiver_open_id
        self._receiver_mobile = receiver_mobile
        self._receiver_email = receiver_email
        self._token: str | None = None
        self._token_expires_at = 0.0

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        response = self._client.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise RuntimeError(
                f"Feishu token request failed: {data.get('msg', 'unknown error')}"
            )
        self._token = data["tenant_access_token"]
        self._token_expires_at = time.monotonic() + max(
            0, int(data.get("expire", 7200)) - 60
        )
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def receiver_open_id(self) -> str:
        """Return the configured ID or resolve it once from mobile/email."""
        if self._receiver_open_id:
            return self._receiver_open_id
        body: dict[str, list[str]] = {}
        if self._receiver_mobile:
            body["mobiles"] = [self._receiver_mobile]
        if self._receiver_email:
            body["emails"] = [self._receiver_email]
        if not body:
            raise RuntimeError(
                "Set FEISHU_RECEIVER_OPEN_ID, FEISHU_RECEIVER_MOBILE, or FEISHU_RECEIVER_EMAIL"
            )
        response = self._client.post(
            "/open-apis/contact/v3/users/batch_get_id",
            params={"user_id_type": "open_id"},
            headers=self._headers(),
            json=body,
        )
        response.raise_for_status()
        data = response.json()
        users = data.get("data", {}).get("user_list", [])
        if data.get("code") != 0 or not users or not users[0].get("user_id"):
            raise RuntimeError(
                f"Feishu receiver lookup failed: {data.get('msg', 'user not found')}"
            )
        self._receiver_open_id = users[0]["user_id"]
        return self._receiver_open_id

    def send(self, alert: Alert) -> None:
        """Send an interactive card, then apply the requested urgency."""
        receiver = self.receiver_open_id()
        prefix = {"normal": "提醒", "app": "严重", "phone": "紧急"}[alert.channel]
        elements = [
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": alert.body},
            },
        ]
        if not alert.key.startswith("recovery:"):
            elements.extend(
                [
                    {"tag": "hr"},
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {
                                    "tag": "plain_text",
                                    "content": "已知晓，暂停此项 1 小时",
                                },
                                "type": "primary",
                                "value": {
                                    "action": "mute_alert",
                                    "alert_key": alert.key,
                                },
                            },
                        ],
                    },
                ],
            )
        response = self._client.post(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            headers=self._headers(),
            json={
                "receive_id": receiver,
                "msg_type": "interactive",
                "content": json.dumps(
                    {
                        "config": {"wide_screen_mode": True},
                        "header": {
                            "template": "red" if alert.channel == "phone" else "orange",
                            "title": {
                                "tag": "plain_text",
                                "content": f"【{prefix}】{alert.title}",
                            },
                        },
                        "elements": elements,
                    },
                    ensure_ascii=False,
                ),
            },
        )
        response.raise_for_status()
        data = response.json()
        message_id = data.get("data", {}).get("message_id")
        if data.get("code") != 0 or not message_id:
            raise RuntimeError(
                f"Feishu message send failed: {data.get('msg', 'unknown error')}"
            )
        if alert.channel == "normal":
            return
        self._urgent(message_id, receiver, "urgent_app")

    def _urgent(self, message_id: str, receiver: str, kind: str) -> None:
        response = self._client.patch(
            f"/open-apis/im/v1/messages/{message_id}/{kind}",
            params={"user_id_type": "open_id"},
            headers=self._headers(),
            json={"user_id_list": [receiver]},
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.is_error or data.get("code") != 0:
            raise RuntimeError(
                f"Feishu {kind} failed: HTTP {response.status_code}, "
                f"code={data.get('code', '-')}, msg={data.get('msg', 'unknown error')}"
            )


class QQEmailClient:
    """Send alert emails through QQ Mail SMTP over TLS."""

    def __init__(self, username: str, auth_code: str, recipient: str | None = None):
        self._username = username
        self._auth_code = auth_code
        self._recipient = recipient or username

    def send(self, alert: Alert) -> None:
        message = EmailMessage()
        message["Subject"] = f"【紧急】{alert.title}"
        message["From"] = self._username
        message["To"] = self._recipient
        message.set_content(alert.body)
        with smtplib.SMTP_SSL(
            "smtp.qq.com",
            465,
            timeout=10,
            context=ssl.create_default_context(),
        ) as smtp:
            smtp.login(self._username, self._auth_code)
            smtp.send_message(message)


def start_card_action_listener(
    redis: Redis,
    notifier: FeishuClient,
    app_id: str,
    app_secret: str,
) -> threading.Thread:
    """Start the Feishu long connection used by card acknowledgement buttons."""

    def run() -> None:
        try:
            import lark_oapi as lark
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )
            from lark_oapi.ws.const import HEADER_TYPE
            from lark_oapi.ws.enum import MessageType

            expected_open_id = notifier.receiver_open_id()

            def on_card_action(data: Any) -> Any:
                return P2CardActionTriggerResponse(
                    handle_card_action(redis, expected_open_id, data),
                )

            handler = (
                lark.EventDispatcherHandler.builder("", "", lark.LogLevel.ERROR)
                .register_p2_card_action_trigger(on_card_action)
                .build()
            )

            class CardActionClient(lark.ws.Client):
                async def _handle_data_frame(self, frame: Any) -> None:
                    # lark-oapi 1.7.3 drops CARD frames instead of dispatching them
                    _route_card_frame_as_event(
                        frame,
                        HEADER_TYPE,
                        MessageType.CARD.value,
                        MessageType.EVENT.value,
                    )
                    await super()._handle_data_frame(frame)

            LOG.info("Starting Feishu card-action long connection")
            CardActionClient(
                app_id,
                app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.ERROR,
            ).start()
        except Exception:
            LOG.exception("Feishu card-action long connection stopped")

    thread = threading.Thread(target=run, name="feishu-card-actions", daemon=True)
    thread.start()
    return thread


def process_alerts(
    redis: Redis,
    notifier: Any,
    alerts: list[Alert],
    settings: dict[str, Any],
    now_ms: int,
    email_notifier: Any | None = None,
) -> None:
    """Send transitions and controlled repeats, then persist delivery state."""
    state = _json_object(redis.get(STATE_KEY)) or {"schema_version": 1, "alerts": {}}
    previous = state.get("alerts")
    if not isinstance(previous, dict):
        previous = {}
    current = {alert.key: alert for alert in alerts}
    repeat_ms = int(settings.get("repeat_minutes", 30)) * 60_000
    phone_message_ms = int(settings.get("phone_message_interval_seconds", 1)) * 1_000

    for alert in alerts:
        old = previous.get(alert.key, {})
        last_sent_ms = int(old.get("last_sent_ms", 0)) if isinstance(old, dict) else 0
        last_email_attempt_ms = (
            int(old.get("last_email_attempt_ms", 0)) if isinstance(old, dict) else 0
        )
        transition = not old.get("active") or old.get("channel") != alert.channel
        if is_alert_muted(redis, alert.key, now_ms):
            previous[alert.key] = {
                "active": True,
                "channel": alert.channel,
                "title": alert.title,
                "last_sent_ms": last_sent_ms,
                "last_email_attempt_ms": last_email_attempt_ms,
            }
            redis.set(STATE_KEY, json.dumps({"schema_version": 1, "alerts": previous}))
            continue
        include_email = False
        if alert.channel == "phone":
            include_email = transition or now_ms - last_email_attempt_ms >= repeat_ms
            should_send = (
                transition or include_email or now_ms - last_sent_ms >= phone_message_ms
            )
        else:
            should_send = transition or now_ms - last_sent_ms >= repeat_ms
        if should_send:
            notifier.send(alert)
            last_sent_ms = now_ms
            if include_email and email_notifier is not None:
                try:
                    email_notifier.send(alert)
                except Exception:
                    LOG.exception("QQ email alert failed: %s", alert.title)
                last_email_attempt_ms = now_ms
        previous[alert.key] = {
            "active": True,
            "channel": alert.channel,
            "title": alert.title,
            "last_sent_ms": last_sent_ms,
            "last_email_attempt_ms": last_email_attempt_ms,
        }
        redis.set(STATE_KEY, json.dumps({"schema_version": 1, "alerts": previous}))

    for key, old in list(previous.items()):
        if key in current or not isinstance(old, dict) or not old.get("active"):
            continue
        if settings.get("recovery_enabled"):
            notifier.send(
                Alert(
                    key=f"recovery:{key}",
                    title="风险状态已恢复",
                    body=f"{old.get('title', key)} 已不再满足报警条件。",
                    channel="normal",
                )
            )
        previous[key] = {**old, "active": False, "recovered_at_ms": now_ms}
        redis.set(STATE_KEY, json.dumps({"schema_version": 1, "alerts": previous}))


def check_once(
    redis: Redis,
    notifier: Any,
    now_ms: int | None = None,
    email_notifier: Any | None = None,
) -> int:
    """Read current snapshots and process one alert evaluation cycle."""
    settings = load_settings(redis)
    if not settings.get("enabled"):
        return 0
    account = _json_object(redis.get(ACCOUNT_KEY))
    if not account or account.get("schema_version") != 1:
        LOG.warning("Portfolio account snapshot is unavailable")
        return 0
    funding = _json_object(redis.get(FUNDING_KEY))
    alerts = evaluate_alerts(account, funding, settings)
    process_alerts(
        redis,
        notifier,
        alerts,
        settings,
        now_ms or int(time.time() * 1000),
        email_notifier,
    )
    return len(alerts)


class LoggingNotifier:
    """Log notifications without contacting Feishu."""

    def send(self, alert: Alert) -> None:
        LOG.info(
            "DRY RUN [%s] %s: %s",
            alert.channel,
            alert.title,
            alert.body,
        )


def main() -> None:
    """Run the independent Redis-to-Feishu alert worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("MARKET_REDIS_URL", "redis://localhost:6379/0"),
    )
    parser.add_argument("--interval", type=int, default=1)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be positive")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    redis = Redis.from_url(args.redis_url, socket_timeout=3)
    if args.dry_run:
        notifier: Any = LoggingNotifier()
        email_notifier = None
        client = None
    else:
        app_id = os.environ.get("FEISHU_APP_ID")
        app_secret = os.environ.get("FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            parser.error("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
        client = httpx.Client(
            base_url="https://open.feishu.cn",
            timeout=10.0,
            headers={"User-Agent": "portfolio-alert/1"},
        )
        notifier = FeishuClient(
            client,
            app_id,
            app_secret,
            os.environ.get("FEISHU_RECEIVER_OPEN_ID"),
            os.environ.get("FEISHU_RECEIVER_MOBILE"),
            os.environ.get("FEISHU_RECEIVER_EMAIL"),
        )
        qq_email = os.environ.get("QQ_SMTP_EMAIL")
        qq_auth_code = os.environ.get("QQ_SMTP_AUTH_CODE")
        if bool(qq_email) != bool(qq_auth_code):
            parser.error("QQ_SMTP_EMAIL and QQ_SMTP_AUTH_CODE must be configured together")
        email_notifier = (
            QQEmailClient(
                qq_email,
                qq_auth_code,
                os.environ.get("ALERT_EMAIL_TO"),
            )
            if qq_email and qq_auth_code
            else None
        )
        if email_notifier is None:
            LOG.warning(
                "QQ email alerts are disabled; set QQ_SMTP_EMAIL and QQ_SMTP_AUTH_CODE"
            )
    try:
        redis.ping()
        if not args.dry_run and not args.once:
            start_card_action_listener(redis, notifier, app_id, app_secret)
        while True:
            try:
                active = check_once(redis, notifier, email_notifier=email_notifier)
                LOG.info("Alert check complete: %d active", active)
            except Exception:
                LOG.exception("Alert check failed")
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        if client is not None:
            client.close()
        redis.close()


if __name__ == "__main__":
    main()
