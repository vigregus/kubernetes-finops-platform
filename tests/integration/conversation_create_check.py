"""G1-010/G1-011: создание direct-беседы и встречная гонка.

Токены получаются страницей Keycloak и серверным обменом. Новый обработчик
ветки запускается через ASGI в том же pod и ходит в живую базу через PgBouncer;
так проверяется код ветки без подмены развёрнутого API агента A.
"""
from __future__ import annotations

import asyncio
import secrets
import sys
import uuid

import httpx

from login_check import (
    API,
    ORIGIN,
    REDIRECT,
    _cleanup,
    _pool,
    admin_token,
    authorization_code,
    create_user,
    pkce,
)
from messenger.api.main import app
from messenger.domain.ids import direct_key

failures: list[str] = []


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


async def access_token(
    http: httpx.AsyncClient, login: str, password: str
) -> str | None:
    verifier, challenge = pkce()
    code = await authorization_code(login, password, challenge)
    if code is None:
        return None
    response = await http.post(
        f"{API}/auth/callback",
        json={
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT,
            "device_id": str(uuid.uuid4()),
        },
        headers={"Origin": ORIGIN, "User-Agent": "checks/conversation-create"},
    )
    if response.status_code != 200:
        return None
    return response.json().get("access_token")


async def run() -> None:
    marker = uuid.uuid4().hex[:12]
    accounts = [
        (f"conversation-{marker}-{number}@example.org", secrets.token_urlsafe(24))
        for number in range(4)
    ]
    external_ids: list[str] = []
    conversation_ids: list[uuid.UUID] = []
    runtime = app.state.runtime

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as live:
        admin = await admin_token(live)
        try:
            for login, password in accounts:
                external_ids.append(
                    await create_user(
                        live,
                        admin,
                        login=login,
                        password=password,
                        email_verified=True,
                    )
                )

            tokens = [
                await access_token(live, login, password)
                for login, password in accounts
            ]
            check(
                "четыре пользователя получили настоящие токены",
                all(tokens),
                str([bool(token) for token in tokens]),
            )
            if not all(tokens):
                return

            pool = await _pool()
            try:
                rows = await pool.fetch(
                    """
                    SELECT external_id, user_id
                      FROM users
                     WHERE external_id = ANY($1::text[])
                    """,
                    external_ids,
                )
                internal = {row["external_id"]: row["user_id"] for row in rows}
            finally:
                await pool.close()
            first_id, second_id, third_id, fourth_id = [
                internal[item] for item in external_ids
            ]

            await runtime.start()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://branch-api", timeout=15.0
            ) as branch:
                first = await branch.post(
                    "/conversations",
                    json={"participant_id": str(second_id)},
                    headers={"Authorization": f"Bearer {tokens[0]}"},
                )
                check(
                    "первый запрос создаёт беседу",
                    first.status_code == 201,
                    f"{first.status_code}: {first.text[:200]}",
                )
                if first.status_code != 201:
                    return
                body = first.json()
                conversation_id = uuid.UUID(body["conversation_id"])
                conversation_ids.append(conversation_id)
                check(
                    "ответ содержит обоих участников",
                    {item["user_id"] for item in body["participants"]}
                    == {str(first_id), str(second_id)},
                    str(body),
                )

                repeated = await branch.post(
                    "/conversations",
                    json={"participant_id": str(second_id)},
                    headers={"Authorization": f"Bearer {tokens[0]}"},
                )
                check(
                    "последовательный повтор возвращает существующую беседу",
                    repeated.status_code == 200
                    and repeated.json().get("conversation_id") == str(conversation_id),
                    f"{repeated.status_code}: {repeated.text[:200]}",
                )

                reverse = await branch.post(
                    "/conversations",
                    json={"participant_id": str(first_id)},
                    headers={"Authorization": f"Bearer {tokens[1]}"},
                )
                check(
                    "обратный последовательный запрос видит ту же пару",
                    reverse.status_code == 200
                    and reverse.json().get("conversation_id") == str(conversation_id),
                    f"{reverse.status_code}: {reverse.text[:200]}",
                )

                self_chat = await branch.post(
                    "/conversations",
                    json={"participant_id": str(first_id)},
                    headers={"Authorization": f"Bearer {tokens[0]}"},
                )
                check("беседа с собой отклонена явно", self_chat.status_code == 403)

                missing = await branch.post(
                    "/conversations",
                    json={"participant_id": str(uuid.uuid4())},
                    headers={"Authorization": f"Bearer {tokens[0]}"},
                )
                check("неизвестный участник скрыт как отсутствующий", missing.status_code == 404)

                pool = await _pool()
                try:
                    await pool.execute(
                        "INSERT INTO blocks (blocker_id, blocked_id) VALUES ($1, $2)",
                        third_id,
                        first_id,
                    )
                finally:
                    await pool.close()
                blocked = await branch.post(
                    "/conversations",
                    json={"participant_id": str(third_id)},
                    headers={"Authorization": f"Bearer {tokens[0]}"},
                )
                check("блокировка в обратную сторону запрещает создание", blocked.status_code == 403)

                left, right = await asyncio.gather(
                    branch.post(
                        "/conversations",
                        json={"participant_id": str(fourth_id)},
                        headers={"Authorization": f"Bearer {tokens[2]}"},
                    ),
                    branch.post(
                        "/conversations",
                        json={"participant_id": str(third_id)},
                        headers={"Authorization": f"Bearer {tokens[3]}"},
                    ),
                )
                check(
                    "встречные запросы оба завершились успешно",
                    sorted((left.status_code, right.status_code)) == [200, 201],
                    f"статусы: {left.status_code}, {right.status_code}",
                )
                race_ids = [
                    response.json().get("conversation_id")
                    for response in (left, right)
                    if response.status_code in (200, 201)
                ]
                check(
                    "встречные запросы вернули один conversation_id",
                    len(race_ids) == 2 and len(set(race_ids)) == 1,
                    str(race_ids),
                )
                if race_ids:
                    conversation_ids.append(uuid.UUID(race_ids[0]))

            pool = await _pool()
            try:
                pair = direct_key(first_id, second_id)
                count = await pool.fetchval(
                    "SELECT count(*) FROM conversations WHERE direct_key = $1", pair
                )
                members = await pool.fetchval(
                    """
                    SELECT count(*)
                      FROM conversation_members
                     WHERE conversation_id = $1 AND left_at IS NULL
                    """,
                    conversation_id,
                )
                blocked_pair = await pool.fetchval(
                    """
                    SELECT count(*)
                      FROM conversations
                     WHERE direct_key = $1
                    """,
                    direct_key(first_id, third_id),
                )
                race_count = await pool.fetchval(
                    "SELECT count(*) FROM conversations WHERE direct_key = $1",
                    direct_key(third_id, fourth_id),
                )
                race_members = await pool.fetchval(
                    """
                    SELECT count(*)
                      FROM conversation_members
                     WHERE conversation_id = $1 AND left_at IS NULL
                    """,
                    conversation_ids[-1],
                )
            finally:
                await pool.close()
            check("в базе существует ровно одна беседа пары", count == 1, f"строк: {count}")
            check("у беседы ровно два действующих участника", members == 2, f"строк: {members}")
            check("для заблокированной пары беседа не появилась", blocked_pair == 0)
            check("после встречной гонки в базе одна беседа", race_count == 1)
            check("у победившей беседы ровно два участника", race_members == 2)
        finally:
            await runtime.stop()
            pool = await _pool()
            try:
                await pool.execute(
                    """
                    DELETE FROM blocks
                     WHERE blocker_id IN (
                               SELECT user_id FROM users
                                WHERE external_id = ANY($1::text[])
                           )
                        OR blocked_id IN (
                               SELECT user_id FROM users
                                WHERE external_id = ANY($1::text[])
                           )
                    """,
                    external_ids,
                )
                if conversation_ids:
                    await pool.execute(
                        "DELETE FROM conversation_members WHERE conversation_id = ANY($1::uuid[])",
                        conversation_ids,
                    )
                    await pool.execute(
                        "DELETE FROM conversations WHERE conversation_id = ANY($1::uuid[])",
                        conversation_ids,
                    )
            finally:
                await pool.close()
            for external_id in external_ids:
                await _cleanup(live, admin, external_id)


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nсоздание direct-беседы подтверждено через API и живую базу")
    return 0


if __name__ == "__main__":
    sys.exit(main())
