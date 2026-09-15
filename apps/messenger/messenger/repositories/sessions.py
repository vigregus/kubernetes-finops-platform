"""Сессии и устройства. Только SQL; правила — в `domain/session.py`.

Ключевое свойство всей таблицы: **она может только отнять доступ, но не
дать**. Keycloak остаётся источником истины о том, вошёл ли человек;
наша строка отвечает на другой вопрос — не отозвали ли этот вход с тех
пор. Инвариант И-1 модели авторизации именно об этом: устаревшее
состояние не расширяет права. Отсутствующая строка не считается
разрешением, а отозванная — не воскресает.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg

from messenger.domain.ids import DeviceId, SessionId, UserId
from messenger.domain.session import Device, RevocationReason, Session, SessionView


@dataclass(frozen=True, slots=True)
class RevokedSession:
    """Отозванная сессия и все её realtime-соединения."""

    session_id: SessionId
    realtime_client_ids: tuple[str, ...] = ()


def _to_device(row: asyncpg.Record) -> Device:
    return Device(
        device_id=DeviceId(row["device_id"]),
        user_id=UserId(row["user_id"]),
        user_agent=row["user_agent"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )


def _to_session(row: asyncpg.Record) -> Session:
    return Session(
        session_id=SessionId(row["session_id"]),
        user_id=UserId(row["user_id"]),
        device_id=DeviceId(row["device_id"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        revoked_reason=row["revoked_reason"],
    )


async def ensure_device(
    conn: asyncpg.Connection,
    *,
    device_id: DeviceId,
    user_id: UserId,
    user_agent: str | None,
) -> Device | None:
    """Заводит или обновляет устройство. `None` — если оно чужое.

    Идентификатор устройства приходит от клиента, то есть подобрать чужой
    можно. Поэтому условие `user_id` в `DO UPDATE` — не перестраховка:
    без него подобранный идентификатор перепривязывает чужое устройство
    к себе, и в списке «где я вошёл» у человека появляется строка,
    которой он не узнаёт, а у нападающего — сессия на чужом устройстве.

    `None` означает «возьми другой идентификатор», а не ошибку.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO devices (device_id, user_id, user_agent)
        VALUES ($1, $2, $3)
        ON CONFLICT (device_id) DO UPDATE
           SET last_seen_at = now(),
               -- Пустая строка браузера не должна затирать известную.
               user_agent = COALESCE(EXCLUDED.user_agent, devices.user_agent)
         WHERE devices.user_id = EXCLUDED.user_id
        RETURNING device_id, user_id, user_agent, created_at, last_seen_at
        """,
        device_id,
        user_id,
        user_agent,
    )
    return _to_device(row) if row else None


async def ensure_session(
    conn: asyncpg.Connection,
    *,
    session_id: SessionId,
    user_id: UserId,
    device_id: DeviceId,
    expires_at: datetime,
    external_session_id: str | None = None,
) -> Session:
    """Отражает вход Keycloak в нашей таблице. Идемпотентно по `session_id`.

    `session_id` выведен из `sid` токена детерминированно, поэтому таблицы
    соответствия не нужно. Сырой `sid` кладётся рядом — не для поиска,
    а для разбора инцидентов: без него «какая это сессия Keycloak»
    выясняется обратным перебором, то есть никак.

    Срок продлевается на каждом обращении — так же, как Keycloak двигает
    свой idle-таймаут. Это приближение, и оно намеренно сдвинуто в сторону
    строгости: наша строка может только отнять доступ, поэтому ошибка
    в меньшую сторону означает лишний вход, а не лишний доступ.

    Отозванная сессия не воскресает: условие `revoked_at IS NULL`
    оставляет отметку нетронутой, и вызывающий получает её как есть.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO sessions (session_id, user_id, device_id, expires_at,
                              external_session_id)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (session_id) DO UPDATE
           SET expires_at = EXCLUDED.expires_at,
               device_id = EXCLUDED.device_id,
               external_session_id = COALESCE(EXCLUDED.external_session_id,
                                              sessions.external_session_id)
         WHERE sessions.revoked_at IS NULL
           AND sessions.user_id = EXCLUDED.user_id
        RETURNING session_id, user_id, device_id, created_at, expires_at,
                  revoked_at, revoked_reason
        """,
        session_id,
        user_id,
        device_id,
        expires_at,
        external_session_id,
    )
    if row is not None:
        return _to_session(row)

    existing = await fetch_session(conn, session_id=session_id)
    if existing is None:
        # Строка была и исчезла между двумя запросами. Сессии физически
        # удаляются по истечении срока хранения, но не в этот миг -
        # значит, сломан инвариант, а не случилась редкая гонка.
        raise RuntimeError(f"сессия {session_id} исчезла между вставкой и чтением")
    return existing


async def fetch_session(conn: asyncpg.Connection, *, session_id: SessionId) -> Session | None:
    """Сессия по идентификатору. Отозванная возвращается тоже.

    Скрывать отозванную нельзя: «нет такой» и «отозвана» ведут к разным
    ответам, и первый заставил бы завести её заново.
    """
    row = await conn.fetchrow(
        """
        SELECT session_id, user_id, device_id, created_at, expires_at,
               revoked_at, revoked_reason
          FROM sessions
         WHERE session_id = $1
        """,
        session_id,
    )
    return _to_session(row) if row else None


async def list_active_sessions(
    conn: asyncpg.Connection, *, user_id: UserId, current: SessionId | None = None
) -> list[SessionView]:
    """Список «где я вошёл». Только действующие, свежие сверху.

    Истёкшие отсеиваются здесь, а не фоновой уборкой: строка живёт до
    физического удаления по сроку хранения, и показывать её как активную
    значит предлагать закрыть то, что и так закрыто.
    """
    rows = await conn.fetch(
        """
        SELECT s.session_id, s.device_id, d.user_agent,
               s.created_at, d.last_seen_at
          FROM sessions s
          JOIN devices d ON d.device_id = s.device_id
         WHERE s.user_id = $1
           AND s.revoked_at IS NULL
           AND s.expires_at > now()
         ORDER BY d.last_seen_at DESC
        """,
        user_id,
    )
    return [
        SessionView(
            session_id=SessionId(row["session_id"]),
            device_id=DeviceId(row["device_id"]),
            user_agent=row["user_agent"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            current=current is not None and row["session_id"] == current,
        )
        for row in rows
    ]


async def revoke_session(
    conn: asyncpg.Connection,
    *,
    session_id: SessionId,
    user_id: UserId,
    reason: RevocationReason,
) -> RevokedSession | None:
    """Отзывает одну сессию. `None` — если отзывать было нечего.

    `user_id` стоит в условии, а не проверяется до запроса: проверка
    перед изменением — это два действия, между которыми успевает
    вклиниться третье. Чужую сессию так отозвать нельзя, и попытка
    неотличима от попытки отозвать несуществующую.

    Повторный отзыв ничего не меняет: `revoked_at` остаётся временем
    первого. Иначе «выйти везде», нажатое дважды, переписывало бы причину
    и время, а аудит потерял бы момент, который расследуют.

    Идентификаторы всех соединений читаются в той же транзакции, которой
    отзывается сессия: connect-proxy использует ту же блокировку строки.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            UPDATE sessions
               SET revoked_at = now(), revoked_reason = $3
             WHERE session_id = $1
               AND user_id = $2
               AND revoked_at IS NULL
            RETURNING session_id
            """,
            session_id,
            user_id,
            reason.value,
        )
        if row is None:
            return None
        clients = await conn.fetch(
            "SELECT client_id FROM realtime_connections WHERE session_id = $1",
            session_id,
        )
        await conn.execute(
            "DELETE FROM realtime_connections WHERE session_id = $1", session_id
        )
    return RevokedSession(
        session_id=SessionId(row["session_id"]),
        realtime_client_ids=tuple(item["client_id"] for item in clients),
    )


async def set_realtime_client_id(
    conn: asyncpg.Connection,
    *,
    session_id: SessionId,
    user_id: UserId,
    client_id: str,
) -> bool:
    """Привязывает realtime-соединение к действующей и своей сессии.

    `client_id` — значение, которое Centrifugo вернул клиенту в ответе на
    подключение. Оно недоверенное: привязать его можно только к собственной
    действующей сессии, иначе подобранным идентификатором соединения
    оказалось бы то, что чужое. `False` — если сессия уже отозвана или
    чужая; отзыва это не отменяет, а просто оставляет соединение без
    возможности принудительного разрыва.
    """
    row = await conn.fetchrow(
        """
        UPDATE sessions
           SET realtime_client_id = $3
         WHERE session_id = $1
           AND user_id = $2
           AND revoked_at IS NULL
        RETURNING session_id
        """,
        session_id,
        user_id,
        client_id,
    )
    return row is not None


async def register_realtime_connection(
    conn: asyncpg.Connection,
    *,
    session_id: SessionId,
    user_id: UserId,
    client_id: str,
) -> bool:
    """Атомарно допускает соединение только для живой сессии.

    Блокировка строки согласована с отзывом в сервисе: либо соединение
    попадёт в реестр раньше отзыва и будет разорвано, либо увидит уже
    отозванную строку и Centrifugo не примет его вовсе.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT session_id
              FROM sessions
             WHERE session_id = $1
               AND user_id = $2
               AND revoked_at IS NULL
               AND expires_at > now()
             FOR UPDATE
            """,
            session_id,
            user_id,
        )
        if row is None:
            return False
        await conn.execute(
            """
            INSERT INTO realtime_connections (client_id, session_id, user_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (client_id) DO UPDATE
               SET refreshed_at = now()
             WHERE realtime_connections.session_id = EXCLUDED.session_id
               AND realtime_connections.user_id = EXCLUDED.user_id
            """,
            client_id,
            session_id,
            user_id,
        )
    return True


async def refresh_realtime_connection(
    conn: asyncpg.Connection,
    *,
    session_id: SessionId,
    user_id: UserId,
    client_id: str,
) -> bool:
    """Продлевает только зарегистрированное соединение живой сессии."""
    row = await conn.fetchrow(
        """
        UPDATE realtime_connections AS rc
           SET refreshed_at = now()
          FROM sessions AS s
         WHERE rc.client_id = $1
           AND rc.session_id = $2
           AND rc.user_id = $3
           AND s.session_id = rc.session_id
           AND s.user_id = rc.user_id
           AND s.revoked_at IS NULL
           AND s.expires_at > now()
        RETURNING rc.client_id
        """,
        client_id,
        session_id,
        user_id,
    )
    return row is not None


async def revoke_user_sessions(
    conn: asyncpg.Connection, *, user_id: UserId, reason: RevocationReason
) -> list[RevokedSession]:
    """Отзывает все действующие сессии пользователя и возвращает их список.

    Список, а не число: «выйти везде» обязано породить событие и разрыв на
    каждую закрытую сессию, и сравнить доставленное с тем, что реально было
    отозвано, можно только зная сами идентификаторы. Число — `len(списка)` —
    берёт вызывающий.
    """
    async with conn.transaction():
        rows = await conn.fetch(
            """
            UPDATE sessions
               SET revoked_at = now(), revoked_reason = $2
             WHERE user_id = $1
               AND revoked_at IS NULL
            RETURNING session_id
            """,
            user_id,
            reason.value,
        )
        if not rows:
            return []
        session_ids = [SessionId(row["session_id"]) for row in rows]
        clients = await conn.fetch(
            """
            SELECT session_id, client_id
              FROM realtime_connections
             WHERE session_id = ANY($1::uuid[])
            """,
            session_ids,
        )
        await conn.execute(
            "DELETE FROM realtime_connections WHERE session_id = ANY($1::uuid[])",
            session_ids,
        )
    by_session: dict[SessionId, list[str]] = {sid: [] for sid in session_ids}
    for item in clients:
        by_session[SessionId(item["session_id"])].append(item["client_id"])
    return [
        RevokedSession(session_id=sid, realtime_client_ids=tuple(by_session[sid]))
        for sid in session_ids
    ]
