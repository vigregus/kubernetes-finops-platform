"""G4: звонки на живых Postgres, Centrifugo и API (`CALL-001…009`, `CALL-012`, `CALL-013`, `CALL-014`).

Настоящие участники и настоящий WebSocket на **канале звонков** (`call:{id}`,
билет `scope=calls`). Проверяются решения сервера, а не интерфейс: автомат
состояний, права на действия и сигналы, сигнал без истории, итог в ленте,
подметальщик. Браузерная приёмка — `tests/e2e/g4-calls.spec.ts`.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import sys
import uuid

import httpx
from login_check import API, ORIGIN, admin_token, create_user
from realtime_revoke_check import _auth_headers, _connect, _login
from relay_check import pool_settings
from typing_check import check, command, drain, failures, wait_publication

from messenger.repositories.postgres import create_pool

SWEEP_WAIT_SECONDS = 70.0


class Person:
    def __init__(self, login: str, password: str) -> None:
        self.login = login
        self.password = password
        self.external_id = ""
        self.user_id = ""
        self.device = uuid.uuid4()
        self.token = ""
        self.ws = None

    def headers(self) -> dict[str, str]:
        return {**_auth_headers(self.token, self.device), "Origin": ORIGIN}


async def run() -> None:
    marker = uuid.uuid4().hex[:12]
    people = {
        name: Person(f"call-{marker}-{name}@example.org", secrets.token_urlsafe(24))
        for name in ("a", "b", "c")
    }
    pool = await create_pool(pool_settings(), application_name="messenger-integration")
    sockets: list = []

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as http:
        admin = await admin_token(http)

        async def call(method: str, path: str, who: Person, json=None):
            return await http.request(method, f"{API}{path}", json=json, headers=who.headers())

        async def calls_socket(who: Person):
            reply = await http.post(f"{API}/realtime/token", params={"scope": "calls"},
                                    headers=who.headers())
            if reply.status_code != 200:
                return None
            ws, client_id = await _connect(reply.json()["token"])
            if ws is not None:
                sockets.append(ws)
            return ws if client_id else None

        try:
            for person in people.values():
                person.external_id = await create_user(
                    http, admin, login=person.login, password=person.password, email_verified=True)
            logins = [await _login(http, p.login, p.password, p.device) for p in people.values()]
            check("все трое вошли", all(logins))
            if not all(logins):
                return
            for person, entry in zip(people.values(), logins, strict=True):
                person.token = entry[0]
                person.user_id = str(await pool.fetchval(
                    "SELECT user_id FROM users WHERE external_id = $1", person.external_id))
            a, b, c = people["a"], people["b"], people["c"]

            conv_ab = (await call("POST", "/conversations", a, {"participant_id": b.user_id})).json()["conversation_id"]
            conv_ac = (await call("POST", "/conversations", a, {"participant_id": c.user_id})).json()["conversation_id"]
            conv_cb = (await call("POST", "/conversations", c, {"participant_id": b.user_id})).json()["conversation_id"]

            # --- возможность и каналы -----------------------------------------
            me = (await call("GET", "/me", a)).json()
            check("`/me` называет звонки возможностью", "calls" in me["capabilities"], str(me))

            normal = await http.post(f"{API}/realtime/token", headers=a.headers())
            ws_plain, plain_id = await _connect(normal.json()["token"])
            if ws_plain is not None:
                sockets.append(ws_plain)
            denied = await command(ws_plain, 2, {"subscribe": {"channel": f"call:{a.user_id}"}}) if plain_id else {}
            check("обычный билет не даёт канал звонков (подписка отвергнута)", "error" in denied, str(denied))

            a.ws = await calls_socket(a)
            b.ws = await calls_socket(b)
            check("сокеты звонков A и B открыты", a.ws is not None and b.ws is not None)
            if a.ws is None or b.ws is None:
                return
            foreign = await command(a.ws, 2, {"subscribe": {"channel": f"call:{b.user_id}"}})
            check("CALL-006: чужой канал звонков недоступен", "error" in foreign
                  and foreign["error"].get("code") != 105, str(foreign))
            publish = await command(a.ws, 3, {"publish": {"channel": f"call:{b.user_id}",
                                                         "data": {"type": "call.signal"}}})
            check("CALL-006: клиент не публикует в канал звонков сам", "error" in publish, str(publish))

            # --- CALL-001: звонок -------------------------------------------------
            started = await call("POST", "/calls", a, {"conversation_id": conv_ab, "kind": "audio"})
            body = started.json()
            check("звонок создан: 201, ringing, A — звонящий",
                  started.status_code == 201 and body.get("state") == "ringing"
                  and body.get("role") == "caller", f"{started.status_code} {started.text[:160]}")
            call_id = body["call_id"]
            incoming = await wait_publication(b.ws, timeout=10.0)
            check("B получил call.incoming с именем звонящего",
                  incoming is not None and incoming.get("type") == "call.incoming"
                  and incoming.get("call_id") == call_id and incoming.get("caller", {}).get("display_name"),
                  str(incoming))

            # --- CALL-005: права ---------------------------------------------------
            for action in ("accept", "decline", "hangup", "keepalive"):
                reply = await call("POST", f"/calls/{call_id}/{action}", c)
                check(f"CALL-005: посторонний не может {action}: 404", reply.status_code == 404,
                      f"{reply.status_code}")
            reply = await call("GET", f"/calls/{call_id}/ice-servers", c)
            check("CALL-008: TURN постороннему: 404", reply.status_code == 404, f"{reply.status_code}")
            reply = await call("POST", f"/calls/{call_id}/accept", a)
            check("звонящий не может принять свой звонок: 404", reply.status_code == 404, f"{reply.status_code}")
            reply = await call("POST", f"/calls/{call_id}/signals", c, {"type": "offer", "sdp": "v=0"})
            check("CALL-006: подделка — чужой сигнал отвергнут: 404", reply.status_code == 404,
                  f"{reply.status_code}")
            reply = await call("POST", f"/calls/{call_id}/signals", a, {"type": "offer", "sdp": "v=0"})
            check("сигнал до принятия отвергнут: 409 call_not_ready",
                  reply.status_code == 409 and reply.json().get("code") == "call_not_ready",
                  f"{reply.status_code} {reply.text[:100]}")
            check("в личный канал B ничего не ушло от чужих и ранних сигналов",
                  not await drain(b.ws, 1.0))

            reply = await call("POST", "/calls", a, {"conversation_id": conv_ac, "kind": "audio"})
            check("у звонящего уже идёт звонок: 409 already_in_call",
                  reply.status_code == 409 and reply.json().get("code") == "already_in_call",
                  f"{reply.status_code} {reply.text[:120]}")

            # --- принять -----------------------------------------------------------
            accepted = await call("POST", f"/calls/{call_id}/accept", b)
            check("B принял: 200, accepted, роль callee",
                  accepted.status_code == 200 and accepted.json().get("state") == "accepted"
                  and accepted.json().get("role") == "callee", accepted.text[:160])
            again = await call("POST", f"/calls/{call_id}/accept", b)
            check("CALL-003: повторное принятие — тот же ответ, ничего не меняется",
                  again.status_code == 200 and again.json().get("version") == accepted.json().get("version"),
                  again.text[:120])
            to_a = await wait_publication(a.ws, timeout=10.0)
            check("A получил call.state accepted", to_a is not None and to_a.get("state") == "accepted"
                  and to_a.get("reason") is None, str(to_a))
            to_b = await wait_publication(b.ws, timeout=10.0)
            check("CALL-012: B (все его вкладки) получил accepted_elsewhere",
                  to_b is not None and to_b.get("reason") == "accepted_elsewhere", str(to_b))

            # --- сигналы -----------------------------------------------------------
            sent = await call("POST", f"/calls/{call_id}/signals", a, {"type": "offer", "sdp": "v=0\r\n", "evil": 1})
            check("offer принят: 204", sent.status_code == 204, f"{sent.status_code} {sent.text[:100]}")
            sig = await wait_publication(b.ws, timeout=10.0)
            check("B получил сигнал с номером от сервера и без лишних полей",
                  sig is not None and sig.get("type") == "call.signal" and sig.get("seq") == 1
                  and sig.get("signal") == {"type": "offer", "sdp": "v=0\r\n"}, str(sig))
            sent = await call("POST", f"/calls/{call_id}/signals", b,
                              {"type": "ice", "candidates": [{"candidate": "candidate:1 1 udp 1 10.0.0.1 5000 typ host",
                                                              "sdpMid": "0", "sdpMLineIndex": 0}]})
            sig = await wait_publication(a.ws, timeout=10.0)
            check("кандидаты идут пачкой; номер сигнала растёт",
                  sent.status_code == 204 and sig is not None and sig.get("seq") == 2, str(sig))
            for bad, label in (
                ({"type": "bye"}, "неизвестный тип"),
                ({"type": "offer"}, "offer без sdp"),
                ({"type": "offer", "sdp": "v" * 20000}, "слишком большой sdp"),
                ({"type": "ice", "candidates": []}, "пустая пачка кандидатов"),
            ):
                reply = await call("POST", f"/calls/{call_id}/signals", a, bad)
                check(f"CALL-006: {label} отвергнут: 400", reply.status_code == 400, f"{reply.status_code}")

            ice = await call("GET", f"/calls/{call_id}/ice-servers", a)
            check("CALL-008: участнику выдан список, без кэширования",
                  ice.status_code == 200 and ice.headers.get("cache-control") == "no-store"
                  and ice.json().get("ttl_seconds") == 600, f"{ice.status_code} {ice.text[:120]}")

            servers = ice.json().get("ice_servers", []) if ice.status_code == 200 else []
            check("CALL-004: стенд выдаёт данные своего coturn (имя «срок:метка» и подпись)",
                  bool(servers) and ":" in str(servers[0].get("username", ""))
                  and bool(servers[0].get("credential")), str(servers)[:160])

            # --- CALL-007: сигнал без истории ----------------------------------------
            with contextlib.suppress(Exception):
                await b.ws.close()
            await call("POST", f"/calls/{call_id}/signals", a, {"type": "offer", "sdp": "v=0 stale"})
            b.ws = await calls_socket(b)
            check("B переподключился", b.ws is not None)
            stale = await drain(b.ws, 2.0) if b.ws else ["нет сокета"]
            check("CALL-007: устаревший offer не восстанавливается после переподключения (истории нет)",
                  stale == [], str(stale))

            # --- активен, трубка, итог ------------------------------------------------
            active = await call("POST", f"/calls/{call_id}/connected", a, {"connection_type": "direct"})
            check("медиа пошло: active", active.status_code == 200 and active.json().get("state") == "active",
                  active.text[:120])
            keep = await call("POST", f"/calls/{call_id}/keepalive", b)
            check("keepalive отдаёт состояние", keep.status_code == 200 and keep.json().get("state") == "active",
                  keep.text[:120])

            # --- CALL-013: занято -------------------------------------------------------
            busy = await call("POST", "/calls", c, {"conversation_id": conv_cb, "kind": "audio"})
            check("CALL-013: звонок тому, у кого идёт разговор, — busy сразу",
                  busy.status_code == 201 and busy.json().get("state") == "ended"
                  and busy.json().get("end_reason") == "busy", f"{busy.status_code} {busy.text[:140]}")

            hung = await call("POST", f"/calls/{call_id}/hangup", b)
            check("B повесил трубку: ended, completed",
                  hung.status_code == 200 and hung.json().get("state") == "ended"
                  and hung.json().get("end_reason") == "completed", hung.text[:140])
            for who in (a, b):
                again = await call("POST", f"/calls/{call_id}/hangup", who)
                check("CALL-003: повторная трубка ничего не меняет", again.status_code == 200
                      and again.json().get("end_reason") == "completed", again.text[:100])
            late = await call("POST", f"/calls/{call_id}/accept", b)
            check("принять завершённый звонок нельзя: 409 call_ended",
                  late.status_code == 409 and late.json().get("code") == "call_ended",
                  f"{late.status_code} {late.text[:100]}")
            reply = await call("POST", f"/calls/{call_id}/signals", a, {"type": "offer", "sdp": "v=0"})
            check("сигнал в завершённый звонок отвергнут: 409", reply.status_code == 409, f"{reply.status_code}")
            ice = await call("GET", f"/calls/{call_id}/ice-servers", a)
            check("CALL-008: TURN после завершения: 409", ice.status_code == 409, f"{ice.status_code}")

            summaries = await pool.fetch(
                "SELECT m.payload FROM messages m WHERE m.conversation_id = $1 AND m.type = 'system'"
                " ORDER BY m.conversation_seq", uuid.UUID(conv_ab))
            texts = [r["payload"] if isinstance(r["payload"], str) else r["payload"] for r in summaries]
            check("CALL-005/009: в ленте ровно один итог «completed» с длительностью",
                  len(summaries) == 1 and '"call.completed.audio"' in str(texts[0]) and "duration_ms" in str(texts[0]),
                  str(texts))
            history = await call("GET", f"/conversations/{conv_ab}/messages", a)
            items = history.json().get("items", []) if history.status_code == 200 else []
            check("итог доступен через историю беседы как system-сообщение",
                  any(i.get("type") == "system" and i.get("payload", {}).get("text") == "call.completed.audio"
                      for i in items), str(items)[:200])
            check("линия освобождена: у участников нет живых звонков",
                  await pool.fetchval("SELECT count(*) FROM call_participants WHERE user_id = ANY($1::uuid[])",
                                      [uuid.UUID(p.user_id) for p in people.values()]) == 0)

            # --- CALL-014: вызываемый не в сети ----------------------------------------
            unavailable = await call("POST", "/calls", a, {"conversation_id": conv_ac, "kind": "video"})
            check("CALL-014: вызываемый не в сети — звонок не стартует, причина названа",
                  unavailable.status_code == 201 and unavailable.json().get("end_reason") == "unavailable",
                  f"{unavailable.status_code} {unavailable.text[:140]}")
            check("в ленте — «unavailable»",
                  await pool.fetchval(
                      "SELECT count(*) FROM messages WHERE conversation_id = $1 AND type = 'system'"
                      " AND payload->>'text' = 'call.unavailable.video'", uuid.UUID(conv_ac)) == 1)

            # --- CALL-004: встречный звонок ---------------------------------------------
            first = (await call("POST", "/calls", a, {"conversation_id": conv_ab, "kind": "audio"})).json()
            await drain(b.ws, 1.0)
            counter = await call("POST", "/calls", b, {"conversation_id": conv_ab, "kind": "audio"})
            check("CALL-004: встречный звонок принимает существующий, а не заводит второй",
                  counter.status_code == 201 and counter.json().get("call_id") == first["call_id"]
                  and counter.json().get("state") == "accepted",
                  f"{counter.status_code} {counter.text[:160]}")
            check("звонков в беседе — два (завершённый и этот), не три",
                  await pool.fetchval("SELECT count(*) FROM calls WHERE conversation_id = $1",
                                      uuid.UUID(conv_ab)) == 2)
            await call("POST", f"/calls/{first['call_id']}/hangup", a)

            # --- подметальщик --------------------------------------------------------------
            missed = (await call("POST", "/calls", a, {"conversation_id": conv_ab, "kind": "audio"})).json()
            check("новый звонок звонит", missed.get("state") == "ringing", str(missed))
            deadline = asyncio.get_running_loop().time() + SWEEP_WAIT_SECONDS
            reason = None
            while asyncio.get_running_loop().time() < deadline:
                row = await pool.fetchrow("SELECT state, end_reason FROM calls WHERE call_id = $1",
                                          uuid.UUID(missed["call_id"]))
                if row and row["state"] == "ended":
                    reason = row["end_reason"]
                    break
                await asyncio.sleep(2.0)
            check("CALL-002: через 30 с без ответа подметальщик завершил звонок как missed",
                  reason == "missed", f"причина: {reason}")
            ended_event = None
            for data in await drain(b.ws, 1.0):
                if data.get("type") == "call.state" and data.get("state") == "ended":
                    ended_event = data
            check("вызываемому ушло call.state ended", ended_event is not None
                  and ended_event.get("reason") == "missed", str(ended_event))
            check("CALL-009: итог «missed» записан один раз",
                  await pool.fetchval(
                      "SELECT count(*) FROM messages WHERE conversation_id = $1 AND type = 'system'"
                      " AND payload->>'text' = 'call.missed.audio'", uuid.UUID(conv_ab)) == 1)
        finally:
            for ws in sockets:
                with contextlib.suppress(Exception):
                    await ws.close()
            ids = [p.external_id for p in people.values() if p.external_id]
            await pool.execute(
                "DELETE FROM outbox WHERE aggregate_id IN (SELECT message_id FROM messages"
                " WHERE sender_id IN (SELECT user_id FROM users WHERE external_id = ANY($1::text[])))", ids)
            await pool.execute(
                "UPDATE calls SET summary_message_id = NULL WHERE caller_id IN"
                " (SELECT user_id FROM users WHERE external_id = ANY($1::text[]))", ids)
            for table, column in (("call_participants", "user_id"), ("calls", "caller_id"),
                                  ("messages", "sender_id"), ("conversation_members", "user_id"),
                                  ("realtime_connections", "user_id"), ("sessions", "user_id"),
                                  ("devices", "user_id")):
                await pool.execute(
                    f"DELETE FROM {table} WHERE {column} IN "  # noqa: S608
                    "(SELECT user_id FROM users WHERE external_id = ANY($1::text[]))", ids)
            await pool.execute(
                "DELETE FROM conversations WHERE conversation_id NOT IN "
                "(SELECT conversation_id FROM conversation_members)")
            await pool.execute("DELETE FROM users WHERE external_id = ANY($1::text[])", ids)
            await pool.close()
            for external_id in ids:
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
    print("\nзвонки: автомат, права, сигналы без истории, итог в ленте, подметальщик")
    return 0


if __name__ == "__main__":
    sys.exit(main())
