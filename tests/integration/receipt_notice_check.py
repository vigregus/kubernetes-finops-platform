"""G3-007: квитанция доезжает до собеседника — и молчит, когда не должна.

`RCP-001` с серверной стороны: `message.read` уходит в `conversation:{id}`
ровно тогда, когда состояние чтения **сдвинулось**, и приходит подписанному
собеседнику тем же каналом, которым приходят сообщения. Обратная половина
того же утверждения — тишина: отставшая квитанция не публикует ничего, иначе
собеседник увидел бы прочтение, которого не было, а отметка пошла бы назад.
Поводов у тишины два, и оба здесь: номер назад и первый законный
`{read_seq: 0}`, заводящий строку из отсутствия. Второй — не разнообразие
ради разнообразия: создание строки не есть продвижение watermark, и
сравнение с неприведённым прежним (`stored != None`) публиковало бы
утверждение о нуле, которого никто не делал.

**Половина истины приходит не транспортом.** Состояние чтения отдаётся ещё
и телом беседы — сразу **обоими** REST-маршрутами, `GET /conversations`
и `POST /conversations` (второй умеет вернуть существующую беседу, а не
только создать), — и читается **до** подписки на канал: число, доступное
только вместе с транспортом, не отвечало бы на вопрос, откуда клиент берёт
истину, когда публикация потеряна. Массив при этом **разреженный**: у
участника, который квитанций не присылал, элемента нет вовсе, и ноль на
месте отсутствия здесь так же недопустим, как у `unread_count`
и `last_seen_at`. Механизмов чтения два, поэтому и точек у инварианта две,
и каждая сверяется с номером, а не с другой точкой: сверив ответы между
собой, дефект в одном маршруте закрасил бы красным и второй, и «сломано
вот здесь» перестало бы читаться из отчёта.

**Здесь проверяется связность, а не правила чисел.** Монотонность,
нормализация `delivered >= read` и границы номеров закрыты
`receipts_check.py` и юнит-тестами эндпоинта; повторять их значило бы
дважды описывать одно и то же разными словами. Предмет этой проверки —
три звена, каждое из которых видно только целиком: обработчик решает,
публиковать ли; Centrifugo доставляет; подписанный клиент получает.

**Почему API поднимается в поде, а не зовётся по адресу.** Развёрнутый API
берёт образ из values, а этот образ — сборка `main`, то есть **до** правки.
Проверка, ходящая по адресу, зеленела бы на старом коде: `message.read`
не публиковался бы вовсе, и тишину нельзя было бы отличить от отсутствия
публикатора. Поэтому ветка API поднимается здесь же, в поде проверки,
из того же образа, что и сама проверка, и ходит в живую базу через
PgBouncer. Токены при этом настоящие: их выдаёт Keycloak, и вход идёт
страницей, как у браузера.

**Блокировка глушит канал целиком, и это проверяется с обеих сторон.**
Маска метаданных активности симметрична (D14): блокировка между зрителем
и участником в **любую** сторону — значит ни присутствия, ни квитанций.
Симметрия держится **двумя** точками предиката, и дефект в одной не краснит
на другой; поэтому случаев два, а не один — «читателя заблокировал
собеседник» и «читатель заблокировал собеседника». Мутация, оставляющая
одно направление, обязана краснить ровно на своём случае.

Запись при этом происходит: маскируется канал, а не воля человека прочитать.
Поэтому у каждого заблокированного случая второе утверждение — состояние
в `read_states` всё равно сдвинулось, и прочитанное доступно из REST.

**Тишина доказывается только рядом с доставкой.** «Событие не пришло» само
по себе не улика: того же результата достигают мёртвый сокет, спящий
публикатор и пустой канал. Поэтому каждый случай устроен парой — сначала
та же отправка доезжает без блокировки, и лишь затем повторяется при
вставленной строке в `blocks`. Различие между половинами пары ровно одно:
строка в `blocks`. Отсюда же и то, что ни одно из молчаний не окружено
`finally`-уборкой: уборка краснит на пути успеха и выдаёт себя за находку.
"""
from __future__ import annotations

import asyncio
import json
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

from messenger.api.main import app

failures: list[str] = []

# Сколько ждём публикацию, которая обязана прийти. Путь короткий — обработчик
# публикует после коммита, до клиента остаётся Centrifugo, — но запас нужен
# на переподключение сокета, а не на сам путь.
DEADLINE_SECONDS = 15.0

# Окно наблюдения тишины. Это не пауза «на глазок»: ожидание привязано
# к типу события и потребляется по одному, поэтому событие прошлого шага
# в это окно попасть не может — оно снято с потока тем утверждением,
# которое его ждало.
SILENCE_SECONDS = 8.0


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


async def wait_receipt(ws, *, timeout: float) -> dict | None:
    """Ждёт `message.read` в подписанном канале.

    Отбор по типу, а не «первая публикация»: в канале беседы идут и
    `message.created` — сообщения, которыми двигают голову беседы. Принять
    их за квитанцию значило бы получить зелёную проверку на чужом событии.
    Несовпавшие кадры снимаются с потока и теряются: событий квитанции
    на одну запись приходится ровно одно, и второго шанса у них нет.

    Пустой кадр — протокольный пинг Centrifugo, и он требует ответа.
    Без него сервер закрывает соединение (`no pong`), а проверка падает
    на своём молчании, а не на дефекте.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=max(deadline - loop.time(), 0.1)
            )
        except (TimeoutError, asyncio.TimeoutError):
            return None
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
        if data is None or data.get("type") != "message.read":
            continue
        return data
    return None


async def send_messages(
    branch: httpx.AsyncClient,
    *,
    conversation_id: str,
    headers: dict[str, str],
    marker: str,
    count: int,
) -> int:
    """Пишет сообщения и возвращает голову беседы после них.

    Голова — не украшение: квитанция выше неё отвергается (`RCP-005`),
    поэтому «сдвинуть состояние» без новых сообщений нечем.
    """
    head = 0
    for number in range(count):
        принято = await branch.post(
            f"/conversations/{conversation_id}/messages",
            json={
                "client_message_id": str(uuid.uuid4()),
                "type": "text",
                "payload": {"text": f"квитанция {marker} #{number}"},
            },
            headers=headers,
        )
        if принято.status_code != 201:
            raise RuntimeError(
                f"сообщение не записалось: {принято.status_code}: {принято.text[:200]}"
            )
        head = max(head, int(принято.json()["seq"]))
    return head


async def stored(pool, *, conversation_id: str, user_id: uuid.UUID):
    """Строка `read_states` как есть. `None` — строки нет, и это не пара нулей."""
    row = await pool.fetchrow(
        """
        SELECT last_delivered_seq, last_read_seq
          FROM read_states
         WHERE conversation_id = $1 AND user_id = $2
        """,
        uuid.UUID(conversation_id),
        user_id,
    )
    return None if row is None else (row["last_delivered_seq"], row["last_read_seq"])


async def receipt(
    branch: httpx.AsyncClient,
    *,
    conversation_id: str,
    headers: dict[str, str],
    read: int,
) -> httpx.Response:
    return await branch.post(
        f"/conversations/{conversation_id}/receipts",
        json={"read_seq": read},
        headers=headers,
    )


def _by_user(elements: list[dict]) -> dict[str, dict]:
    """Элементы `read_states` по `user_id`.

    Словарь, а не список, потому что утверждений два и они разные: «элемент
    есть и равен» и «элемента нет». Второе читается отсутствием ключа —
    тем же способом, каким оно выражено в ответе; список заставил бы
    выражать отсутствие поиском по нему.
    """
    return {element["user_id"]: element for element in elements}


async def states_from_list(
    branch: httpx.AsyncClient,
    *,
    conversation_id: str,
    headers: dict[str, str],
) -> dict[str, dict]:
    """`read_states` беседы из `GET /conversations` — по `user_id`."""
    ответ = await branch.get("/conversations", headers=headers)
    if ответ.status_code != 200:
        raise RuntimeError(
            f"список бесед не отдался: {ответ.status_code}: {ответ.text[:200]}"
        )
    for item in ответ.json()["items"]:
        if item["conversation_id"] == conversation_id:
            return _by_user(item.get("read_states", []))
    raise RuntimeError(f"беседа {conversation_id} не попала в список")


async def states_from_create(
    branch: httpx.AsyncClient,
    *,
    participant_id: uuid.UUID,
    headers: dict[str, str],
) -> dict[str, dict]:
    """`read_states` той же беседы — но полученной вторым маршрутом.

    `POST /conversations` по уже существующей паре возвращает **ту же**
    беседу (`ensure_direct_conversation`), поэтому пустое состояние чтения
    здесь было бы другой правдой о ней же, а не отсутствием данных.
    """
    ответ = await branch.post(
        "/conversations",
        json={"participant_id": str(participant_id)},
        headers=headers,
    )
    if ответ.status_code not in (200, 201):
        raise RuntimeError(
            f"беседа не переиспользована: {ответ.status_code}: {ответ.text[:200]}"
        )
    return _by_user(ответ.json().get("read_states", []))


async def run() -> None:
    marker = uuid.uuid4().hex[:12]
    accounts = [
        (f"notice-{marker}-{number}@example.org", secrets.token_urlsafe(24))
        for number in range(2)
    ]
    external_ids: list[str] = []
    pool = await _pool()
    runtime = app.state.runtime

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
            ид_a = внутренние[external_ids[0]]
            ид_b = внутренние[external_ids[1]]

            # Ветка API из того же образа, что и проверка: развёрнутый API
            # собрания `main` не содержит публикации, и проверка на нём
            # зеленела бы на отсутствии публикатора.
            await runtime.start()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://branch-api", timeout=20.0
            ) as branch:
                headers_a = {**_auth_headers(токен_a, device_a), "Origin": ORIGIN}
                headers_b = {**_auth_headers(токен_b, device_b), "Origin": ORIGIN}

                создана = await branch.post(
                    "/conversations",
                    json={"participant_id": str(ид_b)},
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

                # Сообщения пишутся **до** подписки: тогда в канале нет
                # `message.created`, и первое же событие в нём — квитанция.
                head = await send_messages(
                    branch,
                    conversation_id=беседа,
                    headers=headers_a,
                    marker=marker,
                    count=3,
                )
                check("в беседе есть сообщения, и голова известна", head == 3, str(head))

                # --- состояние чтения видно в REST **до** подписки ----------
                #
                # Порядок здесь несущий: подписки в этот момент нет вовсе,
                # значит ни одно прочитанное ниже число не могло приехать
                # транспортом. Истина, доступная только вместе с каналом, —
                # не истина, и ровно поэтому квитанция живёт ещё и в теле
                # беседы, а не только в публикации.
                #
                # Квитанцию до подписки присылает **A**, а не B: B обязан
                # остаться без строки к первому законному нулю — иначе первая
                # пара нулей перестала бы быть первой, и приведение
                # `previous = None` стало бы недостижимо ничем. Отсюда же
                # и второе утверждение: пустое место B в ответе — это ветка
                # «участник без строки». `COALESCE` на элементе массива
                # вернул бы ей пару нулей, и «не присылал» стало бы
                # неотличимо от «прочитал ноль».
                своя = await receipt(
                    branch, conversation_id=беседа, headers=headers_a, read=head
                )
                check(
                    "квитанция отправляющего принята до подписки",
                    своя.status_code == 200,
                    f"{своя.status_code}: {своя.text[:200]}",
                )

                из_списка = await states_from_list(
                    branch, conversation_id=беседа, headers=headers_a
                )
                прочитано = из_списка.get(str(ид_a))
                check(
                    "список отдаёт прочитанное участника под именем колонки",
                    прочитано is not None and прочитано["last_read_seq"] == head,
                    str(из_списка),
                )
                check(
                    "доставленное не ниже прочитанного",
                    прочитано is not None and прочитано["last_delivered_seq"] >= head,
                    str(из_списка),
                )
                check(
                    "участник без квитанции элемента не имеет: отсутствие — не нули",
                    str(ид_b) not in из_списка,
                    str(из_списка),
                )

                из_создания = await states_from_create(
                    branch, participant_id=ид_b, headers=headers_a
                )
                на_втором = из_создания.get(str(ид_a))
                check(
                    "переиспользованная беседа отдаёт то же на втором маршруте",
                    на_втором is not None
                    and на_втором["last_read_seq"] == head
                    and на_втором["last_delivered_seq"] >= head,
                    str(из_создания),
                )
                check(
                    "и на втором маршруте отсутствие остаётся отсутствием",
                    str(ид_b) not in из_создания,
                    str(из_создания),
                )

                # Токен запрашивается после создания беседы: каналы
                # перечисляются на момент выдачи, и в более раннем токене
                # этой беседы просто нет.
                r = await branch.post("/realtime/token", headers=headers_b)
                check(
                    "читатель получил токен Centrifugo",
                    r.status_code == 200,
                    f"{r.status_code}: {r.text[:200]}",
                )
                if r.status_code != 200:
                    return

                ws, client_id = await _connect(r.json()["token"])
                check("WebSocket открыт", bool(ws and client_id))
                if not (ws and client_id):
                    return
                подписан, ответ = await subscribe(ws, f"conversation:{беседа}")
                check("канал беседы выдан сервером, а не выбран клиентом", подписан, ответ)

                # --- первый законный ноль: строки ещё нет -------------------
                #
                # `{read_seq: 0}` законен — оба поля разрешают ноль, — и это
                # самый первый запрос этого человека в беседе: строки
                # `read_states` нет вовсе. Запрос её создаёт, парой нулей,
                # и создание строки **не** есть продвижение watermark.
                # Событие `message.read {read_seq: 0}` объявило бы прочтение,
                # которого никто не делал. Отсутствие и ноль различимы
                # ровно здесь: сравнение с неприведённым прежним (`stored
                # != None`) истинно при неподвижном watermark, и в канал
                # ушло бы утверждение о нуле.
                ноль = await receipt(branch, conversation_id=беседа, headers=headers_b, read=0)
                check(
                    "первый ноль принят",
                    ноль.status_code == 200 and ноль.json().get("read_seq") == 0,
                    f"{ноль.status_code}: {ноль.text[:200]}",
                )
                тишина_нуля = await wait_receipt(ws, timeout=SILENCE_SECONDS)
                check(
                    "первый ноль не публикуется: строки не было, а продвижения нет",
                    тишина_нуля is None,
                    str(тишина_нуля),
                )
                строка_нуля = await stored(pool, conversation_id=беседа, user_id=ид_b)
                check(
                    "строка завелась парой нулей, а не продвинулась",
                    строка_нуля == (0, 0),
                    f"в строке {строка_нуля}, ожидалось (0, 0)",
                )

                # --- сдвиг публикует ---------------------------------------
                ответ_b = await receipt(
                    branch, conversation_id=беседа, headers=headers_b, read=head
                )
                check(
                    "квитанция принята",
                    ответ_b.status_code == 200,
                    f"{ответ_b.status_code}: {ответ_b.text[:200]}",
                )
                событие = await wait_receipt(ws, timeout=DEADLINE_SECONDS)
                check(
                    "собеседник увидел квитанцию в канале беседы",
                    событие is not None,
                    "публикация не пришла",
                )
                if событие is not None:
                    check(
                        "в событии тот, кто прочитал",
                        событие.get("reader_id") == str(ид_b),
                        str(событие),
                    )
                    check(
                        "в событии присланный номер",
                        событие.get("read_seq") == head,
                        str(событие),
                    )
                    check(
                        "доставленное не ниже прочитанного",
                        isinstance(событие.get("delivered_seq"), int)
                        and событие["delivered_seq"] >= событие["read_seq"],
                        str(событие),
                    )
                    check(
                        "номер сообщения не переиспользован под квитанцию",
                        "seq" not in событие,
                        str(событие),
                    )

                # --- отставшая квитанция молчит ----------------------------
                отставшая = await receipt(
                    branch, conversation_id=беседа, headers=headers_b, read=head - 1
                )
                check(
                    "отставшая квитанция принята с текущим значением",
                    отставшая.status_code == 200
                    and отставшая.json().get("read_seq") == head,
                    f"{отставшая.status_code}: {отставшая.text[:200]}",
                )
                тишина = await wait_receipt(ws, timeout=SILENCE_SECONDS)
                check(
                    "отставшая квитанция не публикуется",
                    тишина is None,
                    str(тишина),
                )

                # --- блокировка глушит канал в обе стороны -----------------
                #
                # Каждое молчание стоит вплотную к доставке **тем же сокетом
                # и тем же читателем**: сначала квитанция доезжает без
                # блокировки, и только потом та же самая отправка повторяется
                # при вставленной строке в `blocks`. Без этой пары «событие
                # не пришло» неотличимо от «сокет умер» — а умерший сокет
                # и мёртвый публикатор дают ровно ту же тишину, что и маска.
                for имя, блокирующий, заблокированный in (
                    ("читателя заблокировал собеседник", ид_a, ид_b),
                    ("читатель заблокировал собеседника", ид_b, ид_a),
                ):
                    живая_голова = await send_messages(
                        branch,
                        conversation_id=беседа,
                        headers=headers_a,
                        marker=marker,
                        count=2,
                    )
                    await receipt(
                        branch,
                        conversation_id=беседа,
                        headers=headers_b,
                        read=живая_голова,
                    )
                    доехало = await wait_receipt(ws, timeout=DEADLINE_SECONDS)
                    check(
                        f"{имя}: без блокировки квитанция доезжает",
                        доехало is not None,
                        "сокет или публикатор мёртв — следующее молчание ничего не докажет",
                    )
                    if доехало is not None:
                        check(
                            f"{имя}: доехала именно эта квитанция",
                            доехало.get("read_seq") == живая_голова,
                            str(доехало),
                        )

                    # Голова двигается **до** блокировки: сообщение
                    # заблокированному — не этот гейт, и опираться на его
                    # разрешённость значило бы проверять чужое правило.
                    голова = await send_messages(
                        branch,
                        conversation_id=беседа,
                        headers=headers_a,
                        marker=marker,
                        count=2,
                    )
                    await pool.execute(
                        "INSERT INTO blocks (blocker_id, blocked_id) VALUES ($1, $2)",
                        блокирующий,
                        заблокированный,
                    )
                    принята = await receipt(
                        branch, conversation_id=беседа, headers=headers_b, read=голова
                    )
                    check(
                        f"{имя}: квитанция принята",
                        принята.status_code == 200,
                        f"{принята.status_code}: {принята.text[:200]}",
                    )
                    молчание = await wait_receipt(ws, timeout=SILENCE_SECONDS)
                    check(f"{имя}: квитанция не публикуется", молчание is None, str(молчание))
                    строка = await stored(pool, conversation_id=беседа, user_id=ид_b)
                    check(
                        f"{имя}: состояние всё равно сдвинулось",
                        строка == (голова, голова),
                        f"в строке {строка}, ожидалось {(голова, голова)}",
                    )
                    await pool.execute(
                        "DELETE FROM blocks WHERE blocker_id = $1 AND blocked_id = $2",
                        блокирующий,
                        заблокированный,
                    )
        finally:
            # Пул закрывается после уборки: она идёт тем же соединением,
            # и закрытый раньше пул оставил бы фикстуру в базе.
            await runtime.stop()
            await _cleanup_users(pool, external_ids)
            await pool.close()
            for external_id in external_ids:
                await _cleanup(live, admin, external_id)


async def _cleanup_users(pool, external_ids: list[str]) -> None:
    """Уборка в порядке, обратном созданию.

    `read_states` — первой из таблиц беседы: она ссылается на беседу и
    пользователя без каскада, и забытая строка уронила бы удаление не
    только здесь, но и в уборке других проверок, работающих с теми же
    пользователями. `realtime_connections` — до `sessions`: сессию держит
    внешним ключом запись соединения.
    """
    users = (
        "SELECT user_id FROM users WHERE external_id = ANY($1::text[])"
    )
    await pool.execute(
        f"DELETE FROM blocks WHERE blocker_id IN ({users}) OR blocked_id IN ({users})",  # noqa: S608
        external_ids,
    )
    await pool.execute(
        f"DELETE FROM read_states WHERE user_id IN ({users})",  # noqa: S608
        external_ids,
    )
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
    # Проекция и чекпойнт непрочитанного уходят каскадом (`0011`), а
    # `read_states` — нет: её строки уже сняты выше по пользователю.
    await pool.execute(
        "DELETE FROM conversations WHERE conversation_id NOT IN "
        "(SELECT conversation_id FROM conversation_members)"
    )
    await pool.execute("DELETE FROM users WHERE external_id = ANY($1::text[])", external_ids)


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print(
        "\nквитанция подтверждена: сдвиг доехал до собеседника, отставка и "
        "блокировка промолчали"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
