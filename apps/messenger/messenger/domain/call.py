"""Звонок один-на-один: состояния, переходы, сигналы и события — без базы и сети.

Состояние звонка живёт **на сервере**; клиент не решает, жив ли звонок
(`docs/messenger/16-calls-estimate.md`, часть 2). Здесь — чистые функции:
каждая получает звонок и того, кто действует, и отвечает `Change` — что
получится, не записывая ничего. Запись, блокировку строки и публикацию делает
сервис; отдельно потому, что переход проверяется без базы, а гонка двух
запросов — только с ней.

```
            ┌─ отклонён ──────────────► ended(declined)
            ├─ RING_SECONDS без ответа ► ended(missed)
 ringing ───┼─ звонящий отменил ──────► ended(cancelled)
            ├─ у вызываемого занято ──► ended(busy)
            └─ принят
                 ▼
             accepted ── SETUP_SECONDS без медиа ──► ended(failed)
                 ▼ клиент сообщил «медиа пошло»
              active ── тишина STALE_SECONDS ──► ended(failed)
                 ▼ повесили трубку
            ended(completed)
```

Переходы **идемпотентны**: повторное «принять» и повторная «трубка» ничего не
меняют и не считаются ошибкой — клиент повторяет запрос после обрыва сети, и
отвечать ему отказом за то, что первый запрос всё-таки дошёл, значило бы ронять
исправный звонок.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

# Сколько звонит. Вызываемый, не ответивший за это время, получает «пропущенный».
RING_SECONDS = 30
# Сколько даётся от «принял» до «медиа пошло». Клиент сам делает ICE restart
# в течение 15 с (`CALL-007`); серверный срок больше, чтобы клиент успел
# сдаться первым, а сервер лишь подмёл за ним.
SETUP_SECONDS = 30
# Как часто клиент подтверждает жизнь звонка и как долго сервер ждёт тишину.
KEEPALIVE_SECONDS = 30
STALE_SECONDS = 90

# Границы сигнала. SDP с видео — 5–10 КБ; предел с запасом, но не «любой»:
# иначе звонок — бесплатный канал записи в чужой личный канал.
MAX_SIGNAL_BYTES = 16 * 1024
MAX_CANDIDATES = 40
MAX_CANDIDATE_LENGTH = 1024
# Не чаще этого числа сигналов в минуту на звонок (с запасом: звонок шлёт
# `offer`, `answer` и десятки кандидатов пачками).
SIGNALS_PER_MINUTE = 120
# Сколько живёт сигнал в хранилище (миграция 0017): время на переподключение
# получателя, а не история. Старый `offer` предыдущего согласования не воскресает.
SIGNAL_TTL_SECONDS = 60
# Предел одной выдачи пропущенных сигналов (`GET /calls/{id}/signals`).
SIGNALS_PAGE = 200

# Данные TURN живут **минуты**, а не часы: coturn ничего не знает о таблице звонков,
# и данные, выданные на три часа, пережили бы звонок, который длился двадцать секунд
# (а выданные «на звонок» — позволили бы копить их, начиная звонки подряд). Долгий
# звонок обновляет данные сам: клиент берёт новые за минуту до конца срока
# (`RTCPeerConnection.setConfiguration`), и следующий ICE restart идёт по свежим.
TURN_TTL_SECONDS = 600
# Срок округляется вверх до границы этого окна, поэтому запросы внутри окна дают
# одно и то же имя пользователя (квота coturn на пользователя действует), а
# остаток срока — от 300 до 600 секунд.
TURN_WINDOW_SECONDS = 300
# Запросов данных TURN на звонок и участника в минуту: клиенту хватает одного
# в несколько минут (обновление).
TURN_REQUESTS_PER_MINUTE = 30

_CALL_PREFIX = "call:"


class CallKind(str, Enum):
    AUDIO = "audio"
    VIDEO = "video"


class CallState(str, Enum):
    RINGING = "ringing"
    ACCEPTED = "accepted"
    ACTIVE = "active"
    ENDED = "ended"


class EndReason(str, Enum):
    COMPLETED = "completed"
    DECLINED = "declined"
    MISSED = "missed"
    CANCELLED = "cancelled"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Call:
    call_id: uuid.UUID
    conversation_id: uuid.UUID
    caller_id: uuid.UUID
    callee_id: uuid.UUID
    kind: CallKind
    state: CallState
    version: int
    signal_seq: int
    end_reason: EndReason | None
    created_at: datetime
    accepted_at: datetime | None
    active_at: datetime | None
    ended_at: datetime | None
    last_keepalive_at: datetime
    # Вкладка, принявшая звонок (метка страницы от клиента); `None` — не называла.
    accepted_by: str | None = None

    @property
    def ended(self) -> bool:
        return self.state is CallState.ENDED

    def involves(self, user_id: uuid.UUID) -> bool:
        return user_id in (self.caller_id, self.callee_id)

    def peer_of(self, user_id: uuid.UUID) -> uuid.UUID:
        return self.callee_id if user_id == self.caller_id else self.caller_id


@dataclass(frozen=True, slots=True)
class Change:
    """Итог попытки перехода.

    `denied` — действует не участник или не та сторона; `gone` — действие
    поздно и невозможно (звонок кончился, а действие требует живого);
    ни то ни другое и `state is None` — повтор уже сделанного, ничего не
    меняется.
    """

    state: CallState | None = None
    reason: EndReason | None = None
    denied: bool = False
    gone: bool = False
    # Звонок уже принят **другой** вкладкой этого человека: «принять» проиграло.
    taken: bool = False

    @property
    def changed(self) -> bool:
        return self.state is not None


_NOOP = Change()
_DENIED = Change(denied=True)
_GONE = Change(gone=True)


def _end(reason: EndReason) -> Change:
    return Change(state=CallState.ENDED, reason=reason)


def accept(call: Call, user_id: uuid.UUID, tab: str | None = None) -> Change:
    """Принимает вызываемый. Первый запрос побеждает.

    Повтор **той же вкладки** — пустое действие (клиент повторяет запрос после
    обрыва сети). «Принять» из **другой** вкладки, когда звонок уже принят, —
    `taken`: обе вкладки подняли бы медиа и обе ответили бы на `offer`, поэтому
    проигравшая обязана узнать, что проиграла, а не получить тот же `200`.
    """
    if user_id != call.callee_id:
        return _DENIED
    if call.state is CallState.RINGING:
        return Change(state=CallState.ACCEPTED)
    if call.state in (CallState.ACCEPTED, CallState.ACTIVE):
        if tab is not None and call.accepted_by is not None and call.accepted_by != tab:
            return Change(taken=True)
        return _NOOP
    return _GONE


def decline(call: Call, user_id: uuid.UUID) -> Change:
    if user_id != call.callee_id:
        return _DENIED
    if call.state is CallState.RINGING:
        return _end(EndReason.DECLINED)
    if call.ended and call.end_reason is EndReason.DECLINED:
        return _NOOP
    return _GONE


def hangup(call: Call, user_id: uuid.UUID) -> Change:
    """Трубка любой из сторон; что она значит, зависит от того, как далеко зашло."""
    if not call.involves(user_id):
        return _DENIED
    if call.ended:
        return _NOOP
    if call.state is CallState.RINGING:
        return _end(
            EndReason.CANCELLED if user_id == call.caller_id else EndReason.DECLINED
        )
    if call.state is CallState.ACCEPTED:
        # Медиа не было: звонка как разговора не случилось.
        return _end(EndReason.CANCELLED)
    return _end(EndReason.COMPLETED)


def fail(call: Call, user_id: uuid.UUID) -> Change:
    """Клиент сообщает: соединение не установилось или оборвалось насовсем.

    Отдельный переход, а не «трубка»: `hangup` в активном звонке — это
    `completed`, и обрыв сети записывался бы как состоявшийся разговор — в ленте,
    в метриках, в доле неудач. Допустимо только там, где медиа уже ждали или шло.
    """
    if not call.involves(user_id):
        return _DENIED
    if call.ended:
        return _NOOP
    if call.state in (CallState.ACCEPTED, CallState.ACTIVE):
        return _end(EndReason.FAILED)
    return _GONE


def connected(call: Call, user_id: uuid.UUID) -> Change:
    """«Медиа пошло» — сообщает любая сторона; звонок становится активным."""
    if not call.involves(user_id):
        return _DENIED
    if call.state is CallState.ACCEPTED:
        return Change(state=CallState.ACTIVE)
    if call.state is CallState.ACTIVE:
        return _NOOP
    return _GONE


def expire(call: Call, now: datetime) -> Change:
    """Что сделать с застрявшим звонком. Зовёт подметальщик, не клиент."""
    if call.state is CallState.RINGING:
        if (now - call.created_at).total_seconds() > RING_SECONDS:
            return _end(EndReason.MISSED)
    elif call.state is CallState.ACCEPTED:
        started = call.accepted_at or call.created_at
        if (now - started).total_seconds() > SETUP_SECONDS:
            return _end(EndReason.FAILED)
    elif call.state is CallState.ACTIVE:
        if (now - call.last_keepalive_at).total_seconds() > STALE_SECONDS:
            return _end(EndReason.FAILED)
    return _NOOP


# --- итог в ленте ----------------------------------------------------------------


def summary_code(kind: CallKind, reason: EndReason) -> str:
    """Код итога в тексте системного сообщения: слова подставляет клиент.

    На сервере нет языка интерфейса, а «Пропущенный звонок» в ленте обязан
    читаться на языке читателя, поэтому в сообщении лежит код, а не фраза.
    """
    return f"call.{reason.value}.{kind.value}"


def duration_ms(call: Call) -> int | None:
    """Длительность разговора: только у состоявшегося звонка."""
    if call.end_reason is not EndReason.COMPLETED:
        return None
    if call.active_at is None or call.ended_at is None:
        return None
    millis = int((call.ended_at - call.active_at).total_seconds() * 1000)
    return millis if millis > 0 else None


def summary_sender(call: Call) -> uuid.UUID:
    """Итог пишется от звонящего: направление «исходящий/входящий» читатель выводит сам."""
    return call.caller_id


# --- сигналы ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signal:
    kind: str
    sdp: str | None = None
    candidates: tuple[dict[str, object], ...] = ()
    # Идентификатор логического сигнала от клиента: повтор после потерянного ответа
    # приходит с тем же значением, и получатель отбрасывает дубль (иначе второй
    # `offer` запускал бы пересогласование).
    signal_id: str | None = None


def _candidate(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    text = item.get("candidate")
    if not isinstance(text, str) or len(text) > MAX_CANDIDATE_LENGTH:
        return None
    result: dict[str, object] = {"candidate": text}
    mid = item.get("sdpMid")
    if isinstance(mid, str) and len(mid) <= 64:
        result["sdpMid"] = mid
    index = item.get("sdpMLineIndex")
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < 16:
        result["sdpMLineIndex"] = index
    return result


def parse_signal(data: object) -> Signal | None:
    """Что прислал клиент. Разбирается только известное; остальное — отказ.

    Лишние поля не переходят вызываемому: сервер передаёт собеседнику не
    «то, что прислали», а то, что понял сам.
    """
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    raw_id = data.get("signal_id")
    signal_id = raw_id if isinstance(raw_id, str) and 0 < len(raw_id) <= 64 else None
    if kind in ("offer", "answer"):
        sdp = data.get("sdp")
        if not isinstance(sdp, str) or not sdp or len(sdp.encode()) > MAX_SIGNAL_BYTES:
            return None
        return Signal(kind=str(kind), sdp=sdp, signal_id=signal_id)
    if kind == "ice":
        raw = data.get("candidates")
        if not isinstance(raw, list) or not raw or len(raw) > MAX_CANDIDATES:
            return None
        parsed = [_candidate(item) for item in raw]
        if any(item is None for item in parsed):
            return None
        return Signal(
            kind="ice",
            candidates=tuple(item for item in parsed if item),
            signal_id=signal_id,
        )
    return None


# --- каналы и события ------------------------------------------------------------


def call_channel(user_id: uuid.UUID | str) -> str:
    """Личный канал звонков: без истории, выдаётся только владельцу.

    Отдельное пространство имён, а не `user:{id}`: у личного канала история
    (20 событий, 60 с), и после обрыва соединения Centrifugo отдал бы
    устаревший `offer` как новый (`CALL-007`). У канала звонков истории нет.
    """
    return f"{_CALL_PREFIX}{user_id}"


def incoming_event(call: Call, *, caller_name: str) -> dict[str, object]:
    return {
        "type": "call.incoming",
        "call_id": str(call.call_id),
        "conversation_id": str(call.conversation_id),
        "kind": call.kind.value,
        "caller": {"user_id": str(call.caller_id), "display_name": caller_name},
        "version": call.version,
    }


def state_event(call: Call, *, accepted_elsewhere: bool = False) -> dict[str, object]:
    reason: str | None = call.end_reason.value if call.end_reason else None
    if accepted_elsewhere:
        reason = "accepted_elsewhere"
    return {
        "type": "call.state",
        "call_id": str(call.call_id),
        "state": call.state.value,
        "reason": reason,
        "version": call.version,
    }


def signal_body(signal: Signal) -> dict[str, object]:
    """Что собеседник получает от сигнала: только понятое сервером."""
    body: dict[str, object] = {"type": signal.kind}
    if signal.sdp is not None:
        body["sdp"] = signal.sdp
    if signal.kind == "ice":
        body["candidates"] = list(signal.candidates)
    return body


def stored_signal_event(
    *, call_id: uuid.UUID, seq: int, body: dict[str, object], signal_id: str | None
) -> dict[str, object]:
    """Событие сигнала: одно и то же и в канале, и в выдаче пропущенного."""
    event: dict[str, object] = {
        "type": "call.signal",
        "call_id": str(call_id),
        "seq": seq,
        "signal": body,
    }
    if signal_id is not None:
        event["signal_id"] = signal_id
    return event


def signal_event(*, call: Call, signal: Signal, seq: int) -> dict[str, object]:
    return stored_signal_event(
        call_id=call.call_id, seq=seq, body=signal_body(signal), signal_id=signal.signal_id
    )
