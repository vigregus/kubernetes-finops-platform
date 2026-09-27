"""G3-007-1: `client_message_id` доезжает до канала — и состава беседы там нет.

`message.created` уже несёт `message_id`, `seq`, `sender_id` и содержимое.
Несёт, и этого хватает всему, кроме одного случая: **ответ на отправку
потерялся, а событие доехало**. Отправившая вкладка видит незнакомый
`message_id` и свою оптимистичную запись, и свести их нечем, кроме пары
«текст и время» — то есть угадыванием, исход которого дубль в ленте и
вечное «отправляется». Поэтому у события появляется `client_message_id`
того же сообщения — ровно тот, что был в запросе.

**Чего в событии нет — и это проверяется тем же прогоном.** `recipient_ids`
есть в факте (схема `message.created.v1.json`: «состав беседы на момент
отправки») и в канал не уходит: иначе каждый участник узнаёт полный список
получателей чужого сообщения. Докстринг `_client_event` это охраняет,
и охрана проверяется, а не подразумевается.

**Половина, которую здесь приходится поднимать в поде.** Потребитель
realtime — отдельная нагрузка, и на стенде она собрана из `main`
(`kubectl -n messenger get deploy consumer-realtime` отдаёт образ
`1123ec48…`, то есть код **до** этой правки). Путь «API → outbox →
отправитель → Kafka → потребитель → Centrifugo» на стенде идёт через
прежний `_client_event`, который поля не знает вовсе: проверка, ходящая
этим путём, доказывала бы поведение старой сборки. Поэтому обе половины,
которые правка меняет — запись факта и сборка события, — исполняются здесь,
в поде проверки, из того же образа. Живым при этом остаётся всё остальное:
строка `outbox` читается из настоящего Postgres как есть, кэш половин —
настоящий Redis, публикация — настоящий HTTP-вызов Centrifugo, подписчик —
настоящий WebSocket.

**Что при этом не исполняется.** Переход через Kafka. Отправитель outbox
этой правкой не тронут, а запись он несёт как есть, поэтому тело записи
передаётся `handle_event` дословно — тем же словарём, каким его отдал бы
потребитель. Это названо границей, а не умолчано.

**Две публикации на одно сообщение, и вторая — не дефект проверки.** То же
сообщение обрабатывает и развёрнутый потребитель: он публикует событие
**без** поля, потому что его сборка старше правки. Ожидание отбирает кадр по
`message_id`, который вернул API (поэтому событие прошлого прогона совпасть
не может), а число увиденных кадров печатается: умолчать о втором значило бы
выдать одну публикацию за единственную.

**Почему кэш половин свой.** Отметка `mark_delivered` живёт в Redis по
`message_id`, и общий ключ означал бы гонку с развёрнутым потребителем: кто
первый отметил, тот и опубликовал, а второй молчит. Проверка, зависящая от
того, кто успел, — не проверка. Разные базы разводят ключи, и предмет от
этого не меняется: дедупликация не то, что здесь доказывается.

**Живость сокета доказывается до отправки.** Подписка на канал получает
ответ сервера — значит, к моменту ожидания сокет отвечает, и пустое окно
позже читается как отсутствие публикации, а не как мёртвое соединение.
Без этого «кадров не пришло» и «сокет умер» выглядели бы одинаково.
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
from login_check import (
    ORIGIN,
    _cleanup,
    _pool,
    admin_token,
    create_user,
)
from realtime_receive_check import subscribe
from realtime_revoke_check import _auth_headers, _connect, _login

from messenger.adapters import event_cache
from messenger.api.main import app
from messenger.services import realtime_delivery, runtime

failures: list[str] = []

# Сколько ждём публикацию. Путь здесь короткий — публикует та же реплика,
# что и пишет, — но запас нужен на выборку строки из outbox и на HTTP
# до Centrifugo, а не на сам путь.
DEADLINE_SECONDS = 15.0

# Числа прогона. Печатаются, а не читаются из утверждений: «поле совпало»
# ничего не говорит о том, чему оно совпало.
evidence: dict[str, object] = {}


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


def _publication(frame: dict) -> dict | None:
    """Данные публикации, где бы ни лежал `pub`; `data` — строка или объект."""
    pub = frame.get("pub")
    if not isinstance(pub, dict):
        push = frame.get("push")
        pub = push.get("pub") if isinstance(push, dict) else None
    if not isinstance(pub, dict):
        return None
    data = pub.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


async def wait_created(ws, *, message_id: str, timeout: float) -> list[dict]:
    """Ждёт публикации **нашего** сообщения и возвращает все, что пришли.

    Отбор по `message_id`, а не «первая публикация»: в канале беседы идут
    и чужие события, и кадры соседней проверки. Идентификатор возвращает
    API, поэтому шаблон привязан к этому прогону и с событием прошлого
    совпасть не может.

    Возвращаются **все** кадры по этому сообщению, а не один: публикаций
    здесь две (см. докстринг модуля), и кадр развёрнутого потребителя —
    свидетельство стенда. Разбирать их порознь приходится и утверждениям:
    поле ищется в том кадре, что его несёт, а утечка — во всех, потому что
    течёт она от своего кода, а не от чужого.

    Выход из ожидания — по кадру, который поле **несёт**: он и есть
    произведение проверяемой половины. Кадр без поля ожидание не
    заканчивает, но и не отменяет: до правки поля нет ни в одном, и это
    ровно тот исход, ради которого проверка написана.

    Пустой кадр — протокольный пинг Centrifugo, и он требует ответа: без
    него сервер закрывает соединение (`no pong`), и проверка падала бы
    на своём молчании, а не на дефекте.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    наши: list[dict] = []
    while loop.time() < deadline:
        try:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=max(deadline - loop.time(), 0.1)
            )
        except (TimeoutError, asyncio.TimeoutError):
            break
        try:
            frame = json.loads(raw)
        except ValueError:
            continue
        if frame == {}:
            await ws.send("{}")
            continue
        if not isinstance(frame, dict):
            continue
        data = _publication(frame)
        if data is None or data.get("type") != "message.created":
            continue
        if data.get("message_id") != message_id:
            continue
        наши.append(data)
        if data.get("client_message_id") is not None:
            break
    return наши


async def outbox_records(pool, *, message_id: str) -> dict[str, dict]:
    """Записи outbox сообщения по типу события — как их прочитает отправитель.

    `jsonb` приходит текстом (`repositories/outbox.py` разбирает его так же),
    и разбор здесь нарочно тот же: проверять «а что если по-другому» значило
    бы описывать не тот путь, которым запись уходит в Kafka.
    """
    rows = await pool.fetch(
        "SELECT event_type, payload FROM outbox WHERE aggregate_id = $1",
        uuid.UUID(message_id),
    )
    return {row["event_type"]: json.loads(row["payload"]) for row in rows}


async def run() -> None:
    marker = uuid.uuid4().hex[:12]
    accounts = [
        (f"event-{marker}-{number}@example.org", secrets.token_urlsafe(24))
        for number in range(2)
    ]
    external_ids: list[str] = []
    pool = await _pool()
    runtime_ = app.state.runtime
    cache = None
    centrifugo = None
    ws = None

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as live:
        admin = await admin_token(live)
        try:
            for login, password in accounts:
                external_ids.append(
                    await create_user(
                        live, admin, login=login, password=password, email_verified=True
                    )
                )

            device_a, device_b = uuid.uuid4(), uuid.uuid4()
            вход_a = await _login(live, accounts[0][0], accounts[0][1], device_a)
            вход_b = await _login(live, accounts[1][0], accounts[1][1], device_b)
            check("оба участника вошли", bool(вход_a and вход_b))
            if not (вход_a and вход_b):
                return
            токен_a, токен_b = вход_a[0], вход_b[0]

            rows = await pool.fetch(
                "SELECT external_id, user_id FROM users WHERE external_id = ANY($1::text[])",
                external_ids,
            )
            внутренние = {row["external_id"]: row["user_id"] for row in rows}

            # Ветка API из того же образа, что и проверка: развёрнутый API —
            # сборка `main`, и на нём этой правки нет вовсе.
            await runtime_.start()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://branch-api", timeout=20.0
            ) as branch:
                headers_a = {**_auth_headers(токен_a, device_a), "Origin": ORIGIN}

                создана = await branch.post(
                    "/conversations",
                    json={"participant_id": str(внутренние[external_ids[1]])},
                    headers=headers_a,
                )
                check(
                    "беседа создана",
                    создана.status_code in (200, 201),
                    f"{создана.status_code}: {создана.text[:200]}",
                )
                if создана.status_code not in (200, 201):
                    return
                беседа = создана.json()["conversation_id"]

                # Получатель подключается до отправки: подписка выдаётся
                # сервером, и её ответ — доказательство живого сокета.
                r = await branch.post(
                    "/realtime/token", headers=_auth_headers(токен_b, device_b)
                )
                check(
                    "получатель получил токен Centrifugo",
                    r.status_code == 200,
                    f"{r.status_code}: {r.text[:200]}",
                )
                if r.status_code != 200:
                    return
                ws, client_id = await _connect(r.json()["token"])
                check("WebSocket открыт", bool(ws and client_id), "соединение не открылось")
                if not (ws and client_id):
                    return
                подписан, ответ = await subscribe(ws, f"conversation:{беседа}")
                check(
                    "канал беседы выдан сервером, а сокет на подписку ответил",
                    подписан,
                    ответ,
                )
                if not подписан:
                    return

                отправлен = str(uuid.uuid4())
                текст = f"событие {marker}"
                принято = await branch.post(
                    f"/conversations/{беседа}/messages",
                    json={
                        "client_message_id": отправлен,
                        "type": "text",
                        "payload": {"text": текст},
                    },
                    headers=headers_a,
                )
                check(
                    "сообщение принято API",
                    принято.status_code == 201,
                    f"{принято.status_code}: {принято.text[:200]}",
                )
                if принято.status_code != 201:
                    return
                message_id = принято.json()["message_id"]

                # --- что записано: две половины, и поле — в одной ------
                записи = await outbox_records(pool, message_id=message_id)
                факт = записи.get("message.created", {})
                содержимое = записи.get("message.content", {})
                evidence["client_message_id запроса"] = отправлен
                evidence["в факте"] = факт.get("client_message_id", "поля нет")
                evidence["в содержимом"] = (
                    "поля нет"
                    if "client_message_id" not in содержимое
                    else f"есть: {содержимое['client_message_id']}"
                )
                check(
                    "факт несёт client_message_id того же сообщения",
                    факт.get("client_message_id") == отправлен,
                    f"в факте: {факт.get('client_message_id')!r}, отправлен: {отправлен}",
                )
                # Содержимое — не место для этого поля, и не по вкусу:
                # схема `message.content.v1.json` объявлена
                # `additionalProperties: false` и поля не знает, то есть
                # запись с ним нарушала бы собственный контракт. Заодно
                # это и есть различие между «поле положили в факт» и
                # «поле положили в общий блок»: общий уезжает в обе
                # половины, и вторая получает его молча.
                check(
                    "содержимое его не несёт: общий блок уехал бы в обе половины",
                    "client_message_id" not in содержимое,
                    f"ключи содержимого: {sorted(содержимое)}",
                )
                check(
                    "состав беседы в факте есть — иначе проверка события ниже пуста",
                    isinstance(факт.get("recipient_ids"), list)
                    and факт.get("recipient_ids"),
                    str(факт.get("recipient_ids")),
                )

                # --- что доехало до канала ---------------------------
                # Кэш половин — свой (см. докстринг модуля): общий ключ
                # означал бы гонку с развёрнутым потребителем за отметку
                # `mark_delivered` по тому же `message_id`.
                cache = event_cache.EventCache(
                    settings=event_cache.CacheSettings(
                        url=os.environ.get("REDIS_CHECK_URL", "")
                    )
                )
                centrifugo = runtime.centrifugo_client_from_env()
                check(
                    "клиент Centrifugo поднялся и кэш половин адресован",
                    centrifugo is not None and bool(os.environ.get("REDIS_CHECK_URL")),
                    f"клиент: {centrifugo is not None}, "
                    f"REDIS_CHECK_URL: {bool(os.environ.get('REDIS_CHECK_URL'))}",
                )
                if centrifugo is None:
                    return

                исходы = []
                for topic, тело in (
                    (realtime_delivery.FACT_TOPIC, факт),
                    (realtime_delivery.CONTENT_TOPIC, содержимое),
                ):
                    половина = (
                        "факт"
                        if topic == realtime_delivery.FACT_TOPIC
                        else "содержимое"
                    )
                    исход = await realtime_delivery.handle_event(
                        topic=topic, body=тело, cache=cache, centrifugo=centrifugo
                    )
                    исходы.append((половина, исход))
                    check(
                        f"половина обработана без отказов ({половина})",
                        исход.failed == 0,
                        f"доставлено: {исход.delivered}, в буфере: {исход.buffered}, "
                        f"повторов: {исход.duplicates}, отказов: {исход.failed}",
                    )
                # Публикация на две половины **одна**, и это не подробность:
                # публикует та, что пришла второй, а первая ждёт её в буфере.
                # Требовать `delivered == 1` от каждой значило бы требовать
                # двух событий на одно сообщение — то есть утверждать
                # мигание, против которого написан сам `handle_event`.
                evidence["половины"] = ", ".join(
                    f"{имя}: доставлено={исход.delivered}, в буфере={исход.buffered}"
                    for имя, исход in исходы
                )
                check(
                    "две половины дали ровно одну публикацию",
                    sum(исход.delivered for _, исход in исходы) == 1,
                    evidence["половины"],
                )

                кадры = await wait_created(
                    ws, message_id=message_id, timeout=DEADLINE_SECONDS
                )
                evidence["кадров по сообщению"] = len(кадры)
                evidence["в событии"] = next(
                    (
                        кадр["client_message_id"]
                        for кадр in кадры
                        if кадр.get("client_message_id") is not None
                    ),
                    "поля нет ни в одном кадре",
                )
                evidence["recipient_ids в событии"] = (
                    "кадров нет"
                    if not кадры
                    else ("есть" if any("recipient_ids" in к for к in кадры) else "нет")
                )
                check(
                    "опубликованное событие несёт тот же client_message_id",
                    any(кадр.get("client_message_id") == отправлен for кадр in кадры),
                    f"кадров по сообщению: {len(кадры)}, ни в одном нет поля; "
                    f"отправлен: {отправлен}",
                )
                # Утечка ищется во **всех** кадрах, а не в одном: список
                # получателей отдаёт наша половина, и её кадр обязан быть
                # среди собранных (по нему же ожидание и закончилось).
                check(
                    "список получателей наружу по-прежнему не ушёл",
                    bool(кадры) and all("recipient_ids" not in к for к in кадры),
                    f"кадров: {len(кадры)}, ключи первого: "
                    f"{sorted(кадры[0]) if кадры else '—'}",
                )
        finally:
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            if cache is not None:
                with contextlib.suppress(Exception):
                    await cache.close()
            with contextlib.suppress(Exception):
                await runtime_.stop()
            # Порядок здесь не вкусовой, и это измерено: на сессии ссылаются
            # записи соединений реального времени, а на устройства — сессии
            # (`sessions_device_id_fkey`). Уборка, удаляющая `devices` раньше
            # `sessions`, падает нарушением внешнего ключа — краснеет ровно
            # там, где должна молчать, и находкой это выглядит как дефект,
            # которым не является. `outbox` идёт до сообщений: агрегат у неё
            # тот же.
            await pool.execute(
                "DELETE FROM outbox WHERE aggregate_id IN (SELECT message_id FROM messages"
                " WHERE sender_id IN (SELECT user_id FROM users WHERE external_id ="
                " ANY($1::text[])))",
                external_ids,
            )
            for таблица, поле in (
                ("messages", "sender_id"),
                ("conversation_members", "user_id"),
                ("realtime_connections", "user_id"),
                ("sessions", "user_id"),
                ("devices", "user_id"),
            ):
                await pool.execute(
                    f"DELETE FROM {таблица} WHERE {поле} IN "  # noqa: S608
                    "(SELECT user_id FROM users WHERE external_id = ANY($1::text[]))",
                    external_ids,
                )
            await pool.execute(
                "DELETE FROM conversations WHERE conversation_id NOT IN "
                "(SELECT conversation_id FROM conversation_members)"
            )
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
    print("\nclient_message_id подтверждён на канале, состав беседы — нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
