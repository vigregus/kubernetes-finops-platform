"""«Печатает»: правила без Centrifugo и Redis (RT-003, SEC-008)."""
from __future__ import annotations

import uuid

import pytest

from messenger.domain import typing_indicator as domain

CONVERSATION = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER = "11111111-1111-1111-1111-111111111111"


def test_канал_набора_строится_из_идентификатора_беседы():
    assert domain.typing_channel(CONVERSATION) == f"typing:{CONVERSATION}"


def test_канал_разбирается_только_в_своём_пространстве():
    assert domain.parse_typing_channel(f"typing:{CONVERSATION}") == CONVERSATION
    for foreign in (
        f"conversation:{CONVERSATION}",
        f"user:{USER}",
        "typing:",
        "typing:не-uuid",
        f"typing:{CONVERSATION}:x",
        "",
    ):
        assert domain.parse_typing_channel(foreign) is None


@pytest.mark.parametrize("data", [{"state": "typing"}, {"state": "stop"}])
def test_допустимые_сообщения_клиента(data):
    assert domain.parse_client_message(data) is domain.TypingState(data["state"])


@pytest.mark.parametrize(
    "data",
    [None, [], "typing", {}, {"state": "dancing"}, {"state": 1}, {"STATE": "typing"}],
)
def test_негодное_сообщение_клиента_не_разбирается(data):
    assert domain.parse_client_message(data) is None


def test_автор_берётся_из_соединения_а_не_из_тела():
    # Клиент написал в тело чужого автора и свой срок — оба игнорируются.
    forged = {"state": "typing", "user_id": "другой", "expires_in_ms": 999_999}
    state = domain.parse_client_message(forged)
    payload = domain.event_payload(USER, state)
    assert payload == {"user_id": USER, "expires_in_ms": domain.EXPIRES_IN_MS}


def test_остановка_гасит_индикатор_сразу():
    payload = domain.event_payload(USER, domain.TypingState.STOP)
    assert payload == {"user_id": USER, "expires_in_ms": 0}


def test_срок_индикатора_переживает_сердцебиение_с_запасом_но_гаснет_быстро():
    # Запас на дрожание сети: одно запоздавшее сердцебиение не гасит индикатор.
    assert domain.EXPIRES_IN_MS >= 1.5 * domain.HEARTBEAT_MS
    # И гаснет за считаные секунды, как требует `RT-001` (4–5 с у получателя).
    assert domain.EXPIRES_IN_MS <= 5000
