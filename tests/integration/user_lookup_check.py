"""G3-007-1: поиск человека по адресу — найден, не занят, недоступен.

Проверка держится на одном утверждении, которое легко потерять при правке:
**«не найден» — это один ответ на четыре разных случая**, и тело у него то же,
что у несуществующей беседы. Поэтому здесь тело `404` не просто проверяется на
код, а **сравнивается** с телом `404` на несуществующую беседу: разойдись они
формулировкой, и поиск стал бы каналом перебора — «адрес есть, но человек вам
не отвечает» отличается от «адреса нет».

Второе утверждение — про лимит. Он обязан считать **зрителя**, а не адрес:
счёт по адресу обходится сменой одной буквы, и тогда ограничение стоит ровно
ничего. Проверяется это запросами на **разные** адреса подряд: если счёт
ведётся по адресу, каждый запрос начинает с нуля и `429` не приходит никогда.

Токены получаются страницей Keycloak и серверным обменом; маршрут вызывается
через ASGI в том же pod и ходит в живую базу через PgBouncer — так проверяется
код ветки без подмены развёрнутого API.
"""
from __future__ import annotations

import asyncio
import secrets
import sys
import uuid

import httpx

from login_check import (
    API,
    ORIGIN,
    REDIRECT,
    _cleanup,
    _pool,
    admin_token,
    authorization_code,
    create_user,
    pkce,
)
from messenger.api.main import app
from messenger.services.user_lookup import LOOKUP_LIMIT

failures: list[str] = []

# Числа прогона. Собираются здесь, а не читаются из утверждений: утверждение
# «тело то же» ничего не говорит о том, какое тело было прочитано, и без
# печати отчёт ссылался бы на прогон, из которого ничего не видно.
evidence: dict[str, object] = {}

# Сколько запросов разрешено сделать сверх лимита, прежде чем проверка
# признает, что лимита нет. Берётся с запасом: если ограничение исчезнет
# молча, цикл обязан закончиться, а не идти вечно.
PROBE_CEILING = LOOKUP_LIMIT * 2


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


def _without_trace(body: dict) -> dict:
    """Тело отказа без трассировки — она у каждого запроса своя.

    Сравниваются тела целиком, и `trace_id` в них единственное поле,
    которое обязано различаться. Убрать его перед сравнением честнее, чем
    сравнивать три поля поимённо: поимённое сравнение переживёт добавление
    нового поля в тело, а добавить в тело `404` различающую подробность —
    ровно то, от чего эта проверка и написана.
    """
    return {key: value for key, value in body.items() if key != "trace_id"}


async def access_token(
    http: httpx.AsyncClient, login: str, password: str
) -> str | None:
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
            "device_id": str(uuid.uuid4()),
        },
        headers={"Origin": ORIGIN, "User-Agent": "checks/user-lookup"},
    )
    if response.status_code != 200:
        return None
    return response.json().get("access_token")


async def run() -> None:
    marker = uuid.uuid4().hex[:12]
    accounts = [
        (f"user-lookup-{marker}-{number}@example.org", secrets.token_urlsafe(24))
        for number in range(3)
    ]
    external_ids: list[str] = []
    runtime = app.state.runtime

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as live:
        admin = await admin_token(live)
        try:
            for login, password in accounts:
                external_ids.append(
                    await create_user(
                        live,
                        admin,
                        login=login,
                        password=password,
                        email_verified=True,
                    )
                )

            tokens = [
                await access_token(live, login, password)
                for login, password in accounts
            ]
            check(
                "три пользователя получили настоящие токены",
                all(tokens),
                str([bool(token) for token in tokens]),
            )
            if not all(tokens):
                return

            pool = await _pool()
            try:
                rows = await pool.fetch(
                    """
                    SELECT external_id, user_id
                      FROM users
                     WHERE external_id = ANY($1::text[])
                    """,
                    external_ids,
                )
                internal = {row["external_id"]: row["user_id"] for row in rows}
            finally:
                await pool.close()
            viewer_id, peer_id, blocked_id = [internal[item] for item in external_ids]
            viewer_login = accounts[0][0]
            peer_login = accounts[1][0]

            await runtime.start()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://branch-api", timeout=15.0
            ) as branch:
                viewer = {"Authorization": f"Bearer {tokens[0]}"}

                async def lookup(address: str) -> httpx.Response:
                    return await branch.get(
                        "/users", params={"email": address}, headers=viewer
                    )

                # Сколько запросов дошло до сервиса, то есть до лимита.
                # Пустой адрес и отказ удостоверения сюда не входят, и это
                # часть проверки, а не арифметическая подробность.
                consumed = 0

                found = await lookup(peer_login)
                consumed += 1
                body = found.json() if found.status_code == 200 else {}
                evidence["найден"] = f"{found.status_code} {sorted(body)}"
                check(
                    "живой человек найден по точному адресу",
                    found.status_code == 200
                    and body.get("user_id") == str(peer_id),
                    f"{found.status_code}: {found.text[:200]}",
                )
                check(
                    "в ответе ровно два поля — ни присутствия, ни адреса",
                    set(body) == {"user_id", "display_name"},
                    str(sorted(body)),
                )
                peer_me = await branch.get(
                    "/me", headers={"Authorization": f"Bearer {tokens[1]}"}
                )
                check(
                    "имя то же, что человек видит у себя",
                    found.status_code == 200
                    and peer_me.status_code == 200
                    and body.get("display_name") == peer_me.json().get("display_name"),
                    f"поиск: {body.get('display_name')!r}, /me: "
                    f"{peer_me.json().get('display_name')!r}",
                )

                upper = await lookup(peer_login.upper())
                consumed += 1
                check(
                    "регистр адреса не важен: тот же человек",
                    upper.status_code == 200
                    and upper.json().get("user_id") == str(peer_id),
                    f"{upper.status_code}: {upper.text[:200]}",
                )

                free = await lookup(f"nobody-{marker}@example.org")
                consumed += 1
                evidence["не занят"] = f"{free.status_code} {free.json().get('code')}"
                evidence["регистр"] = f"{upper.status_code}"
                check(
                    "свободный адрес — 404",
                    free.status_code == 404,
                    f"{free.status_code}: {free.text[:200]}",
                )

                # Эталон: тело `404` на несуществующую беседу. Оно берётся
                # с другого маршрута нарочно — сравнивать «не найдено» с ним
                # самим значило бы доказывать тождество тождеством.
                absent = await branch.get(
                    f"/conversations/{uuid.uuid4()}/messages", headers=viewer
                )
                evidence["беседа не найдена"] = (
                    f"{absent.status_code} {absent.json().get('code')}"
                )
                check(
                    "несуществующая беседа отвечает 404",
                    absent.status_code == 404,
                    f"{absent.status_code}: {absent.text[:200]}",
                )
                check(
                    "«человек не найден» отвечает тем же телом, что «беседа не найдена»",
                    free.status_code == absent.status_code
                    and _without_trace(free.json()) == _without_trace(absent.json()),
                    f"человек: {free.text[:200]}\n      беседа: {absent.text[:200]}",
                )

                self_lookup = await lookup(viewer_login)
                consumed += 1
                check(
                    "свой адрес — тот же 404, а не 403",
                    self_lookup.status_code == 404
                    and _without_trace(self_lookup.json()) == _without_trace(absent.json()),
                    f"{self_lookup.status_code}: {self_lookup.text[:200]}",
                )

                # Блокировка в обе стороны отвечает одинаково — и одинаково
                # с «не найден». Оба направления проверяются по отдельности:
                # предикат симметричен, и односторонняя его половина прошла бы
                # зелёной на одной из двух проверок.
                pool = await _pool()
                try:
                    await pool.execute(
                        "INSERT INTO blocks (blocker_id, blocked_id) VALUES ($1, $2)",
                        blocked_id,
                        viewer_id,
                    )
                finally:
                    await pool.close()
                blocked_other = await lookup(accounts[2][0])
                consumed += 1
                evidence["заблокировавший"] = (
                    f"{blocked_other.status_code} "
                    f"{_without_trace(blocked_other.json())}"
                )
                check(
                    "заблокировавший не находится поиском",
                    blocked_other.status_code == 404
                    and _without_trace(blocked_other.json()) == _without_trace(absent.json()),
                    f"{blocked_other.status_code}: {blocked_other.text[:200]}",
                )

                pool = await _pool()
                try:
                    await pool.execute(
                        "INSERT INTO blocks (blocker_id, blocked_id) VALUES ($1, $2)",
                        viewer_id,
                        blocked_id,
                    )
                finally:
                    await pool.close()
                blocked_self = await lookup(accounts[2][0])
                consumed += 1
                check(
                    "заблокированный зрителем тоже не находится",
                    blocked_self.status_code == 404
                    and _without_trace(blocked_self.json()) == _without_trace(absent.json()),
                    f"{blocked_self.status_code}: {blocked_self.text[:200]}",
                )

                empty = await branch.get("/users", params={"email": ""}, headers=viewer)
                check(
                    "пустой адрес — 400 invalid_email",
                    empty.status_code == 400 and empty.json().get("code") == "invalid_email",
                    f"{empty.status_code}: {empty.text[:200]}",
                )

                # Запросы на **разные** адреса. Счёт по зрителю обязан их
                # накопить; счёт по адресу — обнулился бы на каждом.
                limited: httpx.Response | None = None
                for _ in range(PROBE_CEILING):
                    probe = await lookup(f"probe-{uuid.uuid4().hex}@example.org")
                    if probe.status_code == 429:
                        limited = probe
                        break
                    consumed += 1

                evidence["свой адрес"] = f"{self_lookup.status_code}"
                evidence["пустой адрес"] = (
                    f"{empty.status_code} {empty.json().get('code')}"
                )
                evidence["до 429 израсходовано"] = (
                    f"{consumed} из {LOOKUP_LIMIT} объявленных"
                )
                evidence["Retry-After, с"] = (
                    limited.headers.get("Retry-After") if limited else "429 не получен"
                )
                check(
                    "лимит исчерпывается и отвечает 429",
                    limited is not None,
                    f"запросов прошло: {consumed}, 429 не получен",
                )
                check(
                    "счёт ведётся по зрителю, а не по адресу: "
                    "разные адреса копятся в один счётчик",
                    limited is not None and consumed == LOOKUP_LIMIT,
                    f"до 429 прошло запросов: {consumed}, объявленный лимит: {LOOKUP_LIMIT}",
                )
                # На исчерпанном счётчике различие видно лучше всего: если
                # бы нормализация стояла после лимита, запрос без предмета
                # получил бы `429` — то есть человек, ещё не начавший искать,
                # оказался бы наказан за чужие попытки. Ожидание обратное:
                # `400` и здесь.
                empty_after_limit = await branch.get(
                    "/users", params={"email": "   "}, headers=viewer
                )
                check(
                    "пустой адрес отвергается до лимита: 400 и при исчерпанном счётчике",
                    empty_after_limit.status_code == 400
                    and empty_after_limit.json().get("code") == "invalid_email",
                    f"{empty_after_limit.status_code}: {empty_after_limit.text[:200]}",
                )
                check(
                    "429 несёт Retry-After числом секунд",
                    limited is not None
                    and limited.headers.get("Retry-After", "").isdigit()
                    and int(limited.headers["Retry-After"]) > 0,
                    f"Retry-After: {limited.headers.get('Retry-After') if limited else '—'}",
                )

                # Лимит исчерпан — и это не отменяет аутентификации: отказ
                # по удостоверению обязан приходить и здесь, иначе счётчик
                # был бы единственной преградой.
                anonymous = await branch.get(
                    "/users", params={"email": peer_login}
                )
                check(
                    "без удостоверения — 401, а не 429",
                    anonymous.status_code == 401,
                    f"{anonymous.status_code}: {anonymous.text[:200]}",
                )
        finally:
            await runtime.stop()
            pool = await _pool()
            try:
                await pool.execute(
                    """
                    DELETE FROM blocks
                     WHERE blocker_id IN (
                               SELECT user_id FROM users
                                WHERE external_id = ANY($1::text[])
                           )
                        OR blocked_id IN (
                               SELECT user_id FROM users
                                WHERE external_id = ANY($1::text[])
                           )
                    """,
                    external_ids,
                )
            finally:
                await pool.close()
            for external_id in external_ids:
                await _cleanup(live, admin, external_id)


def main() -> int:
    asyncio.run(run())
    print("\nчисла прогона:")
    for what, value in evidence.items():
        print(f"  {what}: {value}")
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nпоиск человека подтверждён через API и живую базу")
    return 0


if __name__ == "__main__":
    sys.exit(main())
