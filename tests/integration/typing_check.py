"""G4: «печатает» на живом Centrifugo (`RT-002`, `RT-003`, `SEC-008`).

Три настоящих участника и настоящий WebSocket: публикация клиента идёт через
publish-proxy API, и именно его решения здесь проверяются, а не заглушка.

`RT-003`: клиент пишет в тело чужого автора и свой срок — получатель видит
автора из соединения и срок сервера. `RT-002`: сто публикаций подряд — дойдёт
не больше лимита, остальным отказ, и обычное сообщение после флуда
доставляется. `SEC-008`: человек вне беседы не подписывается на её канал
набора и не публикует в него.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sys
import uuid

import httpx
from login_check import API, ORIGIN, admin_token, create_user
from realtime_revoke_check import _auth_headers, _connect, _login
from relay_check import pool_settings

from messenger.repositories.postgres import create_pool

failures: list[str] = []
LIMIT = 6


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


def frames(raw: str) -> list[dict]:
    """Centrifugo склеивает несколько ответов и публикаций в один кадр через перевод строки.

    Разбирать кадр целиком нельзя: ответ на публикацию приходит вместе с её
    эхом, и `json.loads` на склейке падает — ответ пропадает без следа.
    """
    out: list[dict] = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


# Публикации, пришедшие в одном кадре с ответом на команду, не теряются: они
# откладываются здесь и отдаются следующему ожиданию публикации.
_pending: dict[int, list[dict]] = {}


async def command(ws, frame_id: int, body: dict) -> dict:
    """Шлёт команду и ждёт ответ с тем же `id`; публикации по пути запоминаются."""
    await ws.send(json.dumps({"id": frame_id, **body}))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10.0
    while loop.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(deadline - loop.time(), 0.1))
        except (TimeoutError, asyncio.TimeoutError):
            break
        reply: dict | None = None
        for frame in frames(raw):
            if frame.get("id") == frame_id:
                reply = frame
            else:
                _pending.setdefault(id(ws), []).append(frame)
        if reply is not None:
            return reply
    return {}


def _publication(frame: dict) -> dict | None:
    push = frame.get("push")
    pub = push.get("pub") if isinstance(push, dict) else None
    return pub["data"] if isinstance(pub, dict) and isinstance(pub.get("data"), dict) else None


async def wait_publication(ws, *, timeout: float) -> dict | None:
    queue = _pending.setdefault(id(ws), [])
    while queue:
        data = _publication(queue.pop(0))
        if data is not None:
            return data
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(deadline - loop.time(), 0.1))
        except (TimeoutError, asyncio.TimeoutError):
            return None
        for frame in frames(raw):
            data = _publication(frame)
            if data is not None:
                queue.append({"push": {"pub": {"data": data}}})
        while queue:
            data = _publication(queue.pop(0))
            if data is not None:
                return data
    return None


async def drain(ws, seconds: float) -> list[dict]:
    """Собирает все публикации за окно, включая пришедшие пачкой."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    got: list[dict] = []
    while loop.time() < deadline:
        data = await wait_publication(ws, timeout=max(deadline - loop.time(), 0.1))
        if data is None:
            break
        got.append(data)
    return got


async def run() -> None:
    marker = uuid.uuid4().hex[:12]
    accounts = [(f"typ-{marker}-{n}@example.org", secrets.token_urlsafe(24)) for n in range(3)]
    external_ids: list[str] = []
    pool = await create_pool(pool_settings(), application_name="messenger-integration")
    sockets = []

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as http:
        admin = await admin_token(http)
        try:
            for login, password in accounts:
                external_ids.append(
                    await create_user(http, admin, login=login, password=password,
                                      email_verified=True)
                )
            devices = [uuid.uuid4() for _ in accounts]
            logins = [await _login(http, a[0], a[1], d) for a, d in zip(accounts, devices, strict=True)]
            check("все трое вошли", all(logins))
            if not all(logins):
                return
            tokens = [entry[0] for entry in logins]
            ids = [
                await pool.fetchval("SELECT user_id FROM users WHERE external_id = $1", e)
                for e in external_ids
            ]

            created = await http.post(
                f"{API}/conversations", json={"participant_id": str(ids[1])},
                headers={**_auth_headers(tokens[0], devices[0]), "Origin": ORIGIN},
            )
            check("беседа A и B создана", created.status_code in (200, 201), created.text[:160])
            if created.status_code not in (200, 201):
                return
            conversation = created.json()["conversation_id"]
            channel = f"typing:{conversation}"

            async def open_socket(index: int):
                reply = await http.post(
                    f"{API}/realtime/token", headers=_auth_headers(tokens[index], devices[index])
                )
                ws, client_id = await _connect(reply.json()["token"])
                if ws is not None:
                    sockets.append(ws)
                return ws, client_id

            ws_a, id_a = await open_socket(0)
            ws_b, id_b = await open_socket(1)
            ws_c, id_c = await open_socket(2)
            check("три сокета открыты", bool(id_a and id_b and id_c))
            if not (id_a and id_b and id_c):
                return

            sub_b = await command(ws_b, 2, {"subscribe": {"channel": channel}})
            sub_a = await command(ws_a, 2, {"subscribe": {"channel": channel}})
            # Повторная подписка отвечает `105 already subscribed`: канал выдан
            # сервером по тикету, клиенту подписываться самому не нужно.
            served = all(r.get("error", {}).get("code") == 105 for r in (sub_a, sub_b))
            check("участники уже подписаны на канал набора: его выдал сервер", served,
                  f"{sub_a} {sub_b}")

            # --- SEC-008: чужой ----------------------------------------------
            sub_c = await command(ws_c, 2, {"subscribe": {"channel": channel}})
            check("SEC-008: не участник не подписывается на канал набора",
                  "error" in sub_c, str(sub_c))
            pub_c = await command(ws_c, 3, {"publish": {"channel": channel,
                                                       "data": {"state": "typing"}}})
            check("SEC-008: не участник не публикует в канал набора",
                  "error" in pub_c, str(pub_c))
            await drain(ws_b, 0.5)

            # --- RT-003: автор из соединения ---------------------------------
            forged = {"state": "typing", "user_id": str(ids[2]), "expires_in_ms": 999999}
            reply = await command(ws_a, 4, {"publish": {"channel": channel, "data": forged}})
            check("публикация A принята", "error" not in reply, str(reply))
            got = await wait_publication(ws_b, timeout=5.0)
            check("RT-003: B видит автором A, а не подделанного",
                  got is not None and got.get("user_id") == str(ids[0]), str(got))
            check("RT-003: срок задал сервер, а не клиент",
                  got is not None and got.get("expires_in_ms") == 5000, str(got))

            await command(ws_a, 5, {"publish": {"channel": channel, "data": {"state": "stop"}}})
            stopped = await wait_publication(ws_b, timeout=5.0)
            check("stop гасит индикатор сразу (срок 0)",
                  stopped is not None and stopped.get("expires_in_ms") == 0, str(stopped))

            invalid = await command(ws_a, 6, {"publish": {"channel": channel,
                                                         "data": {"state": "dancing"}}})
            check("негодное тело отвергнуто", "error" in invalid, str(invalid))

            # --- RT-002: флуд ------------------------------------------------
            await asyncio.sleep(6.0)  # окно лимита обнуляется
            await drain(ws_b, 0.3)
            replies = await asyncio.gather(*[
                command(ws_a, 100 + n, {"publish": {"channel": channel,
                                                   "data": {"state": "typing"}}})
                for n in range(100)
            ])
            accepted = sum(1 for r in replies if "error" not in r)
            rejected = sum(1 for r in replies if "error" in r)
            check(f"RT-002: из 100 публикаций принято не больше {LIMIT}",
                  1 <= accepted <= LIMIT, f"принято {accepted}, отвергнуто {rejected}")
            check("RT-002: остальные отвергнуты, а не потеряны молча",
                  accepted + rejected == 100 and rejected >= 100 - LIMIT)
            delivered = await drain(ws_b, 2.0)
            check("RT-002: получателю дошло не больше лимита",
                  len(delivered) <= LIMIT, f"дошло {len(delivered)}")

            # --- сообщения при флуде продолжают идти -------------------------
            sub_msg = await command(ws_b, 7, {"subscribe": {"channel": f"conversation:{conversation}"}})
            check("B уже подписан на канал беседы (выдан сервером)",
                  sub_msg.get("error", {}).get("code") == 105, str(sub_msg))
            sent = await http.post(
                f"{API}/conversations/{conversation}/messages",
                json={"client_message_id": str(uuid.uuid4()), "type": "text",
                      "payload": {"text": f"после флуда {marker}"}},
                headers={**_auth_headers(tokens[0], devices[0]), "Origin": ORIGIN},
            )
            check("обычное сообщение после флуда принято", sent.status_code == 201,
                  f"{sent.status_code}: {sent.text[:120]}")
            message = await wait_publication(ws_b, timeout=25.0)
            check("и доставлено получателю", message is not None
                  and message.get("payload", {}).get("text") == f"после флуда {marker}",
                  str(message))
        finally:
            for ws in sockets:
                with contextlib.suppress(Exception):
                    await ws.close()
            await pool.execute(
                "DELETE FROM outbox WHERE aggregate_id IN (SELECT message_id FROM messages"
                " WHERE sender_id IN (SELECT user_id FROM users WHERE external_id ="
                " ANY($1::text[])))", external_ids)
            for table, column in (("messages", "sender_id"), ("conversation_members", "user_id"),
                                  ("realtime_connections", "user_id"),
                                  ("sessions", "user_id"), ("devices", "user_id")):
                await pool.execute(
                    f"DELETE FROM {table} WHERE {column} IN "  # noqa: S608
                    "(SELECT user_id FROM users WHERE external_id = ANY($1::text[]))",
                    external_ids)
            await pool.execute(
                "DELETE FROM conversations WHERE conversation_id NOT IN "
                "(SELECT conversation_id FROM conversation_members)")
            await pool.execute("DELETE FROM users WHERE external_id = ANY($1::text[])", external_ids)
            await pool.close()
            for external_id in external_ids:
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
    print("\n«печатает»: автор из соединения, флуд обрывается, чужой не допущен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
