"""История: маршрутизация чтения, границы страницы и чтение надгробия.

Ни базы, ни HTTP. Проверяется решение: какой пул берётся под какую
свежесть, где кончается страница, что отвечает результат на пустую
выборку и переживает ли чтение удалённое сообщение. Настоящие выборки
проверяются интеграционно, на живой беседе (`tests/integration/
history_check.py`), — здесь Postgres не нужен вовсе.

Это и есть локальная половина `DB-001`: решение маршрутизатора. Вторая
половина — что чтение через выбранный путь действительно видит только
что записанное, — интеграционная. Непроверенным остаётся ровно одно
звено: что настоящая реплика отстаёт. Для этого нужен stage.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from messenger.domain.history import (
    MAX_PAGE_SIZE,
    HistoryDirection,
    MessagePage,
    validate_cursors,
)
from messenger.domain.ids import ConversationId, ConversationSeq
from messenger.domain.message import Message, MessageKind, MessagePayload
from messenger.domain.user import User
from messenger.repositories import messages as messages_repository
from messenger.repositories.postgres import PoolSettings
from messenger.services import history as history_service
from messenger.services import runtime as runtime_service
from messenger.services.history import HistoryResult
from messenger.services.runtime import ReadMode

ACTOR_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION_ID = ConversationId(uuid.UUID("33333333-3333-3333-3333-333333333333"))
NOW = datetime(2026, 9, 19, tzinfo=UTC)


def _message(seq: int, *, deleted: bool = False) -> Message:
    return Message(
        message_id=uuid.uuid4(),
        conversation_id=CONVERSATION_ID,
        conversation_seq=ConversationSeq(seq),
        sender_id=ACTOR_ID,
        client_message_id=uuid.uuid4(),
        kind=MessageKind.TEXT,
        payload=MessagePayload() if deleted else MessagePayload(text=str(seq)),
        created_at=NOW,
        deleted_at=NOW if deleted else None,
    )


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


def _row(seq: int, *, payload: str | None = '{"text": "привет"}', deleted: bool = False):
    """Строка `messages` как её отдаёт драйвер: `payload` — текст jsonb."""
    return {
        "message_id": uuid.uuid4(),
        "conversation_id": CONVERSATION_ID,
        "conversation_seq": seq,
        "sender_id": ACTOR_ID,
        "client_message_id": uuid.uuid4(),
        "type": "text",
        "payload": payload,
        "reply_to_message_id": None,
        "created_at": NOW,
        "edited_at": None,
        "deleted_at": NOW if deleted else None,
    }


# --- Правило о курсорах ---------------------------------------------------


@pytest.mark.parametrize(
    "курсоры",
    [
        # 1. Разные направления сразу: нужен один.
        {"before_seq": 5, "after_seq": 1, "through_seq": None, "limit": 50},
        # 2. Граница снимка без самого снимка.
        {"before_seq": None, "after_seq": None, "through_seq": 5, "limit": 50},
        # 3. Диапазона не существует. Пустая страница здесь была бы хуже
        # отказа: клиент прочёл бы её как «синхронизация завершена».
        {"before_seq": None, "after_seq": 5, "through_seq": 4, "limit": 50},
        # 4. Размер страницы вне объявленного контрактом.
        {"before_seq": None, "after_seq": None, "through_seq": None, "limit": 0},
        {"before_seq": None, "after_seq": None, "through_seq": None, "limit": 101},
        # 5. Минимумы: свой у каждой границы.
        {"before_seq": 0, "after_seq": None, "through_seq": None, "limit": 50},
        {"before_seq": None, "after_seq": -1, "through_seq": None, "limit": 50},
        {"before_seq": None, "after_seq": 0, "through_seq": -1, "limit": 50},
    ],
)
def test_запрещённые_сочетания_курсоров_отвергаются(курсоры):
    with pytest.raises(ValueError):
        validate_cursors(**курсоры)


@pytest.mark.parametrize(
    "курсоры",
    [
        # Ни одного курсора: первая страница листания.
        {"before_seq": None, "after_seq": None, "through_seq": None, "limit": 50},
        # Только назад.
        {"before_seq": 5, "after_seq": None, "through_seq": None, "limit": 50},
        # Начало синхронизации: граница берётся заново.
        {"before_seq": None, "after_seq": 0, "through_seq": None, "limit": 50},
        # Продолжение синхронизации.
        {"before_seq": None, "after_seq": 5, "through_seq": 15, "limit": 50},
        # Пустой снимок. Равенство, а не ошибка: так выглядит повтор
        # запроса, который клиент уже выполнил, а повтор после обрыва
        # связи — обычное дело, не сбой клиента.
        {"before_seq": None, "after_seq": 15, "through_seq": 15, "limit": 50},
        # Границы диапазона, обе достижимые.
        {"before_seq": 1, "after_seq": None, "through_seq": None, "limit": 1},
        {"before_seq": None, "after_seq": 0, "through_seq": None, "limit": MAX_PAGE_SIZE},
    ],
)
def test_законные_сочетания_проходят(курсоры):
    validate_cursors(**курсоры)


def test_ноль_законен_для_after_seq_и_запрещён_для_before_seq():
    """Асимметрия минимумов — не опечатка, и её видно только рядом.

    `before_seq=0` — это `seq < 0`, всегда пустая страница, то есть
    ошибка клиента. `after_seq=0` — «не видел ещё ничего», законное
    начало синхронизации.
    """
    validate_cursors(before_seq=None, after_seq=0, through_seq=None, limit=50)
    with pytest.raises(ValueError):
        validate_cursors(before_seq=0, after_seq=None, through_seq=None, limit=50)


# --- Выбор режима чтения --------------------------------------------------


@pytest.mark.parametrize(
    "before_seq, after_seq, ожидание",
    [
        (None, None, ReadMode.STRONG),
        (None, 0, ReadMode.STRONG),
        (None, 15, ReadMode.STRONG),
        (8, None, ReadMode.STALE_OK),
        (1, None, ReadMode.STALE_OK),
    ],
)
def test_режим_чтения_по_типу_запроса(before_seq, after_seq, ожидание):
    """Свежая страница обязана содержать своё же сообщение (DB-001);
    глубокая пагинация отставания не замечает — там оно на секунды."""
    assert history_service.read_mode(before_seq=before_seq, after_seq=after_seq) is ожидание


def test_through_seq_на_режим_не_влияет():
    """Соблазн «курсор есть — значит реплика» сильный, и он неверен.

    `through_seq` продолжает уже начатую синхронизацию, и её страницы
    обязаны читаться так же строго, как первая. Проверяется не значение,
    а отсутствие самого входа: расширить подпись придётся явно.
    """
    with pytest.raises(TypeError):
        history_service.read_mode(before_seq=None, after_seq=5, through_seq=5)


# --- Курсоры продолжения --------------------------------------------------


def test_курсор_продолжения_берётся_с_конца_страницы():
    """Не с начала: первым элементом клиент вернулся бы на шаг назад."""
    result = HistoryResult(
        page=MessagePage(items=(_message(10), _message(9), _message(8)), has_more=True),
        direction=HistoryDirection.BACKWARD,
    )
    assert result.next_before_seq == 8
    assert result.next_after_seq is None


def test_курсор_вперёд_берётся_с_конца_и_назад_его_нет():
    result = HistoryResult(
        page=MessagePage(items=(_message(6), _message(7)), has_more=True),
        direction=HistoryDirection.FORWARD,
    )
    assert result.next_after_seq == 7
    assert result.next_before_seq is None


def test_исчерпанная_и_пустая_страница_не_дают_курсора():
    """Пустая страница — законный ответ, а не повод для `IndexError`."""
    истощено = HistoryResult(
        page=MessagePage(items=(_message(2), _message(1)), has_more=False),
        direction=HistoryDirection.BACKWARD,
    )
    пусто = HistoryResult(page=MessagePage(), direction=HistoryDirection.BACKWARD)
    assert истощено.next_before_seq is None
    assert пусто.next_before_seq is None


def test_пустая_страница_это_успех_а_не_отсутствие_результата():
    """`ok` не про непустоту: снимок есть, и он пуст."""
    assert HistoryResult(page=MessagePage(), sync_to_seq=ConversationSeq(15)).ok


# --- Надгробие ------------------------------------------------------------


def test_надгробие_читается_и_не_падает():
    """Регрессия на найденный дефект.

    `payload IS NULL` — гарантия схемы
    (`messages_payload_matches_state`), а не редкий случай, и чтение
    удалённого сообщения падало здесь `AttributeError` — то есть история
    ломалась ровно там, где обязана показать, что сообщение удалено.
    """
    message = messages_repository._to_message(_row(3, payload=None, deleted=True))
    assert message.is_deleted
    assert message.payload == MessagePayload()
    assert message.visible_payload is None
    assert message.conversation_seq == 3


def test_обычное_сообщение_читается_как_прежде():
    message = messages_repository._to_message(_row(1))
    assert not message.is_deleted
    assert message.payload.text == "привет"


# --- Границы страницы в репозитории ---------------------------------------


class FakeConn:
    """Соединение, отдающее заранее заданные строки и помнящее запрос."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.parameters: tuple | None = None
        self.query: str | None = None

    async def fetch(self, query: str, *parameters):
        self.query = query
        self.parameters = parameters
        return self.rows


@pytest.mark.parametrize(
    "выборка",
    [
        messages_repository.fetch_page_backward,
        messages_repository.fetch_page_forward,
    ],
)
def test_текст_запроса_не_содержит_питоновских_комментариев(выборка):
    """Регрессия на дефект, найденный интеграцией, а не здесь.

    `#` — комментарий для Python и начало комментария для Postgres, но
    только один из них действует до передачи. Строка, начатая как
    `f\"\"\"  # noqa: S608`, уезжает в драйвер целиком, и запрос падает
    `PostgresSyntaxError` уже на разборе — то есть первый же настоящий
    вызов, тогда как поддельное соединение принимает любой текст.

    Проверка слабая и намеренно такая: она ловит не «неправильный SQL»,
    а ровно тот класс ошибок, который на подделке не виден вовсе.
    """
    conn = FakeConn([])
    параметры = (
        {"conversation_id": CONVERSATION_ID, "before_seq": None, "limit": 5}
        if выборка is messages_repository.fetch_page_backward
        else {
            "conversation_id": CONVERSATION_ID,
            "after_seq": ConversationSeq(0),
            "through_seq": ConversationSeq(5),
            "limit": 5,
        }
    )
    asyncio.run(выборка(conn, **параметры))

    assert conn.query is not None
    assert "#" not in conn.query, conn.query
    assert conn.query.lstrip().startswith("SELECT"), conn.query


def test_лишняя_строка_остаётся_за_страницей():
    """`limit + 1` не покидает репозиторий.

    Если взять курсором пробную строку, клиент перескочит `limit`-й элемент
    и потеряет его навсегда — на короткой беседе это не воспроизводится.
    """
    conn = FakeConn([_row(номер) for номер in (12, 11, 10, 9)])
    page = asyncio.run(
        messages_repository.fetch_page_backward(
            conn, conversation_id=CONVERSATION_ID, before_seq=None, limit=3
        )
    )
    assert [message.conversation_seq for message in page.items] == [12, 11, 10]
    assert page.has_more
    # Запрошено на одну больше, чем отдано.
    assert conn.parameters[2] == 4


def test_ровно_исчерпанный_диапазон_не_обещает_продолжения():
    """`len(items) == limit` признаком «есть ещё» не является: клиент
    сделал бы лишний пустой запрос."""
    conn = FakeConn([_row(номер) for номер in (3, 2, 1)])
    page = asyncio.run(
        messages_repository.fetch_page_backward(
            conn, conversation_id=CONVERSATION_ID, before_seq=None, limit=3
        )
    )
    assert len(page.items) == 3
    assert not page.has_more


def test_без_курсора_обратная_страница_идёт_от_максимума():
    """Отсутствие курсора — не «нет условия», а верхняя граница.

    `($2 IS NULL OR ...)` дало бы те же строки, но перестало бы быть
    условием по индексу, и страница снималась бы полным проходом.
    """
    conn = FakeConn([])
    asyncio.run(
        messages_repository.fetch_page_backward(
            conn, conversation_id=CONVERSATION_ID, before_seq=None, limit=5
        )
    )
    assert conn.parameters[1] == 2**63 - 1


def test_курсор_подставляется_как_есть():
    conn = FakeConn([])
    asyncio.run(
        messages_repository.fetch_page_backward(
            conn, conversation_id=CONVERSATION_ID, before_seq=8, limit=5
        )
    )
    assert conn.parameters[1] == 8


# --- Runtime: какой пул берётся -------------------------------------------


class FakeConnPool:
    """Поддельный пул. Отдаёт себя вместо соединения — так видно, чей он."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    @asynccontextmanager
    async def _ctx(self):
        yield self.name

    def acquire(self, timeout: float | None = None):
        return self._ctx()

    async def close(self) -> None:
        self.closed = True


def _runtime(**kwargs) -> runtime_service.Runtime:
    """Владелец соединений без реплики — если не задано обратное."""
    settings = PoolSettings("writer", 5432, "messenger", "messenger", "")
    kwargs.setdefault("read_settings", None)
    return runtime_service.Runtime(settings=settings, **kwargs)


_УМОЛЧАНИЕ = object()


async def _взяли(runtime, mode=_УМОЛЧАНИЕ) -> str:
    """Какое соединение выдал `Runtime`. Без режима — вызов по умолчанию."""
    соединение = (
        runtime.connection() if mode is _УМОЛЧАНИЕ else runtime.connection(mode=mode)
    )
    async with соединение as conn:
        return conn


def test_соединение_по_умолчанию_даже_с_репликой_берёт_писателя():
    """Умолчание — безопасная сторона: забыть режим значит прочитать
    свежее, а не устаревшее."""
    runtime = _runtime()
    runtime.pool = FakeConnPool("писатель")
    runtime.read_pool = FakeConnPool("реплика")
    assert asyncio.run(_взяли(runtime)) == "писатель"


def test_реплика_не_настроена_и_STALE_OK_идёт_к_писателю():
    """Реплика — ускорение, а не условие работы: её отсутствие не отказ."""
    runtime = _runtime()
    runtime.pool = FakeConnPool("писатель")
    assert asyncio.run(_взяли(runtime, ReadMode.STALE_OK)) == "писатель"


def test_настроенная_реплика_обслуживает_STALE_OK_и_не_трогает_STRONG():
    runtime = _runtime()
    runtime.pool = FakeConnPool("писатель")
    runtime.read_pool = FakeConnPool("реплика")
    assert asyncio.run(_взяли(runtime, ReadMode.STALE_OK)) == "реплика"
    assert asyncio.run(_взяли(runtime, ReadMode.STRONG)) == "писатель"


def test_недоступная_реплика_не_роняет_чтение(monkeypatch):
    """Настроенный, но лежащий reader — тот самый случай, ради которого
    развилка и написана. Исключения здесь быть не должно."""
    async def _не_поднимается(*args, **kwargs):
        raise OSError("реплика недоступна")

    monkeypatch.setattr(runtime_service.postgres, "create_pool", _не_поднимается)
    runtime = _runtime(
        read_settings=PoolSettings("реплика", 5432, "messenger", "messenger", "")
    )
    runtime.pool = FakeConnPool("писатель")

    assert asyncio.run(_взяли(runtime, ReadMode.STALE_OK)) == "писатель"
    assert runtime.read_pool is None


def test_настройки_чтения_без_хоста_дают_None(monkeypatch):
    """Умолчание — не адрес писателя. Иначе развилка молча читала бы
    с него, и никто бы не заметил, что она не работает."""
    monkeypatch.delenv("DATABASE_READ_HOST", raising=False)
    assert runtime_service.read_pool_settings_from_env() is None


def test_настройки_чтения_наследуют_остальное_от_писателя(monkeypatch):
    """Отличается адрес, а не база: реплика — та же база, другой под."""
    monkeypatch.setenv("DATABASE_READ_HOST", "messenger-db-replica")
    monkeypatch.setenv("DATABASE_NAME", "messenger")
    monkeypatch.setenv("DATABASE_USER", "messenger")
    monkeypatch.delenv("DATABASE_READ_POOL_MAX", raising=False)

    settings = runtime_service.read_pool_settings_from_env()
    assert settings is not None
    assert settings.host == "messenger-db-replica"
    assert settings.database == "messenger"
    # Ёмкость чтения меньше ёмкости записи: сумма обязана оставаться ниже
    # `default_pool_size` PgBouncer, иначе очередь переезжает в пул.
    assert settings.max_size < runtime_service.pool_settings_from_env().max_size
