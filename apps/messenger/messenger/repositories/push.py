"""Подписки Web Push на устройствах; транзакцией владеет сервис."""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import asyncpg

from messenger.domain.ids import DeviceId, UserId


@dataclass(frozen=True, slots=True)
class PushTarget:
    device_id: DeviceId
    user_id: UserId
    subscription: dict[str, Any]
    # Есть живое соединение: приложение на этом устройстве открыто.
    online: bool


async def set_subscription(
    conn: asyncpg.Connection,
    *,
    device_id: DeviceId,
    user_id: UserId,
    subscription: dict[str, object],
) -> bool:
    """Записывает подписку **своего** устройства. `False` — устройство не найдено."""
    result = await conn.execute(
        """
        UPDATE devices SET push_subscription = $3::jsonb
         WHERE device_id = $1 AND user_id = $2
        """,
        device_id,
        user_id,
        json.dumps(subscription),
    )
    return result.endswith(" 1")


async def clear_subscription(
    conn: asyncpg.Connection, *, device_id: DeviceId, user_id: UserId
) -> bool:
    result = await conn.execute(
        """
        UPDATE devices SET push_subscription = NULL
         WHERE device_id = $1 AND user_id = $2 AND push_subscription IS NOT NULL
        """,
        device_id,
        user_id,
    )
    return result.endswith(" 1")


async def clear_if_endpoint(
    conn: asyncpg.Connection, *, device_id: DeviceId, endpoint: str
) -> bool:
    """Снимает подписку, только если на устройстве всё ещё **этот** адрес.

    Провайдер ответил «подписки нет» на отправку, а за это время браузер мог
    успеть оформить новую: безусловное снятие стёрло бы живую подписку
    (`NTF-007`).
    """
    result = await conn.execute(
        """
        UPDATE devices SET push_subscription = NULL
         WHERE device_id = $1 AND push_subscription ->> 'endpoint' = $2
        """,
        device_id,
        endpoint,
    )
    return result.endswith(" 1")


async def list_targets(
    conn: asyncpg.Connection, *, user_ids: Sequence[UserId], online_window: timedelta
) -> list[PushTarget]:
    """Устройства получателей с подпиской и действующим входом.

    «В сети» считается по **устройству**, а не по пользователю: у человека
    открыт ноутбук, а телефон спит — на телефон уведомление идёт (`NTF-004`).
    Окно — то же, что у присутствия: соединение живо, пока `refreshed_at` в нём.
    """
    rows = await conn.fetch(
        """
        SELECT d.device_id, d.user_id, d.push_subscription,
               EXISTS (
                   SELECT 1
                     FROM realtime_connections rc
                     JOIN sessions s ON s.session_id = rc.session_id
                    WHERE s.device_id = d.device_id
                      AND rc.refreshed_at >= now() - $2::interval
               ) AS online
          FROM devices d
         WHERE d.user_id = ANY($1::uuid[])
           AND d.push_subscription IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM sessions s
                WHERE s.device_id = d.device_id
                  AND s.revoked_at IS NULL AND s.expires_at > now()
           )
        """,
        list(user_ids),
        online_window,
    )
    return [
        PushTarget(
            device_id=DeviceId(row["device_id"]),
            user_id=UserId(row["user_id"]),
            subscription=json.loads(row["push_subscription"])
            if isinstance(row["push_subscription"], str)
            else dict(row["push_subscription"]),
            online=row["online"],
        )
        for row in rows
    ]
