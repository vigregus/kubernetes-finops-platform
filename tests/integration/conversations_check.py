"""G1-009: модели беседы и членства на настоящем Postgres через PgBouncer.

Сценарий создания диалога и встречная гонка принадлежат G1-010/G1-011.
Здесь проверяется нижний слой: обе формы беседы, роли, действующее и бывшее
членство, выборки и ограничения уже существующей схемы.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import asyncpg

from messenger.domain.conversation import ConversationType, MemberRole
from messenger.domain.ids import ConversationId, direct_key
from messenger.repositories import conversations, users
from messenger.repositories.postgres import PoolSettings, create_pool

# Список бесед читается только страницами — беспагинационной выдачи у него
# нет. Здесь нужна одна страница, заведомо покрывающая коллекцию проверки:
# её предмет — отбор по членству, а не листание.
PAGE = 100

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
    pool = await create_pool(settings(), application_name="messenger-integration")
    marker = uuid.uuid4().hex
    direct_id = ConversationId(uuid.uuid4())
    group_id = ConversationId(uuid.uuid4())

    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            profiles = []
            for number in range(3):
                result = await users.ensure_user(
                    conn,
                    external_id=f"conversation-check-{marker}-{number}",
                    display_name=f"Участник {number}",
                    email=f"conversation-{marker}-{number}@example.org",
                    email_verified=True,
                )
                profiles.append(result.user)
            first, second, third = profiles

            pair = direct_key(first.user_id, second.user_id)
            direct = await conversations.insert_conversation(
                conn,
                conversation_id=direct_id,
                type=ConversationType.DIRECT,
                direct_key=pair,
            )
            check(
                "прямая беседа записана с ключом упорядоченной пары",
                direct.is_direct and direct.direct_key == pair and direct.last_seq == 0,
                str(direct),
            )

            group = await conversations.insert_conversation(
                conn,
                conversation_id=group_id,
                type=ConversationType.GROUP,
            )
            check(
                "группа записана без ключа пары",
                not group.is_direct and group.direct_key is None,
                str(group),
            )

            first_member = await conversations.add_member(
                conn,
                conversation_id=direct_id,
                user_id=first.user_id,
                role=MemberRole.ADMIN,
            )
            second_member = await conversations.add_member(
                conn, conversation_id=direct_id, user_id=second.user_id
            )
            await conversations.add_member(
                conn, conversation_id=group_id, user_id=third.user_id
            )
            check(
                "роль и действующее членство возвращены типизированно",
                first_member.role is MemberRole.ADMIN
                and first_member.is_active
                and second_member.role is MemberRole.MEMBER,
            )

            found = await conversations.fetch_conversation(
                conn, conversation_id=direct_id
            )
            check(
                "беседа читается по идентификатору",
                found is not None and found.conversation_id == direct_id,
            )
            missing = await conversations.fetch_conversation(
                conn, conversation_id=ConversationId(uuid.uuid4())
            )
            check("неизвестная беседа не подменяется пустой моделью", missing is None)

            active = await conversations.list_active_members(
                conn, conversation_id=direct_id
            )
            check(
                "список действующих участников содержит обоих",
                {member.user_id for member in active}
                == {first.user_id, second.user_id},
                str(active),
            )

            own = await conversations.list_user_conversations(
                conn, user_id=first.user_id, cursor=None, limit=PAGE
            )
            check(
                "пользователь видит только беседы со своим членством",
                [item.conversation.conversation_id for item in own.items]
                == [direct_id],
                str(own),
            )

            nested = conn.transaction()
            await nested.start()
            try:
                await conversations.add_member(
                    conn, conversation_id=direct_id, user_id=first.user_id
                )
            except asyncpg.UniqueViolationError:
                await nested.rollback()
                check("повторное членство отклонено базой", True)
            else:
                await nested.rollback()
                check("повторное членство отклонено базой", False)

            await conn.execute(
                """
                UPDATE conversation_members
                   SET left_at = now()
                 WHERE conversation_id = $1 AND user_id = $2
                """,
                direct_id,
                second.user_id,
            )
            former = await conversations.fetch_member(
                conn, conversation_id=direct_id, user_id=second.user_id
            )
            check(
                "бывший участник сохраняется и отличим от отсутствующего",
                former is not None and not former.is_active,
                str(former),
            )
            absent = await conversations.fetch_member(
                conn, conversation_id=direct_id, user_id=third.user_id
            )
            check("посторонний не превращается в бывшего участника", absent is None)

            active_after = await conversations.list_active_members(
                conn, conversation_id=direct_id
            )
            check(
                "бывший участник исключён из действующего состава",
                [member.user_id for member in active_after] == [first.user_id],
                str(active_after),
            )

            await conn.execute(
                """
                UPDATE conversation_members
                   SET left_at = now()
                 WHERE conversation_id = $1 AND user_id = $2
                """,
                direct_id,
                first.user_id,
            )
            no_longer_own = await conversations.list_user_conversations(
                conn, user_id=first.user_id, cursor=None, limit=PAGE
            )
            check(
                "бывшее членство не показывает беседу как действующую",
                not no_longer_own.items,
                str(no_longer_own),
            )
        finally:
            await tx.rollback()

    left = await pool.fetchval(
        "SELECT count(*) FROM users WHERE external_id LIKE $1",
        f"conversation-check-{marker}-%",
    )
    check("после отката в базе не осталось следов", left == 0, f"строк: {left}")
    conversations_left = await pool.fetchval(
        "SELECT count(*) FROM conversations WHERE conversation_id = ANY($1::uuid[])",
        [direct_id, group_id],
    )
    check(
        "после отката не осталось бесед",
        conversations_left == 0,
        f"строк: {conversations_left}",
    )
    await pool.close()


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nбеседы и членство подтверждены на живой базе")
    return 0


if __name__ == "__main__":
    sys.exit(main())
