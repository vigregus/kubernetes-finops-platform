"""AUTH-006: письмо, ограниченный профиль, лимит и настоящая ссылка.

Проверка намеренно проходит не административную подмену флага, а ссылку из
письма, которое Keycloak положил в Mailpit. После неё новый токен должен
принести ``email_verified=true``, а локальный профиль — получить полный набор
возможностей.
"""
from __future__ import annotations

import asyncio
import html
import os
import re
import secrets
import sys
import uuid
from urllib.parse import urlparse

import httpx
import redis.asyncio as aioredis

from login_check import (
    API,
    KEYCLOAK,
    ORIGIN,
    REALM,
    REDIRECT,
    _cleanup,
    _pool,
    admin_token,
    authorization_code,
    create_user,
    pkce,
)

MAILPIT = os.getenv(
    "MAILPIT_URL", "http://messenger-mailpit-http.messenger.svc.cluster.local"
)
REDIS = os.getenv(
    "REDIS_SECURITY_URL", "redis://messenger-redis.messenger.svc.cluster.local:6379/2"
)

failures: list[str] = []


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


async def exchange(
    http: httpx.AsyncClient, login: str, password: str
) -> tuple[str, uuid.UUID] | None:
    verifier, challenge = pkce()
    code = await authorization_code(login, password, challenge)
    if code is None:
        return None
    device = uuid.uuid4()
    response = await http.post(
        f"{API}/auth/callback",
        json={
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT,
            "device_id": str(device),
        },
        headers={"Origin": ORIGIN, "User-Agent": "checks/verification"},
    )
    if response.status_code != 200:
        return None
    return response.json()["access_token"], device


async def messages_for(http: httpx.AsyncClient, address: str) -> list[dict]:
    response = await http.get(f"{MAILPIT}/api/v1/messages", params={"limit": 100})
    response.raise_for_status()
    return [
        message
        for message in response.json().get("messages", [])
        if any(recipient.get("Address") == address for recipient in message.get("To", []))
    ]


async def wait_for_messages(
    http: httpx.AsyncClient, address: str, expected: int
) -> list[dict]:
    found: list[dict] = []
    for _ in range(30):
        found = await messages_for(http, address)
        if len(found) >= expected:
            return found
        await asyncio.sleep(0.2)
    return found


async def confirmation_link(http: httpx.AsyncClient, message_id: str) -> str | None:
    response = await http.get(f"{MAILPIT}/api/v1/message/{message_id}")
    response.raise_for_status()
    body = response.json().get("HTML", "") + "\n" + response.json().get("Text", "")
    candidates = re.findall(r'https?://[^"<>\s]+', html.unescape(body))
    return next((url for url in candidates if "login-actions/action-token" in url), None)


def internal_link(external: str) -> str:
    parsed = urlparse(external)
    return f"{KEYCLOAK}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")


async def run() -> None:
    login = f"verify-{uuid.uuid4().hex[:12]}@example.org"
    password = secrets.token_urlsafe(24)
    message_ids: list[str] = []
    local_user_id: str | None = None

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as http:
        admin = await admin_token(http)
        external_id = await create_user(
            http,
            admin,
            login=login,
            password=password,
            email_verified=False,
        )
        try:
            first = await exchange(http, login, password)
            check(
                "неподтверждённый адрес не блокирует выдачу токена",
                first is not None,
                "Keycloak не вернул код или API не обменял его",
            )
            if first is None:
                return
            access, device = first
            headers = {
                "Authorization": f"Bearer {access}",
                "X-Device-Id": str(device),
            }

            profile = await http.get(f"{API}/me", headers=headers)
            body = profile.json() if profile.status_code == 200 else {}
            local_user_id = body.get("user_id")
            check(
                "до подтверждения профиль явно ограничен",
                profile.status_code == 200
                and body.get("email_verified") is False
                and body.get("capabilities") == ["read", "send_message"],
                f"{profile.status_code}: {profile.text[:180]}",
            )

            mail = await wait_for_messages(http, login, 1)
            check("первое письмо дошло до Mailpit", len(mail) == 1, f"писем {len(mail)}")

            statuses = []
            for _ in range(3):
                response = await http.post(f"{API}/auth/verify-email/resend", headers=headers)
                statuses.append(response.status_code)
            check("три повтора письма разрешены", statuses == [202, 202, 202], str(statuses))

            limited = await http.post(f"{API}/auth/verify-email/resend", headers=headers)
            check(
                "четвёртый повтор ограничен с Retry-After",
                limited.status_code == 429
                and int(limited.headers.get("Retry-After", "0")) > 0,
                f"{limited.status_code}: {limited.headers.get('Retry-After')}",
            )

            mail = await wait_for_messages(http, login, 4)
            message_ids = [message["ID"] for message in mail]
            check("лишнее письмо при отказе не отправлено", len(mail) == 4, f"писем {len(mail)}")
            if not mail:
                return

            link = await confirmation_link(http, mail[0]["ID"])
            check("в письме есть одноразовая ссылка Keycloak", link is not None)
            if link is None:
                return
            followed = await http.get(internal_link(link))
            check(
                "ссылка подтверждения принята",
                followed.status_code in (200, 302, 303),
                f"статус {followed.status_code}",
            )

            user_response = await http.get(
                f"{KEYCLOAK}/admin/realms/{REALM}/users/{external_id}",
                headers={"Authorization": f"Bearer {admin}"},
            )
            check(
                "Keycloak отметил адрес подтверждённым",
                user_response.status_code == 200
                and user_response.json().get("emailVerified") is True,
                f"{user_response.status_code}: {user_response.text[:160]}",
            )

            second = await exchange(http, login, password)
            check("после подтверждения выдан новый токен", second is not None)
            if second is None:
                return
            verified_access, verified_device = second
            verified = await http.get(
                f"{API}/me",
                headers={
                    "Authorization": f"Bearer {verified_access}",
                    "X-Device-Id": str(verified_device),
                },
            )
            verified_body = verified.json() if verified.status_code == 200 else {}
            check(
                "после подтверждения доступны все возможности",
                verified.status_code == 200
                and verified_body.get("email_verified") is True
                and verified_body.get("capabilities")
                == ["read", "send_message", "start_conversation"],
                f"{verified.status_code}: {verified.text[:180]}",
            )
            no_more = await http.post(
                f"{API}/auth/verify-email/resend",
                headers={"Authorization": f"Bearer {verified_access}"},
            )
            check("подтверждённому письмо не отправляется", no_more.status_code == 409)

            pool = await _pool()
            try:
                stored = await pool.fetchval(
                    "SELECT email_verified FROM users WHERE external_id = $1", external_id
                )
            finally:
                await pool.close()
            check("локальный профиль обновлён из нового токена", stored is True)
        finally:
            if local_user_id:
                redis = aioredis.from_url(REDIS)
                try:
                    await redis.delete(f"verify-email:{local_user_id}")
                finally:
                    await redis.aclose()
            if message_ids:
                await http.request(
                    "DELETE", f"{MAILPIT}/api/v1/messages", json={"IDs": message_ids}
                )
            await _cleanup(http, admin, external_id)


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nподтверждение адреса доказано письмом и одноразовой ссылкой")
    return 0


if __name__ == "__main__":
    sys.exit(main())
