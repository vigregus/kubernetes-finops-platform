"""G3-002: квитанции доставки и прочтения на живой базе.

Проверяются данные, монотонность и гонка, а не коды ответов: форма тела,
отказы и границы закрыты юнит-тестами эндпоинта
(`apps/messenger/tests/test_receipts_endpoints.py`), а правила чисел —
(`test_receipts_domain.py`). Дублировать их здесь значило бы дважды
описывать одно и то же разными словами.

`RCP-003` — монотонность: опоздавшая квитанция не откатывает счётчик,
и **каждый** ответ несёт текущее значение, а не присланное. `RCP-004` —
чтение подразумевает получение: присланное `(доставлено 2, прочитано 4)`
становится `(4, 4)`, а следующее `(9, 1)` не сбрасывает прочитанное к `1`.
`RCP-005` — номер выше головы беседы отвергается, а не ограничивается
максимумом молча. `RCP-006` — неучастник не пишет и не читает.

**Гонка здесь и есть предмет проверки.** Монотонность держится `GREATEST`
против хранимой строки, и наивное `SET last_read_seq = EXCLUDED.last_read_seq`
проходит `RCP-003` целиком: последовательный пересказ квитанций от гонки
не отличается. Расхождение видно только тогда, когда два устройства одного
пользователя пишут одну строку одновременно, — поэтому проверок две.

Гонка A детерминированная и боевой код не трогает: третье соединение
держит строку незакоммиченной, два устройства встают в `upsert` за ней,
и только потом держатель коммитит. Ожидание их блокировки — не пауза
«на глазок», а наблюдение за `pg_locks` по номеру транзакции держателя:
пауза сделала бы проверку либо мигающей, либо медленной, а наблюдаемое
состояние — нет. Номер транзакции, а не идентификатор процесса: перед
базой PgBouncer в режиме транзакций, и idle-клиенты делят одно серверное
соединение, поэтому снятый заранее `pg_backend_pid()` к моменту ожидания
указывает не на того, кто ждёт.

Гонка B естественная: два устройства шлют наперегонки разные значения,
и максимум отправленного обязан остаться в строке. Максимум в каждом круге
шлёт **другое** устройство, иначе реализация с присваиванием выживала бы
на удобном порядке постановки задач.

Фазы две. Первая идёт в одной транзакции с откатом: это дешевле и безопаснее
уборки, а данные там никому не нужны после проверки. Вторая обязана быть
закоммиченной — незакоммиченную строку второе соединение не увидит вовсе,
а поставив её в откатываемую транзакцию, гонку нельзя провести: третье
соединение встало бы на строке навсегда. Поэтому у второй фазы уборка явная
и в обратном порядке, а `read_states` в ней — **первой**: она ссылается
на беседу и пользователя без `ON DELETE CASCADE` (каскадов в схеме нет
ни одного), и забытая строка уронила бы удаление не только здесь, но и в
уборке других проверок, работающих с теми же пользователями.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

from messenger.domain.conversation import ConversationType
from messenger.domain.errors import Reason, Visibility
from messenger.domain.ids import ClientMessageId, ConversationId
from messenger.domain.message import MessageKind, MessagePayload
from messenger.domain.receipts import InvalidReceipt, Receipts
from messenger.repositories import conversations, users
from messenger.repositories.postgres import PoolSettings, create_pool
from messenger.services import messages as message_service
from messenger.services import receipts as receipts_service

APPLICATION = "receipts-check"

# Три соединения гонки A — держатель строки и два устройства — плюс запас
# на читателя строки, который берётся следом. Меньше трёх здесь не «тесно»,
# а **взаимоблокировка**: третий `pool.acquire()` встанет в очередь
# за первыми двумя, и проверка сама себя остановит вместо того, чтобы
# упасть.
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


async def ensure_user(conn, *, marker: str, role: str, name: str):
    return (
        await users.ensure_user(
            conn,
            external_id=f"receipts-check-{marker}-{role}",
            display_name=name,
            email=f"receipts-{marker}-{role}@example.org",
            email_verified=True,
        )
    ).user


async def make_conversation(conn, *, marker: str, slug: str, members) -> ConversationId:
    conversation_id = ConversationId(uuid.uuid4())
    await conversations.insert_conversation(
        conn,
        conversation_id=conversation_id,
        type=ConversationType.DIRECT,
        direct_key=f"receipts-check:{marker}:{slug}",
    )
    for member in members:
        await conversations.add_member(
            conn, conversation_id=conversation_id, user_id=member.user_id
        )
    return conversation_id


async def write_messages(conn, *, conversation_id, senders, count: int) -> None:
    for number in range(1, count + 1):
        written = await message_service.send_message(
            conn,
            sender_id=senders[number % len(senders)].user_id,
            conversation_id=conversation_id,
            client_message_id=ClientMessageId(uuid.uuid4()),
            kind=MessageKind.TEXT,
            payload=MessagePayload(text=f"сообщение {number}"),
        )
        if not written.ok:
            raise RuntimeError(f"сообщение не записалось: {written.rejection}")


async def stored(conn, *, conversation_id, user_id) -> tuple[int, int] | None:
    row = await conn.fetchrow(
        """
        SELECT last_delivered_seq, last_read_seq
          FROM read_states
         WHERE conversation_id = $1 AND user_id = $2
        """,
        conversation_id,
        user_id,
    )
    if row is None:
        return None
    return row["last_delivered_seq"], row["last_read_seq"]


async def set_receipts(conn, *, viewer, conversation_id, delivered=None, read=None):
    return await receipts_service.set_receipts(
        conn,
        viewer=viewer,
        conversation_id=conversation_id,
        receipts=Receipts(delivered_seq=delivered, read_seq=read),
    )


def pair(result) -> tuple[int, int] | None:
    if not result.ok or result.state is None:
        return None
    return result.state.delivered_seq, result.state.read_seq


# --- гонка A: детерминированная ---------------------------------------------

# Ожидание блокировки наблюдается, а не выдерживается паузой. Считаются
# неполученные блокировки на **транзакцию держателя** — и это не способ
# из многих, а единственный работающий здесь.
#
# Идентификатор процесса на такой вопрос не отвечает вовсе: `DATABASE_HOST`
# по умолчанию — PgBouncer в режиме транзакций, и idle-клиенты делят одно
# серверное соединение. Снятый заранее `pg_backend_pid()` к моменту
# ожидания указывает уже не на того, а у всех устройств оказывается один
# и тот же номер. Номер транзакции держателя от того, кто спрашивает,
# не зависит.
#
# `objid` у этих блокировок пуст — с 9.6 номер транзакции переехал
# в отдельную колонку `transactionid`, — а `pg_current_xact_id()` отдаёт
# 64-битный `xid8`, тогда как в колонке лежит 32-битный `xid`. Отсюда
# сравнение по числу и модуль.
WAITING = """
SELECT count(*)
  FROM pg_locks
 WHERE locktype = 'transactionid'
   AND NOT granted
   AND transactionid::text::bigint = $1
"""

CURRENT_XID = "SELECT pg_current_xact_id()::text::bigint % 4294967296"


async def wait_until_blocked(holder, *, xid: int, expected: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await holder.fetchval(WAITING, xid) >= expected:
            return True
        await asyncio.sleep(0.02)
    return False


async def run_race_a(
    pool, *, label: str, conversation_id, viewer, first, second
) -> None:
    """Два устройства за одной строкой, которую до них держит третье.

    Порядок постановки задач задаётся параметрами: в одном прогоне первым
    в `upsert` уходит большее значение, в другом — меньшее. Второе
    устройство стартует только после того, как первое встало в блокировку,
    — иначе «в обоих порядках» зависело бы от того, кого раньше разбудил
    планировщик, и второй прогон повторял бы первый.

    Смотрит за ожиданием **сам держатель**: его транзакция — то, чего ждут,
    и он же единственный, кто её не ждёт. Отдельного наблюдателя это
    избавляет от необходимости угадывать, кто именно встал, а вместе
    с ним — от лишнего соединения в пуле.

    Оба обязаны получить одно и то же: максимум хранимого.
    """
    async with (
        pool.acquire() as holder_conn,
        pool.acquire() as first_conn,
        pool.acquire() as second_conn,
    ):
        holder = holder_conn.transaction()
        await holder.start()
        tasks: list[asyncio.Task] = []
        committed = False
        # Отмена задач — только на аварийном выходе. `finally` выполняется
        # и при успешном коммите, а отмена после него убивает ровно ту
        # гонку, ради которой проверка заведена: устройства получают
        # `CancelledError` вместо ответа, и ожидание падает за 0.0 с,
        # выглядя как истёкший десятисекундный предел. Ошибка эта тихая —
        # стоит на пути успеха, а не отказа.
        awaiting = False
        try:
            await holder_conn.execute(
                """
                INSERT INTO read_states (
                    conversation_id, user_id,
                    last_delivered_seq, last_read_seq, updated_at
                )
                VALUES ($1, $2, 0, 0, now())
                """,
                conversation_id,
                viewer.user_id,
            )
            # Номер снимается после вставки: до неё транзакции может ещё
            # не быть, и `pg_current_xact_id()` вернул бы NULL.
            xid = await holder_conn.fetchval(CURRENT_XID)

            async def device(conn, value) -> object:
                return await set_receipts(
                    conn,
                    viewer=viewer,
                    conversation_id=conversation_id,
                    delivered=value[0],
                    read=value[1],
                )

            tasks.append(asyncio.create_task(device(first_conn, first)))
            started = await wait_until_blocked(holder_conn, xid=xid, expected=1)
            tasks.append(asyncio.create_task(device(second_conn, second)))
            both = started and await wait_until_blocked(
                holder_conn, xid=xid, expected=2
            )
            check(
                f"{label}: оба устройства стоят в upsert до коммита держателя",
                both,
                "блокировка не наблюдалась — гонка не состоялась",
            )
            await holder.commit()
            committed = True
            awaiting = True
        finally:
            if not committed:
                await holder.rollback()
            if not awaiting:
                for task in tasks:
                    if not task.done():
                        task.cancel()

        # Ограничение по времени здесь обязательное. Без него несостоявшаяся
        # гонка превратилась бы в висящий прогон, и это выглядело бы как
        # «проверка идёт», а не как «проверка не прошла».
        results = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        observed = [pair(result) for result in results]
        sent = [first, second]
        expected = (max(first[0], second[0]), max(first[1], second[1]))

        # Равенства обоих ответов максимуму требовать **нельзя**, и это
        # выяснилось на живой базе: устройство, применившееся первым,
        # честно видит только своё значение — второе к тому моменту ещё
        # не применено. Ответ несёт состояние после **своего** применения,
        # а не чужое будущее. Проверяется то, что обязано держаться при
        # любом порядке пробуждения: ответ не ниже присланного этим
        # устройством (сервер не откатил), не выше максимума присланного
        # (сервер не выдумал номера), и максимум кто-то из них увидел.
        check(
            f"{label}: ни одно устройство не отчиталось об отказе",
            all(value is not None for value in observed),
            str(observed),
        )
        check(
            f"{label}: ответ каждого не ниже присланного им",
            all(
                value is not None and value[0] >= was[0] and value[1] >= was[1]
                for value, was in zip(observed, sent)
            ),
            f"прислано {sent}, получено {observed}",
        )
        check(
            f"{label}: ответ каждого не выше максимума присланного",
            all(
                value is not None
                and value[0] <= expected[0]
                and value[1] <= expected[1]
                for value in observed
            ),
            f"максимум {expected}, получено {observed}",
        )
        check(
            f"{label}: максимум присланного кто-то из устройств увидел",
            expected in observed,
            f"максимум {expected}, получено {observed}",
        )

    async with pool.acquire() as reader:
        row = await stored(
            reader, conversation_id=conversation_id, user_id=viewer.user_id
        )
    check(
        f"{label}: строка в базе осталась максимумом",
        row == expected,
        f"ожидалось {expected}, в строке {row}",
    )


async def run() -> None:
    pool = await create_pool(settings(), application_name=APPLICATION)
    marker = uuid.uuid4().hex

    # --- Фаза 1: одно соединение, откат в конце ------------------------------
    async with pool.acquire() as conn:
        outer = conn.transaction()
        await outer.start()
        try:
            viewer = await ensure_user(conn, marker=marker, role="viewer", name="Читатель")
            peer = await ensure_user(conn, marker=marker, role="peer", name="Собеседник")
            intruder = await ensure_user(conn, marker=marker, role="intruder", name="Чужой")
            lonely = await ensure_user(conn, marker=marker, role="lonely", name="Без бесед")

            conversation = await make_conversation(
                conn, marker=marker, slug="main", members=(viewer, peer)
            )
            await write_messages(
                conn, conversation_id=conversation, senders=(viewer, peer), count=9
            )

            # Посторонний — участник **другой** беседы, а не «пользователь
            # без бесед»: проверяется, что членство в чужой беседе не
            # открывает эту. Беседа нужна как факт членства и больше нигде
            # не участвует.
            await make_conversation(conn, marker=marker, slug="other", members=(intruder,))
            empty = await make_conversation(
                conn, marker=marker, slug="empty", members=(viewer,)
            )

            # --- RCP-003: монотонность ----------------------------------------
            first = await set_receipts(
                conn, viewer=viewer, conversation_id=conversation, read=5
            )
            check(
                "RCP-003: первая квитанция записала пару (5, 5)",
                pair(first) == (5, 5),
                str(pair(first) or first.rejection),
            )
            for value, comment in ((3, "прочитано 3 после 5"), (None, "доставлено 2 после 5")):
                stale = await set_receipts(
                    conn,
                    viewer=viewer,
                    conversation_id=conversation,
                    delivered=2 if value is None else None,
                    read=value,
                )
                check(
                    f"RCP-003: опоздавшая квитанция ({comment}) ничего не откатила",
                    pair(stale) == (5, 5),
                    str(pair(stale) or stale.rejection),
                )
            check(
                "RCP-003: хранимое осталось максимумом",
                await stored(conn, conversation_id=conversation, user_id=viewer.user_id)
                == (5, 5),
                str(await stored(conn, conversation_id=conversation, user_id=viewer.user_id)),
            )

            # --- RCP-004: чтение подразумевает получение ----------------------
            # Строка собеседника ещё пуста, поэтому здесь работает ветка
            # вставки, а не обновления: обе обязаны давать одно и то же.
            raised = await set_receipts(
                conn, viewer=peer, conversation_id=conversation, delivered=2, read=4
            )
            check(
                "RCP-004: прочитано 4 подняло доставленное до 4",
                pair(raised) == (4, 4),
                str(pair(raised) or raised.rejection),
            )
            kept = await set_receipts(
                conn, viewer=peer, conversation_id=conversation, delivered=9, read=1
            )
            check(
                "RCP-004: прочитанное не сброшено к присланному",
                pair(kept) == (9, 4),
                str(pair(kept) or kept.rejection),
            )
            check(
                "RCP-004: строка совпала с ответом по обоим полям",
                await stored(conn, conversation_id=conversation, user_id=peer.user_id)
                == (9, 4),
                str(await stored(conn, conversation_id=conversation, user_id=peer.user_id)),
            )
            check(
                "RCP-004: прочитанное нигде не выше доставленного",
                (await stored(conn, conversation_id=conversation, user_id=peer.user_id))[1]
                <= (await stored(conn, conversation_id=conversation, user_id=peer.user_id))[0],
                "нарушен CHECK из миграции 0009",
            )

            # --- RCP-005: номер выше головы -----------------------------------
            before = await stored(conn, conversation_id=conversation, user_id=viewer.user_id)
            refused: dict[str, str] = {}
            for name, delivered, read in (
                ("доставлено выше головы", 10, None),
                ("прочитано выше головы", None, 10),
                ("предел int64 у доставленного", 2**63, None),
                ("предел int64 у прочитанного", None, 2**63),
                ("предел int64 минус один", 2**63 - 1, None),
            ):
                try:
                    await set_receipts(
                        conn,
                        viewer=viewer,
                        conversation_id=conversation,
                        delivered=delivered,
                        read=read,
                    )
                except InvalidReceipt as exc:
                    refused[name] = str(exc)
                    continue
                check(f"RCP-005: {name} отвергнуто", False, "квитанция принята")
            check(
                "RCP-005: отвергнуты все пять невозможных номеров",
                len(refused) == 5,
                str(sorted(refused)),
            )
            # Две разные причины, и их важно не спутать: `2**63` не доходит
            # до головы вовсе — такого номера не может быть ни у одного
            # сообщения, — а `2**63 - 1` проходит предел и отвергается
            # головой беседы, потому что такого сообщения ещё нет.
            check(
                "RCP-005: предел int64 назван как предел int64",
                # Поиск по «int64», а не по «предел int64»: домен говорит
                # «больше **предела** int64», и проверка на согласованную
                # форму ловила бы падеж, а не причину. Имя типа падежа
                # не имеет, а «головы беседы» здесь быть не должно —
                # иначе две разные причины неотличимы.
                "int64" in refused.get("предел int64 у доставленного", "")
                and "головы беседы"
                not in refused.get("предел int64 у доставленного", ""),
                refused.get("предел int64 у доставленного", "отказа не было"),
            )
            check(
                "RCP-005: предел int64 минус один отвергнут головой беседы",
                "головы беседы" in refused.get("предел int64 минус один", ""),
                refused.get("предел int64 минус один", "отказа не было"),
            )
            check(
                "RCP-005: строка после отказов не сдвинулась",
                await stored(conn, conversation_id=conversation, user_id=viewer.user_id)
                == before,
                f"{before} → "
                f"{await stored(conn, conversation_id=conversation, user_id=viewer.user_id)}",
            )

            # --- RCP-006: неучастник -------------------------------------------
            by_outsider = await set_receipts(
                conn, viewer=intruder, conversation_id=conversation, read=5
            )
            check(
                "RCP-006: участник другой беседы отказ, а не запись",
                by_outsider.rejection is Reason.NOT_A_MEMBER,
                str(by_outsider.rejection),
            )
            check(
                "RCP-006: отказ не выдаёт существование беседы",
                by_outsider.visibility is Visibility.HIDDEN,
                str(by_outsider.visibility),
            )
            by_lonely = await set_receipts(
                conn, viewer=lonely, conversation_id=conversation, read=5
            )
            check(
                "RCP-006: пользователь без бесед отказ, а не запись",
                by_lonely.rejection is Reason.NOT_A_MEMBER,
                str(by_lonely.rejection),
            )
            missing = await set_receipts(
                conn,
                viewer=viewer,
                conversation_id=ConversationId(uuid.uuid4()),
                read=5,
            )
            check(
                "RCP-006: несуществующая беседа — отсутствие ресурса",
                missing.rejection is Reason.CONVERSATION_NOT_FOUND,
                str(missing.rejection),
            )
            rows = await conn.fetchval(
                """
                SELECT count(*)
                  FROM read_states
                 WHERE conversation_id = $1 AND user_id = ANY($2::uuid[])
                """,
                conversation,
                [intruder.user_id, lonely.user_id],
            )
            check("RCP-006: посторонние не оставили строк", rows == 0, f"строк: {rows}")
            check(
                "RCP-006: строка жертвы не тронута",
                await stored(conn, conversation_id=conversation, user_id=viewer.user_id)
                == before,
                str(await stored(conn, conversation_id=conversation, user_id=viewer.user_id)),
            )

            # --- пустая беседа --------------------------------------------------
            try:
                await set_receipts(conn, viewer=viewer, conversation_id=empty, read=1)
                check("пустая беседа отвергает единицу", False, "квитанция принята")
            except InvalidReceipt:
                check("пустая беседа отвергает единицу", True)

            zero = await set_receipts(
                conn, viewer=viewer, conversation_id=empty, delivered=0
            )
            check(
                "пустая беседа принимает ноль и хранит (0, 0)",
                pair(zero) == (0, 0)
                and await stored(conn, conversation_id=empty, user_id=viewer.user_id)
                == (0, 0),
                str(pair(zero) or zero.rejection),
            )

            # --- CHECK из миграции 0009 действительно проверен ------------------
            validated = await conn.fetchval(
                """
                SELECT convalidated
                  FROM pg_constraint
                 WHERE conname = 'read_states_read_le_delivered'
                """
            )
            check(
                "CHECK read ≤ delivered существует и проверен на данных",
                validated is True,
                f"convalidated = {validated}",
            )

        finally:
            await outer.rollback()

    # --- Фаза 2: закоммиченные данные, гонка, явная уборка -------------------
    race_a = ConversationId(uuid.uuid4())
    race_b = ConversationId(uuid.uuid4())
    try:
        async with pool.acquire() as conn:
            racer = await ensure_user(conn, marker=marker, role="racer", name="Гонщик")
            partner = await ensure_user(conn, marker=marker, role="partner", name="Напарник")
            for slug, conversation_id in (("race-a", race_a), ("race-b", race_b)):
                await conversations.insert_conversation(
                    conn,
                    conversation_id=conversation_id,
                    type=ConversationType.DIRECT,
                    direct_key=f"receipts-check:{marker}:{slug}",
                )
                for member in (racer, partner):
                    await conversations.add_member(
                        conn, conversation_id=conversation_id, user_id=member.user_id
                    )
            # Голова беседы A — 5, беседы B — 12: проверка границы обязана
            # отвергать 6 и принимать 5, а гонке B нужен запас номеров,
            # чтобы значения в кругах были разными.
            await write_messages(
                conn, conversation_id=race_a, senders=(racer,), count=5
            )
            await write_messages(
                conn, conversation_id=race_b, senders=(racer,), count=12
            )

        # --- гонка A: детерминированная ----------------------------------
        await run_race_a(
            pool,
            label="гонка A (сначала большее значение)",
            conversation_id=race_a,
            viewer=racer,
            first=(5, 5),
            second=(1, 1),
        )
        # Строка освобождается, чтобы второй порядок проверялся на пустой:
        # удаление — подготовка данных проверки, а не путь продукта.
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM read_states WHERE conversation_id = $1 AND user_id = $2",
                race_a,
                racer.user_id,
            )
        await run_race_a(
            pool,
            label="гонка A (сначала меньшее значение)",
            conversation_id=race_a,
            viewer=racer,
            first=(1, 1),
            second=(5, 5),
        )

        # --- гонка B: естественная ---------------------------------------
        # Максимум в каждом круге шлёт другое устройство. Это не украшение:
        # если бы большее значение всегда уходило первым, реализация
        # с присваиванием выживала бы на удобном порядке постановки задач.
        rounds = [
            ((12, 12), (3, 3)),
            ((4, 4), (12, 12)),
            ((12, 12), (12, 12)),
            ((4, 4), (12, 12)),
            ((12, 12), (1, 1)),
            ((4, 4), (12, 12)),
        ]
        for number, (left, right) in enumerate(rounds, start=1):
            async with pool.acquire() as left_conn, pool.acquire() as right_conn:
                results = await asyncio.wait_for(
                    asyncio.gather(
                        set_receipts(
                            left_conn,
                            viewer=racer,
                            conversation_id=race_b,
                            delivered=left[0],
                            read=left[1],
                        ),
                        set_receipts(
                            right_conn,
                            viewer=racer,
                            conversation_id=race_b,
                            delivered=right[0],
                            read=right[1],
                        ),
                    ),
                    10,
                )
            # Свежее соединение, а не то же самое: запись коммитится
            # оператором, но «увидит ли её следующее соединение» —
            # отдельное утверждение, и оно здесь и проверяется.
            async with pool.acquire() as reader:
                row = await stored(reader, conversation_id=race_b, user_id=racer.user_id)
            check(
                f"гонка B, круг {number}: максимум отправленного уцелел",
                row == (12, 12),
                f"прислано {left} и {right}, в строке {row}",
            )
            check(
                f"гонка B, круг {number}: прочитанное не выше доставленного",
                row is not None and row[1] <= row[0],
                str(row),
            )
            check(
                f"гонка B, круг {number}: оба устройства приняты",
                all(pair(result) is not None for result in results),
                str([pair(result) for result in results]),
            )
    finally:
        # Порядок обратен созданию. `read_states` — первой: она ссылается
        # на беседу и пользователя без каскада, и забытая строка уронила бы
        # удаление `users` и `conversations` здесь, а потом и в уборке
        # других проверок.
        async with pool.acquire() as cleanup_conn:
            await cleanup_conn.execute(
                "DELETE FROM read_states WHERE conversation_id = ANY($1::uuid[])",
                [race_a, race_b],
            )
            await cleanup_conn.execute(
                "DELETE FROM outbox WHERE partition_key = ANY($1::text[])",
                [str(race_a), str(race_b)],
            )
            await cleanup_conn.execute(
                "DELETE FROM messages WHERE conversation_id = ANY($1::uuid[])",
                [race_a, race_b],
            )
            await cleanup_conn.execute(
                "DELETE FROM conversation_members WHERE conversation_id = ANY($1::uuid[])",
                [race_a, race_b],
            )
            await cleanup_conn.execute(
                "DELETE FROM conversations WHERE conversation_id = ANY($1::uuid[])",
                [race_a, race_b],
            )
            await cleanup_conn.execute(
                "DELETE FROM users WHERE external_id LIKE $1",
                f"receipts-check-{marker}-%",
            )

    leftovers = await pool.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM users WHERE external_id LIKE $1) AS users,
            (SELECT count(*) FROM conversations WHERE conversation_id = ANY($2))
                AS conversations,
            (SELECT count(*) FROM conversation_members WHERE conversation_id = ANY($2))
                AS members,
            (SELECT count(*) FROM messages WHERE conversation_id = ANY($2)) AS messages,
            (SELECT count(*) FROM outbox WHERE partition_key = ANY($3)) AS outbox_events,
            (SELECT count(*) FROM read_states WHERE conversation_id = ANY($2))
                AS read_states
        """,
        f"receipts-check-{marker}-%",
        [race_a, race_b],
        [str(race_a), str(race_b)],
    )
    check(
        "после проверки в базе не осталось следов",
        all(value == 0 for value in leftovers.values()),
        str(dict(leftovers)),
    )
    await pool.close()


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nквитанции подтверждены на живой беседе: монотонность, граница, гонка")
    return 0


if __name__ == "__main__":
    sys.exit(main())
