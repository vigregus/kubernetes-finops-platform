"""Свежие данные для авторизации и долговечный аудит решений."""
from __future__ import annotations

import uuid

import asyncpg

from messenger.domain.authorization import Action, ResourceRef, Subject
from messenger.domain.errors import Reason


async def append_audit(
    conn: asyncpg.Connection,
    *,
    subject: Subject,
    resource: ResourceRef,
    action: Action,
    allowed: bool,
    reason: Reason | None,
) -> None:
    """Пишет след до возврата разрешения привилегированной операции."""
    await conn.execute(
        """
        INSERT INTO authorization_audit (
            audit_id, actor_user_id, actor_external_id,
            resource_kind, resource_id, action, allowed, reason
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        uuid.uuid4(),
        subject.user.user_id,
        subject.user.external_id,
        resource.kind.value,
        resource.identifier,
        action.value,
        allowed,
        reason.value if reason else None,
    )
