"""HTTP-контракт квитанции: коды ответа, форма тела и её источник.

Ни базы, ни Kafka: сервис подменяется, потому что проверяется обработчик —
коды, форма тела и то, что негодный номер отвергается до обращения
к хранилищу.

Разделение с интеграционной проверкой намеренное. Здесь — коды и формы,
и только они; утверждения о данных (`RCP-003…006`: монотонность,
`read ≤ delivered ≤ last_seq`, живая гонка двух устройств) живут
в `tests/integration/receipts_check.py`. Пересказывать одно другим значило
бы получить два описания одного правила, расходящихся при первой же правке
контракта.
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
from messenger.domain.ids import ConversationSeq, UserId
from messenger.domain.receipts import InvalidReceipt, ReadState
from messenger.domain.user import User
from messenger.services import identity
from messenger.services import receipts as service

ACTOR_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
CONVERSATION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
NOW = datetime(2026, 9, 19, tzinfo=UTC)

URL = f"/conversations/{CONVERSATION_ID}/receipts"


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


def _state(delivered: int, read: int) -> ReadState:
    return ReadState(
        delivered_seq=ConversationSeq(delivered), read_seq=ConversationSeq(read)
    )


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def runtime():
    """Подменяет владельца соединений и возвращает его же для проверок.

    Автоматически — потому что обработчику он нужен на каждом пути,
    включая отказы; запрошенный по имени, отдаёт ту же подделку, за
    которой можно посчитать обращения.
    """
    original = app.state.runtime
    подделка = Runtime()
    app.state.runtime = подделка
    yield подделка
    app.state.runtime = original


def authenticated(monkeypatch):
    async def _current(*args, **kwargs):
        return identity.AuthResult(user=_user())

    monkeypatch.setattr(main, "_current", _current)


def отвечает(monkeypatch, result):
    async def _set(conn, **kwargs):
        return result

    monkeypatch.setattr(service, "set_receipts", _set)


def test_без_токена_не_принимает(client, monkeypatch, отказ):
    async def _current(*args, **kwargs):
        return identity.AuthResult()

    monkeypatch.setattr(main, "_current", _current)
    отказ(client.post(URL, json={"read_seq": 3}), status=401, code="unauthenticated")


def test_состояние_доезжает_в_форме_контракта(client, monkeypatch):
    authenticated(monkeypatch)
    отвечает(monkeypatch, service.SetReceiptsResult(state=_state(5, 5)))

    r = client.post(URL, json={"read_seq": 5})
    assert r.status_code == 200
    # Ровно эти два ключа: лишний клиенту не обещан, а пропущенный
    # он прочтёт как «сервер не знает».
    assert set(r.json()) == {"delivered_seq", "read_seq"}
    assert r.json() == {"delivered_seq": 5, "read_seq": 5}


def test_нулевое_состояние_доезжает(client, monkeypatch):
    """Ноль — законное значение и всё содержимое свежей строки.

    Проверка на ложность (`if state.read_seq:`) выбросила бы ключ ровно
    здесь, и заметить это по другим тестам нельзя: там везде ненулевые
    числа.
    """
    authenticated(monkeypatch)
    отвечает(monkeypatch, service.SetReceiptsResult(state=_state(0, 0)))

    r = client.post(URL, json={"delivered_seq": 0})
    assert r.status_code == 200
    assert r.json() == {"delivered_seq": 0, "read_seq": 0}


def test_числа_берутся_из_сервиса_а_не_из_запроса(client, monkeypatch):
    """Ответ несёт состояние **после применения**.

    Именно этим клиент отличает «применено» от «проигнорировано»: отставшую
    квитанцию сервер не откатывает, и присланное в ответе выглядело бы
    применённым.
    """
    authenticated(monkeypatch)
    отвечает(monkeypatch, service.SetReceiptsResult(state=_state(9, 9)))

    r = client.post(URL, json={"read_seq": 3})
    assert r.json() == {"delivered_seq": 9, "read_seq": 9}


def test_удостоверение_и_запись_на_одном_соединении(client, monkeypatch, runtime):
    """Одно соединение, а не два, как в истории.

    Там второе берётся под свой режим чтения; здесь путь записи, и режим
    у него один. Проверяется счётчиком: два соединения прошли бы по
    коду незаметно и читали бы голову не тем пулом.
    """
    authenticated(monkeypatch)
    отвечает(monkeypatch, service.SetReceiptsResult(state=_state(5, 5)))

    client.post(URL, json={"read_seq": 5})
    assert runtime.opened == 1


def test_чужая_беседа_неотличима_от_несуществующей(client, monkeypatch):
    """`403` подтвердил бы, что беседа есть, и перебором выяснялось бы,
    кто с кем переписывается."""
    authenticated(monkeypatch)
    отвечает(
        monkeypatch,
        service.SetReceiptsResult(rejection=Reason.NOT_A_MEMBER),
    )

    r = client.post(URL, json={"read_seq": 3})
    assert r.status_code == 404
    assert r.json()["code"] == "resource_not_found"


def test_видимость_отказа_доносится_до_ответа(client, monkeypatch):
    """`403` на этом маршруте объявлен и остаётся достижимым.

    Сервис знает, пришёл субъект по своей ссылке или подобрал
    идентификатор; обработчик только доносит это знание до `to_problem`.
    Замени он его умолчанием — объявленный ответ стал бы недостижимым
    навсегда.
    """
    authenticated(monkeypatch)
    отвечает(
        monkeypatch,
        service.SetReceiptsResult(
            rejection=Reason.NOT_A_MEMBER, visibility=Visibility.KNOWN
        ),
    )

    r = client.post(URL, json={"read_seq": 3})
    assert r.status_code == 403


def test_номер_выше_головы_это_400_invalid_receipt(client, monkeypatch, отказ):
    """Негодность видна только рядом с данными, поэтому приходит
    из сервиса — и объявленный `400` обязан иметь производителя.

    Ловится именно `InvalidReceipt`, а не `ValueError`: широкая форма
    поймала бы любое будущее доменное исключение и выдала бы его
    за ошибку клиента.
    """
    authenticated(monkeypatch)

    async def _отвергает(conn, **kwargs):
        raise InvalidReceipt("read_seq (16) выше головы беседы (15)")

    monkeypatch.setattr(service, "set_receipts", _отвергает)

    отказ(client.post(URL, json={"read_seq": 16}), status=400, code="invalid_receipt")


@pytest.mark.parametrize("тело", [{"delivered_seq": -1}, {"read_seq": 2**63}])
def test_номер_вне_пределов_не_занимает_соединение(
    client, monkeypatch, runtime, отказ, тело
):
    """Ноль и отрицательные проверяются в домене, а не `Field(ge=...)`:
    ограничение в модели вернуло бы `422` в чужом формате раньше домена,
    и объявленный `400` оказался бы недостижим.

    `2**63` без границы дошёл бы до драйвера и стал бы `500` на ошибке
    клиента — поэтому проверка обязана идти до обращения к хранилищу.
    """
    authenticated(monkeypatch)

    async def _не_должно_вызваться(*args, **kwargs):
        raise AssertionError("негодный номер дошёл до сервиса")

    monkeypatch.setattr(service, "set_receipts", _не_должно_вызваться)

    r = client.post(URL, json=тело)
    отказ(r, status=400, code="invalid_receipt")
    # Если бы обработчик успел разобрать удостоверение, тест упал бы здесь:
    # отказ приходит раньше, чем кто-либо спрашивает, кто пришёл.
    assert runtime.opened == 0


def test_оба_поля_пусты_это_422(client, monkeypatch, runtime):
    """Существующая граница, а не новая: `anyOf` требует названного поля,
    иначе опечатка в имени выглядела бы валидным телом."""
    authenticated(monkeypatch)
    r = client.post(URL, json={})
    assert r.status_code == 422
    assert runtime.opened == 0


def test_чужое_поле_это_422(client, monkeypatch, runtime):
    """`additionalProperties: false` из контракта: единственное место
    в API с таким запретом, и оно обязано быть исполнено.

    Лишнее поле идёт **рядом с законным**, а не вместо него. С одним
    `red_seq` тело отверг бы и валидатор «хотя бы один номер», и тест
    проходил бы при снятом запрете — то есть доказывал бы не то, ради
    чего заведён. Здесь при `extra="ignore"` тело стало бы валидным
    и вернуло бы `200`.
    """
    authenticated(monkeypatch)
    r = client.post(URL, json={"read_seq": 3, "red_seq": 3})
    assert r.status_code == 422
    assert runtime.opened == 0


@pytest.mark.parametrize(
    "тело",
    [
        {"delivered_seq": 5},
        {"read_seq": 5},
        {"delivered_seq": 5, "read_seq": 3},
        # `3.0` — законное целое для JSON Schema: `type: integer` совпадает
        # с любым числом без дробной части. Запрет его здесь был бы
        # самоуправством: `3.5` отвергает сам Pydantic, а `3.0` обязан
        # проехать. Без этого случая проверку однажды «ужесточат» до
        # `isinstance(value, int)`, и сломается это молча.
        {"read_seq": 3.0},
    ],
)
def test_любое_названное_поле_законно(client, monkeypatch, тело):
    """Квитанция обязана назвать **хотя бы одно** поле, а не оба."""
    authenticated(monkeypatch)
    отвечает(monkeypatch, service.SetReceiptsResult(state=_state(5, 5)))

    r = client.post(URL, json=тело)
    assert r.status_code == 200


@pytest.mark.parametrize(
    "тело",
    [
        {"read_seq": "3"},
        {"read_seq": True},
        {"read_seq": False},
        {"read_seq": 3, "delivered_seq": None},
    ],
)
def test_не_целое_и_явный_null_это_422(client, monkeypatch, runtime, тело):
    """Контракт объявляет поля необязательными **не-null** целыми.

    Необязательность в OpenAPI значит «ключ можно не присылать», а не
    «можно прислать `null`», — и `int | None` этого не выражает. Pydantic
    в обычном режиме приводит `"3"` к `3`, `true`/`false` — к `1`/`0`,
    а явный `null` становится тем же `None`, которым помечено «поля не
    было»: тело `{"read_seq": 3, "delivered_seq": null}` проходит
    валидатор «хотя бы один номер» и выглядит полностью законным.

    Опаснее всех `false`: он применяется как «прочитано ничего» и отвечает
    `200`, то есть клиент с булевым там, где ждут номер, не узнает об этом
    никогда. Это ровно тот случай, ради которого отвергается опечатка
    `red_seq`, — а «поглотить молча» тут и есть дефект.

    Проверка транспортная, а не доменная: домен работает с целыми и о JSON
    знать не должен, здесь же исполняется форма, объявленная контрактом.
    Поэтому и `runtime.opened == 0` — отказ приходит раньше, чем
    кто-либо спрашивает соединение или удостоверение.
    """
    authenticated(monkeypatch)

    async def _не_должно_вызваться(*args, **kwargs):
        raise AssertionError("тело, запрещённое контрактом, дошло до сервиса")

    monkeypatch.setattr(service, "set_receipts", _не_должно_вызваться)

    r = client.post(URL, json=тело)
    assert r.status_code == 422
    assert runtime.opened == 0
