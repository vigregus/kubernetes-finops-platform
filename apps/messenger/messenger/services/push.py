"""Web Push: подписки устройства и уведомление получателей сообщения.

Уведомление — **сигнал, а не второй путь доставки**: сообщение человек видит
в приложении, а push лишь зовёт открыть его (`NTF-001`). Поэтому устройству
с живым соединением оно не отправляется вовсе (`NTF-003`), а потерянное
уведомление не повторяется: оно best-effort, как и «печатает».

Сервис не пишет в журнал и не ходит в сеть сам — отправитель приходит
параметром (правило слоя), исход возвращается значением.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import asyncpg

from messenger.adapters.webpush import PushResult
from messenger.domain import push as domain
from messenger.domain.ids import DeviceId, UserId
from messenger.domain.presence import ONLINE_WINDOW_SECONDS
from messenger.repositories import push as repo
from messenger.telemetry import metrics

# Сколько провайдер хранит недоставленное уведомление, секунды. Сутки: письмо
# об устаревшем сообщении через неделю — шум, а не помощь.
PUSH_TTL_SECONDS = 24 * 3600


class Sender(Protocol):
    async def send(
        self,
        subscription: domain.PushSubscription,
        payload: dict[str, object],
        *,
        ttl: int,
        topic: str,
    ) -> PushResult: ...


def enabled() -> bool:
    """Аварийный выключатель Web Push. Выключается без потери сообщений.

    Как и у «печатает», пока из окружения и требует перезапуска — зазор
    `CFG-001` закрывает хранилище флагов (G5).
    """
    return os.getenv("PUSH_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def vapid_public_key() -> str | None:
    """Открытый ключ VAPID для браузера. API закрытого ключа не получает."""
    return os.getenv("VAPID_PUBLIC_KEY") or None


def hosts() -> tuple[str, ...]:
    return domain.allowed_hosts(os.getenv("PUSH_ALLOWED_HOSTS", ""))


@dataclass(frozen=True, slots=True)
class SubscribeResult:
    ok: bool = False
    # `invalid` — подписке нельзя верить; `unavailable` — возможность выключена
    # или не настроена; `no_device` — у входа нет устройства.
    reason: str | None = None


async def subscribe(
    conn: asyncpg.Connection, *, device_id: DeviceId, user_id: UserId, data: object
) -> SubscribeResult:
    if not enabled() or vapid_public_key() is None:
        return SubscribeResult(reason="unavailable")
    parsed = domain.parse_subscription(data, hosts())
    if parsed is None:
        return SubscribeResult(reason="invalid")
    stored = await repo.set_subscription(
        conn, device_id=device_id, user_id=user_id, subscription=parsed.to_json()
    )
    return SubscribeResult(ok=stored, reason=None if stored else "no_device")


async def unsubscribe(
    conn: asyncpg.Connection, *, device_id: DeviceId, user_id: UserId
) -> None:
    await repo.clear_subscription(conn, device_id=device_id, user_id=user_id)


@dataclass(frozen=True, slots=True)
class NotifyOutcome:
    sent: int = 0
    skipped_online: int = 0
    gone: int = 0
    rejected: int = 0
    retry: int = 0
    invalid: int = 0
    ignored: bool = False


async def notify_message(
    conn: asyncpg.Connection, *, sender: Sender, body: dict[str, Any]
) -> NotifyOutcome:
    """Одно событие `message.created` → уведомления устройствам-получателям."""
    if not enabled():
        return NotifyOutcome(ignored=True)
    conversation_id = body.get("conversation_id")
    recipients = body.get("recipient_ids")
    if (
        body.get("event_type") != "message.created"
        or not isinstance(conversation_id, str)
        or not isinstance(recipients, list)
        or not recipients
    ):
        return NotifyOutcome(ignored=True)

    try:
        user_ids = [UserId(uuid.UUID(str(value))) for value in recipients]
    except ValueError:
        return NotifyOutcome(ignored=True)

    targets = await repo.list_targets(
        conn, user_ids=user_ids, online_window=timedelta(seconds=ONLINE_WINDOW_SECONDS)
    )
    payload = domain.notification_payload(conversation_id=conversation_id)
    topic = domain.collapse_topic(conversation_id)
    allowed = hosts()

    sent = skipped = gone = rejected = retry = invalid = 0
    for target in targets:
        if not domain.should_notify(has_subscription=True, online=target.online):
            skipped += 1
            metrics.push_sent("skipped_online")
            continue
        subscription = domain.parse_subscription(target.subscription, allowed)
        if subscription is None:
            # Хранимая подписка перестала проходить правила (список провайдеров
            # сузили): она не годится и снимается, а не отправляется.
            invalid += 1
            await repo.clear_if_endpoint(
                conn, device_id=target.device_id,
                endpoint=str(target.subscription.get("endpoint", "")),
            )
            metrics.push_sent("invalid")
            continue
        result = await sender.send(subscription, payload, ttl=PUSH_TTL_SECONDS, topic=topic)
        metrics.push_sent(result.value)
        if result is PushResult.DELIVERED:
            sent += 1
        elif result is PushResult.GONE:
            gone += 1
            # `NTF-002`: подписка отозвана браузером — удаляется, повторов нет.
            await repo.clear_if_endpoint(
                conn, device_id=target.device_id, endpoint=subscription.endpoint
            )
        elif result is PushResult.REJECTED:
            rejected += 1
        else:
            retry += 1
    return NotifyOutcome(
        sent=sent, skipped_online=skipped, gone=gone, rejected=rejected, retry=retry,
        invalid=invalid,
    )

