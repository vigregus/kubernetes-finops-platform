"""Web Push: подписка, допустимые адреса, политика уведомления (NTF-002…004)."""
from __future__ import annotations

import base64

import pytest

from messenger.domain import push as domain

HOSTS = domain.DEFAULT_ALLOWED_HOSTS


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


P256DH = b64(b"\x04" + b"\x01" * 64)
AUTH = b64(b"\x02" * 16)


def sub(endpoint: str = "https://fcm.googleapis.com/fcm/send/abc", **keys):
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": keys.get("p256dh", P256DH), "auth": keys.get("auth", AUTH)},
    }


def test_подписка_с_известного_провайдера_разбирается():
    parsed = domain.parse_subscription(sub(), HOSTS)
    assert parsed is not None
    assert parsed.endpoint == "https://fcm.googleapis.com/fcm/send/abc"
    assert parsed.p256dh == P256DH
    assert parsed.auth == AUTH


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://updates.push.services.mozilla.com/wpush/v2/xyz",
        "https://web.push.apple.com/QWER",
        "https://wns2-par02p.notify.windows.com/w/?token=abc",
    ],
)
def test_другие_провайдеры_браузеров_допускаются(endpoint):
    assert domain.parse_subscription(sub(endpoint), HOSTS) is not None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/abc",  # не https
        "https://evil.example.org/push",  # чужой хост: SSRF
        "https://fcm.googleapis.com.evil.org/x",  # суффикс-обманка
        "https://evilfcm.googleapis.com/x",  # не поддомен через точку
        "https://169.254.169.254/latest/meta-data",  # адрес, а не имя
        "https://localhost/x",
        "https://user:pw@fcm.googleapis.com/x",  # учётные данные в адресе
        "https://fcm.googleapis.com:8443/x",  # нестандартный порт
        "https://fcm.googleapis.com/" + "a" * 2100,  # слишком длинный
        "ftp://fcm.googleapis.com/x",
        "",
    ],
)
def test_адрес_вне_списка_провайдеров_отвергается(endpoint):
    assert domain.parse_subscription(sub(endpoint), HOSTS) is None


def test_сервер_не_ходит_на_чужой_адрес_даже_если_клиент_просит():
    # Главная причина списка: подписка — это адрес, на который **сервер**
    # отправит запрос по просьбе клиента.
    assert domain.parse_subscription(sub("https://127.0.0.1/x"), HOSTS) is None


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"endpoint": "https://fcm.googleapis.com/x"},
        {"endpoint": "https://fcm.googleapis.com/x", "keys": {}},
        {"endpoint": 5, "keys": {"p256dh": P256DH, "auth": AUTH}},
        sub(p256dh="не-base64!!"),
        sub(p256dh=b64(b"\x04" * 10)),  # не 65 байт
        sub(p256dh=b64(b"\x05" + b"\x01" * 64)),  # не несжатая точка
        sub(auth=b64(b"\x01" * 4)),  # не 16 байт
    ],
)
def test_негодная_подписка_не_разбирается(data):
    assert domain.parse_subscription(data, HOSTS) is None


def test_дополнительный_хост_из_настройки_допускается():
    hosts = domain.allowed_hosts("push-sink.messenger.svc.cluster.local")
    assert domain.parse_subscription(sub("https://push-sink.messenger.svc.cluster.local/x"), hosts)
    assert domain.parse_subscription(sub("https://fcm.googleapis.com/x"), hosts)


def test_уведомление_содержит_только_сигнал_без_текста():
    payload = domain.notification_payload(conversation_id="c-1")
    # Текст сообщения и имя отправителя в уведомление не попадают: оно
    # открывает беседу, а читает человек в приложении.
    assert payload == {"type": "message", "conversation_id": "c-1"}


def test_ключ_свёртки_один_на_беседу_и_не_раскрывает_идентификатор():
    a = domain.collapse_topic("conv-1")
    assert a == domain.collapse_topic("conv-1")
    assert a != domain.collapse_topic("conv-2")
    assert "conv-1" not in a and len(a) <= 32 and a.isalnum()


def test_уведомляют_только_устройство_без_живого_соединения():
    assert domain.should_notify(has_subscription=True, online=False)
    assert not domain.should_notify(has_subscription=True, online=True)
    assert not domain.should_notify(has_subscription=False, online=False)
