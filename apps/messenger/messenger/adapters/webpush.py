"""Отправка Web Push: шифрование нагрузки (RFC 8291) и подпись VAPID (RFC 8292).

Единственное место, знающее про протокол. Нагрузка **всегда** шифруется
ключами подписки браузера: провайдер push (FCM, Mozilla, Apple) видит
только факт и время, а текст расшифровать не может. Подпись VAPID —
идентификация нашего сервера перед провайдером; PyJWT уже в зависимостях,
отдельная библиотека ради одной подписи не нужна.

Исходы провайдера разведены по смыслу, потому что реакция у них разная:
`GONE` — подписка мертва, её удаляют (`NTF-002`); `REJECTED` — запрос негоден
(подпись, размер), повторять бессмысленно, но и удалять подписку нельзя: она
не виновата; `RETRY` — временное, подписка цела.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

import http_ece
import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from messenger.domain.push import PushSubscription

log = logging.getLogger(__name__)

# Подпись VAPID живёт часы, а не сутки: потолок спецификации — 24 часа.
_VAPID_TTL_SECONDS = 12 * 3600


class PushResult(str, Enum):
    DELIVERED = "delivered"
    GONE = "gone"
    REJECTED = "rejected"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class VapidSettings:
    private_key_pem: str
    # `mailto:` или `https:` — по нему провайдер может связаться с оператором.
    subject: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def generate_vapid_keys() -> tuple[str, str]:
    """Пара ключей VAPID: закрытый PEM и открытый base64url (несжатая точка)."""
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return private, _b64(public)


def public_key_of(private_key_pem: str) -> str:
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)  # noqa: S101 - формат задаёт генератор
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64(public)


class WebPushSender:
    def __init__(self, settings: VapidSettings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._private = serialization.load_pem_private_key(
            settings.private_key_pem.encode(), password=None
        )
        self.public_key = public_key_of(settings.private_key_pem)

    def _authorization(self, endpoint: str) -> str:
        parts = urlsplit(endpoint)
        claims = {
            "aud": f"{parts.scheme}://{parts.netloc}",
            "exp": int(time.time()) + _VAPID_TTL_SECONDS,
            "sub": self._settings.subject,
        }
        token = jwt.encode(claims, self._private, algorithm="ES256")
        return f"vapid t={token}, k={self.public_key}"

    async def send(
        self,
        subscription: PushSubscription,
        payload: dict[str, object],
        *,
        ttl: int,
        topic: str,
    ) -> PushResult:
        body = http_ece.encrypt(
            json.dumps(payload, separators=(",", ":")).encode(),
            private_key=ec.generate_private_key(ec.SECP256R1()),
            dh=_unb64(subscription.p256dh),
            auth_secret=_unb64(subscription.auth),
            version="aes128gcm",
        )
        headers = {
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(ttl),
            "Topic": topic,
            "Urgency": "normal",
            "Authorization": self._authorization(subscription.endpoint),
        }
        try:
            response = await self._http.post(subscription.endpoint, content=body, headers=headers)
        except httpx.HTTPError:
            return PushResult.RETRY
        status = response.status_code
        if status in (200, 201, 202):
            return PushResult.DELIVERED
        if status in (404, 410):
            return PushResult.GONE
        if status == 429 or status >= 500:
            return PushResult.RETRY
        log.warning(
            "провайдер push отклонил запрос",
            extra={"event": "push_delivery", "result": "failed",
                   "error_code": f"http_{status}"},
        )
        return PushResult.REJECTED
