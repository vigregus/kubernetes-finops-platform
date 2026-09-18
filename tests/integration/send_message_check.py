"""G2-004: отправка сообщения через развёрнутый API и дальше в Kafka.

Путь целиком и снаружи: страница входа Keycloak → токен → создание
беседы → `POST /conversations/{id}/messages` → запись в Postgres → outbox →
отправитель → поток Kafka. Ни одного шага в обход HTTP: проверяется то,
чем будет пользоваться клиент, а не то, что удобно вызвать из кода.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
import uuid

import httpx
from aiokafka import AIOKafkaConsumer, TopicPartition
from login_check import (
    API,
    ORIGIN,
    REDIRECT,
    admin_token,
    authorization_code,
    create_user,
    pkce,
)
from relay_check import BOOTSTRAP, KAFKA_PASSWORD, KAFKA_USER, pool_settings

from messenger.repositories.postgres import create_pool

failures: list[str] = []

ТОПИКИ = ("messenger.events.v1", "messenger.content.v1")

# Оставить учётные записи и напечатать токен: тогда тем же беседой
# и токеном можно продолжить руками. По умолчанию выключено - проверка
# не должна оставлять следов в настоящей базе.
KEEP_ACCOUNTS = os.getenv("KEEP_ACCOUNTS", "") not in ("", "0", "false")
# Отправитель опрашивает очередь дважды в секунду; запас на переподключение.
DEADLINE_SECONDS = 20


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


async def end_offsets() -> dict[str, int]:
    consumer = AIOKafkaConsumer(
        bootstrap_servers=BOOTSTRAP,
        security_protocol="SASL_PLAINTEXT",
        sasl_mechanism="SCRAM-SHA-512",
        sasl_plain_username=KAFKA_USER,
        sasl_plain_password=KAFKA_PASSWORD,
        enable_auto_commit=False,
    )
    await consumer.start()
    try:
        totals: dict[str, int] = {}
        for topic in ТОПИКИ:
            parts = consumer.partitions_for_topic(topic) or set()
            offsets = await consumer.end_offsets([TopicPartition(topic, p) for p in parts])
            totals[topic] = sum(offsets.values())
        return totals
    finally:
        await consumer.stop()


async def access_token(http: httpx.AsyncClient, login: str, password: str) -> str | None:
    verifier, challenge = pkce()
    code = await authorization_code(login, password, challenge)
    if code is None:
        return None
    response = await http.post(
        f"{API}/auth/callback",
        json={"code": code, "code_verifier": verifier, "redirect_uri": REDIRECT,
              "device_id": str(uuid.uuid4())},
        headers={"Origin": ORIGIN, "User-Agent": "checks/send-message"},
    )
    return response.json().get("access_token") if response.status_code == 200 else None


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN,
            "User-Agent": "checks/send-message"}


async def wait_outbox_drained(pool) -> bool:
    """Ждёт, пока отправитель разгребёт очередь.

    Ждём именно опустошения, а не появления записей в Kafka: очередь —
    это то, за что отвечает наша сторона, и её непустота через двадцать
    секунд означает отказ доставки, а не медленный брокер.
    """
    for _ in range(DEADLINE_SECONDS * 2):
        pending = await pool.fetchval(
            "SELECT count(*) FROM outbox WHERE published_at IS NULL"
        )
        if not pending:
            return True
        await asyncio.sleep(0.5)
    return False


async def run() -> None:
    marker = uuid.uuid4().hex[:12]
    accounts = [
        (f"send-{marker}-{n}@example.org", secrets.token_urlsafe(24)) for n in range(3)
    ]
    external_ids: list[str] = []
    pool = await create_pool(pool_settings(), application_name="messenger-integration")

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as http:
        admin = await admin_token(http)
        try:
            for login, password in accounts:
                external_ids.append(
                    await create_user(http, admin, login=login, password=password,
                                      email_verified=True)
                )
            tokens = [await access_token(http, login, pw) for login, pw in accounts]
            check("три участника получили токены", all(tokens),
                  str([bool(t) for t in tokens]))
            if not all(tokens):
                return
            автор, получатель, посторонний = tokens

            # --- беседа ------------------------------------------------
            участник = await pool.fetchval(
                "SELECT user_id FROM users WHERE external_id = $1", external_ids[1]
            )
            создана = await http.post(
                f"{API}/conversations",
                json={"participant_id": str(участник)},
                headers=headers(автор),
            )
            check("беседа создана", создана.status_code in (200, 201),
                  f"{создана.status_code}: {создана.text[:160]}")
            if создана.status_code not in (200, 201):
                return
            беседа = создана.json()["conversation_id"]

            было = await end_offsets()

            # --- отправка ----------------------------------------------
            client_message_id = str(uuid.uuid4())
            тело = {"client_message_id": client_message_id, "type": "text",
                    "payload": {"text": "привет из проверки"}}
            принято = await http.post(
                f"{API}/conversations/{беседа}/messages", json=тело, headers=headers(автор)
            )
            check("сообщение принято с кодом 201", принято.status_code == 201,
                  f"{принято.status_code}: {принято.text[:200]}")
            if принято.status_code != 201:
                return

            сообщение = принято.json()
            check("номер в беседе начинается с единицы", сообщение["seq"] == 1,
                  str(сообщение.get("seq")))
            check("ответ содержит поля контракта",
                  all(k in сообщение for k in
                      ("message_id", "conversation_id", "seq", "sender_id",
                       "client_message_id", "type", "payload", "created_at")),
                  str(sorted(сообщение)))
            check("содержимое вернулось как отправлено",
                  сообщение["payload"].get("text") == "привет из проверки")

            # --- повтор ------------------------------------------------
            повтор = await http.post(
                f"{API}/conversations/{беседа}/messages", json=тело, headers=headers(автор)
            )
            check("повтор возвращает 200, а не создаёт второе",
                  повтор.status_code == 200, str(повтор.status_code))
            check("повтор вернул то же сообщение и тот же номер",
                  повтор.json()["message_id"] == сообщение["message_id"]
                  and повтор.json()["seq"] == сообщение["seq"],
                  str(повтор.json()))

            # --- второй участник ---------------------------------------
            ответ = await http.post(
                f"{API}/conversations/{беседа}/messages",
                json={"client_message_id": str(uuid.uuid4()), "type": "text",
                      "payload": {"text": "и тебе привет"}},
                headers=headers(получатель),
            )
            check("второй участник пишет в ту же беседу", ответ.status_code == 201,
                  f"{ответ.status_code}: {ответ.text[:160]}")
            check("номер выдан следующий", ответ.json().get("seq") == 2,
                  str(ответ.json().get("seq")))

            # --- отказы ------------------------------------------------
            чужой = await http.post(
                f"{API}/conversations/{беседа}/messages",
                json={"client_message_id": str(uuid.uuid4()), "type": "text",
                      "payload": {"text": "я тут не состою"}},
                headers=headers(посторонний),
            )
            check("посторонний не отличает чужую беседу от несуществующей",
                  чужой.status_code == 404, str(чужой.status_code))

            длинный = await http.post(
                f"{API}/conversations/{беседа}/messages",
                json={"client_message_id": str(uuid.uuid4()), "type": "text",
                      "payload": {"text": "я" * 5000}},
                headers=headers(автор),
            )
            check("слишком длинный текст отвергается кодом 413",
                  длинный.status_code == 413, str(длинный.status_code))

            без_токена = await http.post(
                f"{API}/conversations/{беседа}/messages", json=тело,
                headers={"Origin": ORIGIN},
            )
            check("без токена не принимает", без_токена.status_code == 401,
                  str(без_токена.status_code))

            # --- путь до Kafka -----------------------------------------
            check("очередь outbox разгребена отправителем",
                  await wait_outbox_drained(pool))
            стало = await end_offsets()
            check("факты обоих сообщений ушли в поток фактов",
                  стало["messenger.events.v1"] - было["messenger.events.v1"] == 2,
                  f"было {было['messenger.events.v1']}, стало {стало['messenger.events.v1']}")
            check("содержимое ушло отдельным потоком",
                  стало["messenger.content.v1"] - было["messenger.content.v1"] == 2,
                  f"было {было['messenger.content.v1']}, стало {стало['messenger.content.v1']}")
            if KEEP_ACCOUNTS:
                print("\n  --- оставлено для ручной работы ---")
                print(f"  беседа:   {беседа}")
                print(f"  вход A:   {accounts[0][0]} / {accounts[0][1]}")
                print(f"  токен A:  {автор}")
                print("  пример:")
                print(
                    f'    curl -sS -X POST "$API/conversations/{беседа}/messages" \\\n'
                    f'      -H "Authorization: Bearer $TOKEN" -H "Origin: {ORIGIN}" \\\n'
                    "      -H 'Content-Type: application/json' \\\n"
                    '      -d \'{"client_message_id":"\'"$(uuidgen | tr A-Z a-z)"\'",'
                    '"type":"text","payload":{"text":"привет"}}\''
                )
        finally:
            if KEEP_ACCOUNTS:
                await pool.close()
                return
            # Уборка при любом исходе: проверка не оставляет следов
            # в настоящей базе и в реалме.
            for external_id in external_ids:
                await pool.execute(
                    "DELETE FROM outbox WHERE aggregate_id IN "
                    "(SELECT message_id FROM messages WHERE sender_id IN "
                    "(SELECT user_id FROM users WHERE external_id = $1))", external_id
                )
            await pool.execute(
                """
                DELETE FROM messages WHERE sender_id IN
                    (SELECT user_id FROM users WHERE external_id = ANY($1::text[]))
                """, external_ids)
            await pool.execute(
                """
                DELETE FROM conversation_members WHERE user_id IN
                    (SELECT user_id FROM users WHERE external_id = ANY($1::text[]))
                """, external_ids)
            await pool.execute(
                """
                DELETE FROM conversations WHERE conversation_id NOT IN
                    (SELECT conversation_id FROM conversation_members)
                """)
            await pool.execute(
                """
                DELETE FROM sessions WHERE user_id IN
                    (SELECT user_id FROM users WHERE external_id = ANY($1::text[]))
                """, external_ids)
            await pool.execute(
                """
                DELETE FROM devices WHERE user_id IN
                    (SELECT user_id FROM users WHERE external_id = ANY($1::text[]))
                """, external_ids)
            await pool.execute(
                "DELETE FROM users WHERE external_id = ANY($1::text[])", external_ids)
            await pool.close()
            for external_id in external_ids:
                await http.delete(
                    f"{__import__('os').environ.get('KEYCLOAK_URL', '')}"
                    f"/admin/realms/{__import__('os').environ.get('KEYCLOAK_REALM', 'messenger')}"
                    f"/users/{external_id}",
                    headers={"Authorization": f"Bearer {admin}"},
                )


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nотправка сообщения подтверждена от HTTP до потока Kafka")
    return 0


if __name__ == "__main__":
    sys.exit(main())
