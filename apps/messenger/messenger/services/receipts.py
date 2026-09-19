"""Запись квитанции: право, голова беседы и одно состояние вместо двух.

**Только запись.** Квитанция не рассылается: ни строки в `outbox`, ни
топика Kafka, ни публикации в Centrifugo. Схемы события `receipt.*` не
существует, канала под него нет, и выдумать его здесь значило бы выпустить
в провод событие без владельца. Оповещение собеседника — отдельная задача,
и она не входит в этот гейт.

Транзакции сервис не открывает, и это отличие от пути сообщения осознанное.
`send_message` берёт `conn.transaction()`, потому что там несколько записей,
а проверка членства **и есть** блокировка строки беседы: вне транзакции
между проверкой и вставкой помещается гонка, и в беседу, из которой только
что вышли, уедет сообщение. У квитанции запись одна, и она сама себе
атомарная единица. Больше того, обёртка создала бы ложное впечатление: на
READ COMMITTED каждый оператор транзакции получает собственный снимок,
поэтому чтение головы и запись в общей обёртке **не** увидели бы
согласованного состояния — гарантии, ради которой обёртку обычно и берут,
здесь нет. Правило простое: транзакция там, где записей несколько.
"""
from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from messenger.domain.authorization import Action, ResourceRef, Subject
from messenger.domain.errors import Reason, Visibility
from messenger.domain.ids import ConversationId
from messenger.domain.receipts import (
    ReadState,
    Receipts,
    state_of,
    validate_receipt_bound,
    validate_receipts,
)
from messenger.domain.user import User
from messenger.repositories import conversations, read_states
from messenger.services import authorization


@dataclass(frozen=True, slots=True)
class SetReceiptsResult:
    """Состояние после применения либо отказ. Никогда то и другое сразу.

    `visibility` несут отказы авторизации: сервис знает, пришёл субъект по
    своей ссылке или подобрал идентификатор, и выбрасывать это знание здесь
    значило бы потерять его навсегда.
    """

    state: ReadState | None = None
    rejection: Reason | None = None
    visibility: Visibility = Visibility.HIDDEN

    @property
    def ok(self) -> bool:
        return self.state is not None and self.rejection is None


async def set_receipts(
    conn: asyncpg.Connection,
    *,
    viewer: User,
    conversation_id: ConversationId,
    receipts: Receipts,
) -> SetReceiptsResult:
    """Применяет квитанцию и возвращает состояние, а не присланное.

    Ответ на отставшую квитанцию несёт **текущее** значение: этим клиент
    и отличает «применено» от «проигнорировано». Присланное возвращать
    нельзя — клиент счёл бы применённым то, что отвергнуто монотонностью.

    Порядок шагов не переставляется. Проверка пределов идёт первой, потому
    что негодная квитанция не должна занимать соединение с базой. Право
    проверяется **до** чтения головы: неучастник не должен получить
    прочитанную за него голову беседы, и первым он обязан получить отказ,
    а не отказ после работы. Граница — до нормализации, иначе квитанция
    `read_seq` выше головы была бы отвергнута под именем `delivered_seq`.

    Голова читается соединением писателя, и это несущее условие, а не
    аккуратность. Проверка границы односторонняя (`≤ head`), голова не
    убывает, а номер `k` клиент узнаёт только из коммита, сделавшего `k`
    наблюдаемым, — поэтому отвергнуть честную квитанцию эта проверка не
    может. С отставшей реплики довод рассыпается: её `last_seq` меньше
    головы, и квитанция о только что полученном сообщении получила бы
    ложный отказ. Режим соединения здесь не выбирается намеренно — умолчание
    и есть писатель; если однажды появится соблазн увести запись в
    `STALE_OK`, этот абзац и есть ответ.
    """
    validate_receipts(receipts)

    decision = await authorization.authorize(
        conn,
        subject=Subject(viewer),
        resource=ResourceRef.conversation(conversation_id),
        action=Action.WRITE_CONVERSATION,
    )
    if not decision.allowed:
        return SetReceiptsResult(
            rejection=decision.reason or Reason.INTERNAL,
            visibility=decision.visibility,
        )

    head = await conversations.fetch_last_seq(conn, conversation_id=conversation_id)
    if head is None:
        # Беседа исчезла между решением о праве и чтением головы. Сегодня
        # недостижимо — беседы ничто не удаляет, — но `None` здесь сравнился
        # бы с числом и уронил бы обработчик в `500` на ровном месте.
        return SetReceiptsResult(
            rejection=Reason.CONVERSATION_NOT_FOUND,
            visibility=decision.visibility,
        )

    validate_receipt_bound(receipts, head=head)
    state = state_of(receipts)
    stored = await read_states.upsert_read_state(
        conn,
        conversation_id=conversation_id,
        user_id=viewer.user_id,
        delivered_seq=state.delivered_seq,
        read_seq=state.read_seq,
    )
    return SetReceiptsResult(state=stored)
