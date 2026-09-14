"""Правила сессии, не требующие ни базы, ни Keycloak."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from messenger.domain.ids import DeviceId, SessionId, UserId
from messenger.domain.session import RevocationReason, Session

СЕЙЧАС = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def _session(**overrides) -> Session:
    base = {
        "session_id": SessionId(uuid.uuid4()),
        "user_id": UserId(uuid.uuid4()),
        "device_id": DeviceId(uuid.uuid4()),
        "created_at": СЕЙЧАС - timedelta(days=1),
        "expires_at": СЕЙЧАС + timedelta(days=6),
    }
    return Session(**{**base, **overrides})


def test_действующая_сессия_жива():
    assert _session().is_live_at(СЕЙЧАС) is True


def test_отозванная_мертва_независимо_от_срока():
    """Отзыв обязан действовать сразу.

    Ждать истечения токена нельзя: «выйти на всех устройствах» тогда
    ничего не гарантирует ближайшие минуты.
    """
    отозванная = _session(
        revoked_at=СЕЙЧАС - timedelta(minutes=1),
        revoked_reason=RevocationReason.LOGOUT_ALL.value,
    )
    assert отозванная.is_revoked is True
    assert отозванная.is_live_at(СЕЙЧАС) is False


def test_истёкшая_мертва_даже_без_отзыва():
    """Проверять только `revoked_at` значит принимать вход, срок которого
    вышел час назад, если из него никто не выходил."""
    истёкшая = _session(expires_at=СЕЙЧАС - timedelta(hours=1))
    assert истёкшая.is_revoked is False
    assert истёкшая.is_live_at(СЕЙЧАС) is False


def test_причина_отзыва_сохраняется_как_строка():
    """Причина идёт в аудит: «вышел сам» и «отключён администратором»
    расследуются по-разному."""
    assert RevocationReason.ADMIN_DISABLE.value == "admin_disable"
    assert RevocationReason.EXPIRED.value == "expired"


def test_идентификатор_сессии_выводится_из_непрозрачного_sid():
    """`sid` у Keycloak 26 — не UUID.

    Проверено на живом сервере: там короткая строка вроде
    `cyt4SLGLVN3jRXGVyhAs5aAG`, и спецификация OIDC так и говорит.
    Предположение «там UUID» держалось ровно до первого настоящего входа
    и падало с `badly formed hexadecimal UUID string`.
    """
    from messenger.domain.session import session_id_from_external

    внешний = "cyt4SLGLVN3jRXGVyhAs5aAG"
    первый = session_id_from_external(внешний)
    assert session_id_from_external(внешний) == первый, "отображение обязано быть стабильным"
    assert session_id_from_external("другой-sid") != первый
    assert isinstance(первый, uuid.UUID)


def test_отображение_не_зависит_от_процесса():
    """Значение зашито, а не берётся случайно при старте.

    Другое пространство имён означало бы, что все существующие сессии
    перестают находиться, то есть разовый выход у всех.
    """
    from messenger.domain.session import session_id_from_external

    assert str(session_id_from_external("cyt4SLGLVN3jRXGVyhAs5aAG")) == (
        str(uuid.uuid5(uuid.UUID("6f2b4b1e-5a0f-4c52-9b7a-0f4a1e8d3c77"),
                       "cyt4SLGLVN3jRXGVyhAs5aAG"))
    )
