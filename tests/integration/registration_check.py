"""AUTH-001: самостоятельная регистрация и первый вход через живую форму."""
from __future__ import annotations

import asyncio
import html
import re
import secrets
import sys
import uuid
from urllib.parse import parse_qs, urlparse

import httpx

from login_check import (
    API,
    KEYCLOAK,
    ORIGIN,
    REALM,
    REDIRECT,
    _cleanup,
    _pool,
    admin_token,
    pkce,
)
from verification_check import MAILPIT, messages_for, wait_for_messages

failures: list[str] = []


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


def internal_link(external: str) -> str:
    parsed = urlparse(html.unescape(external))
    return f"{KEYCLOAK}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")


async def registration_code(
    *, login: str, password: str, challenge: str
) -> str | None:
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as browser:
        page = await browser.get(
            f"{KEYCLOAK}/realms/{REALM}/protocol/openid-connect/auth",
            params={
                "client_id": "messenger-web",
                "response_type": "code",
                "scope": "openid",
                "redirect_uri": REDIRECT,
                "state": uuid.uuid4().hex,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        page.raise_for_status()
        link = re.search(r'href="([^"]*registration[^"]*)', page.text, re.I)
        if link is None:
            return None
        form_page = await browser.get(internal_link(link.group(1)))
        form_page.raise_for_status()
        form = re.search(r'<form[^>]+action="([^"]+)"', form_page.text, re.I)
        if form is None:
            return None
        submitted = await browser.post(
            internal_link(form.group(1)),
            data={
                "email": login,
                "firstName": "Проверка",
                "lastName": "Регистрации",
                "password": password,
                "password-confirm": password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        location = submitted.headers.get("location", "")
        return parse_qs(urlparse(location).query).get("code", [None])[0]


async def find_external_id(
    http: httpx.AsyncClient, *, admin: str, login: str
) -> str | None:
    response = await http.get(
        f"{KEYCLOAK}/admin/realms/{REALM}/users",
        headers={"Authorization": f"Bearer {admin}"},
        params={"username": login, "exact": "true"},
    )
    response.raise_for_status()
    return response.json()[0]["id"] if response.json() else None


async def run() -> None:
    login = f"register-{uuid.uuid4().hex[:12]}@example.org"
    password = "R9!" + secrets.token_urlsafe(20)
    verifier, challenge = pkce()
    external_id: str | None = None
    message_ids: list[str] = []

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as http:
        admin = await admin_token(http)
        try:
            code = await registration_code(
                login=login, password=password, challenge=challenge
            )
            external_id = await find_external_id(http, admin=admin, login=login)
            check("публичная форма создала учётную запись", external_id is not None)
            check("регистрация выдала код авторизации", code is not None)
            if code is None or external_id is None:
                return

            exchanged = await http.post(
                f"{API}/auth/callback",
                json={
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": REDIRECT,
                    "device_id": str(uuid.uuid4()),
                },
                headers={"Origin": ORIGIN, "User-Agent": "checks/registration"},
            )
            check(
                "код регистрации обменян через API",
                exchanged.status_code == 200,
                f"{exchanged.status_code}: {exchanged.text[:180]}",
            )
            if exchanged.status_code != 200:
                return
            access = exchanged.json().get("access_token")
            check("после регистрации выдан токен", bool(access))
            if not access:
                return

            profile = await http.get(
                f"{API}/me", headers={"Authorization": f"Bearer {access}"}
            )
            body = profile.json() if profile.status_code == 200 else {}
            check(
                "локальный профиль создан неподтверждённым",
                profile.status_code == 200
                and body.get("email") == login
                and body.get("email_verified") is False,
                f"{profile.status_code}: {profile.text[:180]}",
            )

            pool = await _pool()
            try:
                memberships = await pool.fetchval(
                    """
                    SELECT count(*)
                      FROM conversation_members cm
                      JOIN users u ON u.user_id = cm.user_id
                     WHERE u.external_id = $1
                    """,
                    external_id,
                )
            finally:
                await pool.close()
            check("у нового пользователя нет бесед", memberships == 0)

            messages = await wait_for_messages(http, login, 1)
            message_ids = [message["ID"] for message in messages]
            check("первое письмо подтверждения отправлено", len(messages) == 1)
        finally:
            if not message_ids:
                message_ids = [message["ID"] for message in await messages_for(http, login)]
            if message_ids:
                await http.request(
                    "DELETE", f"{MAILPIT}/api/v1/messages", json={"IDs": message_ids}
                )
            if external_id is None:
                external_id = await find_external_id(http, admin=admin, login=login)
            if external_id is not None:
                await _cleanup(http, admin, external_id)


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nсамостоятельная регистрация подтверждена живой формой")
    return 0


if __name__ == "__main__":
    sys.exit(main())
