"""Чтение истории беседы: право, направление, неподвижная точка синхронизации.

Сервис ничего не решает о том, откуда читать. Он называет режим
согласованности (`ReadMode`), а соединение под этот режим выдаёт `Runtime`;
SQL о писателе и реплике не знает вовсе. Шов проходит здесь, а не в
репозитории, именно поэтому: репозиторий получает готовый `asyncpg.Connection`
и не может ошибиться в выборе — ошибиться в выборе можно только там, где
он делается.
"""
from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from messenger.domain.authorization import Action, ResourceRef, Subject
from messenger.domain.errors import Reason, Visibility
from messenger.domain.history import (
    DEFAULT_PAGE_SIZE,
    HistoryDirection,
    MessagePage,
    validate_bound,
    validate_cursors,
)
from messenger.domain.ids import ConversationId, ConversationSeq
from messenger.domain.user import User
from messenger.repositories import conversations, messages
from messenger.services import authorization
from messenger.services.runtime import ReadMode


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """Результат чтения истории: страница, снимок и курсоры продолжения.

    `visibility` здесь единственное такое место среди сервисов. Контракт
    объявляет на этом маршруте `403`, а `to_problem` по умолчанию отдаёт
    `404` (`Visibility.HIDDEN`) — и это правильно для всех, кто сегодня
    зовёт его с умолчанием. Но право выбрать видимость принадлежит тому,
    кто знает, пришёл субъект по своей ссылке или подобрал идентификатор
    (`domain/errors.py`), а знает это сервис: `Decision` уже несёт значение.
    Выбросить его здесь — значит сделать объявленный `403` недостижимым
    навсегда, и обнаружится это при первом же разборе чужой беседы.
    """

    page: MessagePage = MessagePage()
    sync_to_seq: ConversationSeq | None = None
    direction: HistoryDirection = HistoryDirection.BACKWARD
    rejection: Reason | None = None
    visibility: Visibility = Visibility.HIDDEN

    @property
    def ok(self) -> bool:
        # Не `page is not None`: пустая страница — законный успешный ответ
        # («снимок есть, и он пуст»), а не отсутствие результата.
        return self.rejection is None

    def _continuation(self) -> ConversationSeq | None:
        """Курсор продолжения — последний элемент страницы, не первый.

        Взять пробную строку из `limit + 1` нельзя: клиент перескочил бы
        `limit`-й элемент и потерял его навсегда. Пустая страница и
        исчерпанный диапазон дают `null` — и `IndexError` здесь означал бы
        `500` вместо пустого ответа.
        """
        if not self.page.has_more or not self.page.items:
            return None
        return self.page.items[-1].conversation_seq

    @property
    def next_before_seq(self) -> ConversationSeq | None:
        """Продолжение листания назад. У догрузки вперёд его нет."""
        if self.direction is not HistoryDirection.BACKWARD:
            return None
        return self._continuation()

    @property
    def next_after_seq(self) -> ConversationSeq | None:
        """Продолжение догрузки вперёд. У листания назад его нет."""
        if self.direction is not HistoryDirection.FORWARD:
            return None
        return self._continuation()


def read_mode(
    *, before_seq: int | None, after_seq: int | None
) -> ReadMode:
    """Какой согласованности требует этот запрос.

    Правило по типу запроса, а не по возрасту или номеру: страница,
    которую пользователь только что листал, обязана содержать его же
    сообщение (DB-001), а глубокая пагинация в старьё отставания не
    замечает — там оно на секунды и никого не касается.

    `through_seq` в сигнатуре нет намеренно, и это не упущение. Соблазн
    «курсор есть — значит реплика» сильный, а он неверен: `through_seq`
    лишь продолжает уже начатую синхронизацию, и её страницы обязаны
    читаться так же строго, как первая. Не приняв параметра, функция не
    даёт это правило нарушить — нарушение потребовало бы сначала
    расширить её подпись.

    Назвать требование и получить его — разные вещи, и здесь это важно:
    `STALE_OK` говорит «отставание допустимо», а не «читай с реплики».
    Решает `Runtime`, и сегодня он удовлетворяет `STALE_OK` писателем —
    причина и условие включения реплики записаны в `Runtime._pool_for`.
    Поэтому `read_mode` не «указывает, где читать», и читать его так
    нельзя.
    """
    if before_seq is not None and after_seq is None:
        return ReadMode.STALE_OK
    return ReadMode.STRONG


async def list_messages(
    conn: asyncpg.Connection,
    *,
    viewer: User,
    conversation_id: ConversationId,
    before_seq: ConversationSeq | None = None,
    after_seq: ConversationSeq | None = None,
    through_seq: ConversationSeq | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> HistoryResult:
    """Страница истории — назад по `before_seq` или вперёд по `after_seq`.

    Направление определяется наличием `after_seq`, а не тем, что удобно
    вызвавшему: это две разные гарантии. Назад — листание, устойчивое к
    дописи в голову (HIST-002). Вперёд — восстановление пропущенного,
    и оно обязано остановиться на замороженной границе.
    """
    # Та же доменная проверка, что и в обработчике, но раньше: негодный
    # запрос не должен занимать соединение с базой. Правило одно —
    # вызывается дважды, а не переписывается здесь своими словами.
    validate_cursors(
        before_seq=before_seq,
        after_seq=after_seq,
        through_seq=through_seq,
        limit=limit,
    )

    decision = await authorization.authorize(
        conn,
        subject=Subject(viewer),
        resource=ResourceRef.conversation(conversation_id),
        action=Action.READ_CONVERSATION,
    )
    if not decision.allowed:
        return HistoryResult(
            rejection=decision.reason or Reason.INTERNAL,
            visibility=decision.visibility,
        )

    if after_seq is None:
        page = await messages.fetch_page_backward(
            conn,
            conversation_id=conversation_id,
            before_seq=before_seq,
            limit=limit,
        )
        # Снимка нет: листание — не синхронизация, и границы у него нет.
        # `None` здесь не «не удалось прочитать голову», а «границы не
        # существует», и клиент обязан видеть разницу.
        return HistoryResult(
            page=page,
            direction=HistoryDirection.BACKWARD,
            visibility=decision.visibility,
        )

    # Голова читается до страницы, и это не стиль, а порядок гарантии.
    # `last_seq` наблюдаемо тогда и только тогда, когда наблюдаемо
    # сообщение с этим номером: обе записи делает одна транзакция, державшая
    # блокировку строки беседы. Заморозили границу первой — читаем
    # `seq <= границы` второй, и дыр нет. В обратном порядке между двумя
    # чтениями помещается чужое сообщение `k`: страница отдаёт
    # `has_more=false` до `k−1`, `sync_to_seq` возвращается равным `k`,
    # клиент считает синхронизацию завершённой и не видит `k` ни здесь,
    # ни в потоке.
    #
    # Читается она всегда, а не только когда граница берётся из неё, и
    # вторая причина тому — не порядок, а предел: курсор выше головы
    # отвергается (`validate_bound`). Без этого `after_seq = 20` при голове
    # 15 отдаёт `200` с `sync_to_seq = 15`, то есть синхронизацию **назад**,
    # и клиент с разошедшимся курсором считает себя догнавшим и больше
    # не спросит.
    head = await conversations.fetch_last_seq(conn, conversation_id=conversation_id)
    # Беседа исчезла между решением о доступе и чтением. Отдать пустую
    # страницу с `sync_to_seq = null` честнее, чем `None` в поле,
    # которое обязано быть числом: клиент прочтёт это как «снимка не
    # было», а не как «синхронизация завершена на нуле».
    if head is None:
        return HistoryResult(
            direction=HistoryDirection.FORWARD,
            rejection=Reason.CONVERSATION_NOT_FOUND,
            visibility=decision.visibility,
        )
    validate_bound(after_seq=after_seq, through_seq=through_seq, head=head)

    # Границей остаётся эхо `through_seq`, а не только что прочитанная
    # голова: контракт — «менять его по дороге нельзя, иначе снимок поедет».
    # Прочитанная голова гонки не возвращает именно потому, что ничего не
    # замораживает: она отвергает то, чего ещё нет, и не подменяет собой
    # то, что первый запрос уже назвал.
    bound = through_seq if through_seq is not None else head

    page = await messages.fetch_page_forward(
        conn,
        conversation_id=conversation_id,
        after_seq=after_seq,
        through_seq=bound,
        limit=limit,
    )
    return HistoryResult(
        page=page,
        sync_to_seq=bound,
        direction=HistoryDirection.FORWARD,
        visibility=decision.visibility,
    )
