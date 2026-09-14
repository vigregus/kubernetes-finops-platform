"""Отправка письма подтверждения и лимит повторов."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from messenger.adapters.ratelimit import LimitDecision, OnFailure
from messenger.domain.ids import UserId
from messenger.domain.user import User
from messenger.services import verification


def user(*, verified: bool = False) -> User:
    now = datetime.now(UTC)
    return User(
        user_id=UserId(uuid.uuid4()),
        external_id="keycloak-user",
        display_name="Аня",
        email="anya@example.org",
        email_verified=verified,
        created_at=now,
        updated_at=now,
    )


class Limiter:
    def __init__(self, decision: LimitDecision):
        self.decision = decision
        self.calls = []

    async def take(self, key, **kwargs):
        self.calls.append((key, kwargs))
        return self.decision


class Admin:
    def __init__(self, sent: bool = True):
        self.sent = sent
        self.users = []

    async def send_verify_email(self, *, external_user_id: str):
        self.users.append(external_user_id)
        return self.sent


def test_первое_письмо_не_съедает_лимит_повторов():
    admin = Admin()
    assert asyncio.run(verification.send_initial_verification(user=user(), admin=admin))
    assert admin.users == ["keycloak-user"]


def test_подтверждённому_первое_письмо_не_отправляется():
    admin = Admin()
    assert asyncio.run(
        verification.send_initial_verification(user=user(verified=True), admin=admin)
    )
    assert admin.users == []


def test_повтор_идёт_через_лимит_и_keycloak():
    limiter = Limiter(LimitDecision(allowed=True))
    admin = Admin()
    result = asyncio.run(
        verification.resend_verification(user=user(), limiter=limiter, admin=admin)
    )
    assert result.sent
    assert admin.users == ["keycloak-user"]
    _, kwargs = limiter.calls[0]
    assert kwargs["on_failure"] is OnFailure.DENY


def test_лимит_не_пускает_в_keycloak_и_возвращает_retry_after():
    limiter = Limiter(LimitDecision(allowed=False, retry_after_seconds=91))
    admin = Admin()
    result = asyncio.run(
        verification.resend_verification(user=user(), limiter=limiter, admin=admin)
    )
    assert result.limited and result.retry_after_seconds == 91
    assert admin.users == []


def test_отказ_keycloak_отличается_от_лимита():
    result = asyncio.run(
        verification.resend_verification(
            user=user(), limiter=Limiter(LimitDecision(allowed=True)), admin=Admin(False)
        )
    )
    assert result.upstream_failed and not result.limited


def test_подтверждённый_адрес_не_тратит_счётчик():
    limiter = Limiter(LimitDecision(allowed=True))
    admin = Admin()
    result = asyncio.run(
        verification.resend_verification(
            user=user(verified=True), limiter=limiter, admin=admin
        )
    )
    assert result.already_verified
    assert limiter.calls == [] and admin.users == []
