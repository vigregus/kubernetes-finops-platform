"""Отправка Web Push: шифрование для браузера, подпись VAPID, исходы провайдера."""
from __future__ import annotations

import asyncio
import base64
import json
import time

import http_ece
import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from messenger.adapters.webpush import PushResult, VapidSettings, WebPushSender, generate_vapid_keys
from messenger.domain.push import PushSubscription


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class Browser:
    """Получатель: пара ключей и секрет аутентификации, как у подписки в браузере."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"\x07" * 16
        public = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.subscription = PushSubscription(
            endpoint="https://fcm.googleapis.com/fcm/send/xyz",
            p256dh=b64(public),
            auth=b64(self.auth),
        )

    def decrypt(self, body: bytes) -> bytes:
        return http_ece.decrypt(
            body, private_key=self.key, auth_secret=self.auth, version="aes128gcm"
        )


def sender(handler, **kw):
    private, public = generate_vapid_keys()
    settings = VapidSettings(private_key_pem=private, subject="mailto:ops@example.org")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebPushSender(settings, client), public


def run(coro):
    return asyncio.run(coro)


def test_полезная_нагрузка_шифруется_для_ключей_подписки():
    browser = Browser()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["headers"] = dict(request.headers)
        return httpx.Response(201)

    s, _ = sender(handler)
    result = run(s.send(browser.subscription, {"type": "message", "conversation_id": "c-1"},
                        ttl=3600, topic="abc"))
    assert result is PushResult.DELIVERED
    # Провайдер видит только шифртекст; расшифровать может только браузер.
    assert b"conversation_id" not in seen["body"]
    expected = {"type": "message", "conversation_id": "c-1"}
    assert json.loads(browser.decrypt(seen["body"])) == expected
    assert seen["headers"]["content-encoding"] == "aes128gcm"
    assert seen["headers"]["ttl"] == "3600"
    assert seen["headers"]["topic"] == "abc"


def test_подпись_vapid_проверяется_открытым_ключом_сервера():
    browser = Browser()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(201)

    s, public = sender(handler)
    payload = {"type": "message", "conversation_id": "c"}
    run(s.send(browser.subscription, payload, ttl=60, topic="t"))
    scheme, _, rest = seen["auth"].partition(" ")
    assert scheme == "vapid"
    fields = dict(part.strip().split("=", 1) for part in rest.split(","))
    assert fields["k"] == public
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(public))
    claims = jwt.decode(fields["t"], key, algorithms=["ES256"], audience="https://fcm.googleapis.com")
    assert claims["sub"] == "mailto:ops@example.org"
    assert claims["exp"] > time.time()


def test_исходы_провайдера_разведены_по_смыслу():
    browser = Browser()
    cases = {
        201: PushResult.DELIVERED,
        202: PushResult.DELIVERED,
        404: PushResult.GONE,
        410: PushResult.GONE,
        400: PushResult.REJECTED,
        401: PushResult.REJECTED,
        403: PushResult.REJECTED,
        413: PushResult.REJECTED,
        429: PushResult.RETRY,
        500: PushResult.RETRY,
        503: PushResult.RETRY,
    }
    for status, expected in cases.items():
        s, _ = sender(lambda request, status=status: httpx.Response(status))
        got = run(s.send(browser.subscription, {"type": "message", "conversation_id": "c"},
                         ttl=60, topic="t"))
        assert got is expected, status


def test_сетевой_отказ_это_повтор_а_не_конец_подписки():
    browser = Browser()

    def handler(request):
        raise httpx.ConnectError("нет сети")

    s, _ = sender(handler)
    got = run(s.send(browser.subscription, {"type": "message", "conversation_id": "c"},
                     ttl=60, topic="t"))
    assert got is PushResult.RETRY


def test_ключи_vapid_генерируются_парой():
    private, public = generate_vapid_keys()
    assert "BEGIN PRIVATE KEY" in private
    assert len(unb64(public)) == 65 and unb64(public)[0] == 4
