"""Пользовательские сценарии бесед.

G1-011 превращает конфликт двух встречных запросов в один общий результат:
уникальный индекс выбирает победителя, а проигравший читает его строку.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

import asyncpg

from messenger.domain.authorization import Action, ResourceRef, Subject
from messenger.domain.conversation import Conversation
from messenger.domain.conversation_list import (
    DEFAULT_PAGE_SIZE,
    ActivityCursor,
    ConversationPage,
    validate_activity_cursors,
)
from messenger.domain.errors import Reason
from messenger.domain.ids import ConversationId, UserId, direct_key
from messenger.domain.user import User
from messenger.repositories import conversations, messages
from messenger.services import authorization
from messenger.services.unread import restore_lost_counts


@dataclass(frozen=True, slots=True)
class ConversationListResult:
    """Страница списка бесед и её продолжение.

    Поля `rejection` здесь нет, и это утверждение, а не забывчивость.
    Отбор бесед — он сам и есть право: `cm.user_id` и `cm.left_at IS NULL`
    стоят в `WHERE`, а ресурса, о котором следовало бы спросить у
    авторизации, в запросе нет вовсе. Поэтому нет и `authorize()` — не по
    недосмотру, а потому что решать не о чем: перебрать тут нечего.
    Следствия, которые обязаны быть проверены явно: у маршрута нет ни
    `403`, ни `404`, а у человека без бесед ответ — пустой список, а не
    «не найдено».
    """

    page: ConversationPage = ConversationPage()

    @property
    def ok(self) -> bool:
        # Пустая страница — законный успешный ответ («бесед нет»), а не
        # отсутствие результата. Отказать здесь нечем, но `ok` остаётся:
        # обработчик у всех маршрутов читается одинаково.
        return True

    @property
    def next_cursor(self) -> ActivityCursor | None:
        """Продолжение — последний элемент страницы, не пробная строка.

        Взять курсором `limit + 1`-ю строку нельзя: клиент перескочил бы
        `limit`-й элемент и потерял его навсегда. На коротком списке это
        не воспроизводится, а заметно становится на длинном.

        Отметка берётся из самой строки, а не из часов: курсором годится
        только то значение, по которому Postgres сортировал, — посчитанное
        здесь `now()` не совпало бы с ключом сортировки ни в одной строке
        и вернуло бы ту же страницу второй раз.

        `None` вместо курсора — это и есть конец списка. Отдавать его
        всегда и предлагать клиенту остановиться по «страница короче
        `limit`» нельзя: ровно от этого неверного признака ушла история.
        """
        if not self.page.has_more or not self.page.items:
            return None
        last = self.page.items[-1]
        return ActivityCursor(
            updated_at=last.conversation.updated_at,
            conversation_id=last.conversation.conversation_id,
        )


async def list_conversations(
    conn: asyncpg.Connection,
    *,
    viewer_id: UserId,
    cursor: ActivityCursor | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> ConversationListResult:
    """Список бесед страницами: курсор идёт парой компонентов.

    Удостоверение передаётся идентификатором, а не профилем: обработчику
    `User` нужен, чтобы понять, кто пришёл, а сервису — только `user_id`
    для условия отбора. Полный профиль здесь приглашал бы решение о правах
    внутри сервиса, которого тут быть не должно.

    Составление страницы живёт здесь, а не в репозитории, и это следствие
    правила слоёв, а не вкуса: репозиторию разрешено импортировать только
    `domain` и `telemetry`, то есть позвать другой репозиторий он не может.
    Писать в `conversations.py` собственный SQL по `messages` — тоже нет:
    `_COLUMNS` и `_to_message` в модуле сообщений единственные знают форму
    строки, и копия разошлась бы с оригиналом молча.

    Повторная доменная проверка — как в `list_messages`: правило одно,
    вызывается дважды, а не переписывается здесь своими словами. Негодный
    запрос не должен занимать соединение, поэтому обработчик зовёт её
    первым, до сюда дело доходит уже проверенным.

    Дополнение страницы идёт **двумя** запросами на страницу, а не
    запросом на беседу: и последнее сообщение, и счётчик непрочитанного
    читаются пачкой по идентификаторам. Отдельный поход в базу на каждую
    строку списка — это ровно то read amplification, ради устранения
    которого проекция и заводится.

    Счётчик берётся не «как есть»: `restore_lost_counts` читает проекцию
    пачкой и **восстанавливает из источника истины** те строки, которых
    в ней нет. Потеря проекции не должна менять существенный ответ
    системы — производную и заводят ради права её потерять, — поэтому
    на вопрос, на который источник истины отвечает, список отвечает,
    а не сообщает «неизвестно». Дорогая половина (счёт по беседе)
    достаётся только потерянным строкам, и случается она один раз
    на потерю.

    `get` без умолчания у обоих, но причины разные. У беседы без сообщений
    последнего нет по существу. У счётчика отсутствие ключа теперь значит
    не «проекция потеряна» (это лечит восстановление), а «источник истины
    не даёт числа этому читателю» — он не в составе беседы. Подставить
    вместо отсутствия ноль всё так же нельзя: `0` — уверенное «всё
    прочитано», и оно не должно вставать на место ответа, которого нет.
    """
    validate_activity_cursors(
        before_activity_at=cursor.updated_at if cursor is not None else None,
        before_conversation_id=(
            cursor.conversation_id if cursor is not None else None
        ),
        limit=limit,
    )

    page = await conversations.list_user_conversations(
        conn, user_id=viewer_id, cursor=cursor, limit=limit
    )
    conversation_ids = [
        item.conversation.conversation_id for item in page.items
    ]
    latest = await messages.fetch_latest_by_conversation(
        conn, conversation_ids=conversation_ids
    )
    counts = await restore_lost_counts(
        conn, viewer_id=viewer_id, conversation_ids=conversation_ids
    )
    return ConversationListResult(
        page=ConversationPage(
            # `replace`, а не сборка заново: сущность и участники уже
            # проверены репозиторием, и второй сборкой их можно было бы
            # только испортить.
            items=tuple(
                replace(
                    item,
                    last_message=latest.get(item.conversation.conversation_id),
                    unread_count=counts.get(item.conversation.conversation_id),
                )
                for item in page.items
            ),
            has_more=page.has_more,
        )
    )


@dataclass(frozen=True, slots=True)
class CreateDirectResult:
    conversation: Conversation | None = None
    participants: tuple[User, ...] = field(default_factory=tuple)
    created: bool = False
    rejection: Reason | None = None

    @property
    def ok(self) -> bool:
        return self.conversation is not None and self.rejection is None


async def create_direct(
    conn: asyncpg.Connection, *, actor: User, participant_id: UserId
) -> CreateDirectResult:
    """Создаёт диалог атомарно или возвращает уже существующий.

    Участник передаётся внутренним идентификатором, но субъект всегда берётся
    из проверенного токена. Поэтому тело запроса не может создать беседу
    от имени другого пользователя.
    """
    async with conn.transaction():
        decision = await authorization.authorize(
            conn,
            subject=Subject(actor),
            resource=ResourceRef.user(participant_id),
            action=Action.CREATE_CONVERSATION,
        )
        if not decision.allowed or decision.target_user is None:
            return CreateDirectResult(rejection=decision.reason or Reason.INTERNAL)
        participant = decision.target_user

        key = direct_key(actor.user_id, participant.user_id)
        ensured = await conversations.ensure_direct_conversation(
            conn,
            conversation_id=ConversationId(uuid.uuid4()),
            direct_key=key,
        )
        conversation = ensured.conversation
        if not ensured.created:
            return CreateDirectResult(
                conversation=conversation,
                participants=(actor, participant),
                created=False,
            )

        await conversations.add_member(
            conn,
            conversation_id=conversation.conversation_id,
            user_id=actor.user_id,
        )
        await conversations.add_member(
            conn,
            conversation_id=conversation.conversation_id,
            user_id=participant.user_id,
        )
        return CreateDirectResult(
            conversation=conversation,
            participants=(actor, participant),
            created=True,
        )
