"""Вход целиком: страница Keycloak, код, обмен на нашей стороне.

Самая близкая к браузеру проверка, какая возможна без браузера: запрос
на страницу входа, отправка формы, перехват кода из перенаправления
и обмен через наш собственный `POST /auth/callback`. Всё, что после кода,
идёт ровно тем путём, каким пойдёт настоящая вкладка.

Учётная запись заводится на время проверки и удаляется в конце — вместе
со строками, которые появились из-за неё в нашей базе.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import secrets
import sys
import uuid
from urllib.parse import parse_qs, urlparse

import httpx

from messenger.repositories.postgres import PoolSettings, create_pool

failures: list[str] = []

KEYCLOAK = os.getenv(
    "KEYCLOAK_URL", "http://messenger-idp-service.keycloak.svc.cluster.local:8080"
)
REALM = os.getenv("KEYCLOAK_REALM", "messenger")
API = os.getenv("API_URL", "http://api.messenger.svc.cluster.local")
ORIGIN = os.getenv("WEB_ORIGIN", "https://app.finops.local")
REDIRECT = f"{ORIGIN}/callback"
COOKIE = os.getenv("AUTH_COOKIE_NAME", "messenger_refresh")


def ok(what: str) -> None:
    print(f"  \033[32m✓\033[0m {what}")


def bad(what: str, detail: str = "") -> None:
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


def check(what: str, condition: bool, detail: str = "") -> None:
    ok(what) if condition else bad(what, detail)


def pkce() -> tuple[str, str]:
    """Секрет и его отпечаток. Без PKCE перехваченный код меняет кто угодно."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def admin_token(http: httpx.AsyncClient) -> str:
    r = await http.post(
        f"{KEYCLOAK}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": os.environ["KEYCLOAK_ADMIN"],
            "password": os.environ["KEYCLOAK_ADMIN_PASSWORD"],
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


async def create_user(http: httpx.AsyncClient, token: str, *, login: str, password: str) -> str:
    """Заводит учётную запись с подтверждённым адресом.

    Подтверждённым намеренно: реалм требует подтверждения, и без него
    Keycloak показал бы не код, а страницу «проверьте почту». Сам путь
    подтверждения — это AUTH-006 и отдельная задача.
    """
    headers = {"Authorization": f"Bearer {token}"}
    r = await http.post(
        f"{KEYCLOAK}/admin/realms/{REALM}/users",
        headers=headers,
        json={
            "username": login,
            "email": login,
            "emailVerified": True,
            "enabled": True,
            "firstName": "Проверка",
            "lastName": "Входа",
            "requiredActions": [],
            "credentials": [{"type": "password", "value": password, "temporary": False}],
        },
    )
    r.raise_for_status()
    found = await http.get(
        f"{KEYCLOAK}/admin/realms/{REALM}/users", headers=headers, params={"username": login}
    )
    found.raise_for_status()
    return found.json()[0]["id"]


async def authorization_code(login: str, password: str, challenge: str) -> str | None:
    """Проходит страницу входа и возвращает код из перенаправления."""
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as browser:
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
        form = re.search(r'action="([^"]+)"', page.text)
        if not form:
            return None
        action = form.group(1).replace("&amp;", "&")
        # Keycloak строит абсолютные ссылки от своего hostname, то есть
        # форма ведёт на https://idp.finops.local — имя, которого изнутри
        # кластера нет. Браузеру это подходит, проверке нет, поэтому
        # источник подменяется на внутренний. Та же особенность, из-за
        # которой издатель и адрес ключей у приложения разные.
        parsed = urlparse(action)
        action = f"{KEYCLOAK}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")

        submitted = await browser.post(
            action,
            data={"username": login, "password": password},
            cookies=page.cookies,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        location = submitted.headers.get("location", "")
        return parse_qs(urlparse(location).query).get("code", [None])[0]


async def run() -> None:
    login = f"check-{uuid.uuid4().hex[:12]}@example.org"
    password = secrets.token_urlsafe(24)
    verifier, challenge = pkce()

    async with httpx.AsyncClient(timeout=15.0) as http:
        token = await admin_token(http)
        user_id = await create_user(http, token, login=login, password=password)
        try:
            await _run_checks(http, user_id, login, password, verifier, challenge)
        finally:
            await _cleanup(http, token, user_id)


async def _pool():
    return await create_pool(
        PoolSettings(
            host=os.getenv("DATABASE_HOST", "messenger-db-pool"),
            port=int(os.getenv("DATABASE_PORT", "5432")),
            database=os.getenv("DATABASE_NAME", "messenger"),
            user=os.getenv("DATABASE_USER", "messenger"),
            password=os.getenv("DATABASE_PASSWORD", ""),
            min_size=1,
            max_size=2,
        ),
        application_name="messenger-integration",
    )


async def _cleanup(http: httpx.AsyncClient, token: str, user_id: str) -> None:
    """Убирает за собой при любом исходе.

    Уборка внутри успешной ветки — это уборка, которой не будет ровно
    тогда, когда она нужна: первый же отказ оставил профиль в настоящей
    базе, и нашёлся он не проверкой, а взглядом в таблицу.
    """
    pool = await _pool()
    try:
        await pool.execute(
            "DELETE FROM sessions WHERE user_id IN "
            "(SELECT user_id FROM users WHERE external_id = $1)", user_id
        )
        await pool.execute(
            "DELETE FROM devices WHERE user_id IN "
            "(SELECT user_id FROM users WHERE external_id = $1)", user_id
        )
        await pool.execute("DELETE FROM users WHERE external_id = $1", user_id)
    finally:
        await pool.close()
    await http.delete(
        f"{KEYCLOAK}/admin/realms/{REALM}/users/{user_id}",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _run_checks(http, user_id, login, password, verifier, challenge) -> None:
        code = await authorization_code(login, password, challenge)
        check("страница входа выдала код авторизации", code is not None)
        if code is None:
            return

        # --- обмен на нашей стороне ---------------------------------
        exchanged = await http.post(
            f"{API}/auth/callback",
            json={"code": code, "code_verifier": verifier, "redirect_uri": REDIRECT},
            headers={"Origin": ORIGIN, "User-Agent": "checks/1.0"},
        )
        check(
            "код обменян на токены",
            exchanged.status_code == 200,
            f"{exchanged.status_code}: {exchanged.text[:200]}",
        )
        if exchanged.status_code != 200:
            return

        body = exchanged.json()
        check("токен доступа вернулся в теле", bool(body.get("access_token")))
        refresh = exchanged.cookies.get(COOKIE)
        check("токен обновления пришёл только в cookie", bool(refresh))
        установка = exchanged.headers.get("set-cookie", "")
        check("cookie недоступна скрипту", "HttpOnly" in установка, установка[:120])
        check(
            "cookie не уходит на посторонние сайты",
            "strict" in установка.lower(),
            установка[:120],
        )
        check(
            "токена обновления нет в теле ответа",
            refresh is not None and refresh not in exchanged.text,
        )

        # --- что появилось в нашей базе -----------------------------
        pool = await _pool()
        try:
            профиль = await pool.fetchrow(
                "SELECT user_id, email, email_verified FROM users WHERE external_id = $1",
                user_id,
            )
            check(
                "первый вход завёл профиль",
                профиль is not None and профиль["email"] == login,
                f"в базе {профиль['email'] if профиль else None!r}",
            )
            check(
                "признак подтверждённого адреса перенесён из токена",
                профиль is not None and профиль["email_verified"] is True,
            )
            сессий = await pool.fetchval(
                "SELECT count(*) FROM sessions WHERE user_id = $1 AND revoked_at IS NULL",
                профиль["user_id"],
            )
            check("вход отражён сессией", сессий == 1, f"сессий {сессий}")
            устройств = await pool.fetchval(
                "SELECT count(*) FROM devices WHERE user_id = $1", профиль["user_id"]
            )
            check("устройство заведено", устройств == 1, f"устройств {устройств}")

            # --- перезагрузка вкладки ------------------------------
            # cookie передаётся явно: путь у неё `/api/v1/auth`, а перед
            # проверкой нет шлюза, снимающего префикс, — клиент по пути
            # её просто не приложил бы.
            обновление = await http.post(
                f"{API}/auth/refresh",
                cookies={COOKIE: refresh},
                headers={"Origin": ORIGIN, "User-Agent": "checks/1.0"},
            )
            check(
                "cookie меняется на новый токен доступа",
                обновление.status_code == 200,
                f"{обновление.status_code}: {обновление.text[:200]}",
            )
            check(
                "вторая вкладка не завела вторую сессию",
                await pool.fetchval(
                    "SELECT count(*) FROM sessions WHERE user_id = $1 AND revoked_at IS NULL",
                    профиль["user_id"],
                ) == 1,
            )

            # --- отказы --------------------------------------------
            повтор = await http.post(
                f"{API}/auth/callback",
                json={"code": code, "code_verifier": verifier, "redirect_uri": REDIRECT},
                headers={"Origin": ORIGIN},
            )
            check(
                "повторно предъявленный код не работает",
                повтор.status_code == 401,
                str(повтор.status_code),
            )
            чужой = await http.post(
                f"{API}/auth/refresh",
                cookies={COOKIE: refresh},
                headers={"Origin": "https://evil.example"},
            )
            check(
                "запрос с чужой страницы отклонён",
                чужой.status_code == 403,
                str(чужой.status_code),
            )
            # Значение латиницей: заголовки кодируются в latin-1,
            # и кириллица в cookie роняет сам запрос, не дойдя до API.
            мусор = await http.post(
                f"{API}/auth/refresh",
                cookies={COOKIE: "not-a-token"},
                headers={"Origin": ORIGIN},
            )
            check(
                "негодная cookie не пускает и снимается",
                мусор.status_code == 401 and "Max-Age=0" in мусор.headers.get("set-cookie", ""),
                f"{мусор.status_code}: {мусор.headers.get('set-cookie', '')[:80]}",
            )
        finally:
            await pool.close()


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nвход подтверждён от страницы Keycloak до записи в базе")
    return 0


if __name__ == "__main__":
    sys.exit(main())
