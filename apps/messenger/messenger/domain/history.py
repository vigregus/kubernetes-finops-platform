"""Страница истории: диапазон, направление и запрещённые сочетания курсоров.

Страница — это результат запроса, а не доменная сущность рядом с `Message`.
Но живёт она здесь, а не в сервисе, потому что её собирает **репозиторий**:
признак «есть ещё» выводится из выборки `limit + 1`, а `limit + 1` знает
только тот, кто выполняет запрос, и ему по правилам слоёв доступен один
`domain`.

Границы — предмет контракта, а не удобства: `before_seq` строго исключает
собственный номер, `after_seq` строго исключает свой, а `through_seq`
включает свой. Поэтому здесь же лежит и проверка сочетаний, и она вызывается
дважды — рано в обработчике и в сервисе, — как `validate_message_payload`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from messenger.domain.message import Message

# Пределы протокола, а не транспорта: та же природа, что у `MAX_TEXT_LENGTH`
# в `domain/message.py`. Контракт объявляет `Limit` как 1..100.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# Асимметрия минимумов не опечатка. Ноль для `before_seq` дал бы `seq < 0`,
# то есть всегда пустую страницу, — это ошибка клиента. Для `after_seq` ноль
# законен и означает «не видел ещё ничего».
MIN_BEFORE_SEQ = 1
MIN_AFTER_SEQ = 0


class HistoryDirection(str, Enum):
    """Куда листается история.

    Различие не косметическое: от него зависит, как называется курсор
    продолжения. Угадывать это по «задан ли `after_seq`» в третьем месте
    не нужно — направление посчитано один раз.
    """

    # Новые первыми: обычное листание вглубь.
    BACKWARD = "backward"
    # По возрастанию: это догрузка после переподключения, а не листание.
    FORWARD = "forward"


@dataclass(frozen=True, slots=True)
class MessagePage:
    """Страница истории. Направление в неё не входит — оно снаружи.

    `has_more` вычислен репозиторием по лишней строке и здесь только
    переносится: сервис не может его пересчитать, не сделав второй проход.
    """

    items: tuple[Message, ...] = ()
    has_more: bool = False


def validate_cursors(
    *,
    before_seq: int | None,
    after_seq: int | None,
    through_seq: int | None,
    limit: int,
) -> None:
    """Запрещённые сочетания курсоров. Отказ — `ValueError`, как у payload.

    Равенство `after_seq` и `through_seq` ошибкой **не** считается: это
    пустой снимок и обычный ответ на повтор. Запрет стоит на строгом
    неравенстве, и перепутать его с нестрогим — значит наказывать клиента
    за повтор запроса после обрыва связи.
    """

    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise ValueError(
            f"размер страницы должен быть от 1 до {MAX_PAGE_SIZE}, а не {limit}"
        )
    if before_seq is not None and after_seq is not None:
        raise ValueError(
            "before_seq и after_seq задают разные направления — нужен один"
        )
    if through_seq is not None and after_seq is None:
        raise ValueError("through_seq имеет смысл только вместе с after_seq")
    if before_seq is not None and before_seq < MIN_BEFORE_SEQ:
        raise ValueError(f"before_seq не может быть меньше {MIN_BEFORE_SEQ}")
    if after_seq is not None and after_seq < MIN_AFTER_SEQ:
        raise ValueError(f"after_seq не может быть меньше {MIN_AFTER_SEQ}")
    if through_seq is not None and through_seq < MIN_AFTER_SEQ:
        raise ValueError(f"through_seq не может быть меньше {MIN_AFTER_SEQ}")
    if (
        through_seq is not None
        and after_seq is not None
        and through_seq < after_seq
    ):
        raise ValueError(
            f"through_seq ({through_seq}) меньше after_seq ({after_seq}): "
            "такого диапазона не существует"
        )
