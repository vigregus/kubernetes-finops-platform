"""Сессии и устройства на настоящей базе.

Здесь проверяется то, чего не видно в коде: условия `user_id` в `DO UPDATE`
и в `UPDATE ... WHERE`, поведение при повторном отзыве, отсев истёкших
в списке. Каждое из них — про то, можно ли чужими руками получить доступ
или потерять свой.

Всё в одной транзакции, которая откатывается.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

from messenger.domain.ids import DeviceId, SessionId
from messenger.domain.session import RevocationReason
from messenger.repositories import sessions, users
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
    сейчас = datetime.now(UTC)

    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            свой = (await users.ensure_user(
                conn, external_id=f"kc-check-{uuid.uuid4()}",
                display_name="Свой", email=f"{uuid.uuid4()}@example.org",
            )).user
            чужой = (await users.ensure_user(
                conn, external_id=f"kc-check-{uuid.uuid4()}",
                display_name="Чужой", email=f"{uuid.uuid4()}@example.org",
            )).user

            # --- устройство -------------------------------------------
            device_id = DeviceId(uuid.uuid4())
            заведено = await sessions.ensure_device(
                conn, device_id=device_id, user_id=свой.user_id, user_agent="Firefox/143"
            )
            check("устройство заводится", заведено is not None and заведено.device_id == device_id)

            повтор = await sessions.ensure_device(
                conn, device_id=device_id, user_id=свой.user_id, user_agent=None
            )
            check(
                "повторное обращение не заводит второе устройство",
                повтор is not None and повтор.device_id == device_id,
            )
            check(
                "пустая строка браузера не затирает известную",
                повтор is not None and повтор.user_agent == "Firefox/143",
                f"в базе {повтор.user_agent!r}" if повтор else "нет строки",
            )
            check(
                "отметка последнего обращения сдвинулась",
                повтор is not None and заведено is not None
                and повтор.last_seen_at >= заведено.last_seen_at,
            )

            перехват = await sessions.ensure_device(
                conn, device_id=device_id, user_id=чужой.user_id, user_agent="Chrome/141"
            )
            check(
                "чужое устройство не перепривязывается подобранным идентификатором",
                перехват is None,
                "устройство ушло другому пользователю",
            )

            # --- сессия ------------------------------------------------
            sid = SessionId(uuid.uuid4())
            первая = await sessions.ensure_session(
                conn, session_id=sid, user_id=свой.user_id, device_id=device_id,
                expires_at=сейчас + timedelta(days=7),
            )
            check("сессия заводится по sid из токена", первая.session_id == sid)

            продлённая = await sessions.ensure_session(
                conn, session_id=sid, user_id=свой.user_id, device_id=device_id,
                expires_at=сейчас + timedelta(days=8),
            )
            check(
                "повторный вход не заводит вторую сессию и продлевает срок",
                продлённая.session_id == sid and продлённая.expires_at > первая.expires_at,
            )

            чужая_попытка = await sessions.ensure_session(
                conn, session_id=sid, user_id=чужой.user_id, device_id=device_id,
                expires_at=сейчас + timedelta(days=30),
            )
            check(
                "чужую сессию нельзя перехватить тем же sid",
                чужая_попытка.user_id == свой.user_id
                and чужая_попытка.expires_at == продлённая.expires_at,
                f"владелец {чужая_попытка.user_id}",
            )

            # --- список ------------------------------------------------
            истёкший_id = SessionId(uuid.uuid4())
            await sessions.ensure_session(
                conn, session_id=истёкший_id, user_id=свой.user_id, device_id=device_id,
                expires_at=сейчас - timedelta(hours=1),
            )
            список = await sessions.list_active_sessions(
                conn, user_id=свой.user_id, current=sid
            )
            check(
                "истёкшая сессия не показана как активная",
                all(v.session_id != истёкший_id for v in список),
                f"в списке {len(список)} строк",
            )
            текущая = [v for v in список if v.current]
            check(
                "текущая сессия помечена ровно одна",
                len(текущая) == 1 and текущая[0].session_id == sid,
            )
            check(
                "строка браузера и отметка времени взяты у устройства",
                bool(список) and список[0].user_agent == "Firefox/143",
            )

            # --- отзыв -------------------------------------------------
            check(
                "чужую сессию отозвать нельзя",
                await sessions.revoke_session(
                    conn, session_id=sid, user_id=чужой.user_id,
                    reason=RevocationReason.LOGOUT_ALL,
                ) is None,
            )
            check(
                "своя сессия отзывается",
                await sessions.revoke_session(
                    conn, session_id=sid, user_id=свой.user_id,
                    reason=RevocationReason.LOGOUT_DEVICE,
                ) is not None,
            )

            отозванная = await sessions.fetch_session(conn, session_id=sid)
            момент = отозванная.revoked_at if отозванная else None
            check(
                "повторный отзыв ничего не меняет",
                await sessions.revoke_session(
                    conn, session_id=sid, user_id=свой.user_id,
                    reason=RevocationReason.ADMIN_DISABLE,
                ) is None,
            )
            снова = await sessions.fetch_session(conn, session_id=sid)
            check(
                "время и причина первого отзыва сохранены",
                снова is not None and снова.revoked_at == момент
                and снова.revoked_reason == RevocationReason.LOGOUT_DEVICE.value,
                f"в базе {снова.revoked_reason!r}" if снова else "нет строки",
            )

            воскрешение = await sessions.ensure_session(
                conn, session_id=sid, user_id=свой.user_id, device_id=device_id,
                expires_at=сейчас + timedelta(days=30),
            )
            check(
                "отозванная сессия не воскресает повторным входом",
                воскрешение.is_revoked,
                str(воскрешение),
            )

            # --- выйти везде -------------------------------------------
            for _ in range(3):
                await sessions.ensure_session(
                    conn, session_id=SessionId(uuid.uuid4()), user_id=свой.user_id,
                    device_id=device_id, expires_at=сейчас + timedelta(days=7),
                )
            закрыто = await sessions.revoke_user_sessions(
                conn, user_id=свой.user_id, reason=RevocationReason.LOGOUT_ALL
            )
            check(
                "выход везде закрывает все действующие и возвращает их число",
                len(закрыто) == 4,
                f"закрыто {len(закрыто)}, ожидалось 4 (три новых и одна истёкшая)",
            )
            check(
                "после выхода везде активных не осталось",
                await sessions.list_active_sessions(conn, user_id=свой.user_id) == [],
            )
        finally:
            await tx.rollback()

    осталось = await pool.fetchval(
        "SELECT count(*) FROM users WHERE external_id LIKE 'kc-check-%'"
    )
    check("после отката в базе не осталось следов", осталось == 0, f"строк: {осталось}")
    await pool.close()


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nсессии и устройства подтверждены на живой базе")
    return 0


if __name__ == "__main__":
    sys.exit(main())
