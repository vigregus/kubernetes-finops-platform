"""AUTH-002: один вход закрывается, второй продолжает работать.

Два независимых прохода страницы Keycloak дают две SSO-сессии и два
устройства. Выход выполняется через настоящий API, после чего первый access
и refresh больше не работают у нас, а второй вход по-прежнему обновляется.
"""
from __future__ import annotations

import asyncio
import secrets
import sys
import uuid

import httpx

from login_check import (
    API,
    COOKIE,
    ORIGIN,
    REDIRECT,
    _cleanup,
    _pool,
    admin_token,
    authorization_code,
    create_user,
    pkce,
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


async def _login(
    http: httpx.AsyncClient, login: str, password: str, device_id: uuid.UUID
) -> tuple[str, str] | None:
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
            "device_id": str(device_id),
        },
        headers={"Origin": ORIGIN, "User-Agent": "checks/logout"},
    )
    if response.status_code != 200:
        return None
    refresh = response.cookies.get(COOKIE)
    if not refresh:
        return None
    return response.json()["access_token"], refresh


async def run() -> None:
    login = f"logout-{uuid.uuid4().hex[:12]}@example.org"
    password = secrets.token_urlsafe(24)
    device_a, device_b = uuid.uuid4(), uuid.uuid4()

    async with httpx.AsyncClient(timeout=15.0) as http:
        admin = await admin_token(http)
        external_id = await create_user(http, admin, login=login, password=password)
        try:
            first = await _login(http, login, password, device_a)
            second = await _login(http, login, password, device_b)
            check("два независимых входа получили токены", first is not None and second is not None)
            if first is None or second is None:
                return
            access_a, refresh_a = first
            access_b, refresh_b = second

            listed = await http.get(
                f"{API}/sessions",
                headers={
                    "Authorization": f"Bearer {access_a}",
                    "X-Device-Id": str(device_a),
                },
            )
            check(
                "API показывает оба входа",
                listed.status_code == 200 and len(listed.json().get("items", [])) == 2,
                f"{listed.status_code}: {listed.text[:160]}",
            )
            if listed.status_code != 200:
                return
            current = [item for item in listed.json()["items"] if item["current"]]
            check("текущий вход помечен ровно один", len(current) == 1)
            if len(current) != 1:
                return

            ended = await http.delete(
                f"{API}/sessions/{current[0]['session_id']}",
                headers={
                    "Authorization": f"Bearer {access_a}",
                    "X-Device-Id": str(device_a),
                },
            )
            check("текущий вход отозван", ended.status_code == 204, str(ended.status_code))
            check(
                "refresh-cookie текущего входа снята",
                "max-age=0" in ended.headers.get("set-cookie", "").lower(),
                ended.headers.get("set-cookie", "")[:120],
            )

            rejected = await http.get(
                f"{API}/sessions", headers={"Authorization": f"Bearer {access_a}"}
            )
            check("старый access больше не принимается", rejected.status_code == 401)

            stale_refresh = await http.post(
                f"{API}/auth/refresh",
                cookies={COOKIE: refresh_a},
                headers={"Origin": ORIGIN},
            )
            check("старый refresh больше не возвращает доступ", stale_refresh.status_code == 401)

            survivor = await http.get(
                f"{API}/sessions",
                headers={
                    "Authorization": f"Bearer {access_b}",
                    "X-Device-Id": str(device_b),
                },
            )
            check(
                "второй вход продолжает работать",
                survivor.status_code == 200
                and len(survivor.json().get("items", [])) == 1
                and survivor.json()["items"][0]["current"],
                f"{survivor.status_code}: {survivor.text[:160]}",
            )
            refreshed = await http.post(
                f"{API}/auth/refresh",
                cookies={COOKIE: refresh_b},
                headers={"Origin": ORIGIN, "X-Device-Id": str(device_b)},
            )
            check("refresh второго входа работает", refreshed.status_code == 200)

            pool = await _pool()
            try:
                row = await pool.fetchrow(
                    """
                    SELECT count(*) FILTER (WHERE s.revoked_at IS NULL) AS live,
                           count(*) FILTER (
                               WHERE s.revoked_reason = 'logout_device'
                           ) AS logged_out
                      FROM sessions s
                      JOIN users u ON u.user_id = s.user_id
                     WHERE u.external_id = $1
                    """,
                    external_id,
                )
            finally:
                await pool.close()
            check(
                "в базе отозвана ровно одна сессия",
                row is not None and row["live"] == 1 and row["logged_out"] == 1,
                str(dict(row)) if row else "нет строки",
            )
        finally:
            await _cleanup(http, admin, external_id)


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nвыход на одном устройстве подтверждён двумя живыми входами")
    return 0


if __name__ == "__main__":
    sys.exit(main())
