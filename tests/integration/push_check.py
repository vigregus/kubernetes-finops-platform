"""G4: Web Push на живых Postgres, Keycloak и API (`NTF-001…004`).

Серверная половина проверяется целиком, кроме последнего шага — настоящей
службы push браузера (FCM, Mozilla, Apple): до неё из кластера не дойти, и
отправитель здесь — `httpx.MockTransport`, который записывает запрос и
отвечает заданным статусом. Всё остальное настоящее: подписки пишутся через
HTTP API, устройства и «в сети» читаются из настоящей базы, а полезная
нагрузка **расшифровывается ключами браузера** — то есть проверяется, что
сервер шифрует так, как умеет читать браузер.

`NTF-001` уведомление уходит на офлайн-устройство и несёт только сигнал.
`NTF-002` провайдер ответил «подписки нет» — подписка снята, повторов нет.
`NTF-003`/`NTF-004` у устройства с живым соединением уведомления нет, у
соседнего без соединения — есть. SSRF: адрес вне списка провайдеров API
отвергает до записи.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import secrets
import sys
import uuid
from datetime import timedelta

import http_ece
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from login_check import API, ORIGIN, admin_token, create_user
from realtime_revoke_check import _auth_headers, _login
from relay_check import pool_settings

from messenger.adapters.webpush import VapidSettings, WebPushSender, generate_vapid_keys
from messenger.domain.ids import SessionId, UserId
from messenger.repositories import sessions
from messenger.repositories.postgres import create_pool
from messenger.services import push as push_service

failures: list[str] = []


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Browser:
    """Получатель: пара ключей и секрет, как у подписки в браузере."""

    def __init__(self, endpoint: str) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = secrets.token_bytes(16)
        public = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.endpoint = endpoint
        self.body = {"endpoint": endpoint, "keys": {"p256dh": b64(public), "auth": b64(self.auth)}}

    def decrypt(self, content: bytes) -> dict:
        raw = http_ece.decrypt(
            content, private_key=self.key, auth_secret=self.auth, version="aes128gcm"
        )
        return json.loads(raw)


class Provider:
    """Служба push браузера: записывает запросы и отвечает заданным статусом."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status_by_endpoint: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_by_endpoint.get(str(request.url), 201))


async def run() -> None:
    marker = uuid.uuid4().hex[:10]
    accounts = [(f"push-{marker}-{n}@example.org", secrets.token_urlsafe(24)) for n in range(2)]
    external_ids: list[str] = []
    pool = await create_pool(pool_settings(), application_name="messenger-integration")

    private_pem, _ = generate_vapid_keys()
    provider = Provider()
    sender = WebPushSender(
        VapidSettings(private_key_pem=private_pem, subject="mailto:ops@example.org"),
        httpx.AsyncClient(transport=httpx.MockTransport(provider.handler)),
    )

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as http:
        admin = await admin_token(http)
        try:
            for login, password in accounts:
                external_ids.append(
                    await create_user(http, admin, login=login, password=password,
                                      email_verified=True)
                )
            # У B два устройства (ноутбук и телефон), у A одно.
            device_a, laptop, phone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            login_a = await _login(http, accounts[0][0], accounts[0][1], device_a)
            login_laptop = await _login(http, accounts[1][0], accounts[1][1], laptop)
            login_phone = await _login(http, accounts[1][0], accounts[1][1], phone)
            check("A и оба устройства B вошли", all((login_a, login_laptop, login_phone)))
            if not all((login_a, login_laptop, login_phone)):
                return
            b_user = await pool.fetchval(
                "SELECT user_id FROM users WHERE external_id = $1", external_ids[1])

            # --- подписки через настоящий API ------------------------------
            key_reply = await http.get(
                f"{API}/push/vapid-public-key", headers=_auth_headers(login_laptop[0], laptop))
            vapid_configured = key_reply.status_code == 200
            check("открытый ключ VAPID отдаётся вошедшему (или 503, если не настроен)",
                  key_reply.status_code in (200, 503), f"{key_reply.status_code}")
            anonymous = await http.get(f"{API}/push/vapid-public-key")
            check("без входа ключ не отдаётся", anonymous.status_code == 401)

            laptop_browser = Browser("https://fcm.googleapis.com/fcm/send/laptop-" + marker)
            phone_browser = Browser("https://updates.push.services.mozilla.com/wpush/v2/" + marker)

            evil = await http.put(
                f"{API}/me/push-subscription",
                json={**laptop_browser.body, "endpoint": "https://169.254.169.254/latest"},
                headers={**_auth_headers(login_laptop[0], laptop), "Origin": ORIGIN})
            check("SSRF: адрес вне списка провайдеров отвергнут до записи",
                  evil.status_code in (400, 503), f"{evil.status_code}: {evil.text[:120]}")

            if not vapid_configured:
                print("  ключ VAPID на стенде не настроен — запись подписок и отправка"
                      " проверяются на сервисном уровне без HTTP")
            else:
                for browser, who, device in ((laptop_browser, login_laptop, laptop),
                                             (phone_browser, login_phone, phone)):
                    saved = await http.put(
                        f"{API}/me/push-subscription", json=browser.body,
                        headers={**_auth_headers(who[0], device), "Origin": ORIGIN})
                    check(f"подписка устройства принята ({browser.endpoint[:34]}…)",
                          saved.status_code == 204, f"{saved.status_code}: {saved.text[:120]}")

            if not vapid_configured:
                # Без ключа на API подписки пишем тем же сервисом напрямую.
                os.environ["VAPID_PUBLIC_KEY"] = "test"
                for browser, device in ((laptop_browser, laptop), (phone_browser, phone)):
                    async with pool.acquire() as conn:
                        result = await push_service.subscribe(
                            conn, device_id=device, user_id=UserId(b_user), data=browser.body)
                    check("подписка записана сервисом", result.ok, str(result))

            rows = await pool.fetch(
                "SELECT device_id, push_subscription ->> 'endpoint' AS endpoint FROM devices"
                " WHERE user_id = $1 AND push_subscription IS NOT NULL", b_user)
            check("подписки лежат за устройствами, а не за человеком",
                  {r["device_id"] for r in rows} == {laptop, phone}, str(rows))

            event = {
                "event_type": "message.created", "conversation_id": str(uuid.uuid4()),
                "recipient_ids": [str(b_user)], "message_id": str(uuid.uuid4()),
                "sender_id": str(uuid.uuid4()),
            }

            async def notify():
                async with pool.acquire() as conn:
                    return await push_service.notify_message(conn, sender=sender, body=event)

            # --- NTF-001: оба офлайн ----------------------------------------
            provider.requests.clear()
            outcome = await notify()
            check("NTF-001: оба офлайн-устройства получили уведомление",
                  outcome.sent == 2 and len(provider.requests) == 2, str(outcome))
            by_url = {str(r.url): r for r in provider.requests}
            for browser in (laptop_browser, phone_browser):
                request = by_url.get(browser.endpoint)
                if request is None:
                    check(f"запрос ушёл на адрес подписки {browser.endpoint[:30]}…", False)
                    continue
                payload = browser.decrypt(request.content)
                check("полезная нагрузка расшифровывается ключами браузера и несёт только сигнал",
                      payload == {"type": "message", "conversation_id": event["conversation_id"]},
                      str(payload))
                check("в запросе подпись VAPID и свёртка по беседе",
                      request.headers["authorization"].startswith("vapid t=")
                      and bool(request.headers["topic"]))

            # --- NTF-003/004: ноутбук «в сети» ------------------------------
            laptop_session = await pool.fetchval(
                "SELECT session_id FROM sessions WHERE device_id = $1", laptop)
            client_id = f"push-check-{marker}"
            async with pool.acquire() as conn, conn.transaction():
                await sessions.register_realtime_connection(
                    conn, session_id=SessionId(laptop_session), user_id=UserId(b_user),
                    client_id=client_id)
            provider.requests.clear()
            outcome = await notify()
            check("NTF-003/004: у ноутбука с живым соединением уведомления нет",
                  outcome.skipped_online == 1 and outcome.sent == 1, str(outcome))
            check("и уходит только на телефон",
                  [str(r.url) for r in provider.requests] == [phone_browser.endpoint])

            # Соединение протухло — устройство снова офлайн.
            await pool.execute(
                "UPDATE realtime_connections SET refreshed_at = now() - $2::interval"
                " WHERE client_id = $1", client_id, timedelta(hours=1))
            provider.requests.clear()
            outcome = await notify()
            check("протухшее соединение не считается «в сети»", outcome.sent == 2, str(outcome))

            # --- NTF-002: подписка отозвана браузером ------------------------
            provider.status_by_endpoint[phone_browser.endpoint] = 410
            provider.requests.clear()
            outcome = await notify()
            check("NTF-002: провайдер ответил 410 — подписка удалена", outcome.gone == 1, str(outcome))
            left = await pool.fetch(
                "SELECT device_id FROM devices WHERE user_id = $1"
                " AND push_subscription IS NOT NULL", b_user)
            check("снята только мёртвая подписка, живая осталась",
                  {r["device_id"] for r in left} == {laptop}, str(left))
            provider.requests.clear()
            await notify()
            check("повторов нет: на удалённый адрес больше не ходим",
                  phone_browser.endpoint not in {str(r.url) for r in provider.requests})

            # --- отписка через API -------------------------------------------
            if vapid_configured:
                gone = await http.delete(
                    f"{API}/me/push-subscription",
                    headers={**_auth_headers(login_laptop[0], laptop), "Origin": ORIGIN})
                check("отписка принята", gone.status_code == 204)
                rest = await pool.fetchval(
                    "SELECT count(*) FROM devices WHERE user_id = $1"
                    " AND push_subscription IS NOT NULL", b_user)
                check("после отписки подписок нет", rest == 0, str(rest))
        finally:
            await pool.execute(
                "DELETE FROM realtime_connections WHERE user_id IN (SELECT user_id FROM users"
                " WHERE external_id = ANY($1::text[]))", external_ids)
            for table, column in (("sessions", "user_id"), ("devices", "user_id")):
                await pool.execute(
                    f"DELETE FROM {table} WHERE {column} IN "  # noqa: S608
                    "(SELECT user_id FROM users WHERE external_id = ANY($1::text[]))",
                    external_ids)
            await pool.execute("DELETE FROM users WHERE external_id = ANY($1::text[])", external_ids)
            await pool.close()
            for external_id in external_ids:
                with contextlib.suppress(Exception):
                    await http.delete(
                        f"{os.environ.get('KEYCLOAK_URL', '')}/admin/realms/"
                        f"{os.environ.get('KEYCLOAK_REALM', 'messenger')}/users/{external_id}",
                        headers={"Authorization": f"Bearer {admin}"},
                    )


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nWeb Push: подписки по устройствам, сигнал без текста, мёртвая подписка снята")
    return 0


if __name__ == "__main__":
    sys.exit(main())
