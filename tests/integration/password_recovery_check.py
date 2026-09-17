"""AUTH-005: восстановление доступа отзывает все прежние сессии."""
from __future__ import annotations

import asyncio
import html
import os
import re
import secrets
import sys
import uuid
from urllib.parse import urljoin, urlparse

import httpx

from login_check import (
    API,
    COOKIE,
    KEYCLOAK,
    OIDC_CLIENT_ID,
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
from verification_check import (
    MAILPIT,
    confirmation_link,
    internal_link,
    messages_for,
    page_text,
    wait_for_messages,
)

failures: list[str] = []
BACKCHANNEL_TEST_URL = os.getenv("BACKCHANNEL_TEST_URL")


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


async def login(
    http: httpx.AsyncClient, username: str, password: str
) -> tuple[str, str] | None:
    verifier, challenge = pkce()
    code = await authorization_code(username, password, challenge)
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
        headers={"Origin": ORIGIN, "User-Agent": "checks/password-recovery"},
    )
    if response.status_code != 200:
        return None
    refresh = response.cookies.get(COOKIE)
    access = response.json().get("access_token")
    return (access, refresh) if access and refresh else None


async def change_password(link: str, new_password: str) -> httpx.Response:
    """Проходит action-token и форму UPDATE_PASSWORD в настоящем Keycloak."""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as browser:
        response = await browser.get(internal_link(link))
        for _ in range(10):
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    return response
                target = urljoin(str(response.url), html.unescape(location))
                if urlparse(target).path.startswith(f"/realms/{REALM}/"):
                    response = await browser.get(internal_link(target))
                    continue
                return response

            proceed = next(
                (
                    href
                    for href, label in re.findall(
                        r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                        response.text,
                        flags=re.S | re.I,
                    )
                    if "proceed" in page_text(label).lower()
                ),
                None,
            )
            if proceed:
                response = await browser.get(
                    internal_link(html.unescape(proceed))
                )
                continue

            form = re.search(r'<form[^>]+action="([^"]+)"', response.text, re.I)
            if form is None:
                return response
            names = set(re.findall(r'name="([^"]+)"', response.text))
            if "password-new" not in names:
                return response
            response = await browser.post(
                internal_link(html.unescape(form.group(1))),
                data={
                    "password-new": new_password,
                    "password-confirm": new_password,
                    "logout-sessions": "on",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        return response


async def refresh(http: httpx.AsyncClient, token: str) -> httpx.Response:
    return await http.post(
        f"{API}/auth/refresh",
        headers={"Origin": ORIGIN, "Cookie": f"{COOKIE}={token}"},
    )


async def create_test_client(http: httpx.AsyncClient, *, admin: str, url: str) -> str:
    """Клонирует публичный клиент, не меняя используемый окружением."""
    headers = {"Authorization": f"Bearer {admin}"}
    found = await http.get(
        f"{KEYCLOAK}/admin/realms/{REALM}/clients",
        headers=headers,
        params={"clientId": "messenger-web"},
    )
    found.raise_for_status()
    current = await http.get(
        f"{KEYCLOAK}/admin/realms/{REALM}/clients/{found.json()[0]['id']}",
        headers=headers,
    )
    current.raise_for_status()
    source = current.json()
    attributes = {
        key: value
        for key, value in source.get("attributes", {}).items()
        if key.startswith("pkce.") or key == "post.logout.redirect.uris"
    }
    attributes["backchannel.logout.url"] = url
    attributes["backchannel.logout.session.required"] = "true"
    patched = {
        "clientId": OIDC_CLIENT_ID,
        "name": "Временный клиент проверки восстановления доступа",
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": True,
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "frontchannelLogout": False,
        "redirectUris": source.get("redirectUris", [REDIRECT]),
        "webOrigins": source.get("webOrigins", [ORIGIN]),
        "defaultClientScopes": source.get("defaultClientScopes", []),
        "optionalClientScopes": source.get("optionalClientScopes", []),
        "protocolMappers": [
            {
                "name": mapper["name"],
                "protocol": mapper["protocol"],
                "protocolMapper": mapper["protocolMapper"],
                "config": mapper.get("config", {}),
            }
            for mapper in source.get("protocolMappers", [])
        ],
        "attributes": attributes,
    }
    response = await http.post(
        f"{KEYCLOAK}/admin/realms/{REALM}/clients",
        headers={**headers, "Content-Type": "application/json"},
        json=patched,
    )
    if response.is_error:
        raise RuntimeError(
            f"temporary client creation failed: {response.status_code} {response.text}"
        )
    created = await http.get(
        f"{KEYCLOAK}/admin/realms/{REALM}/clients",
        headers=headers,
        params={"clientId": OIDC_CLIENT_ID},
    )
    created.raise_for_status()
    return created.json()[0]["id"]


async def delete_test_client(
    http: httpx.AsyncClient, *, admin: str, client_id: str
) -> None:
    response = await http.delete(
        f"{KEYCLOAK}/admin/realms/{REALM}/clients/{client_id}",
        headers={"Authorization": f"Bearer {admin}"},
    )
    if response.status_code != 404:
        response.raise_for_status()


async def cleanup_stale_artifacts(http: httpx.AsyncClient, *, admin: str) -> None:
    """Убирает только следы прежнего упавшего запуска этой проверки."""
    headers = {"Authorization": f"Bearer {admin}"}
    clients = await http.get(
        f"{KEYCLOAK}/admin/realms/{REALM}/clients",
        headers=headers,
        params={"clientId": OIDC_CLIENT_ID},
    )
    clients.raise_for_status()
    for client in clients.json():
        if client.get("clientId") == OIDC_CLIENT_ID:
            await delete_test_client(http, admin=admin, client_id=client["id"])

    users = await http.get(
        f"{KEYCLOAK}/admin/realms/{REALM}/users",
        headers=headers,
        params={"search": "recover-", "max": 100},
    )
    users.raise_for_status()
    for user in users.json():
        if str(user.get("username", "")).startswith("recover-"):
            await _cleanup(http, admin, user["id"])


async def run() -> None:
    username = f"recover-{uuid.uuid4().hex[:12]}@example.org"
    old_password = "O8!" + secrets.token_urlsafe(20)
    new_password = "N9!" + secrets.token_urlsafe(20)
    message_ids: list[str] = []
    test_client_id: str | None = None

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as http:
        admin = await admin_token(http)
        if BACKCHANNEL_TEST_URL:
            if OIDC_CLIENT_ID == "messenger-web":
                raise RuntimeError("back-channel test must use an isolated OIDC client")
            await cleanup_stale_artifacts(http, admin=admin)
        external_id = await create_user(
            http,
            admin,
            login=username,
            password=old_password,
            email_verified=True,
        )
        try:
            if BACKCHANNEL_TEST_URL:
                test_client_id = await create_test_client(
                    http, admin=admin, url=BACKCHANNEL_TEST_URL
                )
            first = await login(http, username, old_password)
            second = await login(http, username, old_password)
            check("два прежних входа получили токены", first is not None and second is not None)
            if first is None or second is None:
                return

            requested = await http.put(
                f"{KEYCLOAK}/admin/realms/{REALM}/users/{external_id}/execute-actions-email",
                params={"client_id": OIDC_CLIENT_ID, "redirect_uri": REDIRECT},
                headers={"Authorization": f"Bearer {admin}"},
                json=["UPDATE_PASSWORD"],
            )
            check(
                "Keycloak отправил письмо восстановления",
                requested.status_code == 204,
                f"{requested.status_code}: {requested.text[:160]}",
            )
            if requested.status_code != 204:
                return
            mail = await wait_for_messages(http, username, 1)
            message_ids = [message["ID"] for message in mail]
            check("письмо восстановления дошло до Mailpit", len(mail) == 1)
            if not mail:
                return
            link = await confirmation_link(http, mail[0]["ID"])
            check("в письме есть одноразовая ссылка", link is not None)
            if link is None:
                return
            changed = await change_password(link, new_password)
            password_form_remains = "password-new" in set(
                re.findall(r'name="([^"]+)"', changed.text)
            )
            check(
                "форма сменила пароль и завершила действие",
                changed.status_code in (200, 302, 303) and not password_form_remains,
                f"{changed.status_code} {changed.url}: {page_text(changed.text)!r}",
            )
            if password_form_remains:
                return

            for label, pair in (("первый", first), ("второй", second)):
                access, refresh_token = pair
                old_access = await http.get(
                    f"{API}/me", headers={"Authorization": f"Bearer {access}"}
                )
                check(f"старый access: {label} вход отозван", old_access.status_code == 401)
                old_refresh = await refresh(http, refresh_token)
                check(f"старый refresh: {label} вход отозван", old_refresh.status_code == 401)

            pool = await _pool()
            try:
                reasons = await pool.fetch(
                    """
                    SELECT revoked_reason
                      FROM sessions
                     WHERE user_id = (
                         SELECT user_id FROM users WHERE external_id = $1
                     )
                     ORDER BY created_at
                    """,
                    external_id,
                )
            finally:
                await pool.close()
            check(
                "обе локальные сессии отозваны причиной password_change",
                len(reasons) == 2
                and all(row["revoked_reason"] == "password_change" for row in reasons),
                str([row["revoked_reason"] for row in reasons]),
            )

            fresh = await login(http, username, new_password)
            check("новый пароль открывает новый вход", fresh is not None)
        finally:
            try:
                if not message_ids:
                    message_ids = [
                        message["ID"]
                        for message in await messages_for(http, username)
                    ]
                if message_ids:
                    await http.request(
                        "DELETE",
                        f"{MAILPIT}/api/v1/messages",
                        json={"IDs": message_ids},
                    )
                await _cleanup(http, admin, external_id)
            finally:
                if test_client_id is not None:
                    await delete_test_client(
                        http, admin=admin, client_id=test_client_id
                    )
                    remaining = await http.get(
                        f"{KEYCLOAK}/admin/realms/{REALM}/clients",
                        headers={"Authorization": f"Bearer {admin}"},
                        params={"clientId": OIDC_CLIENT_ID},
                    )
                    remaining.raise_for_status()
                    check(
                        "временный OIDC-клиент удалён",
                        not any(
                            client.get("clientId") == OIDC_CLIENT_ID
                            for client in remaining.json()
                        ),
                    )


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nвосстановление доступа отозвало все прежние сессии")
    return 0


if __name__ == "__main__":
    sys.exit(main())
