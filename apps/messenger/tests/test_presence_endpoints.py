"""Присутствие в ответе беседы — и маска блокировки вокруг него.

`G3-007` — первый гейт, который отдаёт присутствие наружу. До него
`users.last_seen_at` уезжал в ответ отметкой времени без признака «онлайн»,
и вопроса «в сети ли человек» контракт не задавал вовсе. Вместе с ответом
появляется и его цена: `online` двигается от активности человека, а значит
список бесед становится каналом наблюдения за тем, когда собеседник был
у устройства. Поэтому вторая половина файла — про маску: у участника, с
которым у зрителя есть блокировка, метаданных активности нет вовсе.

**Маска — отсутствием, а не значением.** `online: false` было бы
утверждением «активности не было», а скрыть требуется другое — сам факт
наблюдения. Отсюда проверки не «равно `false`», а «ключа нет»: разница
между «не в сети» и «не наше дело» — это и есть предмет правила, и тест,
написанный через `is False`, пропустил бы дефект, подменивший одно другим.

Числа присутствия берутся относительно **настоящих** часов, а не
фиксированной отметки: предикат считает сервер от текущего мгновения
(`domain/presence.is_online`), и подставленные часы проверяли бы
арифметику теста, а не окно сервера. Запасы взяты грубые (тридцать секунд
против десяти минут) — так, чтобы секунда, потраченная на запрос, ничего
не решала; граничная отметка в 200 секунд проверяет уже само окно.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from messenger.api import main
from messenger.api.main import app
from messenger.domain.conversation import Conversation, ConversationType
from messenger.domain.conversation_list import (
    ConversationPage,
    ConversationSummary,
)
from messenger.domain.ids import ConversationId, ConversationSeq, UserId
from messenger.domain.receipts import ParticipantReadState, ReadState
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


def _participant(
    user_id: UserId, name: str, *, seen: datetime | None = None
) -> UserSummary:
    return UserSummary(user_id=user_id, display_name=name, last_seen_at=seen)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def runtime():
    original = app.state.runtime
    stub = Runtime()
    app.state.runtime = stub
    yield stub
    app.state.runtime = original


def authenticated(monkeypatch):
    async def _current(*args, **kwargs):
        return identity.AuthResult(user=_user(ACTOR_ID, "Аня"))

    monkeypatch.setattr(main, "_current", _current)


def _summary(
    *,
    seen: datetime | None,
    viewer_seen: datetime | None = None,
    states: tuple[ParticipantReadState, ...] = (),
):
    """Беседа с обоими участниками: у зрителя отметки нет, у собеседника — есть."""
    return ConversationSummary(
        conversation=Conversation(
            conversation_id=CONVERSATION_ID,
            type=ConversationType.DIRECT,
            direct_key="a:b",
            last_seq=ConversationSeq(0),
            created_at=NOW,
            updated_at=NOW,
        ),
        participants=(
            _participant(ACTOR_ID, "Аня", seen=viewer_seen),
            _participant(OTHER_ID, "Борис", seen=seen),
        ),
        read_states=states,
    )


def _список(monkeypatch, summary: ConversationSummary, *, blocked_with=()) -> None:
    """Подменяет сервис списка — удостоверение при этом настоящее."""

    async def _list(*args, **kwargs):
        return service.ConversationListResult(
            page=ConversationPage(items=(summary,)),
            blocked_with=frozenset(blocked_with),
        )

    monkeypatch.setattr(main.conversation_service, "list_conversations", _list)


def _создание(monkeypatch, summary: ConversationSummary, *, blocked_with=()) -> None:
    """Подменяет сервис создания: тот же участник, но другим маршрутом."""

    async def _create(*args, **kwargs):
        return service.CreateDirectResult(
            conversation=summary.conversation,
            participants=summary.participants,
            read_states=summary.read_states,
            blocked_with=frozenset(blocked_with),
            created=False,
        )

    monkeypatch.setattr(main.conversation_service, "create_direct", _create)


def _участники(body: dict[str, object]) -> dict[str, dict]:
    return {item["user_id"]: item for item in body["participants"]}


def _списком(client, monkeypatch, summary, *, blocked_with=()) -> dict:
    """Беседа из `GET /conversations` целиком — участники и состояние чтения."""
    _список(monkeypatch, summary, blocked_with=blocked_with)
    response = client.get("/conversations", headers={"Authorization": "Bearer token"})
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


def _созданием(client, monkeypatch, summary, *, blocked_with=()) -> dict:
    """Та же беседа, но полученная маршрутом создания — переиспользованная."""
    _создание(monkeypatch, summary, blocked_with=blocked_with)
    response = client.post(
        "/conversations",
        json={"participant_id": str(OTHER_ID)},
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def _квитанции(body: dict) -> dict[str, dict]:
    return {item["user_id"]: item for item in body["read_states"]}


def test_свежая_отметка_это_онлайн(client, monkeypatch):
    """Тридцать секунд назад — внутри окна, и сервер говорит это словом."""
    authenticated(monkeypatch)

    участники = _участники(
        _списком(
            client, monkeypatch, _summary(seen=datetime.now(UTC) - timedelta(seconds=30))
        )
    )

    assert участники[str(OTHER_ID)]["online"] is True
    # Отметка при этом остаётся: она не заменяется признаком, а живёт рядом.
    assert "last_seen_at" in участники[str(OTHER_ID)]


def test_старая_отметка_это_не_онлайн(client, monkeypatch):
    """Десять минут — снаружи окна, и `false` здесь положительное утверждение.

    Отметка снята с человека сейчас, а не «когда-то»: `false` говорит именно
    об активности за последние три минуты, и отметка рядом объясняет, когда
    она была.
    """
    authenticated(monkeypatch)

    участники = _участники(
        _списком(
            client, monkeypatch, _summary(seen=datetime.now(UTC) - timedelta(minutes=10))
        )
    )

    assert участники[str(OTHER_ID)]["online"] is False
    assert "last_seen_at" in участники[str(OTHER_ID)]


def test_граничная_отметка_проверяет_окно_а_не_свою_константу(client, monkeypatch):
    """Двести секунд — снаружи объявленных 180, и это `false`.

    Отметка выбрана между двумя правдоподобными окнами: 180 секунд
    (`domain/presence.ONLINE_WINDOW_SECONDS`) и 300. Своя константа на месте
    общей, поменявшая окно, покраснеет здесь и останется зелёной на грубых
    запасах соседних тестов — а разойтись она может молча, потому что окно
    объявлено дважды: здесь на Python и в SQL-половине правила
    (`repositories/sessions.py`).
    """
    authenticated(monkeypatch)

    участники = _участники(
        _списком(
            client,
            monkeypatch,
            _summary(seen=datetime.now(UTC) - timedelta(seconds=200)),
        )
    )

    assert участники[str(OTHER_ID)]["online"] is False


def test_никогда_не_был_в_сети_это_не_онлайн_и_не_отметка(client, monkeypatch):
    """Отметки нет вовсе: `online: false` — уверенное «не в сети».

    Это не то же самое, что маска ниже: «никогда не подтверждался» — факт,
    о котором сервер знает и который вправе сообщить. Ключа `last_seen_at`
    при этом нет, потому что отметкой было бы время, которого не существует.
    """
    authenticated(monkeypatch)

    участники = _участники(_списком(client, monkeypatch, _summary(seen=None)))

    assert участники[str(OTHER_ID)]["online"] is False
    assert "last_seen_at" not in участники[str(OTHER_ID)]


# --- Маска блокировки ------------------------------------------------------
#
# Ниже проверяется симметричный privacy-инвариант `G3-007` — целиком и
# только он. Документальный минимум `BLK-002` (`blocked` не видит `blocker`)
# здесь **не** предъявляется: в наборе участников обе стороны пары
# неразличимы, и отдельной строкой в `08-authorization.md` он закрывается
# проверкой, которая пишет строку в `blocks` прямо (`tests/integration/
# receipt_notice_check.py`) — там у обеих сторон есть свои удостоверения.
#
# Предмет юнита — не запрос к базе (его результат — множество `user_id`,
# и оно приходит сюда подделкой), а **применение** маски: что именно
# скрывается и что остаётся. По одному тесту на каждую из двух вещей
# потому, что маскировки две (присутствие и квитанции), и обходятся они
# одним и тем же множеством только в исполнении; проверь одну — вторая
# осталась бы на слове.


def test_маска_снимает_присутствие_но_не_участника(client, monkeypatch):
    """Заблокированный из беседы не исчезает — исчезает его активность.

    Беседа читается по `BLK-004` («старая переписка остаётся видимой»),
    поэтому участник обязан остаться в ответе с именем и `user_id`: скрыть
    человека значило бы отнять у зрителя беседу, которую тот вправе читать.
    Скрывается ровно то, что рассказывает о его присутствии.
    """
    authenticated(monkeypatch)

    участники = _участники(
        _списком(
            client,
            monkeypatch,
            _summary(seen=datetime.now(UTC) - timedelta(seconds=5)),
            blocked_with={OTHER_ID},
        )
    )

    заблокированный = участники[str(OTHER_ID)]
    assert "online" not in заблокированный
    assert "last_seen_at" not in заблокированный
    # Положительный контроль: участник на месте, а не выброшен из ответа.
    assert заблокированный["display_name"] == "Борис"


def test_маска_снимает_чужую_квитанцию_и_оставляет_свою(client, monkeypatch):
    """Квитанция — такая же метаданные активности, и маскируется тем же множеством.

    Своя запись остаётся: это собственные данные зрителя, утечки в них нет,
    и лишать человека его же числа незачем. Поэтому в одной и той же беседе
    элементов два — свой и ничей: массив разреженный, и на равных нулях
    подмена «чужое скрыто» на «своё потеряно» прошла бы незамеченной.
    """
    authenticated(monkeypatch)

    состояния = (
        ParticipantReadState(
            user_id=ACTOR_ID,
            state=ReadState(
                delivered_seq=ConversationSeq(4), read_seq=ConversationSeq(3)
            ),
        ),
        ParticipantReadState(
            user_id=OTHER_ID,
            state=ReadState(
                delivered_seq=ConversationSeq(9), read_seq=ConversationSeq(7)
            ),
        ),
    )

    беседа = _списком(
        client, monkeypatch, _summary(seen=None, states=состояния), blocked_with={OTHER_ID}
    )
    квитанции = _квитанции(беседа)

    assert str(OTHER_ID) not in квитанции
    assert квитанции[str(ACTOR_ID)]["last_read_seq"] == 3


def test_маска_работает_и_на_втором_маршруте(client, monkeypatch):
    """Сборка тела одна на оба маршрута — значит и маска обязана быть одной.

    Проверяются оба по отдельности, а не «список, а создание такое же»:
    предикат читается каждым маршрутом сам, и маршрут, забывший передать
    его в сборку, выдал бы присутствие собеседника в ответе на создание той
    же самой беседы. Один общий тест на двоих этого не поймал бы.
    """
    authenticated(monkeypatch)

    состояния = (
        ParticipantReadState(
            user_id=OTHER_ID,
            state=ReadState(
                delivered_seq=ConversationSeq(9), read_seq=ConversationSeq(7)
            ),
        ),
    )

    созданная = _созданием(
        client,
        monkeypatch,
        _summary(seen=datetime.now(UTC) - timedelta(seconds=5), states=состояния),
        blocked_with={OTHER_ID},
    )

    заблокированный = _участники(созданная)[str(OTHER_ID)]
    assert "online" not in заблокированный
    assert "last_seen_at" not in заблокированный
    assert str(OTHER_ID) not in _квитанции(созданная)
