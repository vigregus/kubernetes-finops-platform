"""Присутствие: один такт уборки и агрегат по нему.

Сервис, а не воркер, по тому же правилу, что и везде: цикл, сигналы и
метрики живут в `workers/`, а порядок шагов и транзакция — здесь. Такт
целиком помещается в одну транзакцию, и это не про атомарность записи —
запись тут одна, — а про **одну границу окна**: `now()` в Postgres
постоянен внутри транзакции, поэтому уборка, счёт онлайн и счёт учётных
записей говорят об одном моменте времени. Разные моменты дали бы долю,
собранную из двух состояний системы, — например, ноль в числителе при
знаменателе, посчитанном до уборки.

Порядок шагов: сначала уборка, потом счёт. Обратный порядок дал бы
`online` по строкам, которые тут же удаляются, то есть число, заведомо
большее правды, — и заметно это было бы только на границе окна.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import asyncpg

from messenger.domain import presence
from messenger.repositories import sessions, users


@dataclass(frozen=True, slots=True)
class SweepResult:
    """Что вышло из одного такта.

    `registered_users` возвращается наружу, хотя метрика строится на доле:
    доля без знаменателя необъяснима. Оператор, увидевший провал доли,
    обязан отличить «люди ушли» от «учётных записей стало больше» —
    на одном числе эти случаи неразличимы.
    """

    # Сколько протухших соединений убрано. Ноль — законный ответ: уборщик
    # идёт чаще, чем истекает окно, и на спокойной системе удалять нечего.
    deleted: int = 0
    online_users: int = 0
    registered_users: int = 0

    @property
    def share(self) -> float:
        return presence.online_share(
            online=self.online_users, registered=self.registered_users
        )


async def sweep(conn: asyncpg.Connection) -> SweepResult:
    """Один такт: убрать протухшее, посчитать живое.

    Окно берётся из домена и передаётся в репозитории длительностью:
    границу считает Postgres (`now() - $1::interval`), и часов пода
    в этом вычислении нет вовсе.
    """
    window = timedelta(seconds=presence.ONLINE_WINDOW_SECONDS)
    async with conn.transaction():
        deleted = await sessions.delete_stale_realtime_connections(conn, window=window)
        online_users = await sessions.count_online_users(conn, window=window)
        registered_users = await users.count_users(conn)
    return SweepResult(
        deleted=deleted,
        online_users=online_users,
        registered_users=registered_users,
    )
