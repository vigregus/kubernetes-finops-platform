"""Список бесед: курсор активности, страница и их правила.

Отдельным модулем, а не рядом с сущностью в `domain/conversation.py`, по
тому же основанию, по которому `MessagePage` живёт в `domain/history.py`,
а не в `domain/message.py`: страница — это результат запроса, а не
сущность. Сущность отвечает на вопрос «что такое беседа», страница —
«как выглядит её выдача».

Ключ сортировки — пара `(updated_at DESC, conversation_id DESC)`, и курсор
повторяет его целиком. Одной отметки времени мало, и причина не в
небрежности данных: `now()` в Postgres — время **транзакции**, а не
момента, поэтому беседы, созданные или тронутые одной транзакцией,
получают одинаковый `updated_at` до микросекунды. Строгое `<` по одной
колонке теряет такую группу на стыке страниц целиком, нестрогое — отдаёт
её второй раз. Второй компонент уникален, поэтому пара задаёт полный
порядок, а сравнение пары с префиксом — один предикат вместо двух.

Границы — предмет контракта, а не удобства, поэтому правила лежат здесь
и вызываются дважды: рано в обработчике (негодная строка запроса не должна
занимать соединение) и в сервисе, как `validate_cursors` в истории.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from messenger.domain.conversation import Conversation
from messenger.domain.history import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    InvalidCursor,
    validate_limit,
)
from messenger.domain.ids import ConversationId
from messenger.domain.message import Message
from messenger.domain.user import UserSummary

# Переиспользуются, а не объявляются заново: `Limit` в контракте один на
# оба страничных маршрута, и второй набор констант разошёлся бы с первым.
# Здесь они названы только для того, чтобы читающий модуль не ходил
# за ними в соседний.
__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "ActivityCursor",
    "ConversationPage",
    "ConversationSummary",
    "validate_activity_cursors",
]


@dataclass(frozen=True, slots=True)
class ActivityCursor:
    """Точка продолжения списка. Второй компонент — необязательный.

    `conversation_id = None` — законный одиночный курсор «строго старше
    этой отметки». Он не «пара с пустым идентификатором» и не должен быть
    ею: смысл двух вариантов разный, и в типе это видно. Требовать пару
    значило бы сузить уже объявленное контрактом поведение — маршрут
    отвечает `400` на то, что объявлено, то есть ломает клиента, который
    этим параметром пользуется. Поэтому одиночный курсор жив, а наш
    собственный `next_*` всегда отдаёт пару.
    """

    updated_at: datetime
    conversation_id: ConversationId | None = None


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """Беседа как её видит список: сущность, участники, последнее сообщение.

    Сущность лежит целой, а не разобранной на поля, и это не лень.
    `Conversation` проверяет в `__post_init__` согласованность типа
    и `direct_key`, и разобранная по полям копия проверку бы потеряла —
    строка из базы незаметно превратилась бы в невозможное состояние.

    `last_message` необязателен по существу, а не по вкусу: беседа без
    сообщений существует с первой же транзакции создания, и выдумывать
    ей сообщение-заглушку значило бы соврать клиенту о последнем событии
    в беседе.
    """

    conversation: Conversation
    participants: tuple[UserSummary, ...]
    last_message: Message | None = None


@dataclass(frozen=True, slots=True)
class ConversationPage:
    """Страница списка. `has_more` вычислен репозиторием по лишней строке.

    Сервис не может его пересчитать, не сделав второй проход, поэтому
    признак переносится как есть, а не выводится из длины списка:
    ровно исчерпанная страница (`len(items) == limit`) объявила бы «есть
    ещё», и клиент сходил бы за пустой страницей.
    """

    items: tuple[ConversationSummary, ...] = ()
    has_more: bool = False


def validate_activity_cursors(
    *,
    before_activity_at: datetime | None,
    before_conversation_id: ConversationId | None,
    limit: int,
) -> None:
    """Запрещённые сочетания курсоров списка. Отказ — `InvalidCursor`.

    Имя отличается от `validate_cursors` истории намеренно: правила
    разные (там номера, здесь отметка и идентификатор), а одинаковое имя
    для двух разных наборов в двух модулях приглашает алиас в импорте
    и вопрос «а какая из них здесь».

    Здесь только то, что видно по одной строке запроса, — функция обязана
    отвечать без базы, потому что вызывается рано.

    Верхней границы у отметки нет, и это не упущение. `before_seq` в
    истории ограничен сверху головой беседы, потому что номера выше
    головы не существует вовсе; а «позже, чем сейчас» — законная граница:
    она отдаёт весь список, как `before_seq` выше головы отдаёт всю
    историю. Отвергать её значило бы завести правило, которого никто
    не просил.
    """
    validate_limit(limit)
    if before_conversation_id is not None and before_activity_at is None:
        raise InvalidCursor(
            "before_conversation_id имеет смысл только вместе "
            "с before_activity_at: сам по себе он границы не задаёт"
        )
    # Наивная отметка отвергается, и это не педантизм. `format: date-time`
    # в контракте — это RFC 3339, где смещение обязательно. А `asyncpg`
    # для `timestamptz` зовёт `obj.astimezone(utc)` без проверки на
    # наивность (pgproto/codecs/datetime.pyx), и `astimezone` для наивного
    # значения берёт **местную** зону процесса. Ответ был бы посчитан
    # не на тот вопрос, который клиент задал, и узнал бы он об этом
    # не скоро: в контейнере зона UTC, и расхождение вылезло бы только
    # на машине с другой зоной.
    if before_activity_at is not None and before_activity_at.tzinfo is None:
        raise InvalidCursor(
            "before_activity_at без смещения часового пояса: "
            "непонятно, в какой зоне его читать"
        )
