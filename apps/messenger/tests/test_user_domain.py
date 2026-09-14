"""Доменные правила пользователя. Без базы, без сети, без приложения.

Ровно то, ради чего `domain` не импортирует ни `asyncpg`, ни `fastapi`:
правила проверяются за миллисекунды и не зависят от того, поднят ли кластер.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from messenger.domain.ids import UserId
from messenger.domain.user import (
    DISPLAY_NAME_MAX,
    FALLBACK_DISPLAY_NAME,
    User,
    normalize_display_name,
    normalize_email,
)


def _user(**overrides) -> User:
    now = datetime.now(UTC)
    base = {
        "user_id": UserId(uuid.uuid4()),
        "external_id": "kc-1",
        "display_name": "Аня",
        "email": "a@example.org",
        "email_verified": True,
        "created_at": now,
        "updated_at": now,
    }
    return User(**{**base, **overrides})


def test_адрес_приводится_к_нижнему_регистру():
    """Уникальность в базе держится индексом по lower(email).

    Нормализовать в одном месте и не нормализовать в другом — значит завести
    вторую учётную запись на `A@x.ru` рядом с `a@x.ru`, и владелец обеих
    не поймёт, куда делась переписка.
    """
    assert normalize_email("  Anya@Example.ORG ") == "anya@example.org"


def test_пустой_адрес_это_отсутствие_адреса():
    """Keycloak присылает пустую строку для учётной записи без почты.

    Такой «адрес» попал бы в частичный уникальный индекс и занял бы место
    настоящего.
    """
    assert normalize_email("") is None
    assert normalize_email("   ") is None
    assert normalize_email(None) is None


def test_имя_обрезается_а_не_отклоняется():
    """Длинное имя не должно делать вход невозможным.

    Ограничение на стороне базы отклонило бы вставку целиком, то есть
    человек с длинным именем в чужом каталоге просто не смог бы войти.
    """
    name = normalize_display_name("я" * (DISPLAY_NAME_MAX + 50))
    assert len(name) == DISPLAY_NAME_MAX


def test_пустое_имя_заменяется_запасным():
    """Пустое имя в списке бесед выглядит как ошибка загрузки."""
    assert normalize_display_name("   ") == FALLBACK_DISPLAY_NAME
    assert normalize_display_name(None) == FALLBACK_DISPLAY_NAME


def test_удалённый_пользователь_отличим_от_живого():
    """Надгробие остаётся строкой в базе — «нашлось» не значит «здесь».

    На пользователя ссылаются сообщения, и физическое удаление обрушило бы
    историю чужих бесед. Поэтому проверять приходится явно.
    """
    assert _user().is_deleted is False
    assert _user(deleted_at=datetime.now(UTC)).is_deleted is True


def test_профиль_неизменяем():
    """Изменяемый объект, переживший запрос, однажды окажется в кеше
    и начнёт отдавать состояние, которого в базе уже нет."""
    user = _user()
    try:
        user.display_name = "другое"  # type: ignore[misc]
    except (AttributeError, TypeError):
        return
    raise AssertionError("профиль оказался изменяемым")


def test_до_подтверждения_адреса_нельзя_начинать_беседы():
    """AUTH-006: до подтверждения функции ограничены.

    Ограничение бьёт по рассылке незнакомым: учётная запись с выдуманным
    адресом заводится за секунду. Чтение и ответ в уже существующей беседе
    остаются — иначе ограничение бьёт по тому, кого позвали, а не по тому,
    кто рассылает.
    """
    from messenger.domain.user import Capability, can

    неподтверждённый = _user(email_verified=False)
    assert can(неподтверждённый, Capability.READ)
    assert can(неподтверждённый, Capability.SEND_MESSAGE)
    assert not can(неподтверждённый, Capability.START_CONVERSATION)


def test_после_подтверждения_доступно_всё():
    from messenger.domain.user import Capability, can

    подтверждённый = _user(email_verified=True)
    assert all(can(подтверждённый, c) for c in Capability)


def test_надгробие_не_может_ничего():
    """Строка в базе есть, человека нет."""
    from datetime import UTC, datetime

    from messenger.domain.user import capabilities_of

    assert capabilities_of(_user(deleted_at=datetime.now(UTC))) == frozenset()
