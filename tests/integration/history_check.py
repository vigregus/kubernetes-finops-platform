"""G3-001: история страницами, догрузка вперёд и надгробие внутри страницы.

Проверяются данные, а не коды ответов: формы и отказы закрыты юнит-тестами
эндпоинта (`apps/messenger/tests/test_history_endpoints.py`), и дублировать
их здесь значило бы дважды описывать одно и то же разными словами.

Надгробие создаётся настоящим `UPDATE`, а не вызовом сервиса удаления:
пути пользовательского стирания в G3-001 нет, и он сюда не входит.
Ограничение `messages_payload_matches_state` само отвергнет и «удалил,
но содержимое осталось», и «содержимое убрал, а удаление не поставил», —
поэтому валидность строки обеспечивает схема, а не наше утверждение.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

from messenger.domain.conversation import ConversationType
from messenger.domain.errors import Reason, Visibility
from messenger.domain.ids import ClientMessageId, ConversationId, ConversationSeq
from messenger.domain.message import MessageKind, MessagePayload
from messenger.repositories import conversations, users
from messenger.repositories.postgres import PoolSettings, create_pool
from messenger.services import history as history_service
from messenger.services import messages as message_service
from messenger.services.runtime import ReadMode

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


def seqs(page) -> list[int]:
    """Номера страницы как есть, в порядке ответа."""
    return [int(message.conversation_seq) for message in page.items]


async def run() -> None:
    pool = await create_pool(settings(), application_name="history-integration")
    marker = uuid.uuid4().hex
    conversation_id = ConversationId(uuid.uuid4())

    async with pool.acquire() as conn:
        outer = conn.transaction()
        await outer.start()
        try:
            author = (
                await users.ensure_user(
                    conn,
                    external_id=f"history-check-{marker}-author",
                    display_name="Автор",
                    email=f"history-{marker}-author@example.org",
                    email_verified=True,
                )
            ).user
            peer = (
                await users.ensure_user(
                    conn,
                    external_id=f"history-check-{marker}-peer",
                    display_name="Собеседник",
                    email=f"history-{marker}-peer@example.org",
                    email_verified=True,
                )
            ).user
            outsider = (
                await users.ensure_user(
                    conn,
                    external_id=f"history-check-{marker}-outsider",
                    display_name="Посторонний",
                    email=f"history-{marker}-outsider@example.org",
                    email_verified=True,
                )
            ).user
            await conversations.insert_conversation(
                conn,
                conversation_id=conversation_id,
                type=ConversationType.DIRECT,
                direct_key=f"history-check:{marker}",
            )
            await conversations.add_member(
                conn, conversation_id=conversation_id, user_id=author.user_id
            )
            await conversations.add_member(
                conn, conversation_id=conversation_id, user_id=peer.user_id
            )

            async def write(sender, text: str) -> None:
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

            async def page(
                viewer=None,
                *,
                before_seq: int | None = None,
                after_seq: int | None = None,
                through_seq: int | None = None,
                limit: int = 5,
            ):
                return await history_service.list_messages(
                    conn,
                    viewer=viewer or author,
                    conversation_id=conversation_id,
                    before_seq=(
                        ConversationSeq(before_seq) if before_seq is not None else None
                    ),
                    after_seq=(
                        ConversationSeq(after_seq) if after_seq is not None else None
                    ),
                    through_seq=(
                        ConversationSeq(through_seq) if through_seq is not None else None
                    ),
                    limit=limit,
                )

            for number in range(1, 13):
                await write(author if number % 2 else peer, str(number))

            in_database = await conn.fetchval(
                "SELECT count(*) FROM messages WHERE conversation_id = $1",
                conversation_id,
            )
            check(
                "беседа наполнена двенадцатью сообщениями",
                in_database == 12,
                str(in_database),
            )

            # --- HIST-001: стык двух страниц ---------------------------------
            first = await page()
            second = await page(before_seq=8)
            third = await page(before_seq=3)
            check(
                "свежая страница идёт от новых к старым",
                first.ok and seqs(first.page) == [12, 11, 10, 9, 8],
                str(seqs(first.page) if first.ok else first.rejection),
            )
            check(
                "курсор продолжения — последний элемент страницы, а не первый",
                first.ok and first.page.has_more and first.next_before_seq == 8,
                f"next_before_seq={first.next_before_seq}",
            )
            check(
                "вторая страница стыкуется со первой без пропуска",
                second.ok and seqs(second.page) == [7, 6, 5, 4, 3],
                str(seqs(second.page) if second.ok else second.rejection),
            )
            check(
                "последняя страница объявляет конец истории",
                third.ok
                and seqs(third.page) == [2, 1]
                and not third.page.has_more
                and third.next_before_seq is None,
                f"{seqs(third.page) if third.ok else third.rejection} "
                f"has_more={third.page.has_more if third.ok else None}",
            )
            walked = (
                seqs(first.page) + seqs(second.page) + seqs(third.page)
                if first.ok and second.ok and third.ok
                else []
            )
            check(
                "HIST-001: объединение страниц — 1..12 ровно по одному разу",
                sorted(walked) == list(range(1, 13)) and len(set(walked)) == 12,
                str(walked),
            )

            # --- HIST-004: before_seq ровно на границе ------------------------
            on_boundary = await page(before_seq=7)
            above_boundary = await page(before_seq=8)
            check(
                "HIST-004: before_seq строго исключает собственный номер",
                7 not in seqs(on_boundary.page) and 7 in seqs(above_boundary.page),
                f"before_seq=7 → {seqs(on_boundary.page)}, "
                f"before_seq=8 → {seqs(above_boundary.page)}",
            )
            # Размер страницы здесь снят намеренно: утверждение — про
            # границу, а не про `limit`. С умолчанием `page()` (5) край
            # сверху отдал бы `[12, 11, 10, 9, 8]` — верный ответ на
            # другой вопрос, и падение выглядело бы дефектом выборки.
            below_one = await page(before_seq=1, limit=50)
            above_last = await page(before_seq=13, limit=50)
            check(
                "HIST-004: граница ведёт себя одинаково на краях",
                below_one.ok
                and seqs(below_one.page) == []
                and not below_one.page.has_more
                and above_last.ok
                and seqs(above_last.page) == list(range(12, 0, -1))
                and not above_last.page.has_more,
                f"before_seq=1 → {seqs(below_one.page)}, "
                f"before_seq=13 → {seqs(above_last.page)}",
            )

            # --- HIST-003: надгробие внутри страницы --------------------------
            # Номер 3 — последний элемент второй страницы, то есть ровно тот,
            # из которого берётся курсор продолжения. Если бы выборка
            # отфильтровала удалённые, страница стала бы короче, а курсор
            # уехал на четвёрку — и клиент потерял бы сообщение.
            await conn.execute(
                """
                UPDATE messages
                   SET deleted_at = now(), payload = NULL
                 WHERE conversation_id = $1 AND conversation_seq = 3
                """,
                conversation_id,
            )
            with_tombstone = await page(before_seq=8)
            tomb = next(
                (m for m in with_tombstone.page.items if m.conversation_seq == 3), None
            )
            check(
                "HIST-003: надгробие занимает своё место и не меняет размер страницы",
                with_tombstone.ok and seqs(with_tombstone.page) == [7, 6, 5, 4, 3],
                str(seqs(with_tombstone.page) if with_tombstone.ok else None),
            )
            check(
                "HIST-003: удалённое отдаётся надгробием, а не содержимым",
                tomb is not None
                and tomb.is_deleted
                and tomb.payload == MessagePayload()
                and tomb.visible_payload is None,
                str(tomb),
            )
            check(
                "HIST-003: курсор продолжения не съехал с удалённого",
                with_tombstone.next_before_seq == 3,
                f"next_before_seq={with_tombstone.next_before_seq}",
            )
            totals = await conn.fetchrow(
                """
                SELECT count(*) AS messages,
                       count(*) FILTER (WHERE deleted_at IS NOT NULL) AS tombstones
                  FROM messages
                 WHERE conversation_id = $1
                """,
                conversation_id,
            )
            check(
                "HIST-003: соседи не изменились — строка заменена, а не удалена",
                totals["messages"] == 12 and totals["tombstones"] == 1,
                str(dict(totals)),
            )

            # --- HIST-002: во время листания приходят новые -------------------
            loaded = seqs(with_tombstone.page)
            for number in range(13, 16):
                await write(author if number % 2 else peer, str(number))
            reloaded = await page(before_seq=8)
            check(
                "HIST-002: уже загруженная страница не сдвинулась от новых",
                reloaded.ok and seqs(reloaded.page) == loaded,
                f"было {loaded}, стало {seqs(reloaded.page) if reloaded.ok else None}",
            )
            fresh = await page()
            check(
                "HIST-002: новые сообщения видны только с головы истории",
                fresh.ok and seqs(fresh.page) == [15, 14, 13, 12, 11],
                str(seqs(fresh.page) if fresh.ok else fresh.rejection),
            )
            # Пройти историю заново с головы: прежнюю страницу переиспользовать
            # нельзя — она начинается с восьмого номера, а новая голова ушла
            # вперёд, и между ними лежат ещё не прочитанные 8, 9 и 10.
            middle = await page(before_seq=11)
            oldest = await page(before_seq=6)
            stitched = seqs(fresh.page) + seqs(middle.page) + seqs(oldest.page)
            check(
                "HIST-002: после дописи история по-прежнему без дублей и пропусков",
                sorted(stitched) == list(range(1, 16)) and len(set(stitched)) == 15,
                f"собрано {len(stitched)} номеров: {stitched}",
            )

            # --- Синхронизация вперёд: отдельная гарантия ---------------------
            head = await conn.fetchval(
                "SELECT last_seq FROM conversations WHERE conversation_id = $1",
                conversation_id,
            )
            sync = await page(after_seq=0)
            check(
                "догрузка вперёд идёт по возрастанию",
                sync.ok and seqs(sync.page) == [1, 2, 3, 4, 5],
                str(seqs(sync.page) if sync.ok else sync.rejection),
            )
            check(
                "sync_to_seq фиксирует голову беседы",
                sync.ok and sync.sync_to_seq == head == 15,
                f"sync_to_seq={sync.sync_to_seq if sync.ok else None}, голова={head}",
            )
            check(
                "курсор догрузки — последний элемент, и он же включён в снимок",
                sync.ok and sync.page.has_more and sync.next_after_seq == 5,
                f"next_after_seq={sync.next_after_seq if sync.ok else None}",
            )

            # Граница заморожена: дописанное после снимка в догрузку не входит.
            await write(author, "16")
            await write(peer, "17")
            tail = await page(after_seq=5, through_seq=15)
            check(
                "продолжение не выходит за замороженную границу",
                tail.ok and seqs(tail.page) == [6, 7, 8, 9, 10] and tail.sync_to_seq == 15,
                f"{seqs(tail.page) if tail.ok else tail.rejection} "
                f"sync_to_seq={tail.sync_to_seq if tail.ok else None}",
            )
            last = await page(after_seq=10, through_seq=15)
            check(
                "догрузка доходит до конца снимка и говорит об этом",
                last.ok
                and seqs(last.page) == [11, 12, 13, 14, 15]
                and not last.page.has_more
                and last.next_after_seq is None,
                f"{seqs(last.page) if last.ok else last.rejection} "
                f"has_more={last.page.has_more if last.ok else None}",
            )
            recovered = sorted(seqs(sync.page) + seqs(tail.page) + seqs(last.page))
            check(
                "догрузка собрала беседу ровно по одному разу и не выше снимка",
                recovered == list(range(1, 16)),
                str(recovered),
            )
            empty_snapshot = await page(after_seq=15, through_seq=15)
            check(
                "пустой снимок — законный ответ, а не ошибка",
                empty_snapshot.ok
                and seqs(empty_snapshot.page) == []
                and not empty_snapshot.page.has_more
                and empty_snapshot.next_after_seq is None
                and empty_snapshot.sync_to_seq == 15,
                str(empty_snapshot),
            )
            check(
                "sync_to_seq есть ровно там, где задан after_seq",
                first.sync_to_seq is None and with_tombstone.sync_to_seq is None,
                f"без курсоров={first.sync_to_seq}, "
                f"с before_seq={with_tombstone.sync_to_seq}",
            )

            # --- DB-001: своя запись видна сразу ------------------------------
            # Реплики в локальном кластере нет, поэтому проверяется вторая
            # половина гарантии: чтение свежей страницы тем маршрутом, который
            # выбрал маршрутизатор, действительно видит только что записанное.
            # Первая половина — решение маршрутизатора — закрыта юнит-тестом;
            # отставание настоящей реплики остаётся WAITING STAGE.
            mode = history_service.read_mode(before_seq=None, after_seq=None)
            check(
                "DB-001: свежая страница читается с писателя",
                mode is ReadMode.STRONG,
                str(mode),
            )
            just_written = await page()
            check(
                "DB-001: только что отправленное не исчезает",
                just_written.ok and 17 in seqs(just_written.page),
                str(seqs(just_written.page) if just_written.ok else just_written.rejection),
            )

            # --- Доступ -------------------------------------------------------
            denied = await page(viewer=outsider)
            check(
                "посторонний не получает историю чужой беседы",
                not denied.ok and denied.rejection is Reason.NOT_A_MEMBER,
                str(denied.rejection),
            )
            check(
                "отказ постороннему не выдаёт существование беседы",
                denied.visibility is Visibility.HIDDEN,
                str(denied.visibility),
            )
        finally:
            await outer.rollback()

    leftovers = await pool.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM users WHERE external_id LIKE $1) AS users,
            (SELECT count(*) FROM conversations WHERE conversation_id = $2) AS conversations,
            (SELECT count(*) FROM messages WHERE conversation_id = $2) AS messages
        """,
        f"history-check-{marker}-%",
        conversation_id,
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
    print("\nистория страницами подтверждена на живой беседе с надгробием")
    return 0


if __name__ == "__main__":
    sys.exit(main())
