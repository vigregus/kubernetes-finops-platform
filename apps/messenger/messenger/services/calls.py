"""Звонки один-на-один: сценарии поверх автомата из `domain/call.py`.

Состояние живёт в Postgres, а не в процессе: любой под отвечает на любое
действие, и перезапуск ничего не теряет. Каждое действие — одна транзакция,
которая блокирует строку звонка (`FOR UPDATE`): два «принять» из двух вкладок
выстраиваются в очередь, и побеждает первый (`CALL-012`). Итог звонка пишется
системным сообщением **в той же транзакции**, что завершает его: «звонок
завершён, а в ленте пусто» — состояние, из которого нет выхода (`CALL-009`).

События в Centrifugo уходят **после** фиксации и по принципу «лучшее из
возможного»: потерянное событие не портит состояние — клиент узнаёт его через
`GET /calls/current` и сигналом `keepalive` — а откат транзакции из-за
недоступного Centrifugo сделал бы звонки зависимыми от второго хранилища.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import asyncpg

from messenger.adapters.ratelimit import OnFailure, RateLimiter
from messenger.adapters.turn import TurnProvider
from messenger.domain import call as domain
from messenger.domain.call import Call, CallKind, CallState, EndReason
from messenger.domain.errors import Reason
from messenger.domain.ids import ClientMessageId, ConversationId, UserId
from messenger.domain.message import MessagePayload
from messenger.domain.presence import ONLINE_WINDOW_SECONDS
from messenger.repositories import calls as repo
from messenger.repositories import conversations, users
from messenger.services import messages as message_service
from messenger.telemetry import metrics

log = logging.getLogger(__name__)


class Publisher(Protocol):
    async def publish(self, channel: str, data: dict) -> bool: ...


def enabled() -> bool:
    """Выключатель звонков (`calls_enabled`, ADR 0007).

    Пока читается из окружения процесса — зазор, названный в `services/typing.py`:
    применить выключатель без выкатки может только хранилище флагов (G5, `CFG-001`).
    По умолчанию **выключено**: звонки включаются для людей только после
    защиты от нежелательных звонков (`CALL-017`).
    """
    return os.getenv("CALLS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


Event = tuple[str, dict[str, object]]


@dataclass(slots=True)
class CallResult:
    call: Call | None = None
    rejection: Reason | None = None
    # События, которые нужно отправить после фиксации. Сервис отправляет их сам.
    events: list[Event] = field(default_factory=list)
    retry_after_seconds: int | None = None

    @property
    def ok(self) -> bool:
        return self.call is not None and self.rejection is None


async def _publish(realtime: Publisher | None, events: list[Event]) -> None:
    if realtime is None:
        return
    for channel, data in events:
        await realtime.publish(channel, data)


def _state_events(call: Call, *, accepted_elsewhere: bool = False) -> list[Event]:
    """Состояние — обеим сторонам: звонящему и **всем вкладкам** вызываемого."""
    return [
        (domain.call_channel(call.caller_id), domain.state_event(call)),
        (
            domain.call_channel(call.callee_id),
            domain.state_event(call, accepted_elsewhere=accepted_elsewhere),
        ),
    ]


async def _summarize(conn: asyncpg.Connection, call: Call) -> None:
    """Итог звонка в ленту — в транзакции вызывающего, один раз.

    Идемпотентность двойная: идентификатор сообщения от клиента — это
    `call_id`, и уникальный индекс сообщений не пропустит второй итог; а
    `summary_message_id` в строке звонка (она заблокирована) отвечает, что итог
    уже есть.
    """
    if call.end_reason is None:
        return
    message = await message_service.post_system_message(
        conn,
        conversation_id=ConversationId(call.conversation_id),
        sender_id=UserId(domain.summary_sender(call)),
        client_message_id=ClientMessageId(call.call_id),
        payload=MessagePayload(
            text=domain.summary_code(call.kind, call.end_reason),
            duration_ms=domain.duration_ms(call),
        ),
    )
    await repo.set_summary(conn, call.call_id, message.message_id)
    metrics.call_ended(call.kind.value, call.end_reason.value)


async def _transition(
    conn: asyncpg.Connection, call: Call, change: domain.Change
) -> Call:
    updated = await repo.apply(
        conn, call.call_id, state=change.state or call.state, reason=change.reason
    )
    if updated.ended:
        await _summarize(conn, updated)
    return updated


# --- начать звонок ----------------------------------------------------------------


async def start_call(
    conn: asyncpg.Connection,
    *,
    realtime: Publisher | None,
    caller_id: UserId,
    conversation_id: ConversationId,
    kind: CallKind,
) -> CallResult:
    if not enabled():
        return CallResult(rejection=Reason.CALLS_UNAVAILABLE)

    async with conn.transaction():
        conversation = await conversations.fetch_conversation(
            conn, conversation_id=conversation_id
        )
        if conversation is None or not conversation.is_direct:
            return CallResult(rejection=Reason.CONVERSATION_NOT_FOUND)
        members = await conversations.list_active_members(
            conn, conversation_id=conversation_id
        )
        ids = [member.user_id for member in members]
        if caller_id not in ids:
            return CallResult(rejection=Reason.NOT_A_MEMBER)
        others = [user_id for user_id in ids if user_id != caller_id]
        if len(others) != 1:
            return CallResult(rejection=Reason.CONVERSATION_NOT_FOUND)
        callee_id = UserId(others[0])

        blocked = await conversations.blocked_with(
            conn, viewer=caller_id, conversation_id=conversation_id
        )
        if callee_id in blocked:
            return CallResult(rejection=Reason.BLOCKED)

        mine = await repo.fetch_live_for_user(conn, caller_id)
        if mine is None:
            outcome = await _create(
                conn,
                caller_id=caller_id,
                callee_id=callee_id,
                conversation_id=conversation_id,
                kind=kind,
            )
        elif (
            mine.caller_id == callee_id
            and mine.callee_id == caller_id
            and mine.conversation_id == conversation_id
        ):
            outcome = await _meet_counter_call(conn, mine.call_id)
        else:
            outcome = CallResult(rejection=Reason.ALREADY_IN_CALL)

    if outcome.call is None:
        return outcome
    await _publish(realtime, outcome.events)
    return outcome


async def _meet_counter_call(conn: asyncpg.Connection, call_id: uuid.UUID) -> CallResult:
    """Встречный звонок: собеседник звонит мне, а я — ему, — это одно «да».

    Принимается уже существующий звонок, а не заводится второй (`CALL-004`):
    два звонка друг другу при одной линии на человека не сошлись бы вовсе.
    """
    locked = await repo.fetch(conn, call_id, lock=True)
    if locked is None or locked.state is not CallState.RINGING:
        return CallResult(rejection=Reason.ALREADY_IN_CALL)
    updated = await repo.apply(conn, locked.call_id, state=CallState.ACCEPTED)
    return CallResult(call=updated, events=_state_events(updated))


async def _create(
    conn: asyncpg.Connection,
    *,
    caller_id: UserId,
    callee_id: UserId,
    conversation_id: ConversationId,
    kind: CallKind,
) -> CallResult:
    call_id = uuid.uuid4()

    theirs = await repo.fetch_live_for_user(conn, callee_id)
    if theirs is not None:
        return await _record_ended(
            conn, call_id, caller_id, callee_id, conversation_id, kind, EndReason.BUSY
        )

    reachable = await repo.callee_reachable(
        conn, user_id=callee_id, window=timedelta(seconds=ONLINE_WINDOW_SECONDS)
    )
    if not reachable:
        # Звонок в пустоту не начинается: вызываемый не в сети, звонить некому.
        return await _record_ended(
            conn, call_id, caller_id, callee_id, conversation_id, kind,
            EndReason.UNAVAILABLE,
        )

    try:
        # Вложенная транзакция (точка сохранения): нарушение уникальности не
        # должно прерывать внешнюю, иначе «занято» нельзя будет записать.
        async with conn.transaction():
            call = await repo.insert_live(
                conn,
                call_id=call_id,
                conversation_id=conversation_id,
                caller_id=caller_id,
                callee_id=callee_id,
                kind=kind,
            )
    except repo.LiveCallExists:
        # Гонка: между проверкой и вставкой кто-то из двоих занял линию. Решает база.
        return await _record_ended(
            conn, call_id, caller_id, callee_id, conversation_id, kind, EndReason.BUSY
        )

    caller = await users.fetch_user(conn, user_id=caller_id)
    metrics.call_started(kind.value)
    events: list[Event] = [
        (
            domain.call_channel(callee_id),
            domain.incoming_event(
                call, caller_name=caller.display_name if caller else ""
            ),
        )
    ]
    return CallResult(call=call, events=events)


async def _record_ended(
    conn: asyncpg.Connection,
    call_id: uuid.UUID,
    caller_id: UserId,
    callee_id: UserId,
    conversation_id: ConversationId,
    kind: CallKind,
    reason: EndReason,
) -> CallResult:
    call = await repo.insert_ended(
        conn,
        call_id=call_id,
        conversation_id=conversation_id,
        caller_id=caller_id,
        callee_id=callee_id,
        kind=kind,
        reason=reason,
    )
    metrics.call_started(kind.value)
    await _summarize(conn, call)
    # Звонящему сообщать нечего событием: ответ на запрос уже несёт итог;
    # его другие вкладки узнают из ленты (системное сообщение).
    return CallResult(call=call)


# --- действия над звонком ----------------------------------------------------------


async def _locked_for(
    conn: asyncpg.Connection, call_id: uuid.UUID, user_id: UserId
) -> Call | None:
    call = await repo.fetch(conn, call_id, lock=True)
    if call is None or not call.involves(user_id):
        # Чужой звонок неотличим от несуществующего.
        return None
    return call


def _rejection(change: domain.Change) -> Reason | None:
    if change.denied:
        return Reason.CALL_NOT_FOUND
    if change.gone:
        return Reason.CALL_ENDED
    return None


async def accept(
    conn: asyncpg.Connection, *, realtime: Publisher | None, user_id: UserId, call_id: uuid.UUID
) -> CallResult:
    async with conn.transaction():
        call = await _locked_for(conn, call_id, user_id)
        if call is None:
            return CallResult(rejection=Reason.CALL_NOT_FOUND)
        change = domain.accept(call, user_id)
        rejection = _rejection(change)
        if rejection is not None:
            return CallResult(rejection=rejection)
        if not change.changed:
            return CallResult(call=call)
        updated = await _transition(conn, call, change)
        events = _state_events(updated, accepted_elsewhere=True)
        # Звонящему — обычное «принято»: переопределяем первое событие.
        events[0] = (domain.call_channel(updated.caller_id), domain.state_event(updated))
    await _publish(realtime, events)
    return CallResult(call=updated)


async def decline(
    conn: asyncpg.Connection, *, realtime: Publisher | None, user_id: UserId, call_id: uuid.UUID
) -> CallResult:
    return await _end_action(conn, realtime, user_id, call_id, domain.decline)


async def hangup(
    conn: asyncpg.Connection, *, realtime: Publisher | None, user_id: UserId, call_id: uuid.UUID
) -> CallResult:
    return await _end_action(conn, realtime, user_id, call_id, domain.hangup)


async def _end_action(conn, realtime, user_id, call_id, action) -> CallResult:
    async with conn.transaction():
        call = await _locked_for(conn, call_id, user_id)
        if call is None:
            return CallResult(rejection=Reason.CALL_NOT_FOUND)
        change = action(call, user_id)
        rejection = _rejection(change)
        if rejection is not None:
            return CallResult(rejection=rejection)
        if not change.changed:
            return CallResult(call=call)
        updated = await _transition(conn, call, change)
        events = _state_events(updated)
    await _publish(realtime, events)
    return CallResult(call=updated)


async def report_connected(
    conn: asyncpg.Connection,
    *,
    realtime: Publisher | None,
    user_id: UserId,
    call_id: uuid.UUID,
    connection_type: str,
) -> CallResult:
    """«Медиа пошло»: звонок становится активным, путь соединения идёт в метрику."""
    async with conn.transaction():
        call = await _locked_for(conn, call_id, user_id)
        if call is None:
            return CallResult(rejection=Reason.CALL_NOT_FOUND)
        change = domain.connected(call, user_id)
        rejection = _rejection(change)
        if rejection is not None:
            return CallResult(rejection=rejection)
        if not change.changed:
            await repo.touch(conn, call.call_id)
            return CallResult(call=call)
        updated = await _transition(conn, call, change)
        if connection_type in ("direct", "relay"):
            await repo.set_connection_type(conn, call.call_id, connection_type)
            setup = None
            if call.accepted_at is not None:
                setup = (datetime.now(UTC) - call.accepted_at).total_seconds()
            metrics.call_connection(connection_type, setup)
        events = _state_events(updated)
    await _publish(realtime, events)
    return CallResult(call=updated)


async def keepalive(
    conn: asyncpg.Connection, *, user_id: UserId, call_id: uuid.UUID
) -> CallResult:
    """Подтверждение жизни; ответ несёт состояние, чтобы клиент сверился с сервером."""
    async with conn.transaction():
        call = await _locked_for(conn, call_id, user_id)
        if call is None:
            return CallResult(rejection=Reason.CALL_NOT_FOUND)
        if not call.ended:
            await repo.touch(conn, call.call_id)
    return CallResult(call=call)


async def current(conn: asyncpg.Connection, *, user_id: UserId) -> Call | None:
    """Живой звонок человека — после перезагрузки вкладки или с другой вкладки."""
    return await repo.fetch_live_for_user(conn, user_id)


# --- сигналы -----------------------------------------------------------------------


async def send_signal(
    conn: asyncpg.Connection,
    *,
    realtime: Publisher | None,
    limiter: RateLimiter,
    user_id: UserId,
    call_id: uuid.UUID,
    data: object,
) -> CallResult:
    """Сигнал собеседнику: проверка участия → номер от сервера → публикация.

    Клиент не публикует в Centrifugo сам: у него нет такого права, и «подделка
    автора события» закрыта именно этим (`CALL-006`). Собеседнику уходит то, что
    сервер **понял** (`parse_signal`), а не то, что прислали.
    """
    signal = domain.parse_signal(data)
    if signal is None:
        metrics.call_signal("invalid")
        return CallResult(rejection=Reason.INVALID_SIGNAL)

    decision = await limiter.take(
        f"call-signal:{call_id}",
        limit=domain.SIGNALS_PER_MINUTE,
        window_seconds=60,
        # Пропускать при недоступном счётчике: сигнал ограничен размером и
        # участием в звонке, а отказ Redis не должен рвать идущий звонок.
        on_failure=OnFailure.ALLOW,
    )
    if not decision.allowed:
        metrics.call_signal("rate_limited")
        return CallResult(
            rejection=Reason.RATE_LIMITED,
            retry_after_seconds=decision.retry_after_seconds,
        )

    async with conn.transaction():
        call = await _locked_for(conn, call_id, user_id)
        if call is None:
            metrics.call_signal("forbidden")
            return CallResult(rejection=Reason.CALL_NOT_FOUND)
        if call.ended:
            return CallResult(rejection=Reason.CALL_ENDED)
        if call.state is CallState.RINGING:
            # Сигналы начинаются после принятия: так не бывает устаревшего
            # `offer` для вызываемого, который ещё не решил, отвечать ли.
            return CallResult(rejection=Reason.CALL_NOT_READY)
        seq = await repo.next_signal_seq(conn, call.call_id)
        events: list[Event] = [
            (
                domain.call_channel(call.peer_of(user_id)),
                domain.signal_event(call=call, signal=signal, seq=seq),
            )
        ]
    metrics.call_signal("sent")
    await _publish(realtime, events)
    return CallResult(call=call)


# --- TURN ---------------------------------------------------------------------------


@dataclass(slots=True)
class IceResult:
    servers: list[dict[str, object]] | None = None
    rejection: Reason | None = None


async def ice_servers(
    conn: asyncpg.Connection, *, turn: TurnProvider, user_id: UserId, call_id: uuid.UUID
) -> IceResult:
    """Данные для TURN — только участнику **живого** звонка и на ≈ 10 минут."""
    call = await repo.fetch(conn, call_id)
    if call is None or not call.involves(user_id):
        return IceResult(rejection=Reason.CALL_NOT_FOUND)
    if call.ended:
        return IceResult(rejection=Reason.CALL_ENDED)
    servers = await turn.ice_servers(ttl_seconds=domain.TURN_TTL_SECONDS)
    if servers is None:
        # Звонок без релея всё равно возможен: клиент строит соединение без него.
        return IceResult(servers=[])
    return IceResult(servers=[server.as_dict() for server in servers])


# --- подметальщик -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepResult:
    examined: int = 0
    ended: int = 0


async def sweep(
    conn: asyncpg.Connection, *, realtime: Publisher | None, now: datetime | None = None,
    batch: int = 100,
) -> SweepResult:
    """Завершает звонки, у которых вышел срок: «пропущенный», «не соединился», «тишина».

    Подметальщик **не влияет на корректность** живого звонка: устаревший
    `ringing` отвергается и без него, а его работа — довести итог до ленты
    и освободить линию. Смерть процесса оставляет звонок «звонящим» дольше, но
    не ломает ни один другой.
    """
    moment = now or datetime.now(UTC)
    ended = 0
    all_events: list[Event] = []
    async with conn.transaction():
        live = await repo.claim_live(conn, limit=batch)
        for call in live:
            change = domain.expire(call, moment)
            if not change.changed:
                continue
            updated = await _transition(conn, call, change)
            all_events.extend(_state_events(updated))
            ended += 1
    await _publish(realtime, all_events)
    return SweepResult(examined=len(live), ended=ended)
