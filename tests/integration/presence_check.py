"""G3-004: присутствие и «был в сети» на живой системе.

Проверяется то, ради чего гейт заведён: отключение одного из двух устройств
не делает человека офлайн, а отметка «был в сети» переживает потерю
realtime-слоя (`IMPLEMENTATION-PLAN.md:512`). Требования — `PRS-001…003`.

Соединения здесь настоящие: два входа владельца и один вход собеседника
открывают WebSocket к развёрнутому Centrifugo через connect-proxy, то есть
строки в `realtime_connections` и отметки в `users.last_seen_at` пишет тот же
код, что поедет дальше. Проверка не регистрирует их сама: вызов
`register_realtime_connection` руками доказывал бы работу функции, а не петлю
«клиент → Centrifugo → connect-proxy → Postgres».

**Что считается «онлайн» и почему это проверяемо.** У Centrifugo нет прокси
на отключение, поэтому статус — не событие, а предикат о времени: человек
в сети, пока хоть одно его соединение продлевалось внутри окна
(`domain/presence.py`). Наружу статус не отдаётся вовсе, и HTTP-пути,
которым его можно было бы спросить, не существует. Поэтому агрегат
спрашивается у боевой функции (`repositories/sessions.count_online_users`)
**разностью**: до открытия сокетов и после. Разность, а не абсолютное
значение, потому что функция считает по всей базе, а стенд общий.

**Границы, названные явно.**

* Проверка — единственный источник realtime-соединений в этот момент; на
  этом и держится разность. Другие проверки, открывающие сокеты
  (`realtime_receive_check`, `realtime_revoke_check`), идут в алфавитном
  порядке **после** этой, а за собой убирают. Если бы чья-то свежая строка
  перешла границу окна во время прогона, разность поехала бы — поэтому на
  приёмке проверка запускается одна (`INTEGRATION_ONLY`).
* Время не ждут, а сдвигают: `refreshed_at = now() - interval`. Сдвинуть
  отметку назад и подождать три минуты дают одно и то же состояние, но
  первое занимает секунды. Прямой SQL тут — единственный способ: операции
  «состарить соединение» у сервиса нет и быть не должно.
* Уборщик в кластере идёт своим тактом (15 с) и может снять протухшую
  строку раньше проверки. Ни одно утверждение не зависит от того, **кто**
  её снял: проверяются состояние после такта и то, что отметка от уборки
  не поехала, а не автор удаления.
* Окно живости (`ONLINE_WINDOW_SECONDS`) длиннее каденции продления
  (`token_ttl_seconds`), поэтому отдельное запоздание продления не создаёт
  ложный офлайн. Проверка на это не полагается: отсутствие продления во
  время её выполнения — не её условие. Отметка и `refreshed_at` пишутся
  одной транзакцией, поэтому отметка остаётся подтверждённой жизнью и в
  том случае, если продление случится посреди прогона, — а читаются оба
  числа одним запросом, чтобы не разъехаться между двумя снимками.
* Продление Centrifugo шлёт сам, и на живом клиенте оно не наблюдается
  изнутри проверки. Доказательство того, что продление двигает отметку, —
  юнит (`tests/test_realtime.py`, «продление продлевает и отметку»);
  здесь проверяется то, что видно снаружи: отметка есть, она монотонна
  и равна наибольшему подтверждённому времени.
* Стирание эфемерного состояния (`DELETE FROM realtime_connections`)
  изображает перезапуск Centrifugo: реестр соединений — это всё, что
  перезапуск теряет. Настоящий `rollout restart` делает приёмка живой
  среды: у пода проверок нет ни `kubectl`, ни прав в кластере.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import sys
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from login_check import API, ORIGIN, _cleanup, admin_token, create_user
from realtime_revoke_check import _auth_headers, _connect, _login, _realtime_token

from messenger.domain import presence as domain
from messenger.domain.ids import UserId
from messenger.repositories import sessions, users
from messenger.repositories.postgres import PoolSettings, create_pool
from messenger.services import presence as presence_service

APPLICATION = "presence-check"

# Окно живости берётся из домена, а не переписывается сюда: переписанное
# разошлось бы с настоящим молча, и проверка старила бы соединения внутри
# окна — то есть подтверждала бы обратное тому, что проверяет.
WINDOW = timedelta(seconds=domain.ONLINE_WINDOW_SECONDS)

# Насколько состарить соединение. Обязано быть заметно больше окна, иначе
# «протухшее» соединение осталось бы живым и сценарий не отличался бы
# от предыдущего.
PAST = timedelta(minutes=10)

# Каденция продления Centrifugo: продлевать соединение он начинает через
# `token_ttl_seconds` (120 с). Число нужно только для сверки с окном
# живости — длительность прогона на него не опирается.
REFRESH_CADENCE_SECONDS = 120.0

failures: list[str] = []


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


def settings() -> PoolSettings:
    return PoolSettings(
        host=os.getenv("DATABASE_HOST", "messenger-db-pool"),
        port=int(os.getenv("DATABASE_PORT", "5432")),
        database=os.getenv("DATABASE_NAME", "messenger"),
        user=os.getenv("DATABASE_USER", "messenger"),
        password=os.getenv("DATABASE_PASSWORD", ""),
        min_size=1,
        max_size=2,
    )


# --- наблюдение --------------------------------------------------------------


async def online_users(pool) -> int:
    """Боевой агрегат: сколько человек в сети по всей базе.

    Спрашивается у той же функции, которой пользуется уборщик, а не
    у своего SQL: иначе проверялось бы совпадение двух текстов запроса,
    а не то, что боевой путь считает людей, а не соединения.
    """
    async with pool.acquire() as conn:
        return await sessions.count_online_users(conn, window=WINDOW)


async def connections_of(pool, *, user_ids) -> tuple[int, int]:
    """Оракул: `(строк, пользователей)` среди свежих соединений этих людей.

    Написан здесь заново и повторяет требование, а не реализацию: числа
    расходятся ровно тогда, когда требование нарушено. Пара, а не одно
    число, потому что вопрос «сколько людей в сети» имеет смысл только
    рядом с ответом на «сколько для этого соединений».
    """
    row = await pool.fetchrow(
        """
        SELECT count(*) AS rows, count(DISTINCT user_id) AS people
          FROM realtime_connections
         WHERE user_id = ANY($1::uuid[])
           AND refreshed_at >= now() - $2::interval
        """,
        list(user_ids),
        WINDOW,
    )
    return int(row["rows"]), int(row["people"])


async def online_exists(pool, *, user_id) -> bool:
    """Оракул по одному человеку: есть ли у него свежее соединение."""
    return bool(
        await pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM realtime_connections
                 WHERE user_id = $1
                   AND refreshed_at >= now() - $2::interval)
            """,
            user_id,
            WINDOW,
        )
    )


async def stale_rows(pool, *, user_ids) -> int:
    """Сколько протухших строк осталось у этих людей."""
    return int(
        await pool.fetchval(
            """
            SELECT count(*) FROM realtime_connections
             WHERE user_id = ANY($1::uuid[])
               AND refreshed_at < now() - $2::interval
            """,
            list(user_ids),
            WINDOW,
        )
    )


async def last_seen(pool, *, user_id) -> datetime | None:
    return await pool.fetchval(
        "SELECT last_seen_at FROM users WHERE user_id = $1", user_id
    )


async def mark_and_refresh(pool, *, user_id):
    """Отметка человека и наибольшее подтверждённое продление, одним снимком.

    Два запроса подряд прочли бы два снимка, и продление, попавшее между
    ними, развело бы числа на исправном продукте: отметка `13:00:00`,
    продление `13:00:01`. Расходиться им не на чем — оба числа пишет одна
    транзакция, — но только внутри одного снимка.
    """
    row = await pool.fetchrow(
        "SELECT (SELECT last_seen_at FROM users WHERE user_id = $1) AS seen,"
        " (SELECT max(refreshed_at) FROM realtime_connections"
        "  WHERE user_id = $1) AS confirmed",
        user_id,
    )
    return row["seen"], row["confirmed"]


async def age(pool, *, client_id: str, delta: timedelta) -> None:
    """Старит соединение. Единственный способ: операции «состарить» у сервиса нет.

    Прямой SQL здесь не обход правила, а его исполнение: время — не то,
    чем управляют снаружи, и подождать вместо сдвига значило бы растянуть
    проверку на минуты, ничего к ней не добавив.
    """
    await pool.execute(
        "UPDATE realtime_connections SET refreshed_at = now() - $2::interval"
        " WHERE client_id = $1",
        client_id,
        delta,
    )


async def forget_ephemeral(pool, *, user_ids) -> None:
    """Убирает реестр соединений этих людей — то же, что перезапуск Centrifugo.

    Перезапуск теряет именно это состояние: таблица — реестр **живых**
    соединений, а не журнал. Отметка «был в сети» лежит в `users` и
    потеряться вместе с ним не должна — в этом весь `PRS-003`.
    """
    await pool.execute(
        "DELETE FROM realtime_connections WHERE user_id = ANY($1::uuid[])",
        list(user_ids),
    )


# --- путь клиента: только HTTP и WebSocket -----------------------------------


async def list_items(http, *, token, device) -> list[dict]:
    response = await http.get(f"{API}/conversations", headers=_auth_headers(token, device))
    if response.status_code != 200:
        raise RuntimeError(f"список не отдан: {response.status_code} {response.text[:200]}")
    return response.json()["items"]


def item_of(items: list[dict], conversation_id) -> dict | None:
    for item in items:
        if item["conversation_id"] == str(conversation_id):
            return item
    return None


def participant_of(item: dict, user_id) -> dict | None:
    for summary in item["participants"]:
        if summary["user_id"] == str(user_id):
            return summary
    return None


async def conversation_with(http, *, token, device, participant_id) -> uuid.UUID:
    response = await http.post(
        f"{API}/conversations",
        json={"participant_id": str(participant_id)},
        headers={**_auth_headers(token, device), "Origin": ORIGIN},
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"беседа не создана: {response.status_code} {response.text[:200]}"
        )
    return uuid.UUID(response.json()["conversation_id"])


async def socket_for(http, *, access: str, device: uuid.UUID):
    """Выдаёт ticket, открывает WebSocket и возвращает `(сокет, client_id)`.

    Регистрацию до допуска делает connect-proxy, поэтому клиент, вернувший
    `client_id`, означает и строку в реестре: она записана раньше ответа.
    """
    token = await _realtime_token(http, access, device)
    if not token:
        raise RuntimeError("connect-токен не выдан")
    ws, client_id = await _connect(token)
    if ws is None or not client_id:
        raise RuntimeError("WebSocket не открыт")
    return ws, client_id


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


# --- сценарии ----------------------------------------------------------------


async def run() -> None:
    marker = uuid.uuid4().hex[:10]
    accounts = {
        name: (f"presence-{marker}-{name}@example.org", secrets.token_urlsafe(24))
        for name in ("owner", "peer", "silent")
    }
    # У владельца два входа — то есть два устройства и две сессии: ровно то,
    # чем проверяется агрегат по человеку, а не по соединению (`PRS-001`).
    owner_devices = {"first": uuid.uuid4(), "second": uuid.uuid4()}
    peer_device = uuid.uuid4()

    pool = await create_pool(settings(), application_name=APPLICATION)
    external_ids: dict[str, str] = {}
    access: dict[str, str] = {}
    sockets: list = []
    conversations: list = []

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as http:
        admin = await admin_token(http)
        try:
            assets = (
                ("окно живости короче сдвига, которым старятся соединения",
                 WINDOW < PAST),
                ("окно живости длиннее каденции продления,"
                 " поэтому живой клиент не мигает офлайн",
                 domain.ONLINE_WINDOW_SECONDS > REFRESH_CADENCE_SECONDS),
            )
            for what, holds in assets:
                check(what, holds)

            for name, (login, password) in accounts.items():
                external_ids[name] = await create_user(
                    http, admin, login=login, password=password, email_verified=True
                )
            check("три учётные записи заведены", len(external_ids) == 3)

            for name, (login, password) in accounts.items():
                if name == "owner":
                    continue
                device = peer_device if name == "peer" else uuid.uuid4()
                entry = await _login(http, login, password, device)
                if entry is None:
                    raise RuntimeError(f"{name} не вошёл")
                access[name] = entry[0]
            for which, device in owner_devices.items():
                login, password = accounts["owner"]
                entry = await _login(http, login, password, device)
                if entry is None:
                    raise RuntimeError(f"владелец не вошёл ({which})")
                access[which] = entry[0]

            async with pool.acquire() as conn:
                profiles = {
                    name: await users.fetch_user_by_external_id(conn, external_id=value)
                    for name, value in external_ids.items()
                }
            owner_id = UserId(profiles["owner"].user_id)
            peer_id = UserId(profiles["peer"].user_id)
            silent_id = UserId(profiles["silent"].user_id)
            our_ids = [owner_id, peer_id, silent_id]

            # --- беседы ---------------------------------------------------
            # Вторая беседа — с человеком, который realtime-соединения
            # не открывал никогда: на ней проверяется, что «ни разу не был
            # в сети» отдаётся отсутствием ключа, а не значением.
            with_peer = await conversation_with(
                http, token=access["first"], device=owner_devices["first"],
                participant_id=peer_id,
            )
            with_silent = await conversation_with(
                http, token=access["first"], device=owner_devices["first"],
                participant_id=silent_id,
            )
            conversations += [with_peer, with_silent]
            check(
                "беседа с собеседником и беседа с молчащим созданы",
                with_peer != with_silent,
            )

            # --- база отсчёта ---------------------------------------------
            # Снимается до первого сокета: агрегат считается по всей базе,
            # и разность с этим числом — вклад наших людей, а не всё
            # множество онлайн на стенде.
            baseline = await online_users(pool)

            # --- PRS-001: три соединения двух человек ---------------------
            for which, device in owner_devices.items():
                sockets.append(await socket_for(http, access=access[which], device=device))
            sockets.append(await socket_for(http, access=access["peer"], device=peer_device))
            check(
                "три WebSocket открыты и зарегистрированы",
                len(sockets) == 3 and all(client for _, client in sockets),
            )

            online = await online_users(pool)
            check(
                "PRS-001: два устройства одного человека — это один онлайн",
                online - baseline == 2,
                f"агрегат вырос на {online - baseline} при трёх соединениях",
            )
            rows, people = await connections_of(pool, user_ids=our_ids)
            check(
                "соединений три, а людей два: агрегат по человеку, не по строке",
                (rows, people) == (3, 2),
                f"строк {rows}, людей {people}",
            )

            # --- отметка равна наибольшему подтверждённому продлению ------
            # Проверяется здесь, пока ни одна строка ещё не снята: после
            # уборки `MAX(refreshed_at)` не может быть меньше отметки —
            # отметка переживает удаление строки, и это не расхождение.
            seen, confirmed = await mark_and_refresh(pool, user_id=owner_id)
            check(
                "отметка равна наибольшему подтверждённому продлению",
                seen is not None and seen == confirmed,
                f"отметка {seen}, наибольшее продление {confirmed}",
            )

            # --- PRS-001: протухшее устройство не делает офлайн -----------
            await age(pool, client_id=sockets[0][1], delta=PAST)
            online = await online_users(pool)
            check(
                "протухшее одно из двух устройств не делает человека офлайн",
                online - baseline == 2,
                f"агрегат вырос на {online - baseline}",
            )
            check(
                "оракул: у человека осталось свежее соединение",
                await online_exists(pool, user_id=owner_id),
            )

            # --- инвариант владельца: уборка отметку не откатывает -------
            before, _ = await mark_and_refresh(pool, user_id=owner_id)
            async with pool.acquire() as conn:
                # Тот же такт, что крутит воркер: уборка протухшего, счёт
                # живого и счёт заведённых — одной транзакцией.
                await presence_service.sweep(conn)
            after, confirmed_after = await mark_and_refresh(pool, user_id=owner_id)
            # Строка живого соединения обязана остаться: уборка удаляет
            # протухшее, и живое под её предикат не попадает. Осталось
            # ровно две проверки сразу — сколько протухших и сколько
            # всего, — потому что «удалил всё» тоже даёт ноль протухших.
            left_stale = await stale_rows(pool, user_ids=our_ids)
            left_rows = int(
                await pool.fetchval(
                    "SELECT count(*) FROM realtime_connections WHERE user_id = $1",
                    owner_id,
                )
            )
            check(
                "уборщик снял протухшее и не тронул живое",
                left_stale == 0 and left_rows == 1,
                f"протухших осталось {left_stale}, всего строк у человека {left_rows}",
            )
            check(
                "уборка не откатывает отметку назад",
                after is not None and before is not None and after >= before,
                f"до уборки {before}, после {after}",
            )
            # Разделено на два утверждения, чтобы ни одно из них не стало
            # ложным от чужого продления: если оно случится между чтениями,
            # отметка уйдёт вперёд — и это по-прежнему подтверждённая жизнь,
            # а не время такта. Значение, которого здесь быть не должно, —
            # отметка, не равная ни прежней, ни подтверждённой: время самого
            # уборщика не подтверждено ничем.
            check(
                "уборка не ставит своё время: отметка осталась подтверждённой",
                after == before or after == confirmed_after,
                f"до уборки {before}, после {after}, подтверждено {confirmed_after}",
            )
            online = await online_users(pool)
            check(
                "после уборки человек всё ещё онлайн",
                online - baseline == 2,
                f"агрегат вырос на {online - baseline}",
            )

            # --- PRS-002: последнее устройство уходит офлайн по времени ---
            await age(pool, client_id=sockets[1][1], delta=PAST)
            online = await online_users(pool)
            check(
                "PRS-002: офлайн наступает по времени, без сигнала от клиента",
                online - baseline == 1,
                f"агрегат вырос на {online - baseline}",
            )
            check(
                "оракул: свежих соединений у человека не осталось",
                not await online_exists(pool, user_id=owner_id),
            )

            # --- PRS-002: обрыв без закрытия ничего не меняет -------------
            # Сокет убивается без кадра закрытия: соединение исчезает для
            # сервера так же, как при закрытой вкладке или пропавшей сети.
            peer_seen_before = await last_seen(pool, user_id=peer_id)
            sockets[2][0].transport.abort()
            await asyncio.sleep(1.0)
            online = await online_users(pool)
            check(
                "обрыв без закрытия не меняет статуса: сервер о нём не узнаёт",
                online - baseline == 1,
                f"агрегат вырос на {online - baseline}",
            )
            check(
                "обрыв не двигает отметку: сигнала не было, а отметка — факт",
                await last_seen(pool, user_id=peer_id) == peer_seen_before,
            )

            # --- PRS-003: отметка переживает потерю эфемерного состояния --
            owner_before = await last_seen(pool, user_id=owner_id)
            peer_before = await last_seen(pool, user_id=peer_id)
            await forget_ephemeral(pool, user_ids=our_ids)
            online = await online_users(pool)
            check(
                "реестр соединений исчез: в сети никого из наших",
                online - baseline == 0,
                f"агрегат вырос на {online - baseline}",
            )
            check(
                "PRS-003: отметки пережили потерю реестра соединений",
                await last_seen(pool, user_id=owner_id) == owner_before
                and await last_seen(pool, user_id=peer_id) == peer_before
                and owner_before is not None and peer_before is not None,
                f"владелец {owner_before} → {await last_seen(pool, user_id=owner_id)}, "
                f"собеседник {peer_before} → {await last_seen(pool, user_id=peer_id)}",
            )

            # --- REST: что из этого видно снаружи -------------------------
            items = await list_items(
                http, token=access["first"], device=owner_devices["first"]
            )
            peer_item = participant_of(item_of(items, with_peer), peer_id)
            silent_item = participant_of(item_of(items, with_silent), silent_id)
            check(
                "last_seen_at доехала до ответа списка",
                peer_item is not None
                and "last_seen_at" in peer_item
                and parse_time(peer_item["last_seen_at"]) == peer_before,
                f"{peer_item}",
            )
            check(
                "ни разу не бывшему в сети ключ не отдаётся",
                silent_item is not None and "last_seen_at" not in silent_item,
                f"{silent_item}",
            )
        finally:
            for ws, _ in sockets:
                with contextlib.suppress(Exception):
                    await ws.close()
            async with pool.acquire() as conn:
                # Порядок обратен созданию и обязателен: `realtime_connections`
                # ссылается на `sessions` **без каскада**, и уборка встала бы
                # на внешнем ключе — тем надёжнее, чем честнее была проверка.
                # Строки соединений при этом остаются и после закрытия сокета:
                # разрыва сервер не видит, и это как раз то свойство, ради
                # которого гейт существует.
                await conn.execute(
                    "DELETE FROM realtime_connections WHERE user_id IN"
                    " (SELECT user_id FROM users WHERE external_id = ANY($1::text[]))",
                    list(external_ids.values()),
                )
                await conn.execute(
                    "DELETE FROM conversation_members WHERE conversation_id = ANY($1::uuid[])",
                    conversations,
                )
                await conn.execute(
                    "DELETE FROM conversations WHERE conversation_id = ANY($1::uuid[])",
                    conversations,
                )
            await pool.close()
            for external_id in external_ids.values():
                await _cleanup(http, admin, external_id)


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print(
        "\nприсутствие подтверждено на живой системе: отключение устройства "
        "не делает человека офлайн, отметка переживает потерю realtime-слоя"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
