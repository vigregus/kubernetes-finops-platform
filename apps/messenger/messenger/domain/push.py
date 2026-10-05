"""Web Push без знания о Kafka, Postgres и HTTP.

Подписка — это **адрес, на который сервер отправит запрос по просьбе клиента**.
Поэтому главное правило здесь — не формат ключей, а список провайдеров:
без него любой вошедший человек заставил бы наш сервер обращаться к
внутренним адресам кластера и облака (SSRF). Принимаются только адреса
известных служб push браузеров, по `https`, без учётных данных и портов.

Уведомление несёт **сигнал, а не текст** (`11-threat-model.md`, Р-5): какое
сообщение пришло, читает человек в приложении. Содержимое сообщения в
уведомление не попадает — для этого потребителю пришлось бы выдать право
на топик содержимого (`SEC-010`).
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_ENDPOINT_LENGTH = 2048

# Службы push основных браузеров. Совпадение — по границе имени: сам хост или
# поддомен через точку, а не «заканчивается на строку» (`evilfcm.googleapis.com`
# и `fcm.googleapis.com.evil.org` не проходят).
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "fcm.googleapis.com",  # Chrome, Edge, Opera, Brave
    "push.services.mozilla.com",  # Firefox (updates.push.services.mozilla.com)
    "push.apple.com",  # Safari (web.push.apple.com)
    "notify.windows.com",  # Edge на Windows (wns2-*.notify.windows.com)
)


def allowed_hosts(extra: str = "") -> tuple[str, ...]:
    """Список провайдеров плюс дополнительные из настройки (через запятую).

    Дополнение нужно проверкам: подписка с адресом приёмника-имитации
    допускается только там, где его явно назвали в окружении.
    """
    more = tuple(h.strip().lower() for h in extra.split(",") if h.strip())
    return DEFAULT_ALLOWED_HOSTS + more


@dataclass(frozen=True, slots=True)
class PushSubscription:
    endpoint: str
    p256dh: str
    auth: str

    def to_json(self) -> dict[str, object]:
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


def _b64url_len(value: object) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return None


def _host_allowed(host: str, hosts: tuple[str, ...]) -> bool:
    return any(host == allowed or host.endswith("." + allowed) for allowed in hosts)


def parse_subscription(data: object, hosts: tuple[str, ...]) -> PushSubscription | None:
    """Подписка из тела запроса или `None`, если ей нельзя верить."""
    if not isinstance(data, dict):
        return None
    endpoint = data.get("endpoint")
    keys = data.get("keys")
    if not isinstance(endpoint, str) or not isinstance(keys, dict):
        return None
    if not endpoint or len(endpoint) > MAX_ENDPOINT_LENGTH:
        return None

    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port not in (None, 443):
        return None
    # Адрес вместо имени — не провайдер: проверка по списку имён это и так
    # отсекла бы, но явный отказ называет причину и защищает от будущей правки
    # списка, в которую кто-нибудь положил бы адрес.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    if not _host_allowed(host, hosts):
        return None

    p256dh_raw = _b64url_len(keys.get("p256dh"))
    auth_raw = _b64url_len(keys.get("auth"))
    # Ключ получателя — несжатая точка P-256 (0x04 + 64 байта); секрет
    # аутентификации — 16 байт (RFC 8291).
    if p256dh_raw is None or len(p256dh_raw) != 65 or p256dh_raw[0] != 0x04:
        return None
    if auth_raw is None or len(auth_raw) != 16:
        return None
    return PushSubscription(endpoint=endpoint, p256dh=keys["p256dh"], auth=keys["auth"])


def notification_payload(*, conversation_id: str) -> dict[str, object]:
    """Что уходит в push: сигнал и беседа, чтобы нажатие открыло нужную."""
    return {"type": "message", "conversation_id": conversation_id}


def collapse_topic(conversation_id: str) -> str:
    """Заголовок `Topic`: новый сигнал той же беседы заменяет ещё не доставленный.

    Пять сообщений подряд, пока устройство спит, дают одно уведомление, а не
    пять. Значение — хэш, а не идентификатор: оно проходит через провайдера.
    """
    digest = hashlib.sha256(conversation_id.encode()).digest()
    return base64.urlsafe_b64encode(digest)[:22].decode().replace("-", "A").replace("_", "B")


def should_notify(*, has_subscription: bool, online: bool) -> bool:
    """Политика по **состоянию устройства**, а не пользователя (`NTF-003`, `NTF-004`).

    У устройства с живым соединением приложение открыто и получит сообщение
    через Centrifugo; уведомление было бы вторым путём доставки. Уведомляют
    только устройства с подпиской и без соединения.
    """
    return has_subscription and not online
