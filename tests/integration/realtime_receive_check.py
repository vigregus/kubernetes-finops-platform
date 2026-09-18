"""G2-005: отправка одним участником и получение другим по WebSocket.

Здесь же проверяется условие выхода гейта: при повторной публикации
того же события получатель видит сообщение **ровно один раз**. Повтор
не имитируется подделкой — у записи outbox снимается отметка об отправке,
и настоящий отправитель публикует её в Kafka второй раз. Так выглядит
его смерть между публикацией и отметкой, и именно это переживает
дедупликация потребителя.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sys
import uuid

import httpx
from login_check import API, ORIGIN, admin_token, create_user
from realtime_revoke_check import CENTRIFUGO_URL, _auth_headers, _connect, _login
from relay_check import pool_settings

from messenger.repositories.postgres import create_pool

failures: list[str] = []

# Сколько ждём сообщение в сокете. Путь длинный — Postgres, outbox,
# отправитель, Kafka, потребитель, Centrifugo, — но каждый шаг быстрый.
DEADLINE_SECONDS = 25


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


async def subscribe(ws, channel: str) -> tuple[bool, str]:
    """Подписывается на канал и возвращает ответ сервера как есть.

    Ответ возвращается текстом, потому что молчаливое `False` ничего
    не объясняет: подписка может быть отклонена правами, а может просто
    прийти другим кадром, и это разные поводы для правки.
    """
    await ws.send(json.dumps({"id": 2, "subscribe": {"channel": channel}}))
    кадры: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10.0
    while loop.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(deadline - loop.time(), 0.1))
        except (TimeoutError, asyncio.TimeoutError):
            break
        кадры.append(raw[:200])
        try:
            reply = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(reply, dict) or reply.get("id") != 2:
            continue
        # `already subscribed` (105) - не отказ, а подтверждение того,
        # что канал выдал сервер: Centrifugo подписывает клиента сам
        # по списку из connect-токена. Именно этого требует контракт
        # каналов - клиент не выбирает канал и не может подписаться
        # на чужую беседу, зная её идентификатор.
        error = reply.get("error") or {}
        return "error" not in reply or error.get("code") == 105, raw[:200]
    return False, f"ответа с id=2 не пришло; кадры: {кадры}"


async def wait_publication(ws, *, timeout: float) -> dict | None:
    """Ждёт публикацию в подписанном канале.

    Служебные кадры пропускаются: Centrifugo шлёт пустые пинги, и принять
    их за сообщение значило бы получить зелёную проверку на пустом месте.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(deadline - loop.time(), 0.1))
        except (TimeoutError, asyncio.TimeoutError):
            return None
        try:
            frame = json.loads(raw)
        except ValueError:
            continue
        push = frame.get("push") if isinstance(frame, dict) else None
        if not isinstance(push, dict):
            continue
        pub = push.get("pub")
        if isinstance(pub, dict) and isinstance(pub.get("data"), dict):
            return pub["data"]
    return None


async def run() -> None:
    marker = uuid.uuid4().hex[:12]
    accounts = [
        (f"rt-{marker}-{n}@example.org", secrets.token_urlsafe(24)) for n in range(2)
    ]
    external_ids: list[str] = []
    pool = await create_pool(pool_settings(), application_name="messenger-integration")
    ws = None

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as http:
        admin = await admin_token(http)
        try:
            for login, password in accounts:
                external_ids.append(
                    await create_user(http, admin, login=login, password=password,
                                      email_verified=True)
                )

            device_a, device_b = uuid.uuid4(), uuid.uuid4()
            вход_a = await _login(http, accounts[0][0], accounts[0][1], device_a)
            вход_b = await _login(http, accounts[1][0], accounts[1][1], device_b)
            check("оба участника вошли", bool(вход_a and вход_b))
            if not (вход_a and вход_b):
                return
            токен_a, _ = вход_a
            токен_b, _ = вход_b

            # --- беседа ------------------------------------------------
            участник = await pool.fetchval(
                "SELECT user_id FROM users WHERE external_id = $1", external_ids[1]
            )
            создана = await http.post(
                f"{API}/conversations",
                json={"participant_id": str(участник)},
                headers={**_auth_headers(токен_a, device_a), "Origin": ORIGIN},
            )
            check("беседа создана", создана.status_code in (200, 201),
                  f"{создана.status_code}: {создана.text[:160]}")
            if создана.status_code not in (200, 201):
                return
            беседа = создана.json()["conversation_id"]

            # --- получатель подключается -------------------------------
            # Токен запрашивается после создания беседы: каналы
            # перечисляются на момент выдачи, и в более раннем токене
            # этой беседы просто нет.
            r = await http.post(
                f"{API}/realtime/token", headers=_auth_headers(токен_b, device_b)
            )
            check("получатель получил токен Centrifugo", r.status_code == 200,
                  f"{r.status_code}: {r.text[:160]}")
            if r.status_code != 200:
                return

            ws, client_id = await _connect(r.json()["token"])
            check("WebSocket открыт", bool(ws and client_id), CENTRIFUGO_URL)
            if not (ws and client_id):
                return
            подписан, ответ = await subscribe(ws, f"conversation:{беседа}")
            check("канал беседы выдан сервером, а не выбран клиентом", подписан, ответ)

            # --- отправка ----------------------------------------------
            текст = f"живая проверка {marker}"
            принято = await http.post(
                f"{API}/conversations/{беседа}/messages",
                json={"client_message_id": str(uuid.uuid4()), "type": "text",
                      "payload": {"text": текст}},
                headers={**_auth_headers(токен_a, device_a), "Origin": ORIGIN},
            )
            check("сообщение принято API", принято.status_code == 201,
                  f"{принято.status_code}: {принято.text[:160]}")
            if принято.status_code != 201:
                return
            message_id = принято.json()["message_id"]

            доставлено = await wait_publication(ws, timeout=DEADLINE_SECONDS)
            check("получатель увидел сообщение в канале беседы",
                  доставлено is not None, "публикация не пришла")
            if доставлено is None:
                return
            check("в доставленном событии тот же текст",
                  доставлено.get("payload", {}).get("text") == текст, str(доставлено))
            check("в событии есть номер и отправитель",
                  доставлено.get("seq") == 1 and доставлено.get("sender_id"),
                  str(доставлено))
            check("список получателей наружу не ушёл",
                  "recipient_ids" not in доставлено, str(доставлено))

            # --- повтор ------------------------------------------------
            # Снимаем отметку об отправке: отправитель опубликует те же
            # события второй раз - ровно так выглядит его смерть между
            # публикацией и отметкой.
            повторено = await pool.execute(
                """
                UPDATE outbox
                   SET published_at = NULL, lease_owner = NULL, lease_until = NULL
                 WHERE aggregate_id = $1
                """,
                uuid.UUID(message_id),
            )
            check("события отправлены в Kafka повторно", повторено.endswith("2"),
                  повторено)

            лишнее = await wait_publication(ws, timeout=12.0)
            check("повтор не дошёл до получателя вторым сообщением",
                  лишнее is None, str(лишнее))
        finally:
            if ws is not None:
                with __import__("contextlib").suppress(Exception):
                    await ws.close()
            await pool.execute(
                "DELETE FROM outbox WHERE aggregate_id IN (SELECT message_id FROM messages"
                " WHERE sender_id IN (SELECT user_id FROM users WHERE external_id ="
                " ANY($1::text[])))", external_ids)
            # Порядок важен: на сессии ссылаются записи соединений
            # реального времени, и удалить сессию раньше нельзя.
            for таблица, поле in (("messages", "sender_id"), ("conversation_members", "user_id"),
                                  ("realtime_connections", "user_id"),
                                  ("sessions", "user_id"), ("devices", "user_id")):
                await pool.execute(
                    f"DELETE FROM {таблица} WHERE {поле} IN "  # noqa: S608
                    "(SELECT user_id FROM users WHERE external_id = ANY($1::text[]))",
                    external_ids)
            await pool.execute(
                "DELETE FROM conversations WHERE conversation_id NOT IN "
                "(SELECT conversation_id FROM conversation_members)")
            await pool.execute(
                "DELETE FROM users WHERE external_id = ANY($1::text[])", external_ids)
            await pool.close()
            import os as _os
            for external_id in external_ids:
                await http.delete(
                    f"{_os.environ.get('KEYCLOAK_URL', '')}/admin/realms/"
                    f"{_os.environ.get('KEYCLOAK_REALM', 'messenger')}/users/{external_id}",
                    headers={"Authorization": f"Bearer {admin}"},
                )


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nдоставка подтверждена: отправлено по HTTP, получено по WebSocket")
    return 0


if __name__ == "__main__":
    sys.exit(main())
