"""Web Push на уровне сервиса: кому уходит уведомление и что делают исходы (NTF-001…004)."""
from __future__ import annotations

import asyncio
import base64
import uuid

import pytest

from messenger.adapters.webpush import PushResult
from messenger.domain import push as domain
from messenger.domain.ids import DeviceId, UserId
from messenger.repositories.push import PushTarget
from messenger.services import push as service

CONVERSATION = str(uuid.uuid4())
RECIPIENT = uuid.uuid4()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def subscription_json(endpoint: str = "https://fcm.googleapis.com/fcm/send/abc") -> dict:
    return {"endpoint": endpoint,
            "keys": {"p256dh": b64(b"\x04" + b"\x01" * 64), "auth": b64(b"\x02" * 16)}}


def target(*, online=False, endpoint="https://fcm.googleapis.com/fcm/send/abc") -> PushTarget:
    return PushTarget(device_id=DeviceId(uuid.uuid4()), user_id=UserId(RECIPIENT),
                      subscription=subscription_json(endpoint), online=online)


def event(**overrides) -> dict:
    return {"event_type": "message.created", "conversation_id": CONVERSATION,
            "recipient_ids": [str(RECIPIENT)], "message_id": str(uuid.uuid4()),
            "sender_id": str(uuid.uuid4()), **overrides}


class Sender:
    def __init__(self, result=PushResult.DELIVERED):
        self.result = result
        self.calls: list[dict] = []

    async def send(self, subscription, payload, *, ttl, topic):
        self.calls.append({"endpoint": subscription.endpoint, "payload": payload,
                           "ttl": ttl, "topic": topic})
        return self.result


@pytest.fixture
def repo(monkeypatch):
    class Repo:
        targets: list[PushTarget] = []
        cleared: list[tuple] = []

    async def list_targets(conn, *, user_ids, online_window):
        Repo.last_user_ids = list(user_ids)
        return Repo.targets

    async def clear_if_endpoint(conn, *, device_id, endpoint):
        Repo.cleared.append((device_id, endpoint))
        return True

    Repo.targets = []
    Repo.cleared = []
    monkeypatch.setattr(service.repo, "list_targets", list_targets)
    monkeypatch.setattr(service.repo, "clear_if_endpoint", clear_if_endpoint)
    monkeypatch.setenv("PUSH_ENABLED", "true")
    return Repo


def notify(sender, body=None):
    return asyncio.run(service.notify_message(None, sender=sender, body=body or event()))


def test_офлайн_устройство_получает_сигнал_без_текста(repo):
    repo.targets = [target()]
    sender = Sender()
    outcome = notify(sender)
    assert outcome.sent == 1
    (call,) = sender.calls
    # Только тип и беседа: ни текста, ни имени отправителя (Р-5, SEC-010).
    assert call["payload"] == {"type": "message", "conversation_id": CONVERSATION}
    assert call["topic"] == domain.collapse_topic(CONVERSATION)
    assert call["ttl"] == 24 * 3600
    assert str(RECIPIENT) in {str(u) for u in repo.last_user_ids}


def test_устройство_с_живым_соединением_не_уведомляется(repo):
    repo.targets = [target(online=True)]
    sender = Sender()
    outcome = notify(sender)
    assert (outcome.sent, outcome.skipped_online) == (0, 1)
    assert sender.calls == []


def test_из_нескольких_устройств_уведомляются_только_офлайн(repo):
    repo.targets = [target(online=True), target(online=False), target(online=False)]
    sender = Sender()
    outcome = notify(sender)
    assert (outcome.sent, outcome.skipped_online) == (2, 1)


def test_подписка_отозванная_браузером_удаляется_без_повторов(repo):
    repo.targets = [target()]
    sender = Sender(PushResult.GONE)
    outcome = notify(sender)
    assert outcome.gone == 1
    assert len(repo.cleared) == 1
    assert repo.cleared[0][1] == "https://fcm.googleapis.com/fcm/send/abc"
    assert len(sender.calls) == 1


@pytest.mark.parametrize("result", [PushResult.RETRY, PushResult.REJECTED])
def test_отказ_провайдера_подписку_не_удаляет(repo, result):
    repo.targets = [target()]
    outcome = notify(Sender(result))
    assert repo.cleared == []
    assert (outcome.retry + outcome.rejected) == 1


def test_хранимая_подписка_вне_списка_провайдеров_снимается_и_не_отправляется(repo):
    repo.targets = [target(endpoint="https://evil.example.org/x")]
    sender = Sender()
    outcome = notify(sender)
    assert outcome.invalid == 1
    assert sender.calls == []
    assert len(repo.cleared) == 1


@pytest.mark.parametrize(
    "body",
    [
        event(event_type="message.deleted"),
        event(recipient_ids=[]),
        event(recipient_ids="x"),
        event(conversation_id=None),
        event(recipient_ids=["не-uuid"]),
    ],
)
def test_чужое_и_негодное_событие_игнорируется(repo, body):
    repo.targets = [target()]
    sender = Sender()
    assert notify(sender, body).ignored
    assert sender.calls == []


def test_выключенный_push_ничего_не_отправляет(repo, monkeypatch):
    monkeypatch.setenv("PUSH_ENABLED", "false")
    repo.targets = [target()]
    sender = Sender()
    assert notify(sender).ignored
    assert sender.calls == []


def test_подписка_без_ключа_vapid_или_негодная_не_принимается(monkeypatch):
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    result = asyncio.run(service.subscribe(
        None, device_id=DeviceId(uuid.uuid4()), user_id=UserId(RECIPIENT),
        data=subscription_json()))
    assert result.reason == "unavailable"

    monkeypatch.setenv("VAPID_PUBLIC_KEY", "pub")
    bad = asyncio.run(service.subscribe(
        None, device_id=DeviceId(uuid.uuid4()), user_id=UserId(RECIPIENT),
        data=subscription_json("https://169.254.169.254/x")))
    assert bad.reason == "invalid"


def test_подписка_записывается_на_своё_устройство(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("PUSH_ENABLED", "true")
    stored: dict = {}

    async def set_subscription(conn, *, device_id, user_id, subscription):
        stored.update(device_id=device_id, user_id=user_id, subscription=subscription)
        return True

    monkeypatch.setattr(service.repo, "set_subscription", set_subscription)
    device = DeviceId(uuid.uuid4())
    result = asyncio.run(service.subscribe(
        None, device_id=device, user_id=UserId(RECIPIENT), data=subscription_json()))
    assert result.ok
    assert stored["device_id"] == device
    assert stored["subscription"]["endpoint"] == "https://fcm.googleapis.com/fcm/send/abc"
