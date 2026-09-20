"""Предикат непрочитанного и разбор события: по тесту на каждую клаузу.

Проверки названы по дефекту, который каждая ловит. Клауза предиката — это
не строка кода, которую «и так видно»: у неё нет своего наблюдаемого
эффекта в тестах соседних слоёв, и выпавшая клауза не роняет ничего —
счётчик остаётся правдоподобным числом. Поэтому здесь по одному тесту
на каждую из трёх, и отдельно на разбор, где ошибка тоже молчалива:
`true` вместо номера даёт событие, которого не было.
"""
from __future__ import annotations

import uuid

import pytest

from messenger.domain.ids import ConversationSeq, UserId
from messenger.domain.unread import (
    EVENT_TYPE,
    MAX_UNREAD_SEQ,
    NO_SEQ,
    InvalidEvent,
    UnreadOutcomeKind,
    affected_users,
    counts_as_unread,
    event_type_of,
    is_foreign,
    parse_message_created,
)

ANYA = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
BORIS = UserId(uuid.UUID("22222222-2222-2222-2222-222222222222"))
CONVERSATION = uuid.UUID("33333333-3333-3333-3333-333333333333")
MESSAGE = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _body(**overrides: object) -> dict[str, object]:
    """Тело события, каким его отдаёт адаптер: разобранным, но не проверенным.

    `recipient_ids` здесь **есть** — в отличие от схемы, где поле
    необязательно. Так различаются два случая: «состав не пришёл»
    и «состав пуст», и проверяются они отдельно.
    """
    values: dict[str, object] = {
        "event_id": str(MESSAGE),
        "event_type": EVENT_TYPE,
        "conversation_id": str(CONVERSATION),
        "conversation_seq": 5,
        "sender_id": str(BORIS),
        "recipient_ids": [str(ANYA)],
    }
    values.update(overrides)
    return values


def _unread(**overrides: object) -> bool:
    values: dict[str, object] = {
        "sender_id": BORIS,
        "reader_id": ANYA,
        "conversation_seq": ConversationSeq(5),
        "last_read_seq": ConversationSeq(4),
        "applied_through_seq": ConversationSeq(5),
    }
    return counts_as_unread(**{**values, **overrides})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Предикат: три клаузы и ни одной лишней
# ---------------------------------------------------------------------------


def test_чужое_непрочитанное_считается():
    # Основание: без него все проверки ниже зелены на предикате,
    # возвращающем `False` всегда.
    assert _unread() is True


def test_своё_сообщение_не_считается_непрочитанным():
    """Ронит удаление клаузы «чужое» из предиката.

    Без неё отправитель считал бы непрочитанными собственные сообщения,
    и счётчик у него рос бы на каждом отправленном — то есть ровно
    наоборот тому, что видит человек (`UNR-003`).
    """
    assert _unread(reader_id=BORIS) is False


def test_сообщение_стёртого_автора_считается_непрочитанным():
    """Ронит `<>` вместо `IS DISTINCT FROM`.

    `messages.sender_id` потерял `NOT NULL` в `0002_erasure.sql`, и в
    SQL обычное сравнение с `NULL` даёт `NULL`: строка не попадает
    ни в свои, ни в чужие, и счётчик собеседника молча уменьшается.
    В Python разница не видна на `!=`, но видна на `is_foreign`, где
    `None` разобран явной веткой, — и тест держит именно её.
    """
    assert _unread(sender_id=None) is True
    assert is_foreign(None, reader_id=ANYA) is True
    assert is_foreign(BORIS, reader_id=ANYA) is True
    assert is_foreign(BORIS, reader_id=BORIS) is False


def test_прочитанное_не_считается_непрочитанным():
    """Ронит удаление нижней границы.

    Квитанция сдвигает `last_read_seq` вперёд и **включает** свой номер:
    прочитано «до пяти» значит, что пятое прочитано. Строгое сравнение
    в предикате оставило бы пятое в счёте навсегда, и «открыл беседу»
    не обнуляло бы счётчик у того, кто прочитал последнее сообщение.
    """
    assert _unread(last_read_seq=ConversationSeq(5)) is False
    assert _unread(last_read_seq=ConversationSeq(9)) is False
    assert _unread(last_read_seq=ConversationSeq(3)) is True


def test_сообщение_выше_чекпойнта_не_считается():
    """Ронит удаление верхней границы.

    Сообщение уже лежит в Postgres, но потребителем ещё не применено.
    Без границы квитанция, пришедшая вперёд потребителя, учла бы его,
    а потребитель, догнав, прибавил бы единицу по устаревшему
    `last_read_seq`: одно сообщение, два счётчика.
    """
    assert _unread(conversation_seq=ConversationSeq(7)) is False
    assert _unread(
        conversation_seq=ConversationSeq(7), applied_through_seq=ConversationSeq(7)
    ) is True


# ---------------------------------------------------------------------------
# Разбор события
# ---------------------------------------------------------------------------


def test_разбор_собирает_все_поля():
    event = parse_message_created(_body())
    assert event.event_id == MESSAGE
    assert event.conversation_id == CONVERSATION
    assert event.conversation_seq == 5
    assert event.sender_id == BORIS
    assert event.recipient_ids == (ANYA,)


def test_событие_без_номера_отвергается():
    """Ронит выбрасывание проверки обязательного поля.

    Номер — единственное, чем событие отличается от повторного, и
    `None` вместо него уронил бы обработчик сравнением с числом,
    то есть `500` в потребителе вместо пропуска и записи в журнал.
    """
    with pytest.raises(InvalidEvent) as caught:
        parse_message_created(_body(conversation_seq=None))
    assert caught.value.code == "missing_field"


def test_нулевой_и_отрицательный_номер_отвергаются():
    """Ронит отсутствие проверки `>= 1`.

    Чекпойнт начинается с нуля, а контракт объявляет `minimum: 1`.
    Пропущенный ноль лёг бы в чекпойнт как применённое событие
    с номером, которого у сообщения быть не может.
    """
    for bad in (0, -1):
        with pytest.raises(InvalidEvent) as caught:
            parse_message_created(_body(conversation_seq=bad))
        assert caught.value.code == "bad_seq"


def test_номер_выше_предела_int64_отвергается():
    # Тот же случай, что у курсора истории: `asyncpg` отдаст `DataError`
    # на приведении к `bigint`, и ошибка клиента станет ошибкой сервера.
    with pytest.raises(InvalidEvent) as caught:
        parse_message_created(_body(conversation_seq=MAX_UNREAD_SEQ + 1))
    assert caught.value.code == "bad_seq"


def test_булево_не_считается_номером():
    """Ронит проверку `isinstance(value, int)` без оговорки про `bool`.

    `bool` — подкласс `int`, поэтому `"conversation_seq": true` прошло бы
    как номер `1`: событие, которого не было, и чекпойнт, сдвинутый на
    несуществующее сообщение.
    """
    with pytest.raises(InvalidEvent) as caught:
        parse_message_created(_body(conversation_seq=True))
    assert caught.value.code == "missing_field"


def test_негодный_идентификатор_отвергается():
    with pytest.raises(InvalidEvent) as caught:
        parse_message_created(_body(sender_id="не-идентификатор"))
    assert caught.value.code == "bad_uuid"


def test_отсутствие_состава_отличимо_от_пустого_состава():
    """Разные вещи, и различие решает, идти ли в источник истины.

    Поле объявлено необязательным, поэтому «состав не пришёл» — не отказ,
    а повод пересобрать проекцию из `conversation_members`. Пустой список —
    «получателей не было», и пересборка не добавит ничего.
    """
    assert parse_message_created(_body(recipient_ids=None)).recipient_ids is None
    assert parse_message_created(_body(recipient_ids=[])).recipient_ids == ()


def test_состав_не_списком_отвергается():
    with pytest.raises(InvalidEvent) as caught:
        parse_message_created(_body(recipient_ids="Аня"))
    assert caught.value.code == "bad_recipients"


def test_тип_события_читается_без_исключения():
    # Тип нужен отбору, а не разбору: постороннее событие пропускается
    # молча, и исключение здесь заставило бы воркер разбирать текст,
    # чтобы отличить «чужое» от «негодного».
    assert event_type_of(_body()) == EVENT_TYPE
    assert event_type_of({"event_type": "message.content"}) == "message.content"
    assert event_type_of({}) == ""
    assert event_type_of({"event_type": 5}) == ""


# ---------------------------------------------------------------------------
# Состав затронутых
# ---------------------------------------------------------------------------


def test_отправитель_входит_в_затронутых_последним():
    """Строка проекции нужна и ему — нулевая, но существующая.

    Список бесед читает счётчики пользователя, и у отправителя счётчик
    тоже есть. Без его строки собственные беседы показывали бы
    «неизвестно» до первой квитанции, то есть всегда у того, кто только
    пишет. Приращение для него при этом ноль — это делает предикат,
    а не отдельная ветка.
    """
    users = affected_users(recipients=(ANYA,), sender_id=BORIS)
    assert users == (ANYA, BORIS)


def test_повтор_в_составе_не_даёт_двух_строк():
    # `ON CONFLICT` в пределах одного оператора два вхождения одного
    # ключа не переживает: вставка упала бы на «cannot affect row a second
    # time», то есть на событии с задвоенным получателем потребитель встал бы.
    users = affected_users(recipients=(ANYA, ANYA, BORIS), sender_id=BORIS)
    assert users == (ANYA, BORIS)


def test_пустой_состав_и_отправитель_дают_одного_затронутого():
    # Беседа, из которой все вышли, доходит до счётчика одним отправителем,
    # и это законный случай, а не отсутствие данных.
    assert affected_users(recipients=(), sender_id=BORIS) == (BORIS,)


def test_ноль_прочитанного_это_значение_а_не_отсутствие():
    # `NO_SEQ` существует как имя для «не применено ничего», и предикат
    # на нём обязан давать истину: первый номер в беседе — единица,
    # и она выше нуля.
    assert _unread(last_read_seq=NO_SEQ) is True


def test_исходов_ровно_пять():
    # Перечень исходов — часть наблюдаемого поведения: по нему пишутся
    # записи журнала и строки каталога событий. Шестой исход, добавленный
    # без правки каталога, прошёл бы проверку журналов молча.
    assert {kind.value for kind in UnreadOutcomeKind} == {
        "applied",
        "duplicate",
        "rebuilt",
        "ignored",
        "invalid",
    }
