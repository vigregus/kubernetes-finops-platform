"""AUTHZ-003…005 на живой базе: свежесть, сокрытие и аудит."""
from __future__ import annotations

import asyncio
import secrets
import sys
import uuid

import httpx

from login_check import _cleanup, _pool, admin_token, create_user
from messenger.domain.authorization import Action, ResourceRef, Subject
from messenger.domain.conversation import ConversationMember, MemberRole
from messenger.domain.errors import to_problem
from messenger.domain.ids import ConversationId, UserId
from messenger.repositories import users
from messenger.services import authorization

failures: list[str] = []


def check(what: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  \033[32m✓\033[0m {what}")
        return
    print(f"  \033[31m✗\033[0m {what}")
    if detail:
        print(f"      {detail}")
    failures.append(what)


class StaleMembershipCache:
    def __init__(self, member: ConversationMember) -> None:
        self.member = member
        self.reads = 0

    async def get(self, **kwargs) -> ConversationMember:
        self.reads += 1
        return self.member


async def run() -> None:
    marker = uuid.uuid4().hex[:12]
    accounts = [
        (f"authz-{marker}-{number}@example.org", secrets.token_urlsafe(24))
        for number in range(2)
    ]
    external_ids: list[str] = []
    conversation_id = ConversationId(uuid.uuid4())
    audit_resource = f"disable-user-{marker}"

    async with httpx.AsyncClient(timeout=15.0) as http:
        admin = await admin_token(http)
        try:
            for login, password in accounts:
                external_ids.append(
                    await create_user(
                        http, admin, login=login, password=password, email_verified=True
                    )
                )
            pool = await _pool()
            try:
                actor = await users.ensure_user(
                    pool,
                    external_id=external_ids[0],
                    display_name="Автор",
                    email=accounts[0][0],
                    email_verified=True,
                )
                outsider = await users.ensure_user(
                    pool,
                    external_id=external_ids[1],
                    display_name="Посторонний",
                    email=accounts[1][0],
                    email_verified=True,
                )
                await pool.execute(
                    "INSERT INTO conversations (conversation_id, type) VALUES ($1, 'group')",
                    conversation_id,
                )
                member_row = await pool.fetchrow(
                    """
                    INSERT INTO conversation_members (conversation_id, user_id)
                    VALUES ($1, $2)
                    RETURNING conversation_id, user_id, role, joined_at, left_at
                    """,
                    conversation_id,
                    actor.user.user_id,
                )
                cached = ConversationMember(
                    conversation_id=ConversationId(member_row["conversation_id"]),
                    user_id=UserId(member_row["user_id"]),
                    role=MemberRole(member_row["role"]),
                    joined_at=member_row["joined_at"],
                    left_at=member_row["left_at"],
                )
                cache = StaleMembershipCache(cached)
                await pool.execute(
                    """
                    UPDATE conversation_members SET left_at = now()
                     WHERE conversation_id = $1 AND user_id = $2
                    """,
                    conversation_id,
                    actor.user.user_id,
                )
                write = await authorization.authorize(
                    pool,
                    subject=Subject(actor.user),
                    resource=ResourceRef.conversation(conversation_id),
                    action=Action.WRITE_CONVERSATION,
                    membership_cache=cache,
                )
                check(
                    "устаревшее разрешение кеша не расширило право записи",
                    not write.allowed and cache.reads == 0,
                )

                foreign = await authorization.authorize(
                    pool,
                    subject=Subject(outsider.user),
                    resource=ResourceRef.conversation(conversation_id),
                    action=Action.READ_CONVERSATION,
                )
                missing = await authorization.authorize(
                    pool,
                    subject=Subject(outsider.user),
                    resource=ResourceRef.conversation(ConversationId(uuid.uuid4())),
                    action=Action.READ_CONVERSATION,
                )
                check(
                    "чужая и несуществующая беседы дают одинаковый внешний отказ",
                    not foreign.allowed
                    and not missing.allowed
                    and to_problem(foreign.reason) == to_problem(missing.reason),
                )

                admin_decision = await authorization.authorize(
                    pool,
                    subject=Subject(
                        actor.user,
                        roles=frozenset({"admin"}),
                        second_factor_verified=True,
                    ),
                    resource=ResourceRef.admin_action(audit_resource),
                    action=Action.ADMIN_EXECUTE,
                )
                audit = await pool.fetchrow(
                    """
                    SELECT allowed, action, resource_id
                      FROM authorization_audit
                     WHERE resource_id = $1
                    """,
                    audit_resource,
                )
                check(
                    "административное разрешение оставило долговечный аудит",
                    admin_decision.allowed
                    and audit is not None
                    and audit["allowed"] is True
                    and audit["action"] == Action.ADMIN_EXECUTE.value,
                )
            finally:
                await pool.execute(
                    "DELETE FROM authorization_audit WHERE resource_id = $1",
                    audit_resource,
                )
                await pool.execute(
                    "DELETE FROM conversation_members WHERE conversation_id = $1",
                    conversation_id,
                )
                await pool.execute(
                    "DELETE FROM conversations WHERE conversation_id = $1",
                    conversation_id,
                )
                await pool.close()
        finally:
            for external_id in external_ids:
                await _cleanup(http, admin, external_id)


def main() -> int:
    asyncio.run(run())
    if failures:
        print(f"\nне выполнено проверок: {len(failures)}")
        return 1
    print("\nединая авторизация подтверждена на живой базе")
    return 0


if __name__ == "__main__":
    sys.exit(main())
