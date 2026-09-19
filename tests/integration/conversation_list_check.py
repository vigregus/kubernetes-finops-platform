"""G3-001, вторая половина: список бесед страницами на живой базе.

Проверяются данные и порядок, а не коды ответов: форма ответа, отказы
и границы страницы закрыты юнит-тестами эндпоинта
(`apps/messenger/tests/test_conversation_endpoints.py`), и дублировать их
здесь значило бы дважды описывать одно и то же разными словами.

`LIST-001` — порядок ответа и его повторяемость. `LIST-002` — статический
обход страницами: каждая беседа ровно один раз, ни дублей, ни пропусков
на стыке группы равных. `LIST-003` — последнее сообщение: у беседы без
сообщений его нет, у беседы с надгробием последним остаётся надгробие.

Чего здесь нет намеренно: проверки «во время пагинации у беседы изменилась
activity». Протокол не обещает снимка списка, и обещать его обязан был бы
отдельный контракт — замороженная граница вроде `sync_to_seq` у истории, —
а не тест. Обычный keyset по изменяемому ключу такой гарантии не даёт.

**Ключ сортировки здесь не счастливая случайность, а норма.** `now()`
в Postgres — время транзакции, а не момента, поэтому все беседы, тронутые
одной транзакцией, получают одинаковый `updated_at` до микросекунды.
Проверка идёт в одной транзакции, значит группа равных — это четыре беседы
из пяти, а не редкое совпадение. Ровно поэтому курсор задаётся парой,
и `LIST-002` краснеет на первой же странице реализации «по одному времени».

Половины курсора здесь нет и быть не может: «только отметка» отвергается
`400` до базы, и `ActivityCursor` такого состояния не представляет вовсе.
Это проверено модульно, а не в кластере — до запроса дело не доходит.

Отметка одной из бесед поднята явным `UPDATE` — так у первого компонента
пары появляется собственное значение, и видно, что он вообще участвует
в порядке. Это подготовка данных проверки, а не путь продукта: продукт
двигает отметку только через `allocate_sequence` при отправке сообщения.

Всё идёт в одной транзакции с откатом. `ON DELETE CASCADE` в схеме нет
ни одного, а отправка сообщения оставляет записи в `outbox`, поэтому уборка
в обратном порядке была бы длиннее и опаснее, чем откат.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

from messenger.domain.conversation import ConversationType
from messenger.domain.conversation_list import ActivityCursor
from messenger.domain.ids import ClientMessageId, ConversationId
from messenger.domain.message import MessageKind, MessagePayload
from messenger.repositories import conversations, users
from messenger.repositories.postgres import PoolSettings, create_pool
from messenger.services import conversations as conversation_service
from messenger.services import messages as message_service

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


def ids(page) -> list[uuid.UUID]:
    """Идентификаторы страницы как есть, в порядке ответа."""
    return [item.conversation.conversation_id for item in page.items]


async def expected_order(conn, *, user_id) -> list[uuid.UUID]:
    """Порядок списка, посчитанный в Python, а не повторённый запросом.

    Второй копией того же `ORDER BY` проверка доказывала бы только то, что
    Postgres дважды сортирует одинаково. Здесь ключ выписан заново — парой
    `(updated_at, conversation_id)` — и без `ORDER BY` в запросе: расхождение
    означало бы, что в запросе продукта ключ не тот, каким его считают.
    """
    rows = await conn.fetch(
        """
        SELECT c.conversation_id, c.updated_at
          FROM conversation_members cm
          JOIN conversations c ON c.conversation_id = cm.conversation_id
         WHERE cm.user_id = $1
           AND cm.left_at IS NULL
        """,
        user_id,
    )
    ordered = sorted(
        rows,
        key=lambda row: (row["updated_at"], row["conversation_id"]),
        reverse=True,
    )
    return [row["conversation_id"] for row in ordered]


async def run() -> None:
    pool = await create_pool(settings(), application_name="conversation-list-check")
    marker = uuid.uuid4().hex

    async with pool.acquire() as conn:
        outer = conn.transaction()
        await outer.start()
        try:
            viewer = (
                await users.ensure_user(
                    conn,
                    external_id=f"list-check-{marker}-viewer",
                    display_name="Читатель",
                    email=f"list-{marker}-viewer@example.org",
                    email_verified=True,
                )
            ).user
            peer = (
                await users.ensure_user(
                    conn,
                    external_id=f"list-check-{marker}-peer",
                    display_name="Собеседник",
                    email=f"list-{marker}-peer@example.org",
                    email_verified=True,
                )
            ).user

            # Пять бесед: четыре идут подряд по времени транзакции и потому
            # получают одинаковую отметку, пятая — с поднятой отметкой.
            # Роли у них разные, и у каждой своя проверка.
            busy, tombstoned, silent, left_peer, fresh = [
                ConversationId(uuid.uuid4()) for _ in range(5)
            ]
            for number, conversation_id in enumerate(
                (busy, tombstoned, silent, left_peer, fresh)
            ):
                await conversations.insert_conversation(
                    conn,
                    conversation_id=conversation_id,
                    type=ConversationType.DIRECT,
                    direct_key=f"list-check:{marker}:{number}",
                )
                await conversations.add_member(
                    conn, conversation_id=conversation_id, user_id=viewer.user_id
                )
                await conversations.add_member(
                    conn, conversation_id=conversation_id, user_id=peer.user_id
                )

            async def write(conversation_id: ConversationId, sender, text: str) -> None:
                written = await message_service.send_message(
                    conn,
                    sender_id=sender.user_id,
                    conversation_id=conversation_id,
                    client_message_id=ClientMessageId(uuid.uuid4()),
                    kind=MessageKind.TEXT,
                    payload=MessagePayload(text=text),
                )
                if not written.ok:
                    raise RuntimeError(f"сообщение не записалось: {written.rejection}")

            for number in range(1, 4):
                await write(
                    busy, viewer if number % 2 else peer, f"беседа {number}"
                )
            await write(tombstoned, viewer, "останется")
            await write(tombstoned, peer, "будет удалено")
            await write(left_peer, viewer, "один на один")
            await conn.execute(
                """
                UPDATE messages
                   SET deleted_at = now(), payload = NULL
                 WHERE conversation_id = $1 AND conversation_seq = 2
                """,
                tombstoned,
            )

            # Собеседник выходит: беседа остаётся в списке читателя, но
            # в участниках его быть уже не должно.
            await conn.execute(
                """
                UPDATE conversation_members
                   SET left_at = now()
                 WHERE conversation_id = $1 AND user_id = $2
                """,
                left_peer,
                peer.user_id,
            )

            # Отметка пятой беседы поднимается выше группы равных. Без этого
            # первый компонент пары не участвовал бы в порядке вовсе, и
            # ошибка «сортируем только по идентификатору» прошла бы мимо.
            await conn.execute(
                """
                UPDATE conversations
                   SET updated_at = now() + interval '1 hour'
                 WHERE conversation_id = $1
                """,
                fresh,
            )

            marks = await conn.fetch(
                """
                SELECT conversation_id, updated_at
                  FROM conversations
                 WHERE conversation_id = ANY($1::uuid[])
                """,
                [busy, tombstoned, silent, left_peer, fresh],
            )
            tied = {
                row["conversation_id"]
                for row in marks
                if row["conversation_id"] != fresh
            }
            equal_marks = {
                row["updated_at"]
                for row in marks
                if row["conversation_id"] in tied
            }
            tie_mark = next(iter(equal_marks), None)
            check(
                "четыре беседы делят одну отметку активности, пятая выше",
                len(equal_marks) == 1
                and any(
                    row["updated_at"] > tie_mark
                    for row in marks
                    if row["conversation_id"] == fresh
                ),
                str({row["conversation_id"]: row["updated_at"] for row in marks}),
            )

            expected = await expected_order(conn, user_id=viewer.user_id)

            async def page(cursor=None, limit: int = 2):
                return await conversation_service.list_conversations(
                    conn, viewer_id=viewer.user_id, cursor=cursor, limit=limit
                )

            # --- LIST-001: порядок и его повторяемость ------------------------
            whole = await page(limit=100)
            check(
                "список отдан целиком и в объявленном порядке",
                whole.ok and ids(whole.page) == expected,
                str(ids(whole.page) if whole.ok else whole.rejection),
            )
            check(
                "порядок повторяется от чтения к чтению",
                whole.ok and ids((await page(limit=100)).page) == expected,
                str(ids(whole.page) if whole.ok else whole.rejection),
            )
            tie_group = [
                conversation_id
                for conversation_id in expected
                if conversation_id in tied
            ]
            check(
                "внутри группы равных порядок задан вторым компонентом",
                tie_group == sorted(tie_group, reverse=True),
                str(tie_group),
            )
            check(
                "беседа с поднятой отметкой стоит выше группы равных",
                whole.ok and expected[0] == fresh and expected[-1] in tied,
                str(expected),
            )

            # --- LIST-002: статический обход страницами ----------------------
            seen: list[uuid.UUID] = []
            cursors: list[tuple] = []
            cursor = None
            pages = 0
            while True:
                walked = await page(cursor)
                if not walked.ok:
                    raise RuntimeError(f"обход прервался: {walked.rejection}")
                seen.extend(ids(walked.page))
                cursor = walked.next_cursor
                cursors.append(
                    (None, None)
                    if cursor is None
                    else (cursor.updated_at, cursor.conversation_id)
                )
                pages += 1
                # Предохранитель от бесконечного обхода: если курсор перестал
                # двигаться, проверка обязана сказать это, а не зависнуть.
                if cursor is None or pages > 20:
                    break

            check(
                "обход страницами заканчивается сам",
                cursor is None and pages <= 20,
                f"страниц: {pages}",
            )
            check(
                "обход даёт каждую беседу ровно один раз",
                sorted(seen) == sorted(expected) and len(seen) == len(set(seen)),
                f"получено {len(seen)}, из них различных {len(set(seen))}",
            )
            check(
                "обход укладывается в объявленный порядок без пропусков на стыке",
                seen == expected,
                f"{seen} против {expected}",
            )
            check(
                "группа равных пройдена целиком, а не потеряна на стыке",
                tied.issubset(set(seen)),
                f"не найдено: {sorted(tied - set(seen))}",
            )
            # Продолжение отдаётся парой целиком: половина курсора
            # («только время») — это ровно тот слабый вариант, от которого
            # страница теряет группу равных, и собственный `next_*` его
            # не отдаёт никогда.
            halves = [both for both in cursors if both != (None, None)]
            check(
                "продолжение — пара целиком, и на последней странице её нет",
                len(halves) == pages - 1
                and all(
                    both[0] is not None and both[1] is not None for both in halves
                )
                and cursors[-1] == (None, None),
                str(cursors),
            )

            # Половины курсора здесь нет и быть не может: оба компонента
            # `ActivityCursor` обязательны, а «только время» отвергается
            # на границе HTTP до базы. Проверять это в кластере нечем —
            # до запроса дело не доходит, — и проверено оно модульными
            # тестами (`test_одиночная_отметка_даёт_400`). Здесь важно
            # другое: пока половина была законна, она отдавала на группе
            # равных пустую страницу, и это было закреплено проверкой как
            # допустимое поведение.

            # --- LIST-003: последнее сообщение -------------------------------
            by_id = {item.conversation.conversation_id: item for item in whole.page.items}
            check(
                "беседа без сообщений в списке есть, а последнего у неё нет",
                silent in by_id and by_id[silent].last_message is None,
                str(by_id.get(silent)),
            )
            check(
                "последнее сообщение не подменяется соседним по странице",
                by_id[fresh].last_message is None
                and by_id[busy].last_message is not None
                and by_id[tombstoned].last_message is not None,
                f"fresh={by_id[fresh].last_message} busy={by_id[busy].last_message}",
            )
            head = await conn.fetchval(
                """
                SELECT max(conversation_seq)
                  FROM messages
                 WHERE conversation_id = $1
                """,
                busy,
            )
            check(
                "последнее сообщение — самое новое по номеру, а не первое",
                int(by_id[busy].last_message.conversation_seq) == head
                and by_id[busy].last_message.payload.text == "беседа 3",
                f"{by_id[busy].last_message.conversation_seq} против {head}",
            )
            tomb = by_id[tombstoned].last_message
            check(
                "надгробие последним остаётся надгробием, а не пропадает",
                tomb is not None
                and tomb.deleted_at is not None
                and tomb.payload.text is None
                and int(tomb.conversation_seq) == 2,
                str(tomb),
            )
            check(
                "участники беседы отданы без вышедшего",
                [summary.user_id for summary in by_id[left_peer].participants]
                == [viewer.user_id],
                str(by_id[left_peer].participants),
            )
            # Порядок участников — `joined_at, user_id`, а вступили оба
            # в одной транзакции, поэтому решает идентификатор. Порядок
            # объявлен именно такой: без него выдача менялась бы между
            # одинаковыми запросами.
            check(
                "участники отданы в порядке вступления, при равенстве — по идентификатору",
                [summary.user_id for summary in by_id[busy].participants]
                == sorted((viewer.user_id, peer.user_id)),
                str(by_id[busy].participants),
            )
        finally:
            await outer.rollback()

    left = await pool.fetchval(
        "SELECT count(*) FROM users WHERE external_id LIKE $1",
        f"list-check-{marker}-%",
    )
    check("после отката в базе не осталось следов", left == 0, f"строк: {left}")
    await pool.close()


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nсписок бесед подтверждён на живой базе")
    return 0


if __name__ == "__main__":
    sys.exit(main())
