"""HTTP-контракт истории: форма ответа и отказы на негодный запрос.

Ни базы, ни Kafka: сервис подменяется, потому что проверяется обработчик —
коды ответа, форма тела и то, что негодная строка запроса отвергается
до обращения к хранилищу.

Разделение с интеграционной проверкой намеренное. Здесь — коды и формы,
и только они; утверждения о данных (`HIST-001…004`, стык страниц, надгробие
внутри страницы) живут в `tests/integration/history_check.py`. Пересказывать
одно другим значило бы получить два описания одного правила, расходящихся
при первой же правке контракта.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from messenger.api import main
from messenger.api.main import app
from messenger.domain.errors import Reason, Visibility
from messenger.domain.history import HistoryDirection, MessagePage
from messenger.domain.ids import ConversationId, ConversationSeq
from messenger.domain.message import Message, MessageKind, MessagePayload
from messenger.domain.user import User
from messenger.services import history as history_service
from messenger.services import identity
from messenger.services.history import HistoryResult

ACTOR_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
NOW = datetime(2026, 9, 19, tzinfo=UTC)

URL = f"/conversations/{CONVERSATION_ID}/messages"


class Runtime:
    """Владелец соединений, считающий, сколько раз за ним пришли."""

    keys = None
    oidc_settings = None

    def __init__(self) -> None:
        self.opened = 0

    @asynccontextmanager
    async def connection(self, mode=None):
        self.opened += 1
        yield None


def _user() -> User:
    return User(
        user_id=ACTOR_ID,
        external_id="kc-1",
        display_name="Аня",
        email="anya@example.org",
        email_verified=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _message(seq: int) -> Message:
    return Message(
        message_id=uuid.uuid4(),
        conversation_id=ConversationId(CONVERSATION_ID),
        conversation_seq=ConversationSeq(seq),
        sender_id=ACTOR_ID,
        client_message_id=uuid.uuid4(),
        kind=MessageKind.TEXT,
        payload=MessagePayload(text=str(seq)),
        created_at=NOW,
    )


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def runtime():
    """Подменяет владельца соединений и возвращает его же для проверок."""
    original = app.state.runtime
    подделка = Runtime()
    app.state.runtime = подделка
    yield подделка
    app.state.runtime = original


def authenticated(monkeypatch):
    async def _current(*args, **kwargs):
        return identity.AuthResult(user=_user())

    monkeypatch.setattr(main, "_current", _current)


def отвечает(monkeypatch, result: HistoryResult, собранное: dict | None = None):
    async def _list(conn, **kwargs):
        if собранное is not None:
            собранное.update(kwargs)
        return result

    monkeypatch.setattr(history_service, "list_messages", _list)


# --- Успешный ответ -------------------------------------------------------


def test_страница_отдаётся_в_форме_контракта(client, monkeypatch, runtime):
    authenticated(monkeypatch)
    отвечает(
        monkeypatch,
        HistoryResult(
            page=MessagePage(items=(_message(2), _message(1)), has_more=True),
            direction=HistoryDirection.BACKWARD,
        ),
    )

    r = client.get(URL, params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    # Все пять полей присутствуют всегда: клиент различает «курсора нет»
    # и «поля нет» только по наличию ключа.
    for поле in ("items", "has_more", "next_before_seq", "next_after_seq",
                 "sync_to_seq"):
        assert поле in body, f"нет поля {поле}"
    assert body["has_more"] is True
    assert body["next_before_seq"] == 1
    assert body["next_after_seq"] is None
    assert body["sync_to_seq"] is None
    assert [item["seq"] for item in body["items"]] == [2, 1]
    assert body["items"][0]["payload"]["text"] == "2"


def test_пустой_снимок_доезжает_нулём_а_не_отсутствием(client, monkeypatch, runtime):
    """`sync_to_seq = 0` — не `null`. Провал на `if sync_to_seq:` превратил
    бы «снимок есть, и он пуст» в «снимка нет», и клиент перестал бы
    считать беседу синхронизированной."""
    authenticated(monkeypatch)
    отвечает(
        monkeypatch,
        HistoryResult(
            page=MessagePage(),
            sync_to_seq=ConversationSeq(0),
            direction=HistoryDirection.FORWARD,
        ),
    )

    body = client.get(URL, params={"after_seq": 0}).json()
    assert body["sync_to_seq"] == 0
    assert body["sync_to_seq"] is not None
    assert body["items"] == []


def test_курсоры_доезжают_до_сервиса_без_изменений(client, monkeypatch, runtime):
    """Обработчик не переписывает запрос своими словами: иначе правило
    о курсорах было бы двумя правилами — здесь и в сервисе."""
    authenticated(monkeypatch)
    собранное: dict = {}
    отвечает(monkeypatch, HistoryResult(page=MessagePage()), собранное)

    client.get(URL, params={"after_seq": 5, "through_seq": 15, "limit": 7})
    assert собранное["after_seq"] == 5
    assert собранное["through_seq"] == 15
    assert собранное["limit"] == 7
    assert собранное["before_seq"] is None


def test_размер_страницы_по_умолчанию_из_контракта(client, monkeypatch, runtime):
    authenticated(monkeypatch)
    собранное: dict = {}
    отвечает(monkeypatch, HistoryResult(page=MessagePage()), собранное)

    client.get(URL)
    assert собранное["limit"] == 50


# --- Отказы на негодный запрос -------------------------------------------


@pytest.mark.parametrize(
    "параметры",
    [
        # Разные направления сразу.
        {"before_seq": 5, "after_seq": 1},
        # Граница снимка без снимка.
        {"through_seq": 5},
        # Диапазона не существует.
        {"after_seq": 5, "through_seq": 4},
        # Размер страницы вне 1..100.
        {"limit": 0},
        {"limit": 101},
        # Минимумы: свой у каждой границы.
        {"before_seq": 0},
        {"after_seq": -1},
        {"after_seq": 0, "through_seq": -1},
    ],
)
def test_негодный_курсор_отвергается_400(client, monkeypatch, runtime, отказ, параметры):
    """Контракт объявляет здесь `400` и перечисляет ровно эти нарушения,
    а `422` не объявлен нигде. Поэтому ограничения параметров объявлены
    без `Query(ge=…, le=…)`: иначе `limit=0` вернул бы `422` в чужом
    формате раньше, чем проверка дошла бы до дела."""
    # Если бы обработчик успел разобрать удостоверение, тест упал бы здесь:
    # отказ приходит раньше, чем кто-либо спрашивает, кто пришёл.
    async def _не_должно_вызваться(*args, **kwargs):
        raise AssertionError("вопрос об удостоверении задан при негодном запросе")

    monkeypatch.setattr(main, "_current", _не_должно_вызваться)

    r = client.get(URL, params=параметры)
    отказ(r, status=400, code="invalid_cursor")


def test_негодное_сочетание_не_занимает_соединение(client, runtime):
    """Негодный запрос не должен доходить до хранилища: соединение —
    ресурс, за которым стоят другие запросы."""
    r = client.get(URL, params={"before_seq": 5, "after_seq": 1})
    assert r.status_code == 400
    assert runtime.opened == 0


def test_нечисловой_курсор_остаётся_422(client, monkeypatch, runtime):
    """Существующая граница, а не новая: так же ведёт себя нечисловое поле
    в теле. Глобальный обработчик `RequestValidationError` изменил бы
    поведение всех маршрутов разом, и в эту правку он не входит."""
    authenticated(monkeypatch)
    r = client.get(URL, params={"before_seq": "вчера"})
    assert r.status_code == 422


# --- Доступ ---------------------------------------------------------------


def test_без_токена_история_не_отдаётся(client, monkeypatch, runtime, отказ):
    async def _current(*args, **kwargs):
        return identity.AuthResult()

    monkeypatch.setattr(main, "_current", _current)
    отказ(client.get(URL), status=401, code="unauthenticated")


def test_чужая_беседа_неотличима_от_несуществующей(client, monkeypatch, runtime, отказ):
    """`403` подтвердил бы, что беседа есть, и перебором идентификаторов
    выяснялось бы, кто с кем переписывается."""
    authenticated(monkeypatch)
    отвечает(monkeypatch, HistoryResult(rejection=Reason.NOT_A_MEMBER))

    отказ(client.get(URL), status=404, code="resource_not_found")


def test_видимость_отказа_доезжает_до_ответа(client, monkeypatch, runtime, отказ):
    """Единственное место, где сервис не выбрасывает `Decision.visibility`.

    Контракт объявляет на этом маршруте `403`, и до этой правки получить
    его было нельзя: сервисы звали `to_problem` с умолчанием `HIDDEN`.
    Сегодня `KNOWN` не выставляет никто, поэтому проверка синтетическая —
    она доказывает, что поле проезжает обработчик, а не теряется по дороге.
    """
    authenticated(monkeypatch)
    отвечает(
        monkeypatch,
        HistoryResult(rejection=Reason.NOT_A_MEMBER, visibility=Visibility.KNOWN),
    )

    отказ(client.get(URL), status=403, code="forbidden")
