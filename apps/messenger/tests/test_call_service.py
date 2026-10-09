"""Звонок на уровне сервиса: гонки, повторы, права, итог в ленте (CALL-001…009, 012)."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from messenger.adapters.ratelimit import LimitDecision
from messenger.domain import call as domain
from messenger.domain.call import Call, CallKind, CallState, EndReason
from messenger.domain.conversation import (
    Conversation,
    ConversationMember,
    ConversationType,
    MemberRole,
)
from messenger.domain.errors import Reason
from messenger.domain.ids import ConversationId, UserId
from messenger.services import calls as service
from messenger.telemetry.network import NetworkContext

A = UserId(uuid.uuid4())
B = UserId(uuid.uuid4())
C = UserId(uuid.uuid4())
CONV = ConversationId(uuid.uuid4())
# Время теста — настоящее: ленивое истечение сравнивает звонок с часами процесса, и
# фиксированная дата делала бы «свежий» звонок просроченным, как только часы её пройдут.
NOW = datetime.now(UTC)


# Момент внутри окна TURN (середина пятиминутки): от часов теста не зависит. С живыми
# часами тест падал, когда «сейчас + 30 с» переходило границу окна.
TURN_AT = datetime.fromtimestamp(
    1_791_000_000 // domain.TURN_WINDOW_SECONDS * domain.TURN_WINDOW_SECONDS + 100, UTC
)


def run(coro):
    return asyncio.run(coro)


IN_TX = {"depth": 0}
CURRENT = {"store": None}


class Tx:
    """Транзакция фейкового хранилища: при исключении состояние откатывается."""

    async def __aenter__(self):
        IN_TX["depth"] += 1
        store = CURRENT["store"]
        self._snapshot = None if store is None else store.snapshot()
        return self

    async def __aexit__(self, exc_type, *rest):
        IN_TX["depth"] -= 1
        if exc_type is not None and self._snapshot is not None:
            CURRENT["store"].restore(self._snapshot)
        return False


class Conn:
    def transaction(self):
        return Tx()


class Realtime:
    def __init__(self, ok=True):
        self.sent: list[tuple[str, dict]] = []
        self.inside_tx: list[bool] = []
        self.ok = ok

    async def publish(self, channel, data):
        self.sent.append((channel, data))
        self.inside_tx.append(IN_TX["depth"] > 0)
        return self.ok


# Сервис без Centrifugo: для звонков это отказ, а не «лучшее из возможного».
NO_REALTIME = None


class Limiter:
    def __init__(self, allowed=True):
        self.allowed = allowed

    async def take(self, key, *, limit, window_seconds, on_failure):
        return LimitDecision(allowed=self.allowed, retry_after_seconds=7)


class Store:
    """Хранилище звонков в памяти с теми же инвариантами, что у миграции 0016."""

    def __init__(self):
        self.calls: dict[uuid.UUID, Call] = {}
        self.live: dict[uuid.UUID, uuid.UUID] = {}  # user_id -> call_id (UNIQUE)
        self.online: set[uuid.UUID] = {A, B, C}
        self.messages: list[dict] = []
        self.summaries: dict[uuid.UUID, uuid.UUID] = {}
        self.blocked: set[uuid.UUID] = set()
        self.members = [A, B]
        self.direct = True
        self.connection: dict[uuid.UUID, str] = {}
        self.signals: list[service.repo.StoredSignal] = []
        self.signal_calls: list[uuid.UUID] = []

    def snapshot(self):
        return (
            dict(self.calls), dict(self.live), list(self.messages), dict(self.summaries),
            list(self.signals), list(self.signal_calls),
        )

    def restore(self, snap):
        self.calls, self.live, self.messages, self.summaries = (
            dict(snap[0]), dict(snap[1]), list(snap[2]), dict(snap[3]),
        )
        self.signals, self.signal_calls = list(snap[4]), list(snap[5])


@pytest.fixture
def store(monkeypatch):
    st = Store()
    CURRENT["store"] = st
    seq = {"n": 0}

    async def fetch_conversation(conn, *, conversation_id):
        if conversation_id != CONV:
            return None
        return Conversation(
            conversation_id=CONV,
            type=ConversationType.DIRECT if st.direct else ConversationType.GROUP,
            direct_key="a:b" if st.direct else None,
            last_seq=0, created_at=NOW, updated_at=NOW,
        )

    async def list_active_members(conn, *, conversation_id):
        return [
            ConversationMember(CONV, UserId(u), MemberRole.MEMBER, NOW) for u in st.members
        ]

    async def blocked_with(conn, *, viewer, conversation_id=None):
        return frozenset(st.blocked)

    async def fetch_user(conn, *, user_id):
        from types import SimpleNamespace
        return SimpleNamespace(display_name={A: "Alice", B: "Bob", C: "Carol"}[user_id])

    async def callee_reachable(conn, *, user_id, window):
        return user_id in st.online

    def build(call_id, conv, caller, callee, kind, state, reason):
        return Call(
            call_id=call_id, conversation_id=conv, caller_id=caller, callee_id=callee,
            kind=kind, state=state, version=1, signal_seq=0, end_reason=reason,
            created_at=NOW, accepted_at=None, active_at=None,
            ended_at=NOW if state is CallState.ENDED else None, last_keepalive_at=NOW,
        )

    async def insert_live(conn, *, call_id, conversation_id, caller_id, callee_id, kind):
        if caller_id in st.live or callee_id in st.live:
            raise service.repo.LiveCallExists
        call = build(call_id, conversation_id, caller_id, callee_id, kind, CallState.RINGING, None)
        st.calls[call_id] = call
        st.live[caller_id] = call_id
        st.live[callee_id] = call_id
        return call

    async def insert_ended(conn, *, call_id, conversation_id, caller_id, callee_id, kind, reason):
        call = build(call_id, conversation_id, caller_id, callee_id, kind, CallState.ENDED, reason)
        st.calls[call_id] = call
        return call

    async def fetch(conn, call_id, *, lock=False):
        return st.calls.get(call_id)

    async def fetch_live_for_user(conn, user_id):
        call_id = st.live.get(user_id)
        return st.calls[call_id] if call_id else None

    async def apply(conn, call_id, *, state, reason=None, accepted_by=None):
        old = st.calls[call_id]
        new = replace(
            old, state=state, end_reason=reason, version=old.version + 1,
            accepted_by=accepted_by if state is CallState.ACCEPTED else old.accepted_by,
            accepted_at=NOW if state is CallState.ACCEPTED else old.accepted_at,
            active_at=NOW if state is CallState.ACTIVE else old.active_at,
            ended_at=NOW + timedelta(seconds=42) if state is CallState.ENDED else old.ended_at,
        )
        st.calls[call_id] = new
        if state is CallState.ENDED:
            st.live.pop(old.caller_id, None)
            st.live.pop(old.callee_id, None)
        return new

    async def next_signal_seq(conn, call_id):
        old = st.calls[call_id]
        st.calls[call_id] = replace(old, signal_seq=old.signal_seq + 1)
        return old.signal_seq + 1

    async def find_signal(conn, call_id, sender_id, signal_id):
        for stored, owner in zip(st.signals, st.signal_calls, strict=True):
            if owner == call_id and stored.sender_id == sender_id and stored.signal_id == signal_id:
                return stored
        return None

    async def insert_signal(conn, *, call_id, seq, sender_id, signal_id, body, ttl_seconds):
        st.signals.append(service.repo.StoredSignal(seq, sender_id, signal_id, body))
        st.signal_calls.append(call_id)

    async def list_signals(conn, call_id, *, for_user, after, limit):
        return [
            stored
            for stored, owner in zip(st.signals, st.signal_calls, strict=True)
            if owner == call_id and stored.sender_id != for_user and stored.seq > after
        ][:limit]

    async def purge_signals(conn):
        return 0

    async def touch(conn, call_id):
        return None

    async def connection_type(conn, call_id):
        return st.connection.get(call_id)

    async def set_connection_type(conn, call_id, value):
        st.connection[call_id] = value

    async def set_summary(conn, call_id, message_id):
        st.summaries[call_id] = message_id

    async def claim_live(conn, *, limit):
        return [st.calls[c] for c in {*st.live.values()}]

    async def fetch_live(conn, user_id):
        call_id = st.live.get(user_id)
        return st.calls[call_id] if call_id else None

    async def post_system_message(conn, *, conversation_id, sender_id, client_message_id, payload):
        from types import SimpleNamespace
        # Уникальный индекс сообщений: второй итог того же звонка невозможен.
        assert all(m["client_message_id"] != client_message_id for m in st.messages), "итог дважды"
        message = {
            "message_id": uuid.uuid4(), "client_message_id": client_message_id,
            "sender_id": sender_id, "text": payload.text, "duration_ms": payload.duration_ms,
        }
        st.messages.append(message)
        seq["n"] += 1
        return SimpleNamespace(message_id=message["message_id"])

    for module, name, fn in [
        (service.conversations, "fetch_conversation", fetch_conversation),
        (service.conversations, "list_active_members", list_active_members),
        (service.conversations, "blocked_with", blocked_with),
        (service.users, "fetch_user", fetch_user),
        (service.repo, "callee_reachable", callee_reachable),
        (service.repo, "insert_live", insert_live),
        (service.repo, "insert_ended", insert_ended),
        (service.repo, "fetch", fetch),
        (service.repo, "fetch_live_for_user", fetch_live_for_user),
        (service.repo, "apply", apply),
        (service.repo, "next_signal_seq", next_signal_seq),
        (service.repo, "find_signal", find_signal),
        (service.repo, "insert_signal", insert_signal),
        (service.repo, "list_signals", list_signals),
        (service.repo, "purge_signals", purge_signals),
        (service.repo, "touch", touch),
        (service.repo, "connection_type", connection_type),
        (service.repo, "set_connection_type", set_connection_type),
        (service.repo, "set_summary", set_summary),
        (service.repo, "claim_live", claim_live),
        (service.message_service, "post_system_message", post_system_message),
    ]:
        monkeypatch.setattr(module, name, fn)
    monkeypatch.setenv("CALLS_ENABLED", "true")
    return st


def start(by=A, kind=CallKind.AUDIO, rt=None):
    realtime = rt if rt is not None else Realtime()
    return run(service.start_call(
        Conn(), realtime=realtime, caller_id=by, conversation_id=CONV, kind=kind))


# --- CALL-001: полный звонок ----------------------------------------------------------


def test_полный_звонок_состояния_идут_по_порядку_и_итог_в_ленте_один(store):
    rt = Realtime()
    started = start(rt=rt)
    assert started.ok and started.call.state is CallState.RINGING
    call_id = started.call.call_id
    # вызываемому — входящий звонок в его личный канал
    assert rt.sent[0][0] == f"call:{B}"
    assert rt.sent[0][1]["type"] == "call.incoming"

    run(service.accept(Conn(), realtime=rt, user_id=B, call_id=call_id))
    assert store.calls[call_id].state is CallState.ACCEPTED

    run(service.report_connected(
        Conn(), realtime=rt, user_id=A, call_id=call_id, connection_type="direct"))
    assert store.calls[call_id].state is CallState.ACTIVE

    run(service.hangup(Conn(), realtime=rt, user_id=B, call_id=call_id))
    done = store.calls[call_id]
    assert (done.state, done.end_reason) == (CallState.ENDED, EndReason.COMPLETED)
    assert [m["text"] for m in store.messages] == ["call.completed.audio"]
    assert store.messages[0]["duration_ms"] == 42_000
    assert store.messages[0]["sender_id"] == A
    assert store.live == {}


def test_видеозвонок_пишет_видео_в_итоге(store):
    started = start(kind=CallKind.VIDEO)
    run(service.hangup(Conn(), realtime=Realtime(), user_id=A, call_id=started.call.call_id))
    assert store.messages[0]["text"] == "call.cancelled.video"


# --- CALL-003: повторы ------------------------------------------------------------------


def test_повторные_принять_и_повесить_ничего_не_меняют_и_итог_один(store):
    call_id = start().call.call_id
    run(service.accept(Conn(), realtime=Realtime(), user_id=B, call_id=call_id))
    again = run(service.accept(Conn(), realtime=Realtime(), user_id=B, call_id=call_id))
    assert again.ok and store.calls[call_id].version == 2

    run(service.hangup(Conn(), realtime=Realtime(), user_id=A, call_id=call_id))
    run(service.hangup(Conn(), realtime=Realtime(), user_id=A, call_id=call_id))
    run(service.hangup(Conn(), realtime=Realtime(), user_id=B, call_id=call_id))
    assert len(store.messages) == 1


def test_принять_завершённый_звонок_отказ_а_не_оживление(store):
    call_id = start().call.call_id
    run(service.hangup(Conn(), realtime=Realtime(), user_id=A, call_id=call_id))
    result = run(service.accept(Conn(), realtime=Realtime(), user_id=B, call_id=call_id))
    assert result.rejection is Reason.CALL_ENDED
    assert store.calls[call_id].state is CallState.ENDED


# --- CALL-004: встречный звонок ---------------------------------------------------------


def test_встречный_звонок_принимает_существующий_а_не_заводит_второй(store):
    first = start(by=A)
    second = start(by=B)
    assert second.ok
    assert second.call.call_id == first.call.call_id
    assert second.call.state is CallState.ACCEPTED
    assert len(store.calls) == 1


# --- CALL-005: права --------------------------------------------------------------------


def test_не_участник_беседы_не_может_позвонить(store):
    result = start(by=C)
    assert result.rejection is Reason.NOT_A_MEMBER
    assert store.calls == {}


def test_звонок_в_групповую_беседу_отвергается(store):
    store.direct = False
    assert start().rejection is Reason.CONVERSATION_NOT_FOUND


def test_заблокированному_звонить_нельзя(store):
    store.blocked = {B}
    assert start().rejection is Reason.BLOCKED
    assert store.calls == {}


def test_чужой_звонок_неотличим_от_несуществующего(store):
    call_id = start().call.call_id
    for action in (service.accept, service.decline, service.hangup):
        result = run(action(Conn(), realtime=Realtime(), user_id=C, call_id=call_id))
        assert result.rejection is Reason.CALL_NOT_FOUND
    ghost = run(service.accept(Conn(), realtime=Realtime(), user_id=B, call_id=uuid.uuid4()))
    assert ghost.rejection is Reason.CALL_NOT_FOUND


def test_звонящий_не_может_принять_свой_звонок(store):
    call_id = start().call.call_id
    result = run(service.accept(Conn(), realtime=Realtime(), user_id=A, call_id=call_id))
    assert result.rejection is Reason.CALL_NOT_FOUND
    assert store.calls[call_id].state is CallState.RINGING


def test_звонки_выключены_флагом(store, monkeypatch):
    monkeypatch.setenv("CALLS_ENABLED", "false")
    assert start().rejection is Reason.CALLS_UNAVAILABLE


# --- CALL-013/014: занято, не в сети ----------------------------------------------------


def test_занятому_звонок_заканчивается_занято_и_у_звонящего_понятная_причина(store):
    store.members = [A, B]
    store.live[B] = uuid.uuid4()  # у B уже идёт чужой звонок
    store.calls[store.live[B]] = domain_call(B, C)
    result = start()
    assert result.ok
    assert result.call.end_reason is EndReason.BUSY
    assert [m["text"] for m in store.messages] == ["call.busy.audio"]
    assert store.live[B] != result.call.call_id


def domain_call(caller, callee) -> Call:
    return Call(
        call_id=uuid.uuid4(), conversation_id=uuid.uuid4(), caller_id=caller, callee_id=callee,
        kind=CallKind.AUDIO, state=CallState.ACTIVE, version=2, signal_seq=0, end_reason=None,
        created_at=NOW, accepted_at=NOW, active_at=NOW, ended_at=None, last_keepalive_at=NOW,
    )


def test_у_самого_звонящего_уже_идёт_звонок_отказ(store):
    other = domain_call(A, C)
    store.calls[other.call_id] = other
    store.live[A] = other.call_id
    store.live[C] = other.call_id
    assert start().rejection is Reason.ALREADY_IN_CALL


def test_вызываемый_не_в_сети_звонок_не_стартует_и_пишется_пропущенный(store):
    store.online.discard(B)
    rt = Realtime()
    result = start(rt=rt)
    assert result.call.end_reason is EndReason.UNAVAILABLE
    assert rt.sent == []  # у собеседника ничего не звонит
    assert [m["text"] for m in store.messages] == ["call.unavailable.audio"]
    assert store.live == {}


# --- CALL-006/007: сигналы ---------------------------------------------------------------


def signal(call_id, by, data, rt=None, limiter=None):
    return run(service.send_signal(
        Conn(), realtime=rt if rt is not None else Realtime(),
        limiter=limiter or Limiter(), user_id=by, call_id=call_id, data=data))


OFFER = {"type": "offer", "sdp": "v=0"}


def test_сигнал_уходит_собеседнику_с_номером_от_сервера(store):
    call_id = start().call.call_id
    run(service.accept(Conn(), realtime=Realtime(), user_id=B, call_id=call_id))
    rt = Realtime()
    first = signal(call_id, A, OFFER, rt)
    second = signal(call_id, A, {"type": "ice", "candidates": [{"candidate": "c"}]}, rt)
    assert first.ok and second.ok
    assert [c for c, _ in rt.sent] == [f"call:{B}", f"call:{B}"]
    assert [e["seq"] for _, e in rt.sent] == [1, 2]


def test_подделка_сигнала_чужим_пользователем_отвергнута_и_ничего_не_ушло(store):
    call_id = start().call.call_id
    run(service.accept(Conn(), realtime=Realtime(), user_id=B, call_id=call_id))
    rt = Realtime()
    result = signal(call_id, C, OFFER, rt)
    assert result.rejection is Reason.CALL_NOT_FOUND
    assert rt.sent == []


def test_сигнал_до_принятия_отвергнут(store):
    call_id = start().call.call_id
    assert signal(call_id, A, OFFER).rejection is Reason.CALL_NOT_READY


def test_сигнал_в_завершённый_звонок_отвергнут(store):
    call_id = start().call.call_id
    run(service.hangup(Conn(), realtime=Realtime(), user_id=A, call_id=call_id))
    assert signal(call_id, A, OFFER).rejection is Reason.CALL_ENDED


def test_негодный_сигнал_отвергнут_до_базы(store):
    call_id = start().call.call_id
    assert signal(call_id, A, {"type": "offer"}).rejection is Reason.INVALID_SIGNAL


def test_частота_сигналов_ограничена_и_клиент_получает_время_ожидания(store):
    call_id = start().call.call_id
    run(service.accept(Conn(), realtime=Realtime(), user_id=B, call_id=call_id))
    rt = Realtime()
    result = signal(call_id, A, OFFER, rt, Limiter(allowed=False))
    assert result.rejection is Reason.RATE_LIMITED
    assert result.retry_after_seconds == 7
    assert rt.sent == []


# --- CALL-012: вкладки ---------------------------------------------------------------------


def test_принятие_в_одной_вкладке_гасит_вызов_в_остальных(store):
    rt = Realtime()
    call_id = start().call.call_id
    run(service.accept(Conn(), realtime=rt, user_id=B, call_id=call_id))
    to_callee = [e for c, e in rt.sent if c == f"call:{B}"]
    to_caller = [e for c, e in rt.sent if c == f"call:{A}"]
    assert to_callee[-1]["reason"] == "accepted_elsewhere"
    assert to_caller[-1]["state"] == "accepted" and to_caller[-1]["reason"] is None


# --- подметальщик --------------------------------------------------------------------------


def test_подметальщик_закрывает_пропущенный_и_освобождает_линию(store):
    call_id = start().call.call_id
    later = NOW + timedelta(seconds=domain.RING_SECONDS + 1)
    result = run(service.sweep(Conn(), realtime=Realtime(), now=later))
    assert result.ended == 1
    assert store.calls[call_id].end_reason is EndReason.MISSED
    assert [m["text"] for m in store.messages] == ["call.missed.audio"]
    assert store.live == {}
    # второй проход ничего не делает
    assert run(service.sweep(Conn(), realtime=Realtime(), now=later)).ended == 0
    assert len(store.messages) == 1


def test_подметальщик_не_трогает_свежий_звонок(store):
    start()
    assert run(service.sweep(Conn(), realtime=Realtime(), now=NOW)).ended == 0


# --- TURN ----------------------------------------------------------------------------------


class Turn:
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    async def ice_servers(self, *, ttl_seconds, expires_at=None, subject=None):
        self.calls.append({"ttl": ttl_seconds, "expires_at": expires_at, "subject": subject})
        return self.result


def ice(call_id, by, turn, limiter=None, now=None):
    return run(service.ice_servers(
        Conn(), turn=turn, limiter=limiter or Limiter(), user_id=by, call_id=call_id, now=now))


def accepted_call(store):
    call_id = start().call.call_id
    run(service.accept(Conn(), realtime=Realtime(), user_id=B, call_id=call_id))
    return call_id


def test_turn_выдаётся_участнику_принятого_звонка_на_минуты(store):
    from messenger.adapters.turn import IceServer
    call_id = accepted_call(store)
    turn = Turn([IceServer(urls=("turn:x",), username="u", credential="c")])
    result = ice(call_id, B, turn, now=TURN_AT)
    assert result.servers == [{"urls": ["turn:x"], "username": "u", "credential": "c"}]
    # Срок — минуты: данные, выданные на часы, пережили бы звонок в двадцать секунд.
    assert 300 <= result.ttl_seconds <= domain.TURN_TTL_SECONDS
    assert turn.calls[0]["expires_at"] == int(TURN_AT.timestamp()) + result.ttl_seconds
    assert turn.calls[0]["expires_at"] % domain.TURN_WINDOW_SECONDS == 0
    # Субъект — человек, а не звонок: квота coturn не сбрасывается новым звонком.
    assert turn.calls[0]["subject"] == str(B)


def test_пока_звонит_данные_turn_не_выдаются(store):
    """Данные, выданные на стадии `ringing`, позволяли бы копить их, бросая звонки."""
    call_id = start().call.call_id
    turn = Turn([])
    result = ice(call_id, A, turn)
    assert result.rejection is Reason.CALL_NOT_READY
    assert turn.calls == []


def test_данные_turn_стабильны_для_человека_внутри_окна_и_различны_у_людей(store):
    call_id = accepted_call(store)
    from messenger.adapters.turn import CoturnProvider
    provider = CoturnProvider(secret="s", urls=("turn:x",), now=lambda: 1.0)

    def username(by, when):
        return ice(call_id, by, provider, now=when).servers[0]["username"]

    inside = TURN_AT + timedelta(seconds=30)
    assert username(A, TURN_AT) == username(A, TURN_AT) == username(A, inside)
    assert username(A, TURN_AT) != username(B, TURN_AT)
    # следующее окно — новое имя: старые данные не живут вечно
    later = TURN_AT + timedelta(seconds=domain.TURN_WINDOW_SECONDS + 1)
    assert username(A, TURN_AT) != username(A, later)


def test_новый_звонок_того_же_человека_даёт_то_же_имя_а_не_свежую_квоту(store):
    from messenger.adapters.turn import CoturnProvider
    provider = CoturnProvider(secret="s", urls=("turn:x",), now=lambda: 1.0)
    first = accepted_call(store)
    name = ice(first, A, provider, now=TURN_AT).servers[0]["username"]
    run(service.hangup(Conn(), realtime=Realtime(), user_id=A, call_id=first))
    second = accepted_call(store)
    assert ice(second, A, provider, now=TURN_AT).servers[0]["username"] == name


def test_частые_запросы_данных_turn_ограничены(store):
    call_id = accepted_call(store)
    result = ice(call_id, A, Turn([]), limiter=Limiter(allowed=False))
    assert result.rejection is Reason.RATE_LIMITED
    assert result.retry_after_seconds == 7


def test_turn_не_выдаётся_постороннему_и_после_завершения(store):
    call_id = accepted_call(store)
    turn = Turn([])
    assert ice(call_id, C, turn).rejection is Reason.CALL_NOT_FOUND
    run(service.hangup(Conn(), realtime=Realtime(), user_id=A, call_id=call_id))
    assert ice(call_id, A, turn).rejection is Reason.CALL_ENDED
    assert turn.calls == []  # провайдера не спрашивали


def test_недоступный_turn_не_роняет_звонок(store):
    call_id = accepted_call(store)
    assert ice(call_id, A, Turn(None)).servers == []


# --- ленивое истечение: корректность не зависит от подметальщика ----------------------------


def make_stale(store, call_id, seconds=domain.RING_SECONDS + 60):
    created = datetime.now(UTC) - timedelta(seconds=seconds)
    store.calls[call_id] = replace(store.calls[call_id], created_at=created)


def test_просроченный_звонок_нельзя_принять_и_без_подметальщика(store):
    call_id = start().call.call_id
    make_stale(store, call_id)
    rt = Realtime()
    result = run(service.accept(Conn(), realtime=rt, user_id=B, call_id=call_id))
    assert result.rejection is Reason.CALL_ENDED
    assert store.calls[call_id].end_reason is EndReason.MISSED
    assert [m["text"] for m in store.messages] == ["call.missed.audio"]
    assert store.live == {}
    # обеим сторонам ушло событие о конце, а не только ответ на запрос
    assert {c for c, _ in rt.sent} == {f"call:{A}", f"call:{B}"}


def test_просроченный_звонок_при_трубке_отвечает_итогом_а_не_ошибкой(store):
    call_id = start().call.call_id
    make_stale(store, call_id)
    result = run(service.hangup(Conn(), realtime=Realtime(), user_id=A, call_id=call_id))
    assert result.ok and result.call.end_reason is EndReason.MISSED
    assert len(store.messages) == 1


def test_зависший_просроченный_звонок_не_держит_линию_занятой(store):
    stale = start().call.call_id
    make_stale(store, stale)
    fresh = start()
    assert fresh.ok and fresh.call.state is CallState.RINGING
    assert fresh.call.call_id != stale
    assert store.calls[stale].end_reason is EndReason.MISSED


# --- принять: кто победил -----------------------------------------------------------------------


def accept_as(call_id, tab):
    return run(service.accept(Conn(), realtime=Realtime(), user_id=B, call_id=call_id, tab=tab))


def test_две_вкладки_принимают_одновременно_проигравшая_узнаёт_об_этом(store):
    call_id = start().call.call_id
    first = accept_as(call_id, "tab-1")
    second = accept_as(call_id, "tab-2")
    assert first.ok
    assert second.rejection is Reason.CALL_TAKEN
    assert store.calls[call_id].accepted_by == "tab-1"
    assert store.calls[call_id].version == 2  # проигравшая ничего не изменила


def test_повтор_принятия_той_же_вкладки_не_проигрыш(store):
    call_id = start().call.call_id
    accept_as(call_id, "tab-1")
    assert accept_as(call_id, "tab-1").ok


# --- не соединилось -----------------------------------------------------------------------------


def test_обрыв_записывается_как_неудача_а_не_как_состоявшийся_разговор(store):
    call_id = start().call.call_id
    run(service.accept(Conn(), realtime=Realtime(), user_id=B, call_id=call_id))
    run(service.report_connected(
        Conn(), realtime=Realtime(), user_id=A, call_id=call_id, connection_type="direct"))
    result = run(service.fail(Conn(), realtime=Realtime(), user_id=A, call_id=call_id))
    assert result.call.end_reason is EndReason.FAILED
    assert [m["text"] for m in store.messages] == ["call.failed.audio"]
    assert store.messages[0]["duration_ms"] is None


def test_неудача_до_принятия_отвергается(store):
    call_id = start().call.call_id
    result = run(service.fail(Conn(), realtime=Realtime(), user_id=A, call_id=call_id))
    assert result.rejection is Reason.CALL_ENDED


def test_неудача_идемпотентна_и_только_для_участника(store):
    call_id = start().call.call_id
    run(service.accept(Conn(), realtime=Realtime(), user_id=B, call_id=call_id))
    run(service.fail(Conn(), realtime=Realtime(), user_id=A, call_id=call_id))
    assert run(service.fail(Conn(), realtime=Realtime(), user_id=B, call_id=call_id)).ok
    assert len(store.messages) == 1
    other = start()
    stranger = run(service.fail(Conn(), realtime=Realtime(), user_id=C, call_id=other.call.call_id))
    assert stranger.rejection is Reason.CALL_NOT_FOUND


# --- порядок сигналов ---------------------------------------------------------------------


def test_сигнал_публикуется_только_после_фиксации(store):
    """Публикация до фиксации показывала получателю то, чего другое соединение не видит."""
    call_id = start().call.call_id
    rt_accept = Realtime()
    run(service.accept(Conn(), realtime=rt_accept, user_id=B, call_id=call_id))
    rt = Realtime()
    signal(call_id, A, OFFER, rt)
    signal(call_id, A, {"type": "ice", "candidates": [{"candidate": "c"}]}, rt)
    assert rt.inside_tx == [False, False]
    assert rt_accept.inside_tx and not any(rt_accept.inside_tx)
    assert [e["seq"] for _, e in rt.sent] == [1, 2]


def test_входящий_и_принятие_публикуются_после_фиксации(store):
    rt = Realtime()
    call_id = start(rt=rt).call.call_id
    assert rt.inside_tx == [False]
    rt2 = Realtime()
    run(service.accept(Conn(), realtime=rt2, user_id=B, call_id=call_id))
    assert rt2.inside_tx and not any(rt2.inside_tx)


# --- недоставленный сигнал не считается доставленным ----------------------------------------


def test_сигнал_не_ушёл_в_centrifugo_503_а_не_204_и_повтор_не_расходует_номер(store):
    call_id = accepted_call(store)
    broken = Realtime(ok=False)
    data = {**OFFER, "signal_id": "sig-1"}
    result = signal(call_id, A, data, broken)
    assert result.rejection is Reason.REALTIME_UNAVAILABLE
    # сигнал записан (получатель может забрать его сам), номер выдан один раз
    assert [s.seq for s in store.signals] == [1]
    healthy = Realtime()
    again = signal(call_id, A, data, healthy)
    assert again.ok and healthy.sent[0][1]["seq"] == 1
    assert [s.seq for s in store.signals] == [1]


def test_повтор_с_тем_же_идентификатором_это_тот_же_сигнал_а_не_второй(store):
    """Серверная идемпотентность: потерянный ответ и повтор не создают второй `offer`."""
    call_id = accepted_call(store)
    rt = Realtime()
    data = {**OFFER, "signal_id": "sig-7"}
    assert signal(call_id, A, data, rt).ok
    assert signal(call_id, A, data, rt).ok
    assert [e["seq"] for _, e in rt.sent] == [1, 1]
    assert len(store.signals) == 1
    assert store.calls[call_id].signal_seq == 1
    # другой идентификатор — другой сигнал
    assert signal(call_id, A, {**OFFER, "signal_id": "sig-8"}, rt).ok
    assert [s.seq for s in store.signals] == [1, 2]


def test_пропущенные_сигналы_собеседника_выдаются_после_номера_по_порядку(store):
    call_id = accepted_call(store)
    rt = Realtime()
    signal(call_id, A, OFFER, rt)
    signal(call_id, A, {"type": "ice", "candidates": [{"candidate": "c"}]}, rt)
    signal(call_id, B, {"type": "answer", "sdp": "v=0"}, rt)
    got = run(service.list_signals(Conn(), user_id=B, call_id=call_id, after=0))
    assert [e["seq"] for e in got.events] == [1, 2]
    assert got.events[0]["signal"]["type"] == "offer"
    assert got.events[0]["call_id"] == str(call_id)
    after = run(service.list_signals(Conn(), user_id=B, call_id=call_id, after=1))
    assert [e["seq"] for e in after.events] == [2]
    # свои сигналы не возвращаются
    mine = run(service.list_signals(Conn(), user_id=A, call_id=call_id, after=0))
    assert [e["seq"] for e in mine.events] == [3]


def test_пропущенные_сигналы_только_участнику(store):
    call_id = accepted_call(store)
    result = run(service.list_signals(Conn(), user_id=C, call_id=call_id, after=0))
    assert result.rejection is Reason.CALL_NOT_FOUND


def test_метрика_sent_не_растёт_при_недоставленном_сигнале(store, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(service.metrics, "call_signal", seen.append)
    call_id = accepted_call(store)
    signal(call_id, A, OFFER, Realtime(ok=False))
    assert seen == ["undelivered"]
    seen.clear()
    signal(call_id, A, {**OFFER, "signal_id": "x"}, Realtime())
    assert seen == ["sent"]


def test_без_realtime_сигнал_не_считается_отправленным(store):
    call_id = accepted_call(store)
    result = run(service.send_signal(
        Conn(), realtime=NO_REALTIME, limiter=Limiter(), user_id=A, call_id=call_id, data=OFFER))
    assert result.rejection is Reason.REALTIME_UNAVAILABLE


def test_идентификатор_сигнала_доходит_до_получателя_для_отсева_дублей(store):
    call_id = accepted_call(store)
    rt = Realtime()
    signal(call_id, A, {**OFFER, "signal_id": "sig-1"}, rt)
    assert rt.sent[0][1]["signal_id"] == "sig-1"


def test_входящий_не_дошёл_звонок_закрывается_и_линия_свободна(store):
    broken = Realtime(ok=False)
    result = start(rt=broken)
    assert result.rejection is Reason.REALTIME_UNAVAILABLE
    # зафиксированное не откатить: звонок закрыт как неудавшийся, а не висит гудками
    (call,) = store.calls.values()
    assert call.state is CallState.ENDED and call.end_reason is EndReason.FAILED
    assert store.live == {}
    # и следующая попытка проходит
    assert start(rt=Realtime()).ok


def test_принятие_не_дошло_до_звонящего_звонок_остаётся_принятым(store, monkeypatch):
    """Состояние в базе — истина; звонящий узнаёт его сверкой за 5 секунд."""
    seen: list[str] = []
    monkeypatch.setattr(service.metrics, "call_signal", seen.append)
    call_id = start().call.call_id
    broken = Realtime(ok=False)
    result = run(service.accept(Conn(), realtime=broken, user_id=B, call_id=call_id))
    assert result.ok and store.calls[call_id].state is CallState.ACCEPTED
    assert "state_undelivered" in seen
    # и повторное «принять» идемпотентно
    assert run(service.accept(Conn(), realtime=Realtime(), user_id=B, call_id=call_id)).ok


def test_конец_звонка_не_зависит_от_доступности_centrifugo(store):
    """Трубка обязана сработать: состояние в базе, событие — лучшее из возможного."""
    call_id = accepted_call(store)
    result = run(service.hangup(Conn(), realtime=Realtime(ok=False), user_id=A, call_id=call_id))
    assert result.ok and result.call.state is CallState.ENDED


# --- путь соединения при смене сети (метрика стоимости) -----------------------------------------


def connect_as(call_id, by, kind):
    return run(service.report_connected(
        Conn(), realtime=Realtime(), user_id=by, call_id=call_id, connection_type=kind))


def test_прямой_звонок_ушёл_на_релей_после_смены_сети_это_записано(store, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(service.metrics, "call_relay_used",
                        lambda *, switched: seen.append(("relay_used", switched)))
    call_id = accepted_call(store)
    connect_as(call_id, A, "direct")
    assert store.connection[call_id] == "direct"
    assert seen == []

    connect_as(call_id, A, "relay")  # связь вернулась уже через TURN
    assert store.connection[call_id] == "relay"
    assert seen == [("relay_used", True)]


def test_релей_с_самого_начала_считается_использованным_но_не_сменой(store, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(service.metrics, "call_relay_used",
                        lambda *, switched: seen.append(("relay_used", switched)))
    call_id = accepted_call(store)
    connect_as(call_id, A, "relay")
    assert seen == [("relay_used", False)]


def test_возврат_на_прямой_путь_не_стирает_факт_релея_и_не_считается_дважды(store, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(service.metrics, "call_relay_used",
                        lambda *, switched: seen.append(("relay_used", switched)))
    call_id = accepted_call(store)
    connect_as(call_id, A, "direct")
    connect_as(call_id, A, "relay")
    connect_as(call_id, A, "direct")
    connect_as(call_id, A, "relay")
    assert store.connection[call_id] == "relay"
    assert seen == [("relay_used", True)]


def test_повтор_того_же_пути_ничего_не_меняет(store, monkeypatch):
    counted: list[str] = []
    monkeypatch.setattr(service.metrics, "call_connection",
                        lambda kind, setup: counted.append(kind))
    call_id = accepted_call(store)
    connect_as(call_id, A, "direct")
    connect_as(call_id, A, "direct")
    assert counted == ["direct"]


# --- RES-009: путь, транспорт TURN и причина отказа в метриках ---------------------------


@pytest.fixture
def seen(monkeypatch):
    """Что ушло в метрики пути и отказов."""
    records = {"paths": [], "failures": []}
    monkeypatch.setattr(
        service.metrics, "call_media_path", lambda *args: records["paths"].append(args)
    )
    monkeypatch.setattr(
        service.metrics, "call_failure", lambda *args: records["failures"].append(args)
    )
    return records


def test_путь_и_транспорт_релея_пишутся_один_раз_за_звонок(store, seen):
    rt = Realtime()
    call_id = accepted_call(store)
    net = NetworkContext(country="RU", net_class="mobile")
    for _ in range(2):
        run(service.report_connected(
            Conn(), realtime=rt, user_id=A, call_id=call_id, connection_type="relay",
            media_path="relay", turn_transport="tls", network=net))
    assert seen["paths"] == [("relay", "tls", "RU", "mobile")]


def test_прямой_путь_пишется_без_транспорта(store, seen):
    rt = Realtime()
    call_id = accepted_call(store)
    run(service.report_connected(
        Conn(), realtime=rt, user_id=A, call_id=call_id, connection_type="direct",
        media_path="srflx", turn_transport="udp"))
    # транспорт TURN при нерелейном пути не считается: иначе «direct через tls» сбивало бы долю
    assert seen["paths"] == [("srflx", "none", "unknown", "unknown")]


def test_старый_клиент_без_подробностей_получает_путь_по_типу_соединения(store, seen):
    rt = Realtime()
    call_id = accepted_call(store)
    run(service.report_connected(
        Conn(), realtime=rt, user_id=A, call_id=call_id, connection_type="relay"))
    assert seen["paths"] == [("relay", "none", "unknown", "unknown")]


def test_негодные_путь_и_транспорт_не_становятся_метками(store, seen):
    rt = Realtime()
    call_id = accepted_call(store)
    run(service.report_connected(
        Conn(), realtime=rt, user_id=A, call_id=call_id, connection_type="relay",
        media_path="carrier-pigeon", turn_transport="smoke"))
    assert seen["paths"] == [("relay", "none", "unknown", "unknown")]


def test_отказ_пишет_причину_один_раз_и_повтор_не_удваивает(store, seen):
    rt = Realtime()
    call_id = accepted_call(store)
    net = NetworkContext(country="RU", net_class="fixed")
    for _ in range(2):
        run(service.fail(
            Conn(), realtime=rt, user_id=A, call_id=call_id,
            reason="ice_connect_timeout", network=net))
    assert store.calls[call_id].end_reason is EndReason.FAILED
    assert seen["failures"] == [("ice_connect_timeout", "RU", "fixed")]


def test_неизвестная_причина_отказа_считается_unknown(store, seen):
    rt = Realtime()
    call_id = accepted_call(store)
    run(service.fail(Conn(), realtime=rt, user_id=A, call_id=call_id, reason="aliens"))
    assert seen["failures"] == [("unknown", "unknown", "unknown")]


def test_отказ_без_причины_тоже_считается(store, seen):
    rt = Realtime()
    call_id = accepted_call(store)
    run(service.fail(Conn(), realtime=rt, user_id=A, call_id=call_id))
    assert seen["failures"] == [("unknown", "unknown", "unknown")]
