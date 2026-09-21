"""Хранение профилей. Только SQL; правила — в `domain/user.py`.

Ключ узнавания — `external_id` из токена Keycloak. Адрес почты ключом быть
не может: он меняется, и привязка к нему однажды разъедет учётную запись
с её перепиской.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

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


async def fetch_last_seen_at(
    conn: asyncpg.Connection, *, user_ids: Sequence[UserId]
) -> dict[UserId, datetime]:
    """Отметки «был в сети» на пачку пользователей. Отсутствие ключа — «ни разу».

    Пачкой, а не по одному: вызывающий — маршрут создания беседы, где
    участников двое, но правило чтения то же, что у списка, и второй способ
    читать те же отметки разошёлся бы с первым.

    Отметка **не** подставляется умолчанием: пользователь, ни разу не
    подтвердивший соединение, в карту не попадает вовсе. `NULL` в ответе и
    отсутствие ключа означали бы разное — «неизвестно с какого времени»
    против «не был», — и свести их здесь значило бы выдумать факт.

    Пустой список — пустая карта без похода в базу: `= ANY('{}')` вернул бы
    то же самое, но за соединение из пула, а вызов с пустым списком
    законен (беседа, из которой все вышли).
    """
    if not user_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT user_id, last_seen_at
          FROM users
         WHERE user_id = ANY($1::uuid[])
           AND last_seen_at IS NOT NULL
        """,
        list(user_ids),
    )
    return {UserId(row["user_id"]): row["last_seen_at"] for row in rows}


async def count_users(conn: asyncpg.Connection) -> int:
    """Сколько учётных записей заведено. Знаменатель доли онлайн.

    Надгробия не считаются: у стёртого профиля соединений быть не может
    (стирание отзывает сессии), и держать его в знаменателе значило бы
    занижать долю на каждого, кто ушёл из системы навсегда.
    """
    row = await conn.fetchrow("SELECT count(*) AS total FROM users WHERE deleted_at IS NULL")
    return int(row["total"]) if row is not None else 0


async def touch_last_seen(conn: asyncpg.Connection, *, user_id: UserId) -> None:
    """Отмечает подтверждённую жизнь пользователя. Монотонно.

    Вызывается в той же транзакции, что и upsert соединения
    (`services/realtime.py`), поэтому отметка — это в точности время
    того продления, которое её вызвало: `now()` в Postgres постоянен
    внутри транзакции, и оба значения берутся из одного источника.

    `GREATEST`, а не присваивание: два устройства одного человека
    продлеваются независимо, и запись «как есть» позволила бы более
    старой транзакции затереть более новую отметку. `NULL` в `GREATEST`
    игнорируется — «ни разу не был» становится первым подтверждением
    без отдельной ветки.

    Надгробие отметку не получает: стирание не должно оставлять в базе
    следов активности человека, которого в системе больше нет. Условие
    стоит в самом запросе, а не проверкой до него: проверка перед
    изменением — это два действия, между которыми успевает вклиниться
    стирание.
    """
    await conn.execute(
        """
        UPDATE users
           SET last_seen_at = GREATEST(last_seen_at, now())
         WHERE user_id = $1
           AND deleted_at IS NULL
        """,
        user_id,
    )


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
