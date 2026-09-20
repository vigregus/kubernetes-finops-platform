"""Сценарий обычного создания direct-беседы; конкурентная гонка — G1-011."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from messenger.domain.authorization import Decision
from messenger.domain.conversation import (
    Conversation,
    ConversationType,
    EnsureConversationResult,
)
from messenger.domain.conversation_list import (
    ActivityCursor,
    ConversationPage,
    ConversationSummary,
)
from messenger.domain.errors import Reason
from messenger.domain.history import InvalidCursor
from messenger.domain.ids import (
    ClientMessageId,
    ConversationId,
    ConversationSeq,
    MessageId,
    UserId,
    direct_key,
)
from messenger.domain.message import Message, MessageKind, MessagePayload
from messenger.domain.unread import UnreadCount
from messenger.domain.user import User, UserSummary
from messenger.services import conversations as service

NOW = datetime(2026, 9, 15, tzinfo=UTC)
ACTOR_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
OTHER_ID = UserId(uuid.UUID("22222222-2222-2222-2222-222222222222"))


class Transaction:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True


class Connection:
    def __init__(self) -> None:
        self.tx = Transaction()

    def transaction(self) -> Transaction:
        return self.tx


def _user(user_id: UserId, *, verified: bool = True, deleted: bool = False) -> User:
    return User(
        user_id=user_id,
        external_id=f"kc-{user_id}",
        display_name="Аня" if user_id == ACTOR_ID else "Борис",
        email=f"{user_id}@example.org",
        email_verified=verified,
        created_at=NOW,
        updated_at=NOW,
        deleted_at=NOW if deleted else None,
    )


def _conversation() -> Conversation:
    return Conversation(
        conversation_id=ConversationId(uuid.uuid4()),
        type=ConversationType.DIRECT,
        direct_key=direct_key(ACTOR_ID, OTHER_ID),
        last_seq=ConversationSeq(0),
        created_at=NOW,
        updated_at=NOW,
    )


def test_неподтверждённый_не_может_начать_беседу(monkeypatch):
    async def _deny(*args, **kwargs):
        return Decision.deny(Reason.EMAIL_UNVERIFIED)

    monkeypatch.setattr(service.authorization, "authorize", _deny)
    result = asyncio.run(
        service.create_direct(
            Connection(), actor=_user(ACTOR_ID, verified=False), participant_id=OTHER_ID
        )
    )
    assert result.rejection is Reason.EMAIL_UNVERIFIED


def test_беседа_с_собой_отклоняется_до_базы(monkeypatch):
    async def _deny(*args, **kwargs):
        return Decision.deny(Reason.SELF_CONVERSATION)

    monkeypatch.setattr(service.authorization, "authorize", _deny)
    result = asyncio.run(
        service.create_direct(Connection(), actor=_user(ACTOR_ID), participant_id=ACTOR_ID)
    )
    assert result.rejection is Reason.SELF_CONVERSATION


@pytest.mark.parametrize("participant", [None, _user(OTHER_ID, deleted=True)])
def test_отсутствующий_или_удалённый_участник_скрыт(monkeypatch, participant):
    async def _deny(*args, **kwargs):
        return Decision.deny(Reason.USER_NOT_FOUND)

    monkeypatch.setattr(service.authorization, "authorize", _deny)
    result = asyncio.run(
        service.create_direct(Connection(), actor=_user(ACTOR_ID), participant_id=OTHER_ID)
    )
    assert result.rejection is Reason.USER_NOT_FOUND


def test_блокировка_в_любую_сторону_запрещает_создание(monkeypatch):
    async def _deny(*args, **kwargs):
        return Decision.deny(Reason.BLOCKED)

    monkeypatch.setattr(service.authorization, "authorize", _deny)
    result = asyncio.run(
        service.create_direct(Connection(), actor=_user(ACTOR_ID), participant_id=OTHER_ID)
    )
    assert result.rejection is Reason.BLOCKED


def test_первый_запрос_атомарно_создаёт_беседу_и_два_членства(monkeypatch):
    conn = Connection()
    expected = _conversation()
    members: list[UserId] = []

    async def _allow(*args, **kwargs):
        return Decision.allow(target_user=_user(OTHER_ID))

    async def _ensure(*args, **kwargs):
        assert kwargs["direct_key"] == direct_key(ACTOR_ID, OTHER_ID)
        return EnsureConversationResult(conversation=expected, created=True)

    async def _add(*args, **kwargs):
        members.append(kwargs["user_id"])

    monkeypatch.setattr(service.authorization, "authorize", _allow)
    monkeypatch.setattr(service.conversations, "ensure_direct_conversation", _ensure)
    monkeypatch.setattr(service.conversations, "add_member", _add)

    result = asyncio.run(
        service.create_direct(conn, actor=_user(ACTOR_ID), participant_id=OTHER_ID)
    )
    assert result.ok and result.created and result.conversation == expected
    assert members == [ACTOR_ID, OTHER_ID]
    assert conn.tx.entered and conn.tx.exited


def test_последовательный_повтор_возвращает_существующую(monkeypatch):
    expected = _conversation()

    async def _allow(*args, **kwargs):
        return Decision.allow(target_user=_user(OTHER_ID))

    async def _ensure(*args, **kwargs):
        return EnsureConversationResult(conversation=expected, created=False)

    async def _не_добавлять(*args, **kwargs):
        raise AssertionError("повтор попытался создать членство заново")

    monkeypatch.setattr(service.authorization, "authorize", _allow)
    monkeypatch.setattr(service.conversations, "ensure_direct_conversation", _ensure)
    monkeypatch.setattr(service.conversations, "add_member", _не_добавлять)

    result = asyncio.run(
        service.create_direct(Connection(), actor=_user(ACTOR_ID), participant_id=OTHER_ID)
    )
    assert result.ok and not result.created and result.conversation == expected
    assert [user.user_id for user in result.participants] == [ACTOR_ID, OTHER_ID]


# --- список бесед ----------------------------------------------------------

FIRST_ID = ConversationId(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
SECOND_ID = ConversationId(uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"))


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
        message_id=MessageId(uuid.uuid4()),
        conversation_id=conversation_id,
        conversation_seq=ConversationSeq(1),
        sender_id=ACTOR_ID,
        client_message_id=ClientMessageId(uuid.uuid4()),
        kind=MessageKind.TEXT,
        payload=MessagePayload(text="привет"),
        created_at=NOW,
    )


def _stub_page(
    monkeypatch,
    page: ConversationPage,
    latest: dict | None = None,
    counts: dict | None = None,
):
    """Подменяет все три выборки и записывает, с чем их позвали."""
    позвали: dict = {}

    async def _page(*args, **kwargs):
        позвали.update(kwargs)
        return page

    async def _latest(*args, **kwargs):
        позвали["latest_ids"] = kwargs["conversation_ids"]
        return latest or {}

    async def _counts(*args, **kwargs):
        # Подменяется **восстановление** счётчиков, а не чтение проекции:
        # список зовёт `restore_lost_counts`, и подмена `fetch_projection`
        # (её внутренностей) не покраснела бы, а перестала бы подменять
        # что-либо — читающая половина пошла бы настоящим запросом
        # в подставленное соединение.
        позвали["restore"] = (
            kwargs["viewer_id"],
            kwargs["conversation_ids"],
        )
        return counts or {}

    monkeypatch.setattr(service.conversations, "list_user_conversations", _page)
    monkeypatch.setattr(service.messages, "fetch_latest_by_conversation", _latest)
    monkeypatch.setattr(service, "restore_lost_counts", _counts)
    return позвали


def test_страница_собирается_из_трёх_выборок(monkeypatch):
    message = _message(FIRST_ID)
    page = ConversationPage(
        items=(_summary(FIRST_ID), _summary(SECOND_ID)), has_more=True
    )
    позвали = _stub_page(
        monkeypatch, page, {FIRST_ID: message}, {FIRST_ID: UnreadCount(2)}
    )

    result = asyncio.run(
        service.list_conversations(Connection(), viewer_id=ACTOR_ID, limit=2)
    )
    assert result.ok
    assert позвали["user_id"] == ACTOR_ID and позвали["cursor"] is None
    # Сообщения спрашиваются ровно по беседам страницы, а не по всей
    # таблице: иначе N+1 вернулся бы через чёрный ход.
    assert позвали["latest_ids"] == [FIRST_ID, SECOND_ID]
    assert result.page.items[0].last_message == message
    assert result.page.items[1].last_message is None
    # Счётчики — тем же правилом и по **спрашивающему**: число
    # принадлежит пользователю, а не беседе, и запрос «по беседам» без
    # `viewer_id` отдал бы чужие числа или потребовал бы второго похода
    # в базу на каждого участника.
    assert позвали["restore"] == (ACTOR_ID, [FIRST_ID, SECOND_ID])
    assert result.page.items[0].unread_count == 2
    assert result.page.items[1].unread_count is None


def test_отсутствие_числа_у_источника_истины_не_превращается_в_ноль(monkeypatch):
    """Ронит подстановку нуля вместо отсутствия.

    `0` — уверенный ответ «всё прочитано», отсутствие числа — «спросить
    не у кого». Подставить одно вместо другого здесь легко: `dict.get`
    с умолчанием `UnreadCount(0)` выглядит заботой о типах, а на деле
    стирает различие, которое клиент обязан видеть (`LIST-003`).

    Отсутствие здесь значит **не** «проекция потеряна»: это лечится
    восстановлением ниже по стеку (`restore_lost_counts`), и строка на
    вопрос, на который источник истины отвечает, до сервиса не доедет.
    Остаётся другой случай, ради которого поле и объявлено необязательным:
    источник истины числа этому читателю не даёт вовсе. Обе причины
    выглядят одинаково — ключа в словаре нет, — и обе обязаны доехать
    до ответа отсутствием, а не нулём.
    """
    page = ConversationPage(items=(_summary(FIRST_ID), _summary(SECOND_ID)))
    _stub_page(monkeypatch, page, counts={FIRST_ID: UnreadCount(0)})

    result = asyncio.run(service.list_conversations(Connection(), viewer_id=ACTOR_ID))

    assert result.page.items[0].unread_count == 0
    assert result.page.items[1].unread_count is None


def test_продолжение_берётся_из_последнего_элемента_парой(monkeypatch):
    # Отметки равны намеренно: у бесед, тронутых одной транзакцией,
    # `now()` совпадает до микросекунды, и именно на этом стыке одиночный
    # курсор теряет или дублирует беседы. Второй компонент обязан уехать
    # вместе с первым.
    page = ConversationPage(
        items=(_summary(FIRST_ID), _summary(SECOND_ID, updated_at=NOW)),
        has_more=True,
    )
    _stub_page(monkeypatch, page)

    result = asyncio.run(
        service.list_conversations(Connection(), viewer_id=ACTOR_ID, limit=1)
    )
    # `has_more` переносится как есть, а не выводится из длины: страница
    # ровно исчерпана, и пересчёт здесь объявил бы конец списка.
    assert result.page.has_more
    assert result.next_cursor == ActivityCursor(
        updated_at=NOW, conversation_id=SECOND_ID
    )


def test_конец_списка_не_даёт_курсора(monkeypatch):
    page = ConversationPage(items=(_summary(FIRST_ID),), has_more=False)
    _stub_page(monkeypatch, page)

    result = asyncio.run(service.list_conversations(Connection(), viewer_id=ACTOR_ID))
    assert result.next_cursor is None


def test_пустая_страница_не_даёт_курсора(monkeypatch):
    # У человека без бесед ответ — пустой список, а не отказ. Курсора
    # здесь нет и браться ему неоткуда: элементов нет вовсе. Счётчики
    # спрашиваются с пустым списком, а не пропускаются: ветка «нечего
    # спрашивать» живёт ниже по стеку (`{}` без похода в базу), и второе
    # такое решение здесь разошлось бы с первым.
    позвали = _stub_page(monkeypatch, ConversationPage())

    result = asyncio.run(service.list_conversations(Connection(), viewer_id=ACTOR_ID))
    assert result.page.items == () and not result.page.has_more
    assert result.next_cursor is None
    assert позвали["latest_ids"] == []
    assert позвали["restore"] == (ACTOR_ID, [])


def test_курсор_доезжает_до_репозитория_целиком(monkeypatch):
    # Курсор передаётся как есть, а не «укрепляется» и не пересобирается:
    # сервису нечего добавить к паре, которую собрал вызывающий. Проверка
    # ниже по стеку — не про форму курсора, а про то, что оба компонента
    # доехали вместе: репозиторий сравнивает именно пару.
    позвали = _stub_page(monkeypatch, ConversationPage())
    cursor = ActivityCursor(updated_at=NOW, conversation_id=FIRST_ID)

    asyncio.run(
        service.list_conversations(
            Connection(), viewer_id=ACTOR_ID, cursor=cursor, limit=10
        )
    )
    assert позвали["cursor"] == cursor and позвали["limit"] == 10
    assert позвали["cursor"].conversation_id == FIRST_ID


def test_негодный_размер_страницы_не_доходит_до_репозитория(monkeypatch):
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("репозиторий позван с негодным размером страницы")

    monkeypatch.setattr(service.conversations, "list_user_conversations", _не_вызывать)
    with pytest.raises(InvalidCursor):
        asyncio.run(
            service.list_conversations(Connection(), viewer_id=ACTOR_ID, limit=0)
        )


def test_наивная_отметка_отвергается_и_сервисом(monkeypatch):
    # Та же проверка, что в обработчике, и это не дублирование: обработчик
    # зовёт её рано, чтобы негодный запрос не занял соединение, но сервис
    # обязан остаться правым сам по себе — его зовут не только из HTTP.
    # Отметка приходит в паре: одиночную отвергает правило выше, и тест
    # на время зеленел бы по чужой причине.
    async def _не_вызывать(*args, **kwargs):
        raise AssertionError("репозиторий позван с наивной отметкой")

    monkeypatch.setattr(service.conversations, "list_user_conversations", _не_вызывать)
    with pytest.raises(InvalidCursor):
        asyncio.run(
            service.list_conversations(
                Connection(),
                viewer_id=ACTOR_ID,
                cursor=ActivityCursor(
                    updated_at=datetime(2026, 9, 15),
                    conversation_id=FIRST_ID,
                ),
                limit=10,
            )
        )


def test_половина_курсора_непредставима_типом():
    # «Второй компонент без первого» отвергнуть в сервисе нечем, и это
    # не пробел: оба поля `ActivityCursor` обязательны, поэтому такого
    # состояния нельзя даже собрать — правило живёт на границе HTTP,
    # где параметры приходят по отдельности. Проверка нужна здесь
    # именно потому, что через сервис половина пары не проходит: если
    # она когда-нибудь пройдёт, узнать об этом будет неоткуда.
    with pytest.raises(TypeError):
        ActivityCursor(updated_at=NOW)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ActivityCursor(conversation_id=FIRST_ID)  # type: ignore[call-arg]


def test_отказ_в_сервисе_отсутствует_по_построению():
    # Список отбирает субъект, и отбор — он же и право: ресурса, о котором
    # следовало бы спросить авторизацию, в запросе нет. Поэтому у
    # результата нет поля `rejection`, а `ok` истинно на пустой странице.
    result = service.ConversationListResult()
    assert result.ok and not hasattr(result, "rejection")
