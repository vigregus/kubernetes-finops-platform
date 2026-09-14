"""Хранение профилей. Только SQL; правила — в `domain/user.py`.

Ключ узнавания — `external_id` из токена Keycloak. Адрес почты ключом быть
не может: он меняется, и привязка к нему однажды разъедет учётную запись
с её перепиской.
"""
from __future__ import annotations

import uuid

import asyncpg

from messenger.domain.ids import UserId
from messenger.domain.user import (
    EnsureUserResult,
    User,
    normalize_display_name,
    normalize_email,
)

# Список полей выписан явно, а не `SELECT *`: добавленная колонка не должна
# молча менять форму того, что читает код, а порядок в `*` вообще не обещан.
#
# И выписан он в каждом запросе целиком, а не подставляется из константы.
# Подстановка потребовала бы f-строки в SQL, а линтер отклоняет их все
# без разбора — и правильно делает: правило, обойдённое один раз «здесь же
# безопасно», обходится вторым разом уже с чужим значением внутри.


def _to_user(row: asyncpg.Record) -> User:
    return User(
        user_id=UserId(row["user_id"]),
        external_id=row["external_id"],
        display_name=row["display_name"],
        email=row["email"],
        email_verified=row["email_verified"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


async def fetch_user(conn: asyncpg.Connection, *, user_id: UserId) -> User | None:
    """Профиль по внутреннему идентификатору. Надгробие тоже возвращается.

    Отфильтровать удалённых здесь значило бы отдать `None` там, где
    пользователь есть, и вызывающий принял бы это за «не найден» — а разница
    между «нет такого» и «удалён» решает, показывать ли его прошлые
    сообщения как сообщения постороннего.
    """
    row = await conn.fetchrow(
        """
        SELECT user_id, external_id, display_name, email, email_verified,
               created_at, updated_at, deleted_at
          FROM users
         WHERE user_id = $1
        """,
        user_id,
    )
    return _to_user(row) if row else None


async def fetch_user_by_external_id(
    conn: asyncpg.Connection, *, external_id: str
) -> User | None:
    """Профиль по идентификатору из Keycloak."""
    row = await conn.fetchrow(
        """
        SELECT user_id, external_id, display_name, email, email_verified,
               created_at, updated_at, deleted_at
          FROM users
         WHERE external_id = $1
        """,
        external_id,
    )
    return _to_user(row) if row else None


async def ensure_user(
    conn: asyncpg.Connection,
    *,
    external_id: str,
    display_name: str | None,
    email: str | None,
    email_verified: bool = False,
) -> EnsureUserResult:
    """Приводит профиль к состоянию из токена. Идемпотентно по external_id.

    Один запрос, а не «сначала SELECT, потом INSERT»: между ними успевает
    вклиниться второй вход того же человека с другого устройства, и в базе
    оказываются две учётные записи на один `external_id` — точнее, вторая
    падает на уникальном индексе, и вход просто не работает.

    `xmax = 0` отличает вставку от обновления: у строки, вставленной этой же
    транзакцией, идентификатор удаляющей транзакции пуст. Способ опирается
    на внутреннее поле Postgres и потому объяснён здесь — снаружи он
    выглядит как магия.

    Удалённая учётная запись повторным входом не воскресает: условие
    `deleted_at IS NULL` оставляет надгробие нетронутым, и вызывающий
    получает его как есть. Решение о том, пускать ли такого человека,
    принимает сервисный слой — репозиторий не знает правил доступа.
    """
    name = normalize_display_name(display_name)
    address = normalize_email(email)

    row = await conn.fetchrow(
        """
        INSERT INTO users (user_id, external_id, display_name, email, email_verified)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (external_id) DO UPDATE
           SET display_name   = EXCLUDED.display_name,
               email          = EXCLUDED.email,
               email_verified = EXCLUDED.email_verified,
               updated_at     = now()
         WHERE users.deleted_at IS NULL
           AND (users.display_name, users.email, users.email_verified)
               IS DISTINCT FROM
               (EXCLUDED.display_name, EXCLUDED.email, EXCLUDED.email_verified)
        RETURNING user_id, external_id, display_name, email, email_verified,
                  created_at, updated_at, deleted_at, (xmax = 0) AS inserted
        """,
        uuid.uuid4(),
        external_id,
        name,
        address,
        email_verified,
    )

    if row is not None:
        return EnsureUserResult(
            user=_to_user(row), created=row["inserted"], updated=not row["inserted"]
        )

    # Пусто — значит, конфликт был, а обновлять нечего: профиль совпадает
    # с токеном либо учётная запись удалена. `DO UPDATE ... WHERE` в этом
    # случае строку не возвращает, и вторым запросом приходится её прочитать.
    existing = await fetch_user_by_external_id(conn, external_id=external_id)
    if existing is None:
        # Строка была и исчезла между двумя запросами. Физического удаления
        # в системе нет, поэтому это не «редкая гонка», а сломанный
        # инвариант — и молчать о нём нельзя.
        raise RuntimeError(f"профиль {external_id} исчез между вставкой и чтением")
    return EnsureUserResult(user=existing, created=False, updated=False)
