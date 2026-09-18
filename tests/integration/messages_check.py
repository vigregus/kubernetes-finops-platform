"""G2-002: сообщение и два события outbox на настоящем Postgres."""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

from messenger.domain.conversation import ConversationType
from messenger.domain.errors import Reason
from messenger.domain.ids import ClientMessageId, ConversationId
from messenger.domain.message import MessageKind, MessagePayload
from messenger.repositories import conversations, outbox, users
from messenger.repositories.postgres import PoolSettings, create_pool
from messenger.services import messages as service

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


async def run() -> None:
    pool = await create_pool(settings(), application_name="message-integration")
    marker = uuid.uuid4().hex
    conversation_id = ConversationId(uuid.uuid4())

    async with pool.acquire() as conn:
        outer = conn.transaction()
        await outer.start()
        try:
            sender = (
                await users.ensure_user(
                    conn,
                    external_id=f"message-check-{marker}-sender",
                    display_name="Отправитель",
                    email=f"message-{marker}-sender@example.org",
                    email_verified=True,
                )
            ).user
            recipient = (
                await users.ensure_user(
                    conn,
                    external_id=f"message-check-{marker}-recipient",
                    display_name="Получатель",
                    email=f"message-{marker}-recipient@example.org",
                    email_verified=True,
                )
            ).user
            outsider = (
                await users.ensure_user(
                    conn,
                    external_id=f"message-check-{marker}-outsider",
                    display_name="Посторонний",
                    email=f"message-{marker}-outsider@example.org",
                    email_verified=True,
                )
            ).user
            await conversations.insert_conversation(
                conn,
                conversation_id=conversation_id,
                type=ConversationType.DIRECT,
                direct_key=f"message-check:{marker}",
            )
            await conversations.add_member(
                conn, conversation_id=conversation_id, user_id=sender.user_id
            )
            await conversations.add_member(
                conn, conversation_id=conversation_id, user_id=recipient.user_id
            )

            client_id = ClientMessageId(uuid.uuid4())
            first = await service.send_message(
                conn,
                sender_id=sender.user_id,
                conversation_id=conversation_id,
                client_message_id=client_id,
                kind=MessageKind.TEXT,
                payload=MessagePayload(text="привет"),
                trace_id=f"trace-{marker}",
            )
            check(
                "первое сообщение получило серверный seq=1",
                first.ok
                and first.created
                and first.message is not None
                and first.message.conversation_seq == 1,
                str(first),
            )
            counts = await conn.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM messages WHERE conversation_id = $1) AS messages,
                    (SELECT count(*) FROM outbox WHERE aggregate_id = $2) AS events
                """,
                conversation_id,
                first.message.message_id,
            )
            check(
                "сообщение и два события outbox записаны одним вызовом",
                counts["messages"] == 1 and counts["events"] == 2,
                str(dict(counts)),
            )
            event_types = await conn.fetch(
                """
                SELECT event_type
                  FROM outbox
                 WHERE aggregate_id = $1
                 ORDER BY id
                """,
                first.message.message_id,
            )
            check(
                "outbox разделяет факт и содержимое",
                [row["event_type"] for row in event_types]
                == ["message.created", "message.content"],
                str(event_types),
            )

            repeated = await service.send_message(
                conn,
                sender_id=sender.user_id,
                conversation_id=conversation_id,
                client_message_id=client_id,
                kind=MessageKind.TEXT,
                payload=MessagePayload(text="привет"),
            )
            last_seq = await conn.fetchval(
                "SELECT last_seq FROM conversations WHERE conversation_id = $1",
                conversation_id,
            )
            check(
                "повтор вернул то же сообщение и не потратил новый seq",
                repeated.ok
                and not repeated.created
                and repeated.message is not None
                and repeated.message.message_id == first.message.message_id
                and last_seq == 1,
            )

            denied = await service.send_message(
                conn,
                sender_id=outsider.user_id,
                conversation_id=conversation_id,
                client_message_id=ClientMessageId(uuid.uuid4()),
                kind=MessageKind.TEXT,
                payload=MessagePayload(text="чужое"),
            )
            check(
                "посторонний не получил номер в беседе",
                denied.rejection is Reason.NOT_A_MEMBER,
            )

            original_insert = outbox.insert_event
            calls = 0

            async def fail_second_event(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("инъекция отказа второго outbox")
                return await original_insert(*args, **kwargs)

            service.outbox.insert_event = fail_second_event
            try:
                try:
                    await service.send_message(
                        conn,
                        sender_id=sender.user_id,
                        conversation_id=conversation_id,
                        client_message_id=ClientMessageId(uuid.uuid4()),
                        kind=MessageKind.TEXT,
                        payload=MessagePayload(text="должно откатиться"),
                    )
                except RuntimeError as exc:
                    failed_as_expected = "инъекция" in str(exc)
                else:
                    failed_as_expected = False
            finally:
                service.outbox.insert_event = original_insert

            after_failure = await conn.fetchrow(
                """
                SELECT
                    (SELECT last_seq FROM conversations WHERE conversation_id = $1) AS seq,
                    (SELECT count(*) FROM messages WHERE conversation_id = $1) AS messages,
                    (SELECT count(*) FROM outbox WHERE partition_key = $2) AS events
                """,
                conversation_id,
                str(conversation_id),
            )
            check("инъекция отказа действительно сработала", failed_as_expected)
            check(
                "TX-001: отказ outbox откатил сообщение, событие и seq",
                after_failure["seq"] == 1
                and after_failure["messages"] == 1
                and after_failure["events"] == 2,
                str(dict(after_failure)),
            )
        finally:
            await outer.rollback()

    leftovers = await pool.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM users WHERE external_id LIKE $1) AS users,
            (SELECT count(*) FROM conversations WHERE conversation_id = $2) AS conversations,
            (SELECT count(*) FROM messages WHERE conversation_id = $2) AS messages,
            (SELECT count(*) FROM outbox WHERE partition_key = $3) AS events
        """,
        f"message-check-{marker}-%",
        conversation_id,
        str(conversation_id),
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
    print("\nатомарная запись сообщения и outbox подтверждена на живой базе")
    return 0


if __name__ == "__main__":
    sys.exit(main())
