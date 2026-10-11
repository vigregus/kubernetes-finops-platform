"""Звонки один-на-один: сценарии поверх автомата из `domain/call.py`.

Состояние живёт в Postgres, а не в процессе: любой под отвечает на любое
действие, и перезапуск ничего не теряет. Каждое действие — одна транзакция,
которая блокирует строку звонка (`FOR UPDATE`): два «принять» из двух вкладок
выстраиваются в очередь, и побеждает первый (`CALL-012`). Итог звонка пишется
системным сообщением **в той же транзакции**, что завершает его: «звонок
завершён, а в ленте пусто» — состояние, из которого нет выхода (`CALL-009`).

События в Centrifugo уходят **строго после** фиксации: публикация внутри
транзакции показывала бы получателю состояние, которого другое соединение с базой
ещё не видит (он нажимает «принять», получает `404` и закрывает звонок, а через
миллисекунды звонок «появляется»). Состояние — истина в Postgres; потерянное
событие клиент узнаёт через `GET /calls/current`, а потерянный сигнал — через
`GET /calls/{id}/signals?after=` (миграция `0017`): `publish() == true` значит
«принято Centrifugo», а не «получил браузер».
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
from messenger.adapters.turn import TurnProvider, restricted_only
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
from messenger.telemetry.network import NetworkContext

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


class RealtimeUnavailable(Exception):
    """Событие не удалось передать: Centrifugo недоступен или отказал."""


async def _publish(realtime: Publisher | None, events: list[Event]) -> None:
    """Лучшее из возможного: состояние уже в базе, клиент узнает его сверкой."""
    if realtime is None:
        return
    for channel, data in events:
        await realtime.publish(channel, data)


async def _publish_required(realtime: Publisher | None, events: list[Event]) -> None:
    """Событие **обязано** быть принято Centrifugo: иначе исключение.

    Вызывается **после** фиксации. Канал звонков без истории, и «успех» при
    недоставленном событии оставляет звонящего уверенным, что собеседник его
    получил. Что делать с отказом, решает вызывающий: сигнал остаётся в базе, и
    повтор клиента с тем же `signal_id` публикует тот же номер; недоставленное
    входящее закрывается (`_abort_undelivered`).
    """
    if realtime is None:
        raise RealtimeUnavailable
    for channel, data in events:
        if not await realtime.publish(channel, data):
            raise RealtimeUnavailable


async def _try_publish(realtime: Publisher | None, events: list[Event]) -> bool:
    """`True`, если все события приняты Centrifugo; без исключения."""
    try:
        await _publish_required(realtime, events)
    except RealtimeUnavailable:
        return False
    return True


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
    conn: asyncpg.Connection,
    call: Call,
    change: domain.Change,
    accepted_by: str | None = None,
) -> Call:
    updated = await repo.apply(
        conn,
        call.call_id,
        state=change.state or call.state,
        reason=change.reason,
        accepted_by=accepted_by,
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
    return await _start_call(
        conn,
        realtime=realtime,
        caller_id=caller_id,
        conversation_id=conversation_id,
        kind=kind,
    )


async def _start_call(
    conn: asyncpg.Connection,
    *,
    realtime: Publisher | None,
    caller_id: UserId,
    conversation_id: ConversationId,
    kind: CallKind,
) -> CallResult:
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

        # Застрявшие просроченные звонки участников завершаются здесь, а не ждут
        # подметальщика: иначе мёртвый `call-sweeper` держал бы линию занятой.
        expiry_events: list[Event] = []
        for user in (caller_id, callee_id):
            await _expire_live(conn, user, expiry_events)

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

    await _publish(realtime, expiry_events)
    # Входящий звонок **обязан** дойти до вызываемого: у канала нет истории, и
    # звонок, которого собеседник не увидит, — это 30 секунд гудков в пустоту.
    # Публикация после фиксации (иначе вызываемый примет звонок, которого другое
    # соединение с базой ещё не видит). Не принято Centrifugo — звонок закрывается
    # `failed` и линия освобождается: откатить уже зафиксированное нельзя.
    if outcome.call is not None and outcome.events:
        try:
            await _publish_required(realtime, outcome.events)
        except RealtimeUnavailable:
            await _abort_undelivered(conn, realtime, outcome.call.call_id)
            return CallResult(rejection=Reason.REALTIME_UNAVAILABLE)
    return outcome


async def _abort_undelivered(
    conn: asyncpg.Connection, realtime: Publisher | None, call_id: uuid.UUID
) -> None:
    """Входящий не дошёл: звонок, о котором вызываемый не узнает, закрывается."""
    events: list[Event] = []
    async with conn.transaction():
        call = await repo.fetch(conn, call_id, lock=True)
        if call is not None and not call.ended:
            updated = await _transition(
                conn, call, domain.Change(state=CallState.ENDED, reason=EndReason.FAILED)
            )
            events.extend(_state_events(updated))
    await _publish(realtime, events)


async def _expire_live(conn: asyncpg.Connection, user_id: UserId, events: list[Event]) -> None:
    live = await repo.fetch_live_for_user(conn, user_id)
    if live is None:
        return
    locked = await repo.fetch(conn, live.call_id, lock=True)
    if locked is not None:
        await _fresh(conn, locked, events)


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


async def _fresh(
    conn: asyncpg.Connection, call: Call, events: list[Event]
) -> tuple[Call, bool]:
    """Ленивое истечение срока: просроченный звонок завершается **здесь**, а не ждёт
    подметальщика.

    Без этого «просроченный `ringing` отвергается и без подметальщика» было бы
    словами, а не свойством: мёртвый `call-sweeper` оставлял бы звонок
    принимаемым через пять минут. Строка уже заблокирована вызывающим. Истёкший
    звонок записывает итог в ленту и освобождает линию в этой же транзакции, а
    события о нём уйдут вместе с событиями действия.
    """
    change = domain.expire(call, datetime.now(UTC))
    if not change.changed:
        return call, False
    updated = await _transition(conn, call, change)
    events.extend(_state_events(updated))
    return updated, True


async def _open(
    conn: asyncpg.Connection, call_id: uuid.UUID, user_id: UserId, events: list[Event]
) -> tuple[Call | None, bool]:
    """Блокирует звонок участника и подтягивает истёкший срок."""
    call = await repo.fetch(conn, call_id, lock=True)
    if call is None or not call.involves(user_id):
        # Чужой звонок неотличим от несуществующего.
        return None, False
    return await _fresh(conn, call, events)


def _rejection(change: domain.Change) -> Reason | None:
    if change.denied:
        return Reason.CALL_NOT_FOUND
    if change.taken:
        return Reason.CALL_TAKEN
    if change.gone:
        return Reason.CALL_ENDED
    return None


async def accept(
    conn: asyncpg.Connection,
    *,
    realtime: Publisher | None,
    user_id: UserId,
    call_id: uuid.UUID,
    tab: str | None = None,
) -> CallResult:
    return await _accept(conn, realtime, user_id, call_id, tab)


async def _accept(conn, realtime, user_id, call_id, tab) -> CallResult:
    events: list[Event] = []
    to_caller: Event | None = None
    async with conn.transaction():
        call, expired = await _open(conn, call_id, user_id, events)
        if call is None:
            return CallResult(rejection=Reason.CALL_NOT_FOUND)
        if expired:
            outcome = CallResult(rejection=Reason.CALL_ENDED)
        else:
            change = domain.accept(call, user_id, tab)
            rejection = _rejection(change)
            if rejection is not None:
                outcome = CallResult(rejection=rejection)
            elif not change.changed:
                outcome = CallResult(call=call)
            else:
                updated = await _transition(conn, call, change, accepted_by=tab)
                # Остальным вкладкам вызываемого — «принято в другой»: лучшее из возможного.
                events.extend(_state_events(updated, accepted_elsewhere=True)[1:])
                outcome = CallResult(call=updated)
                to_caller = (domain.call_channel(updated.caller_id), domain.state_event(updated))
    await _publish(realtime, events)
    if to_caller is not None:
        # Звонящему — «принято»: по нему он начинает `offer`. Публикуется после
        # фиксации (иначе звонящий пойдёт за данными TURN и увидит `ringing`). Не
        # принято Centrifugo — звонок остаётся принятым: откатывать нечего, а
        # звонящий сверяется с сервером каждые 5 секунд, пока ждёт.
        if not await _try_publish(realtime, [to_caller]):
            metrics.call_signal("state_undelivered")
    return outcome


async def decline(
    conn: asyncpg.Connection, *, realtime: Publisher | None, user_id: UserId, call_id: uuid.UUID
) -> CallResult:
    return await _end_action(conn, realtime, user_id, call_id, domain.decline, expired_ok=False)


async def hangup(
    conn: asyncpg.Connection, *, realtime: Publisher | None, user_id: UserId, call_id: uuid.UUID
) -> CallResult:
    return await _end_action(conn, realtime, user_id, call_id, domain.hangup, expired_ok=True)


async def fail(
    conn: asyncpg.Connection,
    *,
    realtime: Publisher | None,
    user_id: UserId,
    call_id: uuid.UUID,
    reason: str | None = None,
    network: NetworkContext | None = None,
) -> CallResult:
    """Клиент сообщает, что соединение не состоялось: итог `failed`, а не `completed`.

    Причина (`domain.FAILURE_REASONS`) идёт в метрику таксономии отказов (`RES-009`) **один
    раз** — когда звонок действительно перешёл в `failed`, а не на повторе того же запроса.
    """
    context = network or NetworkContext()
    code = reason if reason in domain.FAILURE_REASONS else "unknown"

    def count() -> None:
        metrics.call_failure(code, context.country, context.net_class)

    return await _end_action(
        conn, realtime, user_id, call_id, domain.fail, expired_ok=True, on_changed=count
    )


async def _end_action(
    conn, realtime, user_id, call_id, action, *, expired_ok: bool, on_changed=None
) -> CallResult:
    events: list[Event] = []
    async with conn.transaction():
        call, expired = await _open(conn, call_id, user_id, events)
        if call is None:
            return CallResult(rejection=Reason.CALL_NOT_FOUND)
        if expired:
            # Идемпотентные действия отвечают итогом; остальным звонок уже кончился.
            outcome = (
                CallResult(call=call)
                if expired_ok
                else CallResult(rejection=Reason.CALL_ENDED)
            )
        else:
            change = action(call, user_id)
            rejection = _rejection(change)
            if rejection is not None:
                outcome = CallResult(rejection=rejection)
            elif not change.changed:
                outcome = CallResult(call=call)
            else:
                updated = await _transition(conn, call, change)
                if on_changed is not None:
                    on_changed()
                events.extend(_state_events(updated))
                outcome = CallResult(call=updated)
    await _publish(realtime, events)
    return outcome


async def report_connected(
    conn: asyncpg.Connection,
    *,
    realtime: Publisher | None,
    user_id: UserId,
    call_id: uuid.UUID,
    connection_type: str,
    media_path: str | None = None,
    turn_transport: str | None = None,
    profile: str | None = None,
    network: NetworkContext | None = None,
) -> CallResult:
    """«Медиа пошло»: звонок становится активным, путь соединения идёт в метрику.

    `media_path` и `turn_transport` — подробности пути (`host | srflx | prflx | relay`,
    `udp | tcp | tls` у релея) для метрики `RES-009`, `profile` (`normal | restricted`) — для
    `RES-011`; необязательны, старый клиент их не шлёт.
    """
    detail = _PathDetail(
        path=media_path if media_path in domain.MEDIA_PATHS else None,
        transport=turn_transport if turn_transport in domain.TURN_TRANSPORTS else None,
        profile=profile if profile in domain.CALL_PROFILES else None,
        network=network or NetworkContext(),
    )
    events: list[Event] = []
    async with conn.transaction():
        call, expired = await _open(conn, call_id, user_id, events)
        if call is None:
            return CallResult(rejection=Reason.CALL_NOT_FOUND)
        if expired:
            outcome = CallResult(rejection=Reason.CALL_ENDED)
        else:
            change = domain.connected(call, user_id)
            rejection = _rejection(change)
            if rejection is not None:
                outcome = CallResult(rejection=rejection)
            elif not change.changed:
                # Звонок уже активен, а клиент сообщает путь снова: связь вернулась
                # после смены сети, и прямой путь мог стать релейным.
                await repo.touch(conn, call.call_id)
                if connection_type in ("direct", "relay"):
                    await _record_connection(conn, call, connection_type, detail)
                outcome = CallResult(call=call)
            else:
                updated = await _transition(conn, call, change)
                if connection_type in ("direct", "relay"):
                    await _record_connection(conn, call, connection_type, detail)
                events.extend(_state_events(updated))
                outcome = CallResult(call=updated)
    await _publish(realtime, events)
    return outcome


@dataclass(frozen=True, slots=True)
class _PathDetail:
    path: str | None
    transport: str | None
    profile: str | None
    network: NetworkContext


async def _record_connection(
    conn: asyncpg.Connection, call: Call, connection_type: str, detail: _PathDetail | None = None
) -> None:
    """Путь соединения в строку звонка и в метрики; повышение direct → relay учитывается.

    Первое сообщение — начальный путь (`messenger_call_connection_total`, время
    установки). Дальше значение меняется только **к худшему для стоимости**:
    `direct` → `relay`; обратно не возвращается — «хоть раз релей» остаётся фактом
    (`messenger_call_relay_used_total`), а повторное `direct` ничего не стирает.
    """
    previous = await repo.connection_type(conn, call.call_id)
    if previous is None:
        await repo.set_connection_type(conn, call.call_id, connection_type)
        setup = None
        if call.accepted_at is not None:
            setup = (datetime.now(UTC) - call.accepted_at).total_seconds()
        metrics.call_connection(connection_type, setup)
        if detail is not None:
            # Начальный путь и транспорт — один раз за звонок (на первом сообщении).
            path = detail.path or ("relay" if connection_type == "relay" else "host")
            transport = detail.transport if path == "relay" and detail.transport else "none"
            net = detail.network
            metrics.call_media_path(path, transport, net.country, net.net_class)
            if detail.profile is not None:
                metrics.call_profile(detail.profile)
        if connection_type == "relay":
            metrics.call_relay_used(switched=False)
    elif previous == "direct" and connection_type == "relay":
        await repo.set_connection_type(conn, call.call_id, "relay")
        metrics.call_relay_used(switched=True)


async def keepalive(
    conn: asyncpg.Connection,
    *,
    realtime: Publisher | None = None,
    user_id: UserId,
    call_id: uuid.UUID,
) -> CallResult:
    """Подтверждение жизни; ответ несёт состояние, чтобы клиент сверился с сервером."""
    events: list[Event] = []
    async with conn.transaction():
        call, _ = await _open(conn, call_id, user_id, events)
        if call is None:
            return CallResult(rejection=Reason.CALL_NOT_FOUND)
        if not call.ended:
            await repo.touch(conn, call.call_id)
    await _publish(realtime, events)
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
    """Сигнал собеседнику: проверка участия → запись с номером → публикация.

    Клиент не публикует в Centrifugo сам: у него нет такого права, и «подделка
    автора события» закрыта именно этим (`CALL-006`). Собеседнику уходит то, что
    сервер **понял** (`parse_signal`), а не то, что прислали.

    Сигнал **записывается** (миграция `0017`) и лишь после фиксации публикуется:
    пропущенное при обрыве соединения получатель забирает через `list_signals`.
    Повтор с тем же `signal_id` не заводит второй номер — находит записанный и
    публикует его снова (идемпотентность на сервере, а не только отсев дублей у
    получателя). Недоставленный сигнал — `503`, а не `204`: клиент повторяет, и
    повтор безопасен.
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

    events: list[Event] = []
    to_peer: Event | None = None
    async with conn.transaction():
        call, expired = await _open(conn, call_id, user_id, events)
        if call is None:
            metrics.call_signal("forbidden")
            return CallResult(rejection=Reason.CALL_NOT_FOUND)
        if expired or call.ended:
            outcome = CallResult(rejection=Reason.CALL_ENDED)
        elif call.state is CallState.RINGING:
            # Сигналы начинаются после принятия: так не бывает устаревшего
            # `offer` для вызываемого, который ещё не решил, отвечать ли.
            outcome = CallResult(rejection=Reason.CALL_NOT_READY)
        else:
            body = domain.signal_body(signal)
            stored = (
                await repo.find_signal(conn, call.call_id, user_id, signal.signal_id)
                if signal.signal_id is not None
                else None
            )
            if stored is not None:
                seq, body = stored.seq, stored.body
            else:
                seq = await repo.next_signal_seq(conn, call.call_id)
                await repo.insert_signal(
                    conn,
                    call_id=call.call_id,
                    seq=seq,
                    sender_id=user_id,
                    signal_id=signal.signal_id,
                    body=body,
                    ttl_seconds=domain.SIGNAL_TTL_SECONDS,
                )
            to_peer = (
                domain.call_channel(call.peer_of(user_id)),
                domain.stored_signal_event(
                    call_id=call.call_id, seq=seq, body=body, signal_id=signal.signal_id
                ),
            )
            outcome = CallResult(call=call)
    await _publish(realtime, events)
    if to_peer is not None:
        # Строго после фиксации: получатель, не нашедший сигнал в базе, — это ровно то
        # окно, которое закрывает запись. Порядок публикаций двух запросов не
        # гарантирован, и это не страшно: получатель применяет сигналы по номеру и
        # при разрыве нумерации забирает недостающее.
        try:
            await _publish_required(realtime, [to_peer])
        except RealtimeUnavailable:
            metrics.call_signal("undelivered")
            return CallResult(rejection=Reason.REALTIME_UNAVAILABLE)
        metrics.call_signal("sent")
    return outcome


@dataclass(slots=True)
class SignalsResult:
    events: list[dict[str, object]] = field(default_factory=list)
    rejection: Reason | None = None


async def list_signals(
    conn: asyncpg.Connection, *, user_id: UserId, call_id: uuid.UUID, after: int
) -> SignalsResult:
    """Пропущенные сигналы собеседника: после переподключения и при разрыве нумерации.

    Только участнику; чужой звонок неотличим от несуществующего. У завершённого
    звонка выдача пуста по сроку: сигналы живут минуту.
    """
    call = await repo.fetch(conn, call_id)
    if call is None or not call.involves(user_id):
        return SignalsResult(rejection=Reason.CALL_NOT_FOUND)
    stored = await repo.list_signals(
        conn, call_id, for_user=user_id, after=max(after, 0), limit=domain.SIGNALS_PAGE
    )
    return SignalsResult(
        events=[
            domain.stored_signal_event(
                call_id=call_id, seq=item.seq, body=item.body, signal_id=item.signal_id
            )
            for item in stored
        ]
    )


# --- TURN ---------------------------------------------------------------------------


@dataclass(slots=True)
class IceResult:
    servers: list[dict[str, object]] | None = None
    ttl_seconds: int = 0
    rejection: Reason | None = None
    retry_after_seconds: int | None = None


async def ice_servers(
    conn: asyncpg.Connection,
    *,
    turn: TurnProvider,
    limiter: RateLimiter,
    user_id: UserId,
    call_id: uuid.UUID,
    profile: str = "normal",
    now: datetime | None = None,
) -> IceResult:
    """Данные для TURN — участнику **принятого** звонка, на минуты.

    Только `accepted` и `active`: пока звонок звонит, релей не нужен, а данные,
    выданные на этой стадии, позволяли бы копить их, начиная и бросая звонки.
    Срок короткий (`TURN_TTL_SECONDS`), и данные переживают звонок не дольше
    нескольких минут: coturn о таблице звонков не знает. Долгий звонок обновляет
    данные сам (клиент идёт за новыми до конца срока).

    Имя пользователя стабильно для **человека** внутри окна: десяток запросов не
    рождает десять пользователей со свежей квотой, и квота coturn на пользователя
    ограничивает человека, а не звонок. Выдача ограничена и по частоте; недоступный
    счётчик не повод её закрывать (`ALLOW`): остальное держат квоты coturn.
    """
    moment = now or datetime.now(UTC)
    call = await repo.fetch(conn, call_id)
    if call is None or not call.involves(user_id):
        return IceResult(rejection=Reason.CALL_NOT_FOUND)
    if call.ended:
        return IceResult(rejection=Reason.CALL_ENDED)
    if call.state is CallState.RINGING:
        return IceResult(rejection=Reason.CALL_NOT_READY)

    window = domain.TURN_WINDOW_SECONDS
    expires_at = (int(moment.timestamp()) // window + 2) * window
    ttl = expires_at - int(moment.timestamp())

    decision = await limiter.take(
        f"call-ice:{call_id}:{user_id}",
        limit=domain.TURN_REQUESTS_PER_MINUTE,
        window_seconds=60,
        on_failure=OnFailure.ALLOW,
    )
    if not decision.allowed:
        return IceResult(
            rejection=Reason.RATE_LIMITED, retry_after_seconds=decision.retry_after_seconds
        )

    servers = await turn.ice_servers(ttl_seconds=ttl, expires_at=expires_at, subject=str(user_id))
    if servers is not None and profile == "restricted":
        # `RESTRICTED` (RES-011): только `turns:`; без них релей по TLS недоступен вовсе.
        servers = restricted_only(servers)
    if servers is None:
        # Звонок без релея всё равно возможен: клиент строит соединение без него.
        return IceResult(servers=[], ttl_seconds=ttl)
    return IceResult(servers=[server.as_dict() for server in servers], ttl_seconds=ttl)


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
        await repo.purge_signals(conn)
    await _publish(realtime, all_events)
    return SweepResult(examined=len(live), ended=ended)
