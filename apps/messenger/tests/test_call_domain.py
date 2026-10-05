"""Звонок: автомат состояний, сигналы, события (CALL-002, CALL-003, CALL-005)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from messenger.domain import call as domain
from messenger.domain.call import CallKind, CallState, EndReason

A = uuid.uuid4()
B = uuid.uuid4()
OTHER = uuid.uuid4()
NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


def make(state=CallState.RINGING, reason=None, **fields) -> domain.Call:
    base = dict(
        call_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        caller_id=A,
        callee_id=B,
        kind=CallKind.AUDIO,
        state=state,
        version=1,
        signal_seq=0,
        end_reason=reason,
        created_at=NOW,
        accepted_at=None,
        active_at=None,
        ended_at=None,
        last_keepalive_at=NOW,
    )
    base.update(fields)
    return domain.Call(**base)


# --- принять, отклонить, повесить трубку -----------------------------------------


def test_вызываемый_принимает_звонок():
    change = domain.accept(make(), B)
    assert change.state is CallState.ACCEPTED
    assert change.changed


def test_звонящий_не_может_принять_свой_звонок():
    assert domain.accept(make(), A).denied


def test_посторонний_не_может_ни_принять_ни_повесить():
    call = make()
    assert domain.accept(call, OTHER).denied
    assert domain.hangup(call, OTHER).denied
    assert domain.decline(call, OTHER).denied


def test_повторное_принятие_ничего_не_меняет():
    change = domain.accept(make(CallState.ACCEPTED), B)
    assert not change.changed
    assert not change.denied


def test_принять_завершённый_звонок_нельзя():
    assert domain.accept(make(CallState.ENDED, EndReason.MISSED), B).gone


def test_вызываемый_отклоняет_звонок():
    change = domain.decline(make(), B)
    assert change.state is CallState.ENDED
    assert change.reason is EndReason.DECLINED


def test_повторный_отказ_ничего_не_меняет():
    call = make(CallState.ENDED, EndReason.DECLINED)
    change = domain.decline(call, B)
    assert not change.changed
    assert not change.denied


def test_звонящий_отменяет_пока_звонит():
    change = domain.hangup(make(), A)
    assert (change.state, change.reason) == (CallState.ENDED, EndReason.CANCELLED)


def test_вызываемый_кладёт_трубку_пока_звонит_это_отказ():
    change = domain.hangup(make(), B)
    assert change.reason is EndReason.DECLINED


def test_трубка_до_появления_медиа_отмена():
    change = domain.hangup(make(CallState.ACCEPTED), B)
    assert change.reason is EndReason.CANCELLED


def test_трубка_в_активном_звонке_завершает_его_успешно():
    change = domain.hangup(make(CallState.ACTIVE), A)
    assert (change.state, change.reason) == (CallState.ENDED, EndReason.COMPLETED)


def test_повторная_трубка_ничего_не_меняет():
    change = domain.hangup(make(CallState.ENDED, EndReason.COMPLETED), A)
    assert not change.changed
    assert not change.denied


def test_медиа_пошло_делает_принятый_звонок_активным():
    change = domain.connected(make(CallState.ACCEPTED), A)
    assert change.state is CallState.ACTIVE


def test_медиа_пошло_до_принятия_отвергается():
    assert domain.connected(make(CallState.RINGING), A).gone


def test_повторное_медиа_пошло_ничего_не_меняет():
    assert not domain.connected(make(CallState.ACTIVE), B).changed


def test_принять_из_другой_вкладки_когда_уже_принято_проигрыш():
    call = make(CallState.ACCEPTED, accepted_by="tab-1")
    assert domain.accept(call, B, "tab-2").taken
    assert not domain.accept(call, B, "tab-1").taken
    assert not domain.accept(call, B, None).taken


def test_обрыв_после_принятия_это_failed():
    for state in (CallState.ACCEPTED, CallState.ACTIVE):
        change = domain.fail(make(state), A)
        assert (change.state, change.reason) == (CallState.ENDED, EndReason.FAILED)


def test_неудача_пока_звонит_невозможна_а_повтор_пуст():
    assert domain.fail(make(CallState.RINGING), A).gone
    assert not domain.fail(make(CallState.ENDED, EndReason.FAILED), A).changed
    assert domain.fail(make(), OTHER).denied


# --- время: подметальщик ---------------------------------------------------------------


def test_звонок_без_ответа_становится_пропущенным():
    call = make(created_at=NOW - timedelta(seconds=domain.RING_SECONDS + 1))
    change = domain.expire(call, NOW)
    assert (change.state, change.reason) == (CallState.ENDED, EndReason.MISSED)


def test_звонок_в_пределах_времени_звонка_не_трогается():
    call = make(created_at=NOW - timedelta(seconds=domain.RING_SECONDS - 1))
    assert not domain.expire(call, NOW).changed


def test_принятый_звонок_без_медиа_долго_завершается_неудачей():
    call = make(
        CallState.ACCEPTED,
        accepted_at=NOW - timedelta(seconds=domain.SETUP_SECONDS + 1),
    )
    change = domain.expire(call, NOW)
    assert (change.state, change.reason) == (CallState.ENDED, EndReason.FAILED)


def test_активный_звонок_с_тишиной_завершается_неудачей():
    call = make(
        CallState.ACTIVE,
        last_keepalive_at=NOW - timedelta(seconds=domain.STALE_SECONDS + 1),
    )
    assert domain.expire(call, NOW).reason is EndReason.FAILED


def test_живой_активный_звонок_не_трогается():
    call = make(CallState.ACTIVE, last_keepalive_at=NOW - timedelta(seconds=10))
    assert not domain.expire(call, NOW).changed


def test_завершённый_звонок_не_истекает():
    call = make(CallState.ENDED, EndReason.COMPLETED, created_at=NOW - timedelta(days=1))
    assert not domain.expire(call, NOW).changed


# --- итог в ленте ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "kind", "code"),
    [
        (EndReason.COMPLETED, CallKind.AUDIO, "call.completed.audio"),
        (EndReason.COMPLETED, CallKind.VIDEO, "call.completed.video"),
        (EndReason.MISSED, CallKind.VIDEO, "call.missed.video"),
        (EndReason.DECLINED, CallKind.AUDIO, "call.declined.audio"),
        (EndReason.CANCELLED, CallKind.AUDIO, "call.cancelled.audio"),
        (EndReason.BUSY, CallKind.AUDIO, "call.busy.audio"),
        (EndReason.UNAVAILABLE, CallKind.AUDIO, "call.unavailable.audio"),
        (EndReason.FAILED, CallKind.VIDEO, "call.failed.video"),
    ],
)
def test_итог_звонка_это_код_для_клиента(reason, kind, code):
    assert domain.summary_code(kind, reason) == code


def test_длительность_только_у_состоявшегося_звонка():
    done = make(
        CallState.ENDED, EndReason.COMPLETED,
        active_at=NOW, ended_at=NOW + timedelta(seconds=75),
    )
    assert domain.duration_ms(done) == 75_000
    missed = make(CallState.ENDED, EndReason.MISSED, ended_at=NOW)
    assert domain.duration_ms(missed) is None


def test_звонок_в_ленте_виден_участнику_как_исходящий_у_звонящего():
    assert domain.summary_sender(make()) == A


# --- сигналы ---------------------------------------------------------------------------


def test_offer_с_sdp_принимается():
    signal = domain.parse_signal({"type": "offer", "sdp": "v=0\r\n"})
    assert signal is not None and signal.kind == "offer"


def test_answer_принимается():
    assert domain.parse_signal({"type": "answer", "sdp": "v=0\r\n"}) is not None


def test_кандидаты_принимаются_пачкой():
    data = {
        "type": "ice",
        "candidates": [
            {"candidate": "candidate:1 1 udp 1 10.0.0.1 5000 typ host",
             "sdpMid": "0", "sdpMLineIndex": 0},
            {"candidate": "", "sdpMid": "0", "sdpMLineIndex": 0},
        ],
    }
    signal = domain.parse_signal(data)
    assert signal is not None and len(signal.candidates) == 2


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"type": "bye"},
        {"type": "offer"},
        {"type": "offer", "sdp": 5},
        {"type": "offer", "sdp": ""},
        {"type": "ice"},
        {"type": "ice", "candidates": []},
        {"type": "ice", "candidates": "x"},
        {"type": "ice", "candidates": [{"candidate": 5}]},
        {"type": "ice", "candidates": [{"candidate": "x" * 2000}]},
        {"type": "ice", "candidates": [{"candidate": "x"}] * (domain.MAX_CANDIDATES + 1)},
        {"type": "offer", "sdp": "v" * (domain.MAX_SIGNAL_BYTES + 1)},
    ],
)
def test_негодный_сигнал_отвергается(data):
    assert domain.parse_signal(data) is None


def test_лишние_поля_сигнала_не_передаются_дальше():
    signal = domain.parse_signal({"type": "offer", "sdp": "v=0", "evil": {"x": 1}})
    assert signal is not None
    assert "evil" not in domain.signal_event(call=make(), signal=signal, seq=1)["signal"]


# --- события и каналы ------------------------------------------------------------------


def test_канал_звонков_личный_и_без_истории():
    assert domain.call_channel(B) == f"call:{B}"


def test_событие_входящего_звонка_несёт_всё_для_экрана():
    call = make()
    event = domain.incoming_event(call, caller_name="Alice")
    assert event["type"] == "call.incoming"
    assert event["call_id"] == str(call.call_id)
    assert event["conversation_id"] == str(call.conversation_id)
    assert event["kind"] == "audio"
    assert event["caller"] == {"user_id": str(A), "display_name": "Alice"}
    assert event["version"] == call.version


def test_событие_состояния_несёт_номер_и_причину():
    call = make(CallState.ENDED, EndReason.MISSED, version=3)
    event = domain.state_event(call)
    assert event == {
        "type": "call.state",
        "call_id": str(call.call_id),
        "state": "ended",
        "reason": "missed",
        "version": 3,
    }


def test_принято_в_другой_вкладке_отличается_от_принято_здесь():
    call = make(CallState.ACCEPTED, version=2)
    event = domain.state_event(call, accepted_elsewhere=True)
    assert event["reason"] == "accepted_elsewhere"


def test_событие_сигнала_несёт_номер_сигнала():
    signal = domain.parse_signal({"type": "offer", "sdp": "v=0"})
    event = domain.signal_event(call=make(), signal=signal, seq=7)
    assert event["type"] == "call.signal"
    assert event["seq"] == 7
    assert event["signal"] == {"type": "offer", "sdp": "v=0"}
