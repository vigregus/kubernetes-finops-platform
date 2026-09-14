"""AUTH-003 realtime: отзыв сессии рвёт настоящий WebSocket, а не только строку.

G1-008. Два входа открывают по WebSocket-соединению к Centrifugo тем же путём,
что клиент: `POST /realtime/token` даёт connect-токен, подключение сообщает
назначенный Centrifugo `client` обратно через `POST /realtime/connections`.

Дальше доказывается именно разрыв сокета, а не отзыв в базе:
- «выйти на устройстве A» рвёт WebSocket A (disconnect code 3000), при этом
  WebSocket B остаётся открытым и получает событие `session.revoked` с
  идентификатором сессии A;
- «выйти везде» рвёт WebSocket B тем же кодом.

Отзыв только в Postgres сокетов бы не тронул: это проверка на живом
соединении, а не на состоянии таблицы.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import uuid

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from login_check import (
    API,
    COOKIE,
    ORIGIN,
    REDIRECT,
    _cleanup,
    admin_token,
    authorization_code,
    create_user,
    pkce,
)

failures: list[str] = []

# Тот же код и причина, что сервер кладёт в disconnect при отзыве сессии.
DISCONNECT_CODE = 3000
REASON = "session_revoked"

CENTRIFUGO_URL = os.getenv(
    "CENTRIFUGO_CLIENT_URL",
    "ws://messenger-centrifugo.messenger.svc.cluster.local:8000/connection/websocket",
)


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


def _find(msg: dict, key: str):
    """Ключ на верхнем уровне либо внутри обёртки `push`."""
    if not isinstance(msg, dict):
        return None
    if key in msg:
        return msg[key]
    push = msg.get("push")
    if isinstance(push, dict) and key in push:
        return push[key]
    return None


def _find_pub_data(msg: dict):
    """Данные публикации, где бы ни лежал `pub`; `data` — строка или объект."""
    pub = None
    if isinstance(msg.get("pub"), dict):
        pub = msg["pub"]
    else:
        push = msg.get("push")
        if isinstance(push, dict) and isinstance(push.get("pub"), dict):
            pub = push["pub"]
    if pub is None:
        return None
    data = pub.get("data")
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


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
        headers={"Origin": ORIGIN, "User-Agent": "checks/realtime-revoke"},
    )
    if response.status_code != 200:
        return None
    refresh = response.cookies.get(COOKIE)
    if not refresh:
        return None
    return response.json()["access_token"], refresh


def _auth_headers(access: str, device_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}", "X-Device-Id": str(device_id)}


async def _realtime_token(
    http: httpx.AsyncClient, access: str, device_id: uuid.UUID
) -> str | None:
    r = await http.post(
        f"{API}/realtime/token", headers=_auth_headers(access, device_id)
    )
    if r.status_code != 200:
        return None
    return r.json().get("token")


async def _connect(
    http: httpx.AsyncClient, access: str, device_id: uuid.UUID, token: str
) -> tuple[object | None, str | None]:
    """Открывает WebSocket, подключается и регистрирует `client` у API."""
    try:
        ws = await websockets.connect(CENTRIFUGO_URL, open_timeout=10.0)
    except Exception:
        return None, None
    try:
        await ws.send(json.dumps({"id": 1, "connect": {"token": token, "name": "checks"}}))
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
        connect = reply.get("connect") if isinstance(reply, dict) else None
        client_id = connect.get("client") if isinstance(connect, dict) else None
        if not client_id:
            return ws, None
        registered = await http.post(
            f"{API}/realtime/connections",
            json={"client_id": client_id},
            headers=_auth_headers(access, device_id),
        )
        if registered.status_code != 204:
            return ws, None
        return ws, client_id
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass
        return None, None


async def _expect_disconnect(ws, timeout: float = 20.0) -> tuple[bool, str]:
    """Ждёт disconnect-код 3000 либо закрытие сокета — признак разрыва сервером."""
    try:
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            disc = _find(msg, "disconnect")
            if isinstance(disc, dict):
                code = disc.get("code")
                reason = disc.get("reason", "")
                if code == DISCONNECT_CODE and reason == REASON:
                    return True, "disconnect code=3000 reason=session_revoked"
                return True, f"disconnect code={code} reason={reason!r}"
    except ConnectionClosed as exc:
        # Сокет закрылся — сервер уже разорвал соединение, и это главное.
        return True, f"закрыт сервером, close code={exc.code}"
    except asyncio.TimeoutError:
        return False, "нет disconnect за 20с"


async def _expect_event(
    ws, session_id: str, timeout: float = 20.0
) -> tuple[bool, str]:
    """Ждёт публикацию `session.revoked` с нужным session_id, не закрывая сокет."""
    try:
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            data = _find_pub_data(msg)
            if (
                isinstance(data, dict)
                and data.get("type") == "session.revoked"
                and data.get("session_id") == session_id
            ):
                return True, "событие session.revoked получено"
    except ConnectionClosed as exc:
        return False, f"сокет закрылся кодом {exc.code}, а не дождался события"
    except asyncio.TimeoutError:
        return False, "событие не пришло за 20с"


async def run() -> None:
    login = f"realtime-revoke-{uuid.uuid4().hex[:12]}@example.org"
    password = secrets.token_urlsafe(24)
    device_a, device_b = uuid.uuid4(), uuid.uuid4()
    ws_a = ws_b = None

    async with httpx.AsyncClient(timeout=15.0) as http:
        admin = await admin_token(http)
        external_id = await create_user(http, admin, login=login, password=password)
        try:
            first = await _login(http, login, password, device_a)
            second = await _login(http, login, password, device_b)
            check(
                "два независимых входа получили токены",
                first is not None and second is not None,
            )
            if first is None or second is None:
                return
            access_a, _ = first
            access_b, _ = second

            listed = await http.get(
                f"{API}/sessions", headers=_auth_headers(access_a, device_a)
            )
            items = listed.json().get("items", []) if listed.status_code == 200 else []
            check("API показывает оба входа", len(items) == 2, f"{listed.status_code}: {listed.text[:160]}")
            if len(items) != 2:
                return
            session_a = next(i["session_id"] for i in items if i["current"])
            session_b = next(i["session_id"] for i in items if not i["current"])

            token_a = await _realtime_token(http, access_a, device_a)
            token_b = await _realtime_token(http, access_b, device_b)
            check("connect-токены выданы обоим входам", bool(token_a) and bool(token_b))
            if not (token_a and token_b):
                return

            ws_a, client_a = await _connect(http, access_a, device_a, token_a)
            check("WebSocket A открыт и зарегистрирован", ws_a is not None and bool(client_a))
            ws_b, client_b = await _connect(http, access_b, device_b, token_b)
            check("WebSocket B открыт и зарегистрирован", ws_b is not None and bool(client_b))
            if ws_a is None or ws_b is None:
                return

            # --- выход на устройстве A ---------------------------------
            ended = await http.delete(
                f"{API}/sessions/{session_a}", headers=_auth_headers(access_a, device_a)
            )
            check("выход на устройстве A вернул 204", ended.status_code == 204, str(ended.status_code))

            broken_a, detail_a = await _expect_disconnect(ws_a)
            check("WebSocket A разорван сервером (disconnect 3000)", broken_a, detail_a)

            alive_b, detail_b = await _expect_event(ws_b, session_a)
            check(
                "WebSocket B жив и получил session.revoked сессии A",
                alive_b,
                detail_b,
            )

            # --- выход везде через B -----------------------------------
            ended_all = await http.delete(
                f"{API}/sessions", headers=_auth_headers(access_b, device_b)
            )
            check("выход везде вернул 204", ended_all.status_code == 204, str(ended_all.status_code))

            broken_b, detail_b2 = await _expect_disconnect(ws_b)
            check("WebSocket B разорван сервером (disconnect 3000)", broken_b, detail_b2)
        finally:
            for ws in (ws_a, ws_b):
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
            await _cleanup(http, admin, external_id)


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nотзыв сессии рвёт настоящий WebSocket, а не только строку в базе")
    return 0


if __name__ == "__main__":
    sys.exit(main())
