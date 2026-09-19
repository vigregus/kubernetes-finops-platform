"""HTTP-контракт создания беседы один-на-один."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from messenger.api import main
from messenger.api.main import app
from messenger.domain.conversation import Conversation, ConversationType
from messenger.domain.conversation_list import (
    ConversationPage,
    ConversationSummary,
)
from messenger.domain.errors import Reason
from messenger.domain.identity import TokenRejection
from messenger.domain.ids import (
    ClientMessageId,
    ConversationId,
    ConversationSeq,
    MessageId,
    UserId,
)
from messenger.domain.message import Message, MessageKind, MessagePayload
from messenger.domain.user import User, UserSummary
from messenger.services import conversations as service
from messenger.services import identity

ACTOR_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
OTHER_ID = UserId(uuid.UUID("22222222-2222-2222-2222-222222222222"))
CONVERSATION_ID = ConversationId(uuid.UUID("33333333-3333-3333-3333-333333333333"))
NOW = datetime(2026, 9, 15, tzinfo=UTC)


class Runtime:
    keys = None
    oidc_settings = None

    def __init__(self) -> None:
        self.opened = 0

    @asynccontextmanager
    async def connection(self, mode=None):
        # `mode` принимается и не используется: настоящий `Runtime` по нему
        # выбирает между писателем и репликой, а подмене выбирать нечего —
        # она считает открытия и отдаёт `None` вместо соединения.
        self.opened += 1
        yield None


def _user(user_id: UserId, name: str) -> User:
    return User(
        user_id=user_id,
        external_id=f"kc-{user_id}",
        display_name=name,
        email=f"{user_id}@example.org",
        email_verified=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _success(*, created: bool) -> service.CreateDirectResult:
    return service.CreateDirectResult(
        conversation=Conversation(
            conversation_id=CONVERSATION_ID,
            type=ConversationType.DIRECT,
            direct_key="a:b",
            last_seq=ConversationSeq(0),
            created_at=NOW,
            updated_at=NOW,
        ),
        participants=(_user(ACTOR_ID, "Аня"), _user(OTHER_ID, "Борис")),
        created=created,
    )


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def runtime():
    original = app.state.runtime
    stub = Runtime()
    app.state.runtime = stub
    # Возвращается, а не только ставится: по числу открытий проверяется,
    # что негодная строка запроса не занимает соединение.
    yield stub
    app.state.runtime = original


def authenticated(monkeypatch):
    async def _current(*args, **kwargs):
        return identity.AuthResult(user=_user(ACTOR_ID, "Аня"))

    monkeypatch.setattr(main, "_current", _current)


def test_создание_возвращает_201_и_участников(client, monkeypatch):
    authenticated(monkeypatch)

    async def _create(*args, **kwargs):
        assert kwargs["participant_id"] == OTHER_ID
        return _success(created=True)

    monkeypatch.setattr(service, "create_direct", _create)
    response = client.post(
        "/conversations",
        json={"participant_id": str(OTHER_ID)},
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 201
    assert response.json() == {
        "conversation_id": str(CONVERSATION_ID),
        "type": "direct",
        "participants": [
            {"user_id": str(ACTOR_ID), "display_name": "Аня"},
            {"user_id": str(OTHER_ID), "display_name": "Борис"},
        ],
        "created_at": "2026-09-15T00:00:00Z",
    }


def test_повтор_возвращает_200(client, monkeypatch):
    authenticated(monkeypatch)

    async def _create(*args, **kwargs):
        return _success(created=False)

    monkeypatch.setattr(service, "create_direct", _create)
    response = client.post(
        "/conversations",
        json={"participant_id": str(OTHER_ID)},
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 200
    assert response.json()["conversation_id"] == str(CONVERSATION_ID)


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [
        (Reason.BLOCKED, 403, "forbidden"),
        (Reason.EMAIL_UNVERIFIED, 403, "forbidden"),
        (Reason.SELF_CONVERSATION, 403, "forbidden"),
        (Reason.USER_NOT_FOUND, 404, "resource_not_found"),
    ],
)
def test_ожидаемые_отказы_переводятся_в_контракт(
    client, monkeypatch, reason, status, code, отказ
):
    authenticated(monkeypatch)

    async def _create(*args, **kwargs):
        return service.CreateDirectResult(rejection=reason)

    monkeypatch.setattr(service, "create_direct", _create)
    response = client.post(
        "/conversations",
        json={"participant_id": str(OTHER_ID)},
        headers={"Authorization": "Bearer token"},
    )
    отказ(response, status=status, code=code)


def test_без_токена_сервис_не_вызывается(client, monkeypatch, отказ):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("создание началось без удостоверения")

    monkeypatch.setattr(service, "create_direct", _не_вызывать)
    response = client.post("/conversations", json={"participant_id": str(OTHER_ID)})
    отказ(response, status=401, code="unauthenticated")


def test_недоступные_ключи_дают_503(client, monkeypatch, отказ):
    async def _current(*args, **kwargs):
        return identity.AuthResult(rejection=TokenRejection.KEYS_UNAVAILABLE)

    monkeypatch.setattr(main, "_current", _current)
    response = client.post(
        "/conversations",
        json={"participant_id": str(OTHER_ID)},
        headers={"Authorization": "Bearer token"},
    )
    отказ(response, status=503, code="upstream_unavailable")


def test_не_uuid_отклоняется_до_сервиса(client, monkeypatch):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("сервис получил невалидный UUID")

    monkeypatch.setattr(service, "create_direct", _не_вызывать)
    response = client.post(
        "/conversations",
        json={"participant_id": "not-a-uuid"},
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 422


# --- GET /conversations ----------------------------------------------------

SECOND_CONVERSATION_ID = ConversationId(
    uuid.UUID("44444444-4444-4444-4444-444444444444")
)
MESSAGE_ID = MessageId(uuid.UUID("55555555-5555-5555-5555-555555555555"))
CLIENT_MESSAGE_ID = ClientMessageId(uuid.UUID("66666666-6666-6666-6666-666666666666"))


def _summary(conversation_id: ConversationId, *, updated_at: datetime = NOW):
    return ConversationSummary(
        conversation=Conversation(
            conversation_id=conversation_id,
            type=ConversationType.DIRECT,
            direct_key="a:b",
            last_seq=ConversationSeq(0),
            created_at=updated_at,
            updated_at=updated_at,
        ),
        participants=(UserSummary(user_id=ACTOR_ID, display_name="Аня"),),
    )


def _message(conversation_id: ConversationId) -> Message:
    return Message(
        message_id=MESSAGE_ID,
        conversation_id=conversation_id,
        conversation_seq=ConversationSeq(1),
        sender_id=ACTOR_ID,
        client_message_id=CLIENT_MESSAGE_ID,
        kind=MessageKind.TEXT,
        payload=MessagePayload(text="привет"),
        created_at=NOW,
    )


def _listed(page: ConversationPage) -> service.ConversationListResult:
    return service.ConversationListResult(page=page)


def _список(monkeypatch, page: ConversationPage) -> None:
    """Подменяет сервис и требует удостоверения — как остальные маршруты."""

    async def _list(*args, **kwargs):
        return _listed(page)

    monkeypatch.setattr(main.conversation_service, "list_conversations", _list)


def test_список_отдаёт_беседу_без_сообщения(client, monkeypatch):
    authenticated(monkeypatch)
    _список(monkeypatch, ConversationPage(items=(_summary(CONVERSATION_ID),)))

    response = client.get(
        "/conversations", headers={"Authorization": "Bearer token"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == [
        {
            "conversation_id": str(CONVERSATION_ID),
            "type": "direct",
            "participants": [{"user_id": str(ACTOR_ID), "display_name": "Аня"}],
            "created_at": "2026-09-15T00:00:00Z",
        }
    ]
    # Ключа нет вовсе — не «есть, но null»: у беседы без сообщений
    # последнего не существует, а контракт объявляет поле как `Message`.
    assert "last_message" not in body["items"][0]
    # И это тоже утверждение, а не забывчивость: проекция непрочитанных —
    # отдельная работа, и поля нет, пока её нет.
    assert "unread_count" not in body["items"][0]


def test_конец_списка_это_null_в_null(client, monkeypatch):
    authenticated(monkeypatch)
    _список(monkeypatch, ConversationPage(items=(_summary(CONVERSATION_ID),)))

    body = client.get(
        "/conversations", headers={"Authorization": "Bearer token"}
    ).json()
    # Оба поля присутствуют всегда: клиент различает «продолжения нет»
    # и «поля нет» только по наличию ключа.
    assert body["next_before_activity_at"] is None
    assert body["next_before_conversation_id"] is None


def test_последнее_сообщение_отдаётся_полным_телом(client, monkeypatch):
    authenticated(monkeypatch)
    message = _message(CONVERSATION_ID)
    _список(
        monkeypatch,
        ConversationPage(
            items=(
                ConversationSummary(
                    conversation=_summary(CONVERSATION_ID).conversation,
                    participants=(UserSummary(user_id=ACTOR_ID, display_name="Аня"),),
                    last_message=message,
                ),
            )
        ),
    )

    body = client.get(
        "/conversations", headers={"Authorization": "Bearer token"}
    ).json()
    assert body["items"][0]["last_message"] == {
        "message_id": str(MESSAGE_ID),
        "conversation_id": str(CONVERSATION_ID),
        "seq": 1,
        "sender_id": str(ACTOR_ID),
        "client_message_id": str(CLIENT_MESSAGE_ID),
        "type": "text",
        "payload": {"text": "привет"},
        "created_at": "2026-09-15T00:00:00Z",
        "edited_at": None,
        "deleted_at": None,
    }


def test_продолжение_отдаётся_парой_а_не_одним_временем(client, monkeypatch):
    authenticated(monkeypatch)
    # Отметки равны: у бесед одной транзакции `now()` совпадает
    # до микросекунды, и одиночный курсор потерял бы вторую на стыке.
    _список(
        monkeypatch,
        ConversationPage(
            items=(_summary(CONVERSATION_ID), _summary(SECOND_CONVERSATION_ID)),
            has_more=True,
        ),
    )

    body = client.get(
        "/conversations", headers={"Authorization": "Bearer token"}
    ).json()
    assert body["next_before_activity_at"] == "2026-09-15T00:00:00Z"
    assert body["next_before_conversation_id"] == str(SECOND_CONVERSATION_ID)


def test_пустой_список_это_200_а_не_404(client, monkeypatch):
    authenticated(monkeypatch)
    _список(monkeypatch, ConversationPage())

    response = client.get(
        "/conversations", headers={"Authorization": "Bearer token"}
    )
    # «Бесед нет» — законный ответ: свой список отдаёт сам субъект,
    # и отсутствие элементов не делает ресурс ненайденным.
    assert response.status_code == 200
    assert response.json()["items"] == []


@pytest.mark.parametrize("limit", [0, 101])
def test_размер_страницы_вне_диапазона_даёт_400(client, monkeypatch, limit, отказ):
    authenticated(monkeypatch)
    response = client.get(
        f"/conversations?limit={limit}", headers={"Authorization": "Bearer token"}
    )
    отказ(response, status=400, code="invalid_cursor")


def test_второй_компонент_без_первого_даёт_400(client, monkeypatch, отказ):
    authenticated(monkeypatch)
    response = client.get(
        f"/conversations?before_conversation_id={CONVERSATION_ID}",
        headers={"Authorization": "Bearer token"},
    )
    отказ(response, status=400, code="invalid_cursor")


def test_одиночная_отметка_даёт_400(client, monkeypatch, отказ):
    # Половина пары отвергается, а не обслуживается «по слабой границе»:
    # по одной отметке стык страниц теряет беседы с равным `updated_at`,
    # а пустая страница на месте отказа неотличима от конца списка.
    authenticated(monkeypatch)
    response = client.get(
        "/conversations?before_activity_at=2026-09-15T00:00:00Z",
        headers={"Authorization": "Bearer token"},
    )
    отказ(response, status=400, code="invalid_cursor")


def test_наивная_отметка_даёт_400(client, monkeypatch, отказ):
    authenticated(monkeypatch)
    # Отметка передаётся **в паре**: одиночную отвергает правило выше,
    # и тест на часовой пояс зеленел бы по чужой причине, ничего не сказав
    # о времени. Смысл отказа прежний: без смещения сравнивать её не с чем,
    # а `asyncpg` истолковал бы её по местной зоне процесса, то есть ответ
    # зависел бы от развёртывания.
    response = client.get(
        f"/conversations?before_activity_at=2026-09-15T00:00:00"
        f"&before_conversation_id={CONVERSATION_ID}",
        headers={"Authorization": "Bearer token"},
    )
    отказ(response, status=400, code="invalid_cursor")


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        f"before_conversation_id={CONVERSATION_ID}",
        "before_activity_at=2026-09-15T00:00:00Z",
    ],
)
def test_негодный_курсор_не_открывает_соединение(
    client, monkeypatch, runtime, отказ, query
):
    authenticated(monkeypatch)
    response = client.get(
        f"/conversations?{query}", headers={"Authorization": "Bearer token"}
    )
    отказ(response, status=400, code="invalid_cursor")
    # Проверка стоит до соединения намеренно: негодная строка запроса
    # не должна занимать ни соединение, ни чтение удостоверения. Три
    # случая, а не один: половина пары — тот же отказ на той же границе,
    # и соединение он занимать не должен так же.
    assert runtime.opened == 0


def test_законный_курсор_открывает_ровно_одно_соединение(
    client, monkeypatch, runtime
):
    authenticated(monkeypatch)
    _список(monkeypatch, ConversationPage())

    client.get(
        f"/conversations?before_activity_at=2026-09-15T00:00:00Z"
        f"&before_conversation_id={CONVERSATION_ID}",
        headers={"Authorization": "Bearer token"},
    )
    # Одно, а не два, как в истории: там удостоверение обязано читаться
    # с писателя, а страница могла уйти на реплику, поэтому соединений
    # было два и под разные режимы. Здесь оба чтения идут к писателю.
    assert runtime.opened == 1


def test_нечисловой_limit_отклоняется_до_сервиса(client, monkeypatch):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("сервис позван с нечисловым размером страницы")

    monkeypatch.setattr(main.conversation_service, "list_conversations", _не_вызывать)
    response = client.get(
        "/conversations?limit=много", headers={"Authorization": "Bearer token"}
    )
    assert response.status_code == 422


def test_список_без_токена_не_открывает_соединение(
    client, monkeypatch, runtime, отказ
):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("список начался без удостоверения")

    monkeypatch.setattr(main.conversation_service, "list_conversations", _не_вызывать)
    response = client.get("/conversations")
    отказ(response, status=401, code="unauthenticated")
    assert runtime.opened == 1
