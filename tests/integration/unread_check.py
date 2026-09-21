"""G3-003: проекция непрочитанного на живой базе.

Проверяется то, ради чего гейт заведён: повтор события не меняет счётчик,
а удаление проекции восстанавливает его из `messages` и `last_read_seq`
(`IMPLEMENTATION-PLAN.md:511`). Требования — `UNR-001…003`, `CONS-003`,
`DATA-004`.

**Путь сообщения здесь настоящий.** Сообщения уходят в API, дальше их
проводит боевой отправитель outbox, а применяет развёрнутый в кластере
потребитель `consumer-unread` — тот самый, что поедет дальше. Проверка
не применяет события сама: подмена потребителя своим вызовом доказывала бы
работу функции, а не петлю «API → outbox → Kafka → проекция». Отсюда и
способ ожидания: **опрос с пределом, а не пауза** — пауза на медленном
прогоне мигает, а на быстром молча ждёт лишнее.

Проекция читается SQL-ом роли приложения через PgBouncer — тем же путём,
что и список бесед.

**Прямой SQL применяется только там, где сервисного пути не существует**,
и всякое такое место названо в коде: удаление строки проекции (её не
удаляет ничто, и в этом смысл — она производная), надгробие сообщения
(операции удаления в v1 нет; так же делает `history_check.py`), стирание
автора (`0002_erasure.sql` снял `NOT NULL`, но операции ещё нет), снятие
отметки публикации у outbox (так выглядит смерть отправителя между
публикацией и отметкой — техника `realtime_receive_check.py`).

**Асимметрия у потери проекции такая: сносит её SQL, а чинит сервис.** Удалить
строку нечем и не нужно — производная ровно затем и заведена, чтобы её
можно было потерять. А вот спросить число есть чем: список спрашивает
его у `messages` и `read_states`, и до вопроса восстанавливает потерянные
строки. Поэтому обратная половина сценария идёт **через API**: вызов
`restore_lost_counts` руками доказал бы работу функции, а не то, что до
неё доходит список. Тот же путь закрывает начальную загрузку — беседу,
чьи события ушли из Kafka по сроку хранения: потребитель её больше не
увидит, и ждать события, которого не будет, нечем.

**Ожидание считается своим SQL-ом, а не вызовом боевой функции.** Текст
`ORACLE` написан здесь заново и повторяет требование, а не реализацию:
вызов `recount_unread` или `rebuild` сделал бы проверку зелёной при любой
ошибке, общей для обоих путей. Общая у них ровно одна — предикат, — и
тождество его половин (Python в приращении и SQL в пересчёте) проверяется
сценарием `reconciliation`.

**Границы, названные явно.**

* Квитанция не пересчитывает за потребителя, поэтому её число не может
  превышать чекпойнт. Это проверяется гонкой (там неприменённое событие
  лежит в `messages`, а в счёт попасть не должно), а не отдельным
  сценарием: без гонки «число по чекпойнту» и «число по голове беседы»
  неразличимы.
* Порядок очереди и возврат незавершённой пачки — **два сценария**,
  а не один. Гонка держит строку меньше предела запроса (`command_timeout`,
  5 с), потому что за ту же строку встаёт квитанция и ждёт её всё это
  время: продержи держатель дольше — красное было бы про таймаут
  квитанции, а не про порядок. Отказ потребителя, наоборот, требует
  держать строку дольше предела, и у него своё утверждение: та же запись
  вернулась и применилась. Смешав их, проверка при красном не отвечала бы,
  что именно сломалось.
* **Перезапуск пода в сценарии отказа не опознаётся, а исключается
  окном.** Изнутри пода не видно ни Kubernetes, ни имени процесса:
  `application_name` до сервера не доходит через PgBouncer в режиме
  транзакций (`repositories/postgres.py`). Поэтому «запись обработана
  без перезапуска» утверждается временем возврата — окном, в которое
  поднявшемуся заново контейнеру не встать, — а не тождеством процесса.
  Граница названа, чтобы её не читали шире, чем она есть.
* Стирание автора меняет вердикт предиката и для **самого автора**: его
  собственное сообщение перестаёт быть своим (`IS DISTINCT FROM` без
  субъекта — любое), а уцелевшая строка автора об этом не узнаёт, потому
  что пересборка сужается до потерянных (`_rebuild_lost`). Проверка это
  место не утверждает: операции стирания в v1 нет, а сцена, в которой
  пересобирается строка автора, потребовала бы отдельной постановки.
  Названо, чтобы расхождение не открывали заново.
* Версия строки `unread_offsets` при повторе не меняется, и в живом
  прогоне это неотличимо от «меняется»: чтобы момент обработки повтора
  стал наблюдаемым, нужна вторая запись в ту же строку — а она и есть
  новая версия. Свойство закрыто юнитом (`test_unread_service.py`,
  «повтор не пишет версию строки смещений»).
* В сценарии `исчезнувшая беседа` отравленная запись приходит в партицию
  живой беседы: её ключ переставлен **затем, чтобы порядок двух записей
  был доказуем** (записи одной партиции выдаются по смещениям). Потребитель
  ключ не читает, поэтому применение от перестановки не меняется. Границы
  у этого приёма две, и обе названы: инвариант «все события беседы в одной
  партиции» не нарушается (у исчезнувшей беседы событий больше нет, а тело
  переставленной записи живой беседе не принадлежит), а **красный прогон
  оставляет отравленную запись в потоке** — снять её оттуда нечем, и
  лечится она выкаткой исправленного образа. Краснота здесь не грязь
  проверки, а ровно тот след, ради устранения которого она написана.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
import time
import uuid

import asyncpg
import httpx
from login_check import API, ORIGIN, admin_token, create_user
from realtime_revoke_check import _auth_headers, _login

from messenger.domain.ids import ConversationId, UserId
from messenger.domain.receipts import Receipts
from messenger.repositories import unread as unread_repo
from messenger.repositories import users
from messenger.repositories.postgres import PoolSettings, create_pool
from messenger.services import receipts as receipts_service

APPLICATION = "unread-check"

# Сколько ждём проекцию. Предел здесь — утверждение, а не «достаточно
# большое число»: за это время потребитель обязан применить событие.
# Запас взят на первый пуск потребителя: группа заводится с `earliest`,
# и первый разбор накопленного потока идёт дольше установившегося режима.
DEADLINE_SECONDS = 90.0

# Четыре соединения: два берёт гонка (держатель строки чекпойнта и
# квитанция), остальные — читатели, которые ходят в пул одновременно
# с ней. Меньше двух здесь не «тесно», а взаимоблокировка: третий
# `acquire()` встал бы в очередь за первыми двумя, и проверка сама себя
# остановила бы вместо того, чтобы упасть.
POOL_MAX_SIZE = 4

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
        max_size=POOL_MAX_SIZE,
    )


# --- наблюдение за базой -----------------------------------------------------

# Нижняя и верхняя границы и «чужое» — то же, из чего состоит требование,
# и записан предикат здесь **заново**, а не вызовом боевого: иначе
# ожидание и реализация ошибались бы вместе, и проверка не доказывала бы
# ничего. Совпадение клауз с `_unread_predicate` — следствие того, что
# требование одно, а не копирование: тождество половин и проверяется
# сценарием `reconciliation`.
#
# `IS DISTINCT FROM`, а не `<>`: `messages.sender_id` стал nullable
# в `0002_erasure.sql`, и обычное сравнение выбросило бы стёртое сообщение
# из счёта — разойдясь с тем, что уже накоплено приращением.
#
# Надгробие (`deleted_at`) не упоминается намеренно: события удаления
# в потоке нет, поэтому исключение удалённых сделало бы пересчёт меньше
# приращения — то есть сломало бы ровно ту сходимость, которую проверяет
# `DATA-004`.
ORACLE = """
SELECT count(*)
  FROM messages m
 WHERE m.conversation_id = $1
   AND m.conversation_seq > $2
   AND m.conversation_seq <= $3
   AND m.sender_id IS DISTINCT FROM $4
"""


async def counter(pool, *, conversation_id, user_id) -> int | None:
    """Строка проекции. `None` — строки нет, то есть «неизвестно»."""
    return await pool.fetchval(
        """
        SELECT unread_count
          FROM unread_projection
         WHERE conversation_id = $1 AND user_id = $2
        """,
        conversation_id,
        user_id,
    )


async def checkpoint(pool, *, conversation_id) -> int | None:
    """Чекпойнт беседы. `None` — потребитель её ещё не видел."""
    return await pool.fetchval(
        "SELECT applied_through_seq FROM unread_offsets WHERE conversation_id = $1",
        conversation_id,
    )


async def read_seq(pool, *, conversation_id, user_id) -> int:
    return (
        await pool.fetchval(
            "SELECT last_read_seq FROM read_states"
            " WHERE conversation_id = $1 AND user_id = $2",
            conversation_id,
            user_id,
        )
        or 0
    )


async def source_of_truth(pool, *, conversation_id, user_id) -> int:
    """Число из источника истины при текущих границах беседы и читателя."""
    return await pool.fetchval(
        ORACLE,
        conversation_id,
        await read_seq(pool, conversation_id=conversation_id, user_id=user_id),
        await checkpoint(pool, conversation_id=conversation_id),
        user_id,
    )


async def head(pool, *, conversation_id) -> int:
    """Последний номер беседы — то, до чего чекпойнт обязан догнать."""
    return (
        await pool.fetchval(
            "SELECT max(conversation_seq) FROM messages WHERE conversation_id = $1",
            conversation_id,
        )
        or 0
    )


async def until(what: str, observe, *, expected, seconds: float = DEADLINE_SECONDS):
    """Ждёт, пока наблюдение не станет ожидаемым, и печатает итог.

    Опрос с пределом, а не `sleep`: пауза сделала бы проверку мигающей
    на медленном прогоне и медленной на быстром, а предел здесь ещё и
    утверждение — «за это время потребитель обязан был применить событие».
    """
    deadline = time.monotonic() + seconds
    seen = None
    while time.monotonic() < deadline:
        seen = await observe()
        if seen == expected:
            check(what, True)
            return seen
        await asyncio.sleep(0.25)
    check(what, False, f"ждали {expected!r}, дождались {seen!r} за {seconds:.0f} с")
    return seen


# --- путь сообщения: только HTTP ---------------------------------------------


async def send(http, *, token, device, conversation_id, text: str) -> uuid.UUID:
    """Отправляет сообщение боевым API и возвращает его идентификатор.

    Падение, а не `check`, при отказе: без сообщений проверять нечего,
    и продолжать значило бы напечатать десяток красных строк об одном
    и том же. Ненулевой код возврата и так виден в результате пода.
    """
    response = await http.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={
            "client_message_id": str(uuid.uuid4()),
            "type": "text",
            "payload": {"text": text},
        },
        headers={**_auth_headers(token, device), "Origin": ORIGIN},
    )
    if response.status_code != 201:
        raise RuntimeError(f"сообщение не принято: {response.status_code} {response.text[:200]}")
    return uuid.UUID(response.json()["message_id"])


async def conversation_with(http, *, token, device, participant_id) -> ConversationId:
    """Беседа один-на-один.

    Вторая такая же между той же парой не создастся: `direct_key` уникален,
    и повторный запрос вернул бы первую беседу. Поэтому гонка проходит
    в этой же беседе, а не в отдельной.
    """
    response = await http.post(
        f"{API}/conversations",
        json={"participant_id": str(participant_id)},
        headers={**_auth_headers(token, device), "Origin": ORIGIN},
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"беседа не создана: {response.status_code} {response.text[:200]}")
    return ConversationId(uuid.UUID(response.json()["conversation_id"]))


async def list_items(http, *, token, device) -> list[dict]:
    response = await http.get(f"{API}/conversations", headers=_auth_headers(token, device))
    if response.status_code != 200:
        raise RuntimeError(f"список не отдан: {response.status_code} {response.text[:200]}")
    return response.json()["items"]


def item_of(items: list[dict], conversation_id) -> dict | None:
    """Строка списка по идентификатору беседы.

    Поиск по идентификатору, а не по позиции: список открыт и содержит
    беседы других проверок, если те не убрали за собой. Своя беседа
    находится в любом окружении, а чужие не трогаются вовсе.
    """
    for item in items:
        if item["conversation_id"] == str(conversation_id):
            return item
    return None


async def receipt(
    conn, *, external_id, conversation_id, read: int | None = None, delivered: int | None = None
):
    """Квитанция боевым сервисом: тем же путём, что маршрут, но с числом.

    Сервис, а не маршрут, потому что пересчитанное число возвращается
    вызывающему внутри процесса, а в тело ответа маршрута оно не попадает
    намеренно: контракт квитанции в этом гейте не расширяется. Число нужно
    здесь как аргумент проверки — «пересчитал из источника истины» и
    «вычел единицу» отличаются только им.
    """
    viewer = await users.fetch_user_by_external_id(conn, external_id=external_id)
    if viewer is None:
        raise RuntimeError(f"профиля {external_id} нет в базе")
    return await receipts_service.set_receipts(
        conn,
        viewer=viewer,
        conversation_id=conversation_id,
        receipts=Receipts(delivered_seq=delivered, read_seq=read),
    )


# --- гонка квитанции и потребителя -------------------------------------------

# Ожидание блокировки наблюдается, а не выдерживается паузой, и считается
# оно по **всей очереди за строкой**, а не по числу ждущих её транзакцию.
# Формула составная, и вот почему — обе половины сняты разбором на живой
# базе, а не выведены из чтения документации.
#
# Очередь выглядит по-разному в зависимости от того, что с строкой делают.
# Если строка уже лежит и держатель занял её `FOR UPDATE`, первый ждущий
# встаёт на транзакцию держателя (`locktype='transactionid'`, `granted=false`)
# и сам при этом владеет кортежем, а каждый следующий ждёт **кортеж**
# первого и виден только строкой `locktype='tuple'`. Если же строку вставили
# и ещё не закоммитили, кортежа не ждёт никто: все ждут транзакцию, и
# кортежных строк нет вовсе.
#
# Отсюда обе половины: кортеж берётся у всякого, кто его держит или ждёт,
# а ожидание транзакции добавляется тем же счётом по номеру процесса —
# поэтому двойного счёта первого ждущего не будет. На выходе — число
# ждущих: держателя в нём нет, у него ни кортежа этой строки, ни ожидания.
# Одной половины мало в обе стороны: по транзакции очередь упирается
# в единицу, сколько бы ждущих ни стояло, а по кортежу видно ноль там,
# где строку вставили и держат.
#
# Номер процесса, а не ожидаемая единица измерения, здесь выбран потому,
# что перед базой PgBouncer в режиме транзакций: idle-клиенты делят одно
# серверное соединение, и `application_name` до сервера доходит не всегда.
# Номер транзакции от того, кто спрашивает, не зависит, и это второй
# ключ формулы. Разбор целиком — в `receipts_check.py`, там же эта пара
# запросов и заведена.
WAITING = """
SELECT count(DISTINCT l.pid)
  FROM pg_locks l
  JOIN unread_offsets u ON u.conversation_id = $1
 WHERE (
         l.locktype = 'tuple'
     AND l.relation = 'unread_offsets'::regclass
     AND l.page  = (u.ctid::text::point)[0]::int
     AND l.tuple = (u.ctid::text::point)[1]::int
       )
    OR (
         l.locktype = 'transactionid'
     AND NOT l.granted
     AND l.transactionid::text::bigint = $2
       )
"""

CURRENT_XID = "SELECT pg_current_xact_id()::text::bigint % 4294967296"

# Предел одной команды у потребителя: `acquire_timeout_seconds` из
# `PoolSettings` (5 с), который `create_pool` передаёт asyncpg как
# `command_timeout`. Число нужно здесь дважды и в разные стороны: гонка
# обязана отпустить строку **раньше** него, а отказ — держать **дольше**.
COMMAND_TIMEOUT_SECONDS = 5.0

# Сколько строку держат в гонке. Меньше предела команды с запасом, потому
# что за ту же строку встаёт квитанция и ждёт её всё это время: продержи
# держатель дольше — красное было бы про её таймаут, а не про порядок,
# и гонка перестала бы показывать то, ради чего заведена.
HOLD_BUDGET_SECONDS = 4.0

# Сколько ждут подхода потребителя к занятой строке. Событие идёт путём
# «HTTP → outbox → отправитель → Kafka → опрос»: пауза отправителя 0.5 с,
# опрос потребителя до 1 с, остальное — сеть и запись. Запас взят на
# планировщик, а не «на всякий случай»: предел здесь утверждение о пути.
FIRST_APPROACH_SECONDS = 45.0

# Сколько ждут возврата той же записи после обрыва. Пауза цикла после
# отказа — секунда (`_sleep(stop, 1.0)`), следующий опрос — ещё до
# секунды, значит укладывается в две. Предел короткий, и это тоже
# утверждение, а не «достаточно большое число»: контейнеру, поднятому
# заново, в это окно не встать — kubelet держит упавший контейнер
# в паузе не меньше десяти секунд, а потом потребителю нужно ещё
# присоединиться к группе. Так и проверяется «без перезапуска пода»:
# опознать процесс изнутри пода нечем.
RETURN_BUDGET_SECONDS = 8.0


async def blocked_count(
    conn, *, conversation_id, xid: int, expected: int, deadline: float
) -> bool:
    """Ждёт, пока за строкой беседы встанет `expected` ждущих, до предела.

    Предел передаётся моментом, а не длительностью: две последовательные
    паузы сложились бы, и держатель продержал бы строку вдвое дольше
    отведённого ему.
    """
    while time.monotonic() < deadline:
        if await conn.fetchval(WAITING, conversation_id, xid) >= expected:
            return True
        await asyncio.sleep(0.02)
    return False


async def waiting(conn, *, conversation_id, xid: int) -> int:
    """Сколько клиентов ждут строку чекпойнта этой беседы.

    Считается очередь за **строкой**, а не число ожиданий транзакции
    держателя: за `unread_offsets` встают оба устройства квитанции, и то,
    каким из двух способов каждое из них видно в `pg_locks`, зависит от
    того, что с строкой делают. Разбор — над `WAITING`.
    """
    return await conn.fetchval(WAITING, conversation_id, xid)


async def waiting_for(
    conn, *, conversation_id, xid: int, seconds: float
) -> float | None:
    """Ждёт, пока за строкой встанет хоть один, и возвращает момент.

    Момент, а не признак: по расстоянию между обрывом и следующим
    подходом проверяется, что вернулся живой цикл, а не контейнер,
    поднятый заново.
    """
    deadline = time.monotonic() + seconds
    while True:
        if await waiting(conn, conversation_id=conversation_id, xid=xid) >= 1:
            return time.monotonic()
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.05)


async def waiting_gone(
    conn, *, conversation_id, xid: int, seconds: float
) -> float | None:
    """Ждёт, пока ждущих не останется вовсе, и возвращает момент.

    Именно ноль, а не «меньше прежнего»: попытка, оборванная по пределу
    команды, уходит из `pg_locks` целиком, и это единственное, что
    отличает обрыв от «ждёт дальше».
    """
    deadline = time.monotonic() + seconds
    while True:
        if await waiting(conn, conversation_id=conversation_id, xid=xid) == 0:
            return time.monotonic()
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.05)


async def run_race(
    pool,
    *,
    conversation_id,
    external_id,
    read: int,
    expected_count: int,
    head_seq: int,
    send_new,
    label: str,
) -> None:
    """Квитанция и потребитель за строкой чекпойнта одной беседы.

    Строку держит третье соединение, а событие в беседу приходит уже
    после того, как она занята, — поэтому потребитель физически не может
    его применить, и чекпойнт стоит. Это не подпорка под потребителя,
    а воспроизведение того единственного порядка, в котором потеря
    обновления видна: абсолютная запись обязана упорядочиться
    относительно приращения, и упорядочивает их строка `unread_offsets`.

    Порядок очереди задаётся постановкой, а не планировщиком. Сначала
    встаёт квитанция — и до отправки сообщения второго ждущего быть не
    может: в беседе никто ничего не меняет, кроме неё. Только после этого
    уходит событие, и вторым ждущим оказывается обязательно потребитель.
    Обратный порядок оставил бы вопрос «кто из них первый» на волю
    планировщика, а вместе с ним и число, которое квитанция обязана
    вернуть.

    Прочитанное квитанция сдвигает вперёд, но **ниже** чекпойнта, а
    отправленное сообщение ложится в `messages` сразу и уже видно голове
    беседы. Значит пересчёт обязан упереться в чекпойнт: реализация,
    считающая по голове, положит в проекцию лишнее, а потребитель
    прибавит к этому своё событие — и итог разойдётся с источником истины
    по обоим концам сразу.

    Строка держится `HOLD_BUDGET_SECONDS` — меньше предела команды
    потребителя и квитанции. Отказ потребителя на занятой строке
    проверяется **отдельным** сценарием (`run_failure_retry`): смешав их,
    гонка при красном не отвечала бы, что именно сломалось — порядок
    очереди или возврат незавершённой пачки.
    """
    async with pool.acquire() as holder_conn, pool.acquire() as viewer_conn:
        holder = holder_conn.transaction()
        await holder.start()
        task: asyncio.Task | None = None
        committed = False
        try:
            # Пара операторов вместо `DO UPDATE ... RETURNING`: запись новой
            # версии строки на каждом событии — ровно то, чего монотонный
            # чекпойнт позволяет избежать (разбор — в `repositories/unread.py`).
            await holder_conn.execute(
                """
                INSERT INTO unread_offsets (conversation_id, applied_through_seq)
                VALUES ($1, 0)
                ON CONFLICT (conversation_id) DO NOTHING
                """,
                conversation_id,
            )
            await holder_conn.fetchval(
                "SELECT applied_through_seq FROM unread_offsets"
                " WHERE conversation_id = $1 FOR UPDATE",
                conversation_id,
            )
            # Номер снимается после вставки: до неё транзакции может ещё
            # не быть, и `pg_current_xact_id()` вернул бы NULL.
            xid = await holder_conn.fetchval(CURRENT_XID)

            # Предел очереди один на оба ожидания: держатель отпускает
            # строку раньше, чем истечёт предел команды у квитанции,
            # и второго подхода потребителя сверх этого бюджета не ждёт.
            deadline = time.monotonic() + HOLD_BUDGET_SECONDS

            task = asyncio.create_task(
                receipt(
                    viewer_conn,
                    external_id=external_id,
                    conversation_id=conversation_id,
                    read=read,
                )
            )
            queued = await blocked_count(
                holder_conn,
                conversation_id=conversation_id,
                xid=xid,
                expected=1,
                deadline=deadline,
            )
            if not queued:
                check(
                    f"{label}: квитанция встала за строкой чекпойнта",
                    False,
                    "ожидания не наблюдалось — квитанция не берёт замок беседы",
                )
            await send_new()
            both = queued and await blocked_count(
                holder_conn,
                conversation_id=conversation_id,
                xid=xid,
                expected=2,
                deadline=deadline,
            )
            check(
                f"{label}: потребитель встал за той же строкой",
                both,
                f"за {HOLD_BUDGET_SECONDS:.0f} с второго ждущего не было; строка держится"
                " меньше предела запроса квитанции, поэтому это либо событие,"
                " до потребителя не дошедшее, либо применение мимо замка",
            )
            await holder.commit()
            committed = True
        finally:
            if not committed:
                await holder.rollback()
                if task is not None and not task.done():
                    task.cancel()

        if task is None:
            # Отказ выше уже назван: сюда приходят только тогда, когда
            # до постановки квитанции дело не дошло, и ждать нечего.
            return

        # Ограничение по времени обязательное: несостоявшаяся гонка иначе
        # превратилась бы в висящий прогон, и это выглядело бы как
        # «проверка идёт», а не как «проверка не прошла».
        try:
            result = await asyncio.wait_for(task, 30)
        except Exception as exc:
            # Отдельным `check`, а не всплытием наверх: оборвавшаяся
            # квитанция — это находка о строке и её пределе запроса,
            # а не повод уронить прогон с трассировкой.
            check(
                f"{label}: квитанция дождалась строки, а не оборвалась",
                False,
                f"{type(exc).__name__}: строка держалась {HOLD_BUDGET_SECONDS:.0f} с"
                f" при пределе запроса {COMMAND_TIMEOUT_SECONDS:.0f} с",
            )
            return
        check(
            f"{label}: квитанция принята, а не отвергнута",
            result.ok,
            str(result.rejection or result.state),
        )
        check(
            f"{label}: квитанция пересчитала по чекпойнту, а не по голове беседы",
            result.unread_count == expected_count,
            f"пересчитано {result.unread_count}, ожидалось {expected_count},"
            f" голова беседы {head_seq}",
        )


# --- отказ потребителя на занятой строке -------------------------------------


async def run_failure_retry(pool, *, conversation_id, send_new, label: str) -> None:
    """Отказ потребителя на занятой строке и возврат той же записи.

    Строку чекпойнта держит третье соединение, и держит дольше предела
    одной команды потребителя (`command_timeout`, 5 с). Это не подпорка
    под отказ, а единственный отказ, который в этой системе обязан
    переживаться повтором: строка беседы занята чужой транзакцией.
    Потребитель получает на ней `TimeoutError`, откатывает событие
    и возвращает пачку в поток — иначе запись осталась бы прочитанной
    и неприменённой навсегда (`Subscriber.rewind`).

    Доказывается рядом наблюдаемых событий, а не одним числом:

        подход → обрыв → подход снова → (строка отпущена) применение

    Обрыв виден как исчезновение ждущего **при занятой строке**: дождаться
    он не мог — строка наша, — значит оборвался сам. Второй подход — это
    тот же цикл, вернувший ту же запись: не вернув пачку, потребитель
    о ней уже забыл бы, и подойти во второй раз ему стало бы нечем.

    **Перезапуск пода здесь не опознаётся, а исключается окном.**
    Изнутри пода не видно ни Kubernetes, ни имени процесса:
    `application_name` до сервера не доходит через PgBouncer в режиме
    транзакций (`repositories/postgres.py`), а другого признака «это тот
    же процесс» у проверки нет. Поэтому утверждается время: возврат
    приходит через паузу цикла, в окно, куда поднявшемуся заново
    контейнеру не встать. Граница названа, чтобы её не читали шире, чем
    она есть.
    """
    async with pool.acquire() as holder_conn:
        holder = holder_conn.transaction()
        await holder.start()
        released = False
        try:
            await holder_conn.execute(
                """
                INSERT INTO unread_offsets (conversation_id, applied_through_seq)
                VALUES ($1, 0)
                ON CONFLICT (conversation_id) DO NOTHING
                """,
                conversation_id,
            )
            await holder_conn.fetchval(
                "SELECT applied_through_seq FROM unread_offsets"
                " WHERE conversation_id = $1 FOR UPDATE",
                conversation_id,
            )
            xid = await holder_conn.fetchval(CURRENT_XID)

            # Сообщение уходит уже под замком: иначе событие применилось бы
            # до того, как строка занята, и обрывать было бы нечего.
            await send_new()
            approach = await waiting_for(
                holder_conn,
                conversation_id=conversation_id,
                xid=xid,
                seconds=FIRST_APPROACH_SECONDS,
            )
            check(
                f"{label}: потребитель подошёл к занятой строке чекпойнта",
                approach is not None,
                f"ждущего не было за {FIRST_APPROACH_SECONDS:.0f} с"
                " — событие не дошло до потребителя",
            )
            if approach is None:
                return

            # Запас сверх предела команды: он отсчитывается с момента, когда
            # запрос дошёл до сервера, а ждущий наблюдался до этого.
            broke = await waiting_gone(
                holder_conn,
                conversation_id=conversation_id,
                xid=xid,
                seconds=COMMAND_TIMEOUT_SECONDS + 5.0,
            )
            check(
                f"{label}: попытка потребителя оборвана на пределе запроса",
                broke is not None,
                f"ждущий не исчез за {COMMAND_TIMEOUT_SECONDS + 5.0:.0f} с,"
                " а строка всё ещё занята — отказа не было",
            )
            if broke is None:
                return

            returned = await waiting_for(
                holder_conn,
                conversation_id=conversation_id,
                xid=xid,
                seconds=RETURN_BUDGET_SECONDS,
            )
            check(
                f"{label}: та же запись вернулась к потребителю без перезапуска пода",
                returned is not None,
                f"второго подхода не было за {RETURN_BUDGET_SECONDS:.0f} с: без возврата"
                " пачки запись осталась бы прочитанной и неприменённой навсегда",
            )
            if returned is None:
                return
            print(
                f"      возврат через {returned - broke:.1f} с после обрыва"
                " (пауза цикла — 1 с)"
            )

            # Строка отпускается после наблюдений, а не до: отпусти её
            # раньше — и возврат было бы нечем отличить от первой попытки.
            await holder.commit()
            released = True
        finally:
            if not released:
                # Отпускается всегда: держать строку дольше предела запроса
                # значило бы проверять таймаут вместо возврата, а брошенная
                # транзакция держала бы её и после проверки.
                await holder.rollback()


# --- событие беседы, которой уже нет ------------------------------------------


async def run_vanished_conversation(
    pool,
    *,
    http,
    token: str,
    device: uuid.UUID,
    participant_id: UserId,
    live: ConversationId,
    live_sends,
    reader_id: UserId,
    label: str,
) -> None:
    """Событие беседы, удалённой до его доставки.

    Это та находка, ради которой правка делалась. Живой прогон показал
    потребителя, вставшего **навсегда** на `message.created` удалённой
    беседы: `unread_offsets` вставлялась и падала на внешнем ключе
    (`ForeignKeyViolationError`, 749 отказов за 15 ч), смещение не
    фиксировалось, пачка возвращалась в поток и упиралась в ту же запись —
    ни одно следующее событие не применялось уже никогда. Перезапуск пода
    не помогал: зафиксированное смещение группы стоит перед отравленной
    записью.

    **Путь сообщения настоящий до конца.** Сообщение уходит в API, его
    публикует боевой отправитель outbox, применяет развёрнутый потребитель
    `consumer-unread`. Не подделывается ни запись, ни её тело: тело
    собирает сервис, и оно остаётся тем же — правится только момент
    доставки.

    **Доставка откладывается снятием отметки об отправке.** Запись outbox
    уже опубликована и уже применена — снимается `published_at`, и
    настоящий отправитель публикует её второй раз (техника `CONS-003` выше
    и `realtime_receive_check.py`). Между двумя публикациями беседа
    удаляется целиком, поэтому второе событие приходит потребителю
    о беседе, которой нет.

    **Удаление и снятие отметки — одна транзакция, и это несущее
    условие.** Сними отметку раньше удаления — отправитель успел бы
    опубликовать запись до него, и потребитель применил бы её к живой
    беседе. Проверка осталась бы зелёной, ничего не доказав: окно, в
    котором запись уже в потоке, а беседа ещё есть, обязано быть
    недостижимым, и транзакция его убирает — отправитель видит снятую
    отметку только после фиксации удаления.

    **Ключ отравленной записи переставлен на живую беседу, и это про
    порядок, а не про смысл.** Потребитель ключ не читает вовсе — адаптер
    отдаёт ему тело (`adapters/kafka.py`), — поэтому применение записи от
    перестановки не меняется. Меняется порядок: записи одной партиции
    выдаются по смещениям, и только так доказуемо «живая запись лежит
    позади отравленной». Порядок нужен потому, что наблюдать здесь можно
    ровно одно: **потребитель пошёл дальше**. У отравленной записи
    применять нечего — беседы нет, и правильное поведение не пишет
    ничего, — а вставший потребитель не применяет ничего после неё.
    Отсюда и устройство: живое сообщение уходит в ту же партицию следом.

    **Живое событие уходит после отметки о повторной отправке.** Отметку
    ставит отправитель после подтверждения брокера
    (`adapters/kafka.py::Publisher.publish`), поэтому к моменту отправки
    живая запись в потоке позже отравленной, и дойти до неё, не пройдя
    отравленную, потребитель не может. «Живое событие применено» и
    означает «на отравленной он не встал». На сломанном коде то же
    утверждение краснеет: пачка возвращается на отравленную запись, и
    живая не применяется никогда.

    **Инвариант партиционирования при этом цел.** «Все события беседы
    лежат в одной партиции» — про события беседы; у отравленной беседы
    событий больше нет, а тело переставленной записи живой беседе не
    принадлежит. Порядок записей живой беседы тоже не страдает: чужая
    запись встаёт между ними, не меняя их взаимного порядка.

    **Красный прогон оставляет потребителя вставшим, и это не побочный
    эффект, а сам дефект.** Отравленная запись остаётся в потоке — снять
    её оттуда нечем, — и лечится она только выкаткой исправленного
    образа. Названо, чтобы красноту не приняли за грязь проверки:
    зелёный прогон следов не оставляет, а красный оставляет ровно тот
    след, ради устранения которого проверка написана.
    """
    doomed_conversation = await conversation_with(
        http, token=token, device=device, participant_id=participant_id
    )
    doomed_message = await send(
        http,
        token=token,
        device=device,
        conversation_id=doomed_conversation,
        text="сообщение, которое беседа не переживёт",
    )
    # Обычный путь доказан **до** вмешательства: событие применено,
    # чекпойнт сдвинут, проекция получателя записана. Без этого «после
    # удаления» не отличалось бы от «и не было никогда», а удаление — от
    # удаления беседы, в которой ничего не происходило.
    await until(
        f"{label}: событие исчезающей беседы применено",
        lambda: checkpoint(pool, conversation_id=doomed_conversation),
        expected=1,
    )
    doomed_count = await counter(
        pool, conversation_id=doomed_conversation, user_id=participant_id
    )
    check(
        f"{label}: проекция исчезающей беседы заведена",
        doomed_count == 1,
        str(doomed_count),
    )

    live_checkpoint = await checkpoint(pool, conversation_id=live)
    live_count = await counter(pool, conversation_id=live, user_id=reader_id)
    if live_checkpoint is None or live_count is None:
        # Живая беседа обязана быть применённой и учтённой до этого места —
        # все сценарии выше это утверждают. Отсутствие любой из строк
        # означало бы, что сломалось раньше, а продолжать нечем: ожидать
        # «на единицу больше неизвестного» не получится.
        check(
            f"{label}: живая беседа учтена до опыта",
            False,
            f"чекпойнт {live_checkpoint}, проекция {live_count}",
        )
        return

    try:
        async with pool.acquire() as conn:
            try:
                async with conn.transaction():
                    dropped = await conn.execute(
                        """
                        UPDATE outbox
                           SET published_at = NULL,
                               lease_owner = NULL,
                               lease_until = NULL,
                               partition_key = $2
                         WHERE aggregate_id = $1 AND event_type = 'message.created'
                        """,
                        doomed_message,
                        str(live),
                    )
                    # Порядок обратен созданию: `read_states` и `messages`
                    # ссылаются на беседу. Запись outbox не удаляется — она
                    # как раз и возвращается в поток.
                    await conn.execute(
                        "DELETE FROM read_states WHERE conversation_id = $1",
                        doomed_conversation,
                    )
                    await conn.execute(
                        "DELETE FROM messages WHERE conversation_id = $1",
                        doomed_conversation,
                    )
                    await conn.execute(
                        "DELETE FROM conversation_members WHERE conversation_id = $1",
                        doomed_conversation,
                    )
                    await conn.execute(
                        "DELETE FROM conversations WHERE conversation_id = $1",
                        doomed_conversation,
                    )
            except asyncpg.exceptions.ForeignKeyViolationError as exc:
                # Каскада нет — и дальше идти нечем. Удаление беседы здесь
                # предусловие, а не утверждение: сам инвариант («производные
                # уходят вместе с беседой») проверяется уборкой, у которой
                # для него есть числа до и после. Печатается своим
                # утверждением, а не трейсбеком.
                check(
                    f"{label}: беседа удалена вместе с производными",
                    False,
                    f"беседа не удалилась: {type(exc).__name__}:"
                    f" {str(exc).splitlines()[0]}; каскад объявлен миграцией 0011",
                )
                return

        check(
            f"{label}: отравленная запись найдена в outbox",
            dropped.endswith("1"),
            dropped,
        )
        await until(
            f"{label}: отравленная запись ушла в поток второй раз",
            lambda: pool.fetchval(
                "SELECT count(*) FROM outbox"
                " WHERE aggregate_id = $1 AND event_type = 'message.created'"
                "   AND published_at IS NOT NULL",
                doomed_message,
            ),
            expected=1,
        )

        await live_sends("сообщение в живой беседе после исчезнувшей")
        await until(
            f"{label}: живая беседа применила событие позади отравленного",
            lambda: checkpoint(pool, conversation_id=live),
            expected=live_checkpoint + 1,
        )
        live_now = await counter(pool, conversation_id=live, user_id=reader_id)
        check(
            f"{label}: живое событие дошло и до проекции",
            live_now == live_count + 1,
            f"проекция {live_now}, было {live_count}",
        )
    finally:
        # Записи outbox убираются за собой всегда: оставленная строка
        # с переставленным ключом легла бы в партицию живой беседы и
        # попалась бы на глаза чужой уборке. Отравленную запись в Kafka
        # этим не снять — снять её оттуда нечем (см. докстринг).
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM outbox WHERE aggregate_id = $1", doomed_message
            )


# --- прогон ------------------------------------------------------------------


async def run() -> None:
    marker = uuid.uuid4().hex[:10]
    accounts = {
        name: (f"unread-{marker}-{name}@example.org", secrets.token_urlsafe(24))
        for name in ("author", "reader", "outsider")
    }
    pool = await create_pool(settings(), application_name=APPLICATION)
    external_ids: dict[str, str] = {}
    tokens: dict[str, str] = {}
    devices: dict[str, uuid.UUID] = {}
    conversations: list[ConversationId] = []

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as http:
        admin = await admin_token(http)
        try:
            for name, (login, password) in accounts.items():
                external_ids[name] = await create_user(
                    http, admin, login=login, password=password, email_verified=True
                )
            check("три учётные записи заведены", len(external_ids) == 3)
            for name, (login, password) in accounts.items():
                devices[name] = uuid.uuid4()
                вход = await _login(http, login, password, devices[name])
                if вход is None:
                    raise RuntimeError(f"{name} не вошёл: обмена кода на токен не было")
                tokens[name] = вход[0]

            async with pool.acquire() as conn:
                profiles = {
                    name: await users.fetch_user_by_external_id(conn, external_id=value)
                    for name, value in external_ids.items()
                }
            reader_id = UserId(profiles["reader"].user_id)
            author_id = UserId(profiles["author"].user_id)
            outsider_id = UserId(profiles["outsider"].user_id)

            # --- беседы ---------------------------------------------------
            # Беседа проверки и беседа постороннего. Вторая нужна, чтобы
            # «посторонний не получил ничего» проверялось на человеке,
            # у которого что-то есть: иначе отсутствие строки проекции
            # неотличимо от отсутствия событий у него вообще.
            conversation = await conversation_with(
                http,
                token=tokens["author"],
                device=devices["author"],
                participant_id=reader_id,
            )
            outside = await conversation_with(
                http,
                token=tokens["author"],
                device=devices["author"],
                participant_id=outsider_id,
            )
            conversations += [conversation, outside]
            check(
                "беседа с читателем и беседа с посторонним созданы",
                conversation != outside,
                f"{conversation}, {outside}",
            )
            for text in ("в чужой беседе", "и ещё одно"):
                await send(
                    http,
                    token=tokens["author"],
                    device=devices["author"],
                    conversation_id=outside,
                    text=text,
                )

            async def our_sends(text: str) -> uuid.UUID:
                return await send(
                    http,
                    token=tokens["author"],
                    device=devices["author"],
                    conversation_id=conversation,
                    text=text,
                )

            # --- UNR-002: счётчик растёт от чужих --------------------------
            for number in (1, 2, 3):
                await our_sends(f"сообщение {number}")
            await until(
                "UNR-002: три чужих сообщения дали читателю счётчик 3",
                lambda: counter(pool, conversation_id=conversation, user_id=reader_id),
                expected=3,
            )
            check(
                "свои сообщения не подняли счётчик отправителю",
                await counter(pool, conversation_id=conversation, user_id=author_id) == 0,
                str(await counter(pool, conversation_id=conversation, user_id=author_id)),
            )

            # --- UNR-003: своё не считается --------------------------------
            await send(
                http,
                token=tokens["reader"],
                device=devices["reader"],
                conversation_id=conversation,
                text="ответ читателя",
            )
            await until(
                "UNR-003: четвёртое сообщение применено",
                lambda: checkpoint(pool, conversation_id=conversation),
                expected=4,
            )
            check(
                "UNR-003: собственное сообщение не увеличило счётчик читателя",
                await counter(pool, conversation_id=conversation, user_id=reader_id) == 3,
                str(await counter(pool, conversation_id=conversation, user_id=reader_id)),
            )
            check(
                "UNR-003: ответ читателя стал непрочитанным у автора",
                await counter(pool, conversation_id=conversation, user_id=author_id) == 1,
                str(await counter(pool, conversation_id=conversation, user_id=author_id)),
            )

            # --- UNR-001: квитанция считает от источника истины ------------
            async with pool.acquire() as conn:
                short = await receipt(
                    conn,
                    external_id=external_ids["reader"],
                    conversation_id=conversation,
                    read=2,
                )
            check(
                "UNR-001: квитанция вернула число из источника истины",
                short.ok and short.unread_count == 1,
                f"состояние {short.state}, число {short.unread_count}",
            )
            check(
                "UNR-001: то же число легло в проекцию",
                await counter(pool, conversation_id=conversation, user_id=reader_id)
                == 1
                == await source_of_truth(pool, conversation_id=conversation, user_id=reader_id),
                f"проекция {await counter(pool, conversation_id=conversation, user_id=reader_id)},"
                f" источник {await source_of_truth(pool, conversation_id=conversation, user_id=reader_id)}",
            )

            for number in (5, 6):
                await our_sends(f"сообщение {number}")
            await until(
                "квитанция догнала: в беседе шесть сообщений",
                lambda: checkpoint(pool, conversation_id=conversation),
                expected=6,
            )
            check(
                "в беседе накопилось три непрочитанных",
                await counter(pool, conversation_id=conversation, user_id=reader_id) == 3,
                str(await counter(pool, conversation_id=conversation, user_id=reader_id)),
            )
            # Прыжок через несколько сообщений сразу: пересчёт вычитанием
            # («минус единица») виден именно здесь — он не знает, через
            # сколько сообщений перепрыгнула квитанция.
            async with pool.acquire() as conn:
                jumped = await receipt(
                    conn,
                    external_id=external_ids["reader"],
                    conversation_id=conversation,
                    read=6,
                )
            check(
                "UNR-001: квитанция через три сообщения обнулила счётчик",
                jumped.unread_count == 0
                and await counter(pool, conversation_id=conversation, user_id=reader_id) == 0,
                f"пересчитано {jumped.unread_count}",
            )

            # --- CONS-003: повтор события ---------------------------------
            # Повтор не подделывается: у записи outbox снимается отметка
            # об отправке, и настоящий отправитель публикует её второй раз —
            # так выглядит его смерть между публикацией и отметкой. Техника
            # и её доводы — в `realtime_receive_check.py`.
            await our_sends("седьмое")
            await until(
                "седьмое сообщение применено",
                lambda: counter(pool, conversation_id=conversation, user_id=reader_id),
                expected=1,
            )
            eight = await our_sends("повторяемое")
            await until(
                "восьмое сообщение применено",
                lambda: checkpoint(pool, conversation_id=conversation),
                expected=8,
            )
            async with pool.acquire() as conn:
                повторено = await conn.execute(
                    """
                    UPDATE outbox
                       SET published_at = NULL, lease_owner = NULL, lease_until = NULL
                     WHERE aggregate_id = $1
                    """,
                    eight,
                )
            check("события отправлены в Kafka повторно", повторено.endswith("2"), повторено)
            # Момент повтора делается наблюдаемым до барьера: отметить
            # запись отправленной может только сам отправитель, поэтому
            # к возврату отметки повтор уже в потоке — и уходит в ту же
            # партицию раньше барьера. Без этого «повтор применён» и
            # «барьер применён» поменялись бы местами, и дефект прошёл бы
            # мимо: повторное применение не двигает чекпойнт, и ждать его
            # по чекпойнту невозможно.
            await until(
                "повтор отправлен отправителем",
                lambda: pool.fetchval(
                    "SELECT count(*) FROM outbox"
                    " WHERE aggregate_id = $1 AND published_at IS NOT NULL",
                    eight,
                ),
                expected=2,
            )
            await our_sends("после повтора")
            await until(
                "CONS-003: повтор применён, девятое сообщение учтено",
                lambda: checkpoint(pool, conversation_id=conversation),
                expected=9,
            )
            check(
                "CONS-003: повтор события не изменил счётчик дважды",
                await counter(pool, conversation_id=conversation, user_id=reader_id)
                == 3
                == await source_of_truth(pool, conversation_id=conversation, user_id=reader_id),
                f"проекция {await counter(pool, conversation_id=conversation, user_id=reader_id)},"
                f" источник {await source_of_truth(pool, conversation_id=conversation, user_id=reader_id)}",
            )

            # --- UNR-002 в списке бесед -----------------------------------
            items = await list_items(http, token=tokens["reader"], device=devices["reader"])
            row = item_of(items, conversation)
            check(
                "UNR-002: список бесед несёт счётчик непрочитанного",
                row is not None and row.get("unread_count") == 3,
                str(row),
            )
            check(
                "читатель не видит беседу, в которой не состоит",
                item_of(items, outside) is None,
                str(item_of(items, outside)),
            )
            items = await list_items(http, token=tokens["author"], device=devices["author"])
            row = item_of(items, conversation)
            check(
                "UNR-002: у автора в том же списке своё непрочитанное",
                row is not None and row.get("unread_count") == 1,
                str(row),
            )
            # Список постороннего, а не автора: автор — участник обеих бесед,
            # и «чужой беседы» в его списке нет вовсе, поэтому проверка на
            # нём не проверяла бы ничего.
            items = await list_items(http, token=tokens["outsider"], device=devices["outsider"])
            row = item_of(items, conversation)
            check(
                "посторонний не видит чужую беседу в своём списке",
                row is None,
                str(row),
            )

            # Второе устройство того же человека: счётчик один на
            # пользователя, а не на устройство.
            second_device = uuid.uuid4()
            second = await _login(
                http, accounts["reader"][0], accounts["reader"][1], second_device
            )
            if second is None:
                raise RuntimeError("читатель не вошёл вторым устройством")
            items = await list_items(http, token=second[0], device=second_device)
            row = item_of(items, conversation)
            check(
                "UNR-002: второе устройство видит тот же счётчик",
                row is not None and row.get("unread_count") == 3,
                str(row),
            )
            # Отдельной проверки «строка проекции одна на пару» здесь нет
            # и быть не может: её нечем провалить. Второе устройство не
            # заводит второй строки не потому, что так написано, а потому
            # что ключ проекции — `(user_id, conversation_id)`, и устройства
            # в нём нет вовсе. Тождество числа с двух устройств выше и есть
            # проверяемая половина утверждения.

            # --- сверка предиката: события против пересборки ---------------
            # Тождество двух половин предиката (Python в приращении и SQL
            # в пересчёте и пересборке) не постулируется, а проверяется:
            # проекция, доведённая **событиями**, и проекция, собранная
            # **пересборкой** из источника истины до того же чекпойнта,
            # сверяются поимённо по каждому участнику. Ожидание при этом
            # считает **свой** SQL проверки, а не боевая функция — иначе
            # сверка доказывала бы, что функция равна себе.
            #
            # Место у этой сверки одно, и оно здесь: и читатель, и автор
            # в этот момент держат строки, собранные **только событиями**,
            # а ниже строку читателя удаляют и восстанавливают пересчётом.
            # Проведи сверку после — «события» оказались бы пересчётом,
            # и половина предиката, живущая на Python, не проверялась бы
            # вовсе.
            #
            # Снимок берётся **до** пересборки: `rebuild` пишет строки
            # беседы, поэтому прочитай мы проекцию после него — сравнивали
            # бы пересборку с самой собой. Квитанции здесь нет намеренно:
            # она пересчитывает строку читателя из того же источника, то
            # есть делала бы ровно то, что делает пересборка, и своей
            # половины у сравнения не осталось бы.
            #
            # Автор берётся наравне с читателем: сверка одного человека
            # проверяет только половину предиката — ту, где сообщение чужое
            # (`sender_id IS DISTINCT FROM читатель`). У автора всё наоборот:
            # чужим для него является ровно одно сообщение, и на нём же
            # проверяется ветка «своё не считается».
            async with pool.acquire() as conn:
                through = await checkpoint(pool, conversation_id=conversation)
                by_events = {
                    name: await counter(pool, conversation_id=conversation, user_id=user_id)
                    for name, user_id in (("читатель", reader_id), ("автор", author_id))
                }
                rebuilt = await unread_repo.rebuild(
                    conn, conversation_id=conversation, through_seq=through
                )
            for name, user_id in (("читатель", reader_id), ("автор", author_id)):
                expected = await source_of_truth(
                    pool, conversation_id=conversation, user_id=user_id
                )
                check(
                    f"reconciliation: проекция, собранная событиями, сошлась ({name})",
                    by_events[name] == expected,
                    f"события {by_events[name]}, источник {expected}",
                )
                check(
                    f"reconciliation: пересборка сошлась с источником истины ({name})",
                    rebuilt.get(user_id) == expected,
                    f"пересборка {rebuilt.get(user_id)}, источник {expected}",
                )

            # --- критерий гейта: проекция потеряна, число восстановлено ----
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM unread_projection WHERE conversation_id = $1 AND user_id = $2",
                    conversation,
                    reader_id,
                )
            check(
                "проекция удалена (сервисного пути её удаления нет — она производная)",
                await counter(pool, conversation_id=conversation, user_id=reader_id) is None,
                "строка осталась",
            )
            # Квитанция не двигает прочитанное: число обязано вернуться
            # из `messages` и `last_read_seq`, а не из ничего.
            async with pool.acquire() as conn:
                restored = await receipt(
                    conn,
                    external_id=external_ids["reader"],
                    conversation_id=conversation,
                    read=6,
                )
            check(
                "критерий гейта: квитанция восстановила число из messages и last_read_seq",
                restored.unread_count == 3
                and await counter(pool, conversation_id=conversation, user_id=reader_id) == 3,
                f"пересчитано {restored.unread_count},"
                f" в проекции {await counter(pool, conversation_id=conversation, user_id=reader_id)}",
            )

            # --- самолечение: пропавшую строку чинит потребитель ----------
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM unread_projection WHERE conversation_id = $1 AND user_id = $2",
                    conversation,
                    reader_id,
                )
            await our_sends("десятое")
            await until(
                "десятое сообщение применено",
                lambda: checkpoint(pool, conversation_id=conversation),
                expected=10,
            )
            check(
                "пропавшая строка восстановлена пересборкой, а не приращением",
                await counter(pool, conversation_id=conversation, user_id=reader_id)
                == 4
                == await source_of_truth(pool, conversation_id=conversation, user_id=reader_id),
                f"в проекции {await counter(pool, conversation_id=conversation, user_id=reader_id)}"
                " — единица здесь означала бы приращение вместо пересборки",
            )

            # --- потерянная проекция: число восстанавливает сам список -----
            # Прямой SQL здесь **удаляет**, а восстанавливает сервис.
            # Разделение не формальное: удалить строку проекции нечем —
            # её не удаляет ничто, в этом и смысл производной, — а спросить
            # число есть чем, и это список. Поэтому и проверка идёт через
            # API, а не вызовом `restore_lost_counts`: вызов доказал бы
            # работу функции, а не то, что до неё доходит список.
            #
            # Голова беседы ещё не ушла вперёд чекпойнта (десятое применено),
            # поэтому восстановление берёт готовое число и чекпойнт не
            # двигает — объявлять нечего. Проверяются обе половины: ответ
            # обязан содержать ключ с числом из источника истины, а строка
            # проекции — появиться, иначе каждое чтение платило бы заново.
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM unread_projection WHERE conversation_id = $1 AND user_id = $2",
                    conversation,
                    reader_id,
                )
            check(
                "проекция удалена (её не удаляет ничто — она производная)",
                await counter(pool, conversation_id=conversation, user_id=reader_id) is None,
                "строка осталась",
            )
            lost_expected = await source_of_truth(
                pool, conversation_id=conversation, user_id=reader_id
            )
            items = await list_items(http, token=tokens["reader"], device=devices["reader"])
            row = item_of(items, conversation)
            check(
                "потеря проекции не меняет ответ системы: список отдаёт число из источника истины",
                row is not None
                and row.get("unread_count") == lost_expected
                and lost_expected == 4,
                f"в ответе {row}, источник {lost_expected}",
            )
            check(
                "восстановленное число записано в проекцию, а не посчитано на один ответ",
                await counter(pool, conversation_id=conversation, user_id=reader_id)
                == lost_expected,
                f"в проекции {await counter(pool, conversation_id=conversation, user_id=reader_id)}",
            )

            # --- начальная загрузка: у беседы не осталось ни строки ---------
            # Так выглядит беседа, чьи события ушли из Kafka по сроку
            # хранения, а проекции для неё не заводилось никогда:
            # `auto_offset_reset="earliest"` не делает поток вечным, и этих
            # событий потребитель уже не увидит. Память о беседе стёрта
            # целиком — ни числа, ни чекпойнта, — как если бы он её вовсе
            # не касался.
            #
            # Ожидание берётся **до** потери, и это не перестраховка: оракул
            # ограничивает счёт сверху чекпойнтом, а его сейчас не станет —
            # `conversation_seq <= NULL` не пропустил бы ни одной строки,
            # и ожидание вышло бы нулём, то есть совпало бы с неработающим
            # восстановлением.
            #
            # Удаляется проекция всей беседы, а не одна строка читателя:
            # беседа, которую потребитель не видел, не оставила строк
            # никому.
            bootstrap_expected = await source_of_truth(
                pool, conversation_id=conversation, user_id=reader_id
            )
            top = await head(pool, conversation_id=conversation)
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM unread_projection WHERE conversation_id = $1", conversation
                )
                await conn.execute(
                    "DELETE FROM unread_offsets WHERE conversation_id = $1", conversation
                )
            check(
                "память о беседе стёрта целиком: ни строки проекции, ни чекпойнта",
                await counter(pool, conversation_id=conversation, user_id=reader_id) is None
                and await checkpoint(pool, conversation_id=conversation) is None,
                f"проекция {await counter(pool, conversation_id=conversation, user_id=reader_id)},"
                f" чекпойнт {await checkpoint(pool, conversation_id=conversation)}",
            )
            items = await list_items(http, token=tokens["reader"], device=devices["reader"])
            row = item_of(items, conversation)
            check(
                "беседа без событий в Kafka: число появилось из источника истины, без потока",
                row is not None and row.get("unread_count") == bootstrap_expected,
                f"в ответе {row}, источник {bootstrap_expected}",
            )
            # Чекпойнт объявлен головой — тем же порядком, что чинит разрыв
            # номеров: пересборка посчитала число до головы целиком, и не
            # сдвинуть после неё чекпойнт значило бы оставить в потоке
            # события, которые потребитель применит второй раз.
            check(
                "чекпойнт объявлен головой: уже посчитанное не применится второй раз",
                await checkpoint(pool, conversation_id=conversation) == top,
                f"чекпойнт {await checkpoint(pool, conversation_id=conversation)},"
                f" голова {top}",
            )

            # --- надгробие и стирание --------------------------------------
            # Оба — прямой SQL, и сервисного пути у них нет: удаления
            # сообщений в v1 не существует (`history_check.py` делает так
            # же), а `0002_erasure.sql` снял `NOT NULL`, но операции ещё нет.
            # Берутся сообщения **внутри** текущего диапазона непрочитанного:
            # надгробие за его пределами ничего бы не проверило.
            taken = await pool.fetch(
                """
                SELECT message_id, conversation_seq
                  FROM messages
                 WHERE conversation_id = $1
                   AND sender_id <> $2
                   AND conversation_seq > $3
                 ORDER BY conversation_seq
                 LIMIT 2
                """,
                conversation,
                reader_id,
                await read_seq(pool, conversation_id=conversation, user_id=reader_id),
            )
            check(
                "для надгробия и стирания нашлись сообщения внутри диапазона",
                len(taken) == 2,
                str([row["conversation_seq"] for row in taken]),
            )

            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE messages SET deleted_at = now(), payload = NULL"
                    " WHERE message_id = $1",
                    taken[0]["message_id"],
                )
                await conn.execute(
                    "DELETE FROM unread_projection WHERE conversation_id = $1 AND user_id = $2",
                    conversation,
                    reader_id,
                )
            await our_sends("одиннадцатое")
            await until(
                "одиннадцатое сообщение применено",
                lambda: checkpoint(pool, conversation_id=conversation),
                expected=11,
            )
            check(
                "DATA-004: надгробие остаётся непрочитанным",
                await counter(pool, conversation_id=conversation, user_id=reader_id)
                == 5
                == await source_of_truth(pool, conversation_id=conversation, user_id=reader_id),
                f"в проекции {await counter(pool, conversation_id=conversation, user_id=reader_id)}"
                " — на единицу меньше здесь означало бы выпавшее надгробие",
            )

            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE messages SET sender_id = NULL WHERE message_id = $1",
                    taken[1]["message_id"],
                )
                await conn.execute(
                    "DELETE FROM unread_projection WHERE conversation_id = $1 AND user_id = $2",
                    conversation,
                    reader_id,
                )
            await our_sends("двенадцатое")
            await until(
                "двенадцатое сообщение применено",
                lambda: checkpoint(pool, conversation_id=conversation),
                expected=12,
            )
            check(
                "стирание автора не убрало сообщение из непрочитанного",
                await counter(pool, conversation_id=conversation, user_id=reader_id)
                == 6
                == await source_of_truth(pool, conversation_id=conversation, user_id=reader_id),
                f"в проекции {await counter(pool, conversation_id=conversation, user_id=reader_id)}"
                " — на единицу меньше здесь означало бы `<>` вместо `IS DISTINCT FROM`",
            )

            # --- посторонний ----------------------------------------------
            # Ожиданием, а не разовым чтением: сообщения в беседе
            # постороннего отправлены первыми, но применяются своей
            # партицией и своим замком, и «уже применено» здесь —
            # утверждение о потребителе, которое надо дождаться, а не
            # предположение о расписании.
            await until(
                "посторонний получил счётчик в своей беседе",
                lambda: counter(pool, conversation_id=outside, user_id=outsider_id),
                expected=2,
            )
            rows = await pool.fetchval(
                """
                SELECT count(*)
                  FROM unread_projection
                 WHERE conversation_id = $1 AND user_id = ANY($2::uuid[])
                """,
                conversation,
                [outsider_id],
            )
            check(
                "посторонний не получил строки в чужой беседе",
                rows == 0,
                f"строк: {rows}",
            )

            # --- гонка квитанции и потребителя ----------------------------
            # Читатель перескакивает через прочитанное вперёд, но остаётся
            # ниже головы беседы: к моменту пересчёта тринадцатое сообщение
            # уже в `messages`, а чекпойнт ещё двенадцатый. Число, которое
            # обязана вернуть квитанция, — счёт по чекпойнт.
            await run_race(
                pool,
                conversation_id=conversation,
                external_id=external_ids["reader"],
                read=9,
                expected_count=3,
                head_seq=13,
                send_new=lambda: our_sends("тринадцатое во время квитанции"),
                label="гонка",
            )
            await until(
                "гонка: тринадцатое сообщение применено после снятия замка",
                lambda: checkpoint(pool, conversation_id=conversation),
                expected=13,
            )
            check(
                "гонка: состояние сошлось с источником истины",
                await counter(pool, conversation_id=conversation, user_id=reader_id)
                == 4
                == await source_of_truth(pool, conversation_id=conversation, user_id=reader_id),
                f"проекция {await counter(pool, conversation_id=conversation, user_id=reader_id)},"
                f" источник {await source_of_truth(pool, conversation_id=conversation, user_id=reader_id)}",
            )

            # --- отказ потребителя на занятой строке ----------------------
            # Отдельный сценарий, а не продолжение гонки: у них разные
            # утверждения. Гонка держит строку меньше предела запроса и
            # показывает порядок очереди; здесь строка держится дольше
            # предела, и проверяется возврат незавершённой пачки.
            await run_failure_retry(
                pool,
                conversation_id=conversation,
                send_new=lambda: our_sends("четырнадцатое во время отказа"),
                label="отказ",
            )
            await until(
                "отказ: четырнадцатое сообщение применено после возврата",
                lambda: checkpoint(pool, conversation_id=conversation),
                expected=14,
            )
            check(
                "отказ: состояние сошлось с источником истины",
                await counter(pool, conversation_id=conversation, user_id=reader_id)
                == await source_of_truth(pool, conversation_id=conversation, user_id=reader_id),
                f"проекция {await counter(pool, conversation_id=conversation, user_id=reader_id)},"
                f" источник {await source_of_truth(pool, conversation_id=conversation, user_id=reader_id)}",
            )

            # --- событие беседы, которой уже нет --------------------------
            # Последним, и это не порядок ради порядка: отравленная запись
            # уходит в партицию живой беседы, а сам опыт сдвигает её
            # чекпойнт. Числа всех сценариев выше к этому моменту уже
            # утверждены, поэтому сдвиг ничего не стирает.
            await run_vanished_conversation(
                pool,
                http=http,
                token=tokens["reader"],
                device=devices["reader"],
                participant_id=outsider_id,
                live=conversation,
                live_sends=our_sends,
                reader_id=reader_id,
                label="исчезнувшая беседа",
            )
        finally:
            # Потребитель обязан догнать до уборки: неприменённое событие
            # после удаления беседы сошлётся на неё внешним ключом, и
            # потребитель встанет на нём навсегда. Проверка, оставившая
            # после себя вставший конвейер, хуже непройденной — но и
            # молчание хуже: не догнавший потребитель это и есть находка,
            # поэтому покраснение здесь уборку не отменяет.
            for conversation_id in conversations:
                await until(
                    f"потребитель догнал беседу {conversation_id}",
                    lambda conversation_id=conversation_id: checkpoint(
                        pool, conversation_id=conversation_id
                    ),
                    expected=await head(pool, conversation_id=conversation_id),
                )
            async with pool.acquire() as conn:
                # Производные непрочитанного удаляются **не здесь**, и это
                # проверяемое утверждение, а не пропуск. Каскад объявлен
                # миграцией `0011`, и проверка его — то, ради чего абзац
                # вообще существует: отвались каскад, беседа не удалилась бы
                # вовсе (`ForeignKeyViolationError` на `unread_projection`),
                # а сработай он наполовину — числа после удаления оказались
                # бы не нулями. Ни одна из четырёх чужих проверок про новую
                # производную при этом не знает, и знать не должна.
                #
                # Given: беседа, её строка чекпойнта и её строки проекции.
                # When: беседа удаляется.
                # Then: обе производные исчезли вместе с ней.
                #
                # Числа снимаются **до** удаления: после него ноль вышел бы
                # и без каскада — от несуществующей беседы, — и проверка
                # доказывала бы пустоту вместо каскада. Поэтому же берётся
                # и предусловие: строки обязаны быть, иначе удалять нечего.
                derived = (
                    """
                    SELECT
                        (SELECT count(*) FROM unread_offsets
                          WHERE conversation_id = ANY($1::uuid[])) AS offsets,
                        (SELECT count(*) FROM unread_projection
                          WHERE conversation_id = ANY($1::uuid[])) AS projection
                    """,
                    conversations,
                )
                before = await conn.fetchrow(*derived)

                # Порядок обратен созданию: `read_states` ссылается на
                # беседу и пользователя без каскада, а сессии ссылаются
                # на устройства.
                await conn.execute(
                    "DELETE FROM read_states WHERE conversation_id = ANY($1::uuid[])",
                    conversations,
                )
                await conn.execute(
                    "DELETE FROM outbox WHERE partition_key = ANY($1::text[])",
                    [str(item) for item in conversations],
                )
                await conn.execute(
                    "DELETE FROM messages WHERE conversation_id = ANY($1::uuid[])",
                    conversations,
                )
                await conn.execute(
                    "DELETE FROM conversation_members WHERE conversation_id = ANY($1::uuid[])",
                    conversations,
                )
                try:
                    await conn.execute(
                        "DELETE FROM conversations WHERE conversation_id = ANY($1::uuid[])",
                        conversations,
                    )
                except asyncpg.exceptions.ForeignKeyViolationError as exc:
                    # Каскада нет, и беседа не удаляется вовсе: производная
                    # держит её ключом. Печатается **своим** утверждением,
                    # а не трейсбеком, — иначе краснота приходила бы
                    # из уборки и не называла бы нарушенный инвариант,
                    # а разбирать её пришлось бы по стеку.
                    check(
                        "производные непрочитанного ушли вместе с беседой",
                        False,
                        f"беседа не удалилась: {type(exc).__name__}:"
                        f" {str(exc).splitlines()[0]}; каскад объявлен миграцией 0011",
                    )
                else:
                    after = await conn.fetchrow(*derived)
                    check(
                        "производные непрочитанного ушли вместе с беседой",
                        before["offsets"] > 0
                        and before["projection"] > 0
                        and after["offsets"] == 0
                        and after["projection"] == 0,
                        f"до удаления {dict(before)}, после {dict(after)};"
                        " каскад объявлен миграцией 0011",
                    )
                    # Остальная уборка — только вслед за удавшейся беседой.
                    # Без каскада пользователя тоже не удалить: проекция
                    # ссылается и на него, и каскада по `user_id` нет
                    # намеренно. Упасть здесь значило бы закрыть красноту
                    # трейсбеком поверх уже названного нарушения.
                    await conn.execute(
                        "DELETE FROM sessions WHERE user_id IN"
                        " (SELECT user_id FROM users WHERE external_id = ANY($1::text[]))",
                        list(external_ids.values()),
                    )
                    await conn.execute(
                        "DELETE FROM devices WHERE user_id IN"
                        " (SELECT user_id FROM users WHERE external_id = ANY($1::text[]))",
                        list(external_ids.values()),
                    )
                    await conn.execute(
                        "DELETE FROM users WHERE external_id = ANY($1::text[])",
                        list(external_ids.values()),
                    )
                    leftovers = await conn.fetchrow(
                        """
                        SELECT
                            (SELECT count(*) FROM users WHERE external_id = ANY($1::text[]))
                                AS users,
                            (SELECT count(*) FROM conversations
                              WHERE conversation_id = ANY($2::uuid[])) AS conversations,
                            (SELECT count(*) FROM messages
                              WHERE conversation_id = ANY($2::uuid[])) AS messages,
                            (SELECT count(*) FROM read_states
                              WHERE conversation_id = ANY($2::uuid[])) AS read_states,
                            (SELECT count(*) FROM unread_offsets
                              WHERE conversation_id = ANY($2::uuid[])) AS offsets,
                            (SELECT count(*) FROM unread_projection
                              WHERE conversation_id = ANY($2::uuid[])) AS projection,
                            (SELECT count(*) FROM outbox WHERE partition_key = ANY($3::text[]))
                                AS outbox_events
                        """,
                        list(external_ids.values()),
                        conversations,
                        [str(item) for item in conversations],
                    )
                    check(
                        "после проверки в базе не осталось следов",
                        all(value == 0 for value in leftovers.values()),
                        str(dict(leftovers)),
                    )
            await pool.close()
            for external_id in external_ids.values():
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
    print("\nнепрочитанное подтверждено на живой беседе: повтор, потеря, восстановление")
    return 0


if __name__ == "__main__":
    sys.exit(main())
