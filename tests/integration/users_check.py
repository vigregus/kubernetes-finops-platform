"""Проверка репозитория профилей на настоящей базе.

Модульный уровень не ходит в сеть, поэтому SQL там не проверяется вовсе —
а именно в нём живут ошибки, которых не видно в коде: `ON CONFLICT`
по не тому индексу, `xmax = 0` под PgBouncer, нормализация, разошедшаяся
с частичным уникальным индексом. Здесь всё это выполняется в базе.

Путь тот же, каким пойдёт рабочий запрос: через PgBouncer в режиме
transaction, от роли приложения. Проверка на прямом соединении с primary
доказывала бы работу другого пути.

Всё происходит в одной транзакции, которая откатывается: настоящая база
после проверки обязана остаться такой же, какой была.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import asyncpg

from messenger.repositories import users
from messenger.repositories.postgres import PoolSettings, create_pool

failures: list[str] = []


def ok(what: str) -> None:
    print(f"  \033[32m✓\033[0m {what}")


def bad(what: str, detail: str = "") -> None:
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


def check(what: str, condition: bool, detail: str = "") -> None:
    ok(what) if condition else bad(what, detail)


def settings() -> PoolSettings:
    return PoolSettings(
        host=os.getenv("DATABASE_HOST", "messenger-db-pool"),
        port=int(os.getenv("DATABASE_PORT", "5432")),
        database=os.getenv("DATABASE_NAME", "messenger"),
        user=os.getenv("DATABASE_USER", "messenger"),
        password=os.getenv("DATABASE_PASSWORD", ""),
        min_size=1,
        max_size=2,
    )


async def run() -> None:
    pool = await create_pool(settings(), application_name="messenger-integration")
    external = f"kc-check-{uuid.uuid4()}"
    other = f"kc-check-{uuid.uuid4()}"

    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            first = await users.ensure_user(
                conn, external_id=external, display_name="  Аня  ", email="Anya@Example.ORG"
            )
            check("первый вход заводит учётную запись", first.created, str(first))

            check(
                "адрес нормализован так же, как его сравнивает индекс",
                first.user.email == "anya@example.org",
                f"в базе {first.user.email!r}",
            )
            check(
                "имя обрезано по краям",
                first.user.display_name == "Аня",
                f"в базе {first.user.display_name!r}",
            )

            # Инвариант идемпотентности: повтор того же входа.
            again = await users.ensure_user(
                conn, external_id=external, display_name="Аня", email="anya@example.org"
            )
            check(
                "повторный вход не заводит вторую запись",
                not again.created and again.user.user_id == first.user.user_id,
                f"created={again.created}, id={again.user.user_id}",
            )
            check("совпадающий профиль не переписывается", not again.updated, str(again))

            changed = await users.ensure_user(
                conn, external_id=external, display_name="Анна", email="anya@example.org"
            )
            check(
                "изменившееся имя обновляется у той же записи",
                changed.updated and changed.user.user_id == first.user.user_id,
                str(changed),
            )
            check(
                "имя в базе новое",
                changed.user.display_name == "Анна",
                f"в базе {changed.user.display_name!r}",
            )

            found = await users.fetch_user_by_external_id(conn, external_id=external)
            check(
                "запись находится по идентификатору из Keycloak",
                found is not None and found.user_id == first.user.user_id,
            )

            by_id = await users.fetch_user(conn, user_id=first.user.user_id)
            check("запись находится по внутреннему идентификатору", by_id is not None)

            # Инвариант уникальности адреса среди живых. Проверяется
            # в savepoint: нарушение прерывает транзакцию, и без вложенности
            # оборвались бы все следующие проверки.
            nested = conn.transaction()
            await nested.start()
            try:
                await users.ensure_user(
                    conn, external_id=other, display_name="Двойник", email="anya@example.org"
                )
            except asyncpg.UniqueViolationError:
                await nested.rollback()
                ok("второй живой пользователь на тот же адрес отклонён базой")
            else:
                await nested.rollback()
                bad(
                    "второй живой пользователь на тот же адрес отклонён базой",
                    "вставка прошла — частичный уникальный индекс не работает",
                )

            # Инвариант надгробия: удалённая запись повторным входом
            # не воскресает.
            await conn.execute(
                "UPDATE users SET deleted_at = now() WHERE user_id = $1", first.user.user_id
            )
            after = await users.ensure_user(
                conn, external_id=external, display_name="Кто-то другой", email="new@example.org"
            )
            check(
                "удалённая учётная запись не воскресает повторным входом",
                after.user.is_deleted and not after.created and not after.updated,
                str(after),
            )
            check(
                "профиль надгробия не переписан данными из токена",
                after.user.display_name == "Анна" and after.user.email == "anya@example.org",
                f"в базе {after.user.display_name!r} / {after.user.email!r}",
            )
        finally:
            # База после проверки остаётся такой же, какой была.
            await tx.rollback()

    left = await pool.fetchval("SELECT count(*) FROM users WHERE external_id LIKE 'kc-check-%'")
    check("после отката в базе не осталось следов", left == 0, f"осталось строк: {left}")

    await pool.close()


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nрепозиторий профилей подтверждён на живой базе")
    return 0


if __name__ == "__main__":
    sys.exit(main())
