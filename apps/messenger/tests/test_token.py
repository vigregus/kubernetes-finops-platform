"""Проверка токена. Без сети: ключи кладутся в кеш прямо в тесте.

Здесь проверяется не PyJWT, а то, что ни одна проверка не пропущена.
Пропущенная аудитория означает, что токен любого другого клиента того же
реалма открывает наше API; пропущенный издатель — что его открывает токен
из чужого Keycloak; принятый алгоритм из заголовка — что подпись можно
не предъявлять вовсе.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK
from jwt.utils import to_base64url_uint

from messenger.adapters import oidc
from messenger.domain.identity import TokenRejection

ISSUER = "https://idp.finops.local/realms/messenger"
AUDIENCE = "messenger-api"
KID = "ключ-1"


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(private, *, kid: str, use: str = "sig", alg: str = "RS256") -> dict:
    numbers = private.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": use,
        "alg": alg,
        "n": to_base64url_uint(numbers.n).decode(),
        "e": to_base64url_uint(numbers.e).decode(),
    }


@pytest.fixture(scope="module")
def key():
    return _rsa_key()


@pytest.fixture
def settings():
    return oidc.OidcSettings(
        issuer=ISSUER,
        # Адрес заведомо недостижимый: если проверка всё-таки пойдёт
        # в сеть, тест обязан это показать, а не молча замедлиться.
        jwks_url="http://ключей-нет.invalid/certs",
        audience=AUDIENCE,
    )


@pytest.fixture
def keys(settings, key):
    """Кеш с одним живым ключом и запретом ходить за новыми."""
    cache = oidc.JwksCache(settings=settings)
    now = time.monotonic()
    cache._keys = {KID: PyJWK.from_dict(_jwk(key, kid=KID))}
    cache._fetched_at = now
    cache._last_attempt = now
    return cache


def _token(key, *, kid: str = KID, issuer: str = ISSUER, audience: str = AUDIENCE,
           lifetime: timedelta = timedelta(minutes=5), claims: dict | None = None) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": "8f3c1e2a-0000-4000-8000-000000000001",
        "iss": issuer,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "email": "Anya@Example.ORG",
        "email_verified": True,
        "name": "Аня",
        "sid": "сессия-1",
    }
    payload.update(claims or {})
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


def verify(token, keys, settings):
    return asyncio.run(oidc.verify_access_token(token, keys=keys, settings=settings))


def test_нормальный_токен_принят(key, keys, settings):
    check = verify(_token(key), keys, settings)
    assert check.ok, check.rejection
    assert check.claims.subject == "8f3c1e2a-0000-4000-8000-000000000001"
    assert check.claims.email_verified is True
    assert check.claims.display_name == "Аня"
    assert check.claims.session_state == "сессия-1"


def test_истёкший_токен_отклонён(key, keys, settings):
    check = verify(_token(key, lifetime=timedelta(minutes=-10)), keys, settings)
    assert check.rejection is TokenRejection.EXPIRED


def test_чужая_аудитория_отклонена(key, keys, settings):
    """Токен выдан другому клиенту того же реалма.

    Без этой проверки вход в любое приложение рядом открывает наше API.
    """
    check = verify(_token(key, audience="другое-приложение"), keys, settings)
    assert check.rejection is TokenRejection.WRONG_AUDIENCE


def test_чужой_издатель_отклонён(key, keys, settings):
    """Тот же ключ, другой Keycloak в `iss`."""
    check = verify(_token(key, issuer="https://idp.чужой.local/realms/messenger"), keys, settings)
    assert check.rejection is TokenRejection.WRONG_ISSUER


def test_подпись_чужим_ключом_отклонена(keys, settings):
    """Тест отказа: `kid` известный, подпись — от другого ключа."""
    подделка = _rsa_key()
    check = verify(_token(подделка), keys, settings)
    assert check.rejection is TokenRejection.BAD_SIGNATURE


def test_алгоритм_из_заголовка_не_принимается(keys, settings):
    """`alg: none` — это предъявление токена без подписи вообще."""
    payload = {"sub": "кто-то", "iss": ISSUER, "aud": AUDIENCE,
               "iat": 0, "exp": 9999999999}
    token = jwt.encode(payload, key=None, algorithm="none", headers={"kid": KID})
    check = verify(token, keys, settings)
    assert not check.ok


def test_токен_без_обязательного_поля_отклонён(key, keys, settings):
    token = _token(key, claims={"sub": None})
    # `sub` = None остаётся полем, поэтому убираем его честно: пересобираем
    # полезную нагрузку без него.
    now = datetime.now(UTC)
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "iat": int(now.timestamp()),
         "exp": int((now + timedelta(minutes=5)).timestamp())},
        key, algorithm="RS256", headers={"kid": KID},
    )
    check = verify(token, keys, settings)
    assert check.rejection is TokenRejection.MISSING_CLAIM


def test_неизвестный_ключ_отличим_от_отсутствия_ключей(key, keys, settings):
    """Две разные беды: ротация и недоступный Keycloak.

    Всплеск первого означает, что ключи разъехались; всплеск второго —
    что проверять нечем, и ответ должен быть 503, а не 401.
    """
    check = verify(_token(key, kid="ключ-которого-нет"), keys, settings)
    assert check.rejection is TokenRejection.UNKNOWN_KEY

    пустой = oidc.JwksCache(settings=settings)
    пустой._last_attempt = time.monotonic()
    check = verify(_token(key), пустой, settings)
    assert check.rejection is TokenRejection.KEYS_UNAVAILABLE


def test_мусор_вместо_токена(keys, settings):
    assert verify("не-токен", keys, settings).rejection is TokenRejection.MALFORMED


def test_ключ_шифрования_не_годится_для_подписи(key):
    """У реалма два ключа. Принять подпись ключом шифрования — ошибка
    проверки, а не мелочь."""
    документ = {"keys": [
        _jwk(key, kid="подпись", use="sig", alg="RS256"),
        _jwk(_rsa_key(), kid="шифрование", use="enc", alg="RSA-OAEP"),
    ]}
    отобранные = oidc.usable_keys(документ)
    assert list(отобранные) == ["подпись"]


def test_ротация_сохраняет_старый_ключ_в_окне_перекрытия(
    monkeypatch, settings, key
):
    """ROT-001: новый ``kid`` не разлогинивает токены старого ключа.

    При промахе кеш перечитывает весь JWKS. В окне перекрытия документ
    содержит оба ключа: новый нужен новым токенам, старый — уже выданным.
    Проверяем оба направления после одного обновления, а не только число
    ключей в JSON.
    """
    новый = _rsa_key()
    старый_kid = "ключ-до-ротации"
    новый_kid = "ключ-после-ротации"
    cache = oidc.JwksCache(
        settings=oidc.OidcSettings(
            issuer=settings.issuer,
            jwks_url=settings.jwks_url,
            audience=settings.audience,
            min_refresh_seconds=0,
        )
    )
    cache._keys = {старый_kid: PyJWK.from_dict(_jwk(key, kid=старый_kid))}
    cache._fetched_at = time.monotonic()

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "keys": [
                    _jwk(key, kid=старый_kid),
                    _jwk(новый, kid=новый_kid),
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, url):
            assert url == settings.jwks_url
            return Response()

    monkeypatch.setattr(oidc.httpx, "AsyncClient", lambda **kwargs: Client())

    новый_токен = _token(новый, kid=новый_kid)
    старый_токен = _token(key, kid=старый_kid)
    assert verify(новый_токен, cache, cache.settings).ok
    assert verify(старый_токен, cache, cache.settings).ok


def test_backchannel_logout_token_проверяется_отдельным_контрактом(
    key, keys, settings
):
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": "messenger-web",
            "iat": int(now.timestamp()),
            "jti": "logout-1",
            "sid": "сессия-1",
            "events": {oidc.BACKCHANNEL_LOGOUT_EVENT: {}},
        },
        key,
        algorithm="RS256",
        headers={"kid": KID},
    )
    check = asyncio.run(
        oidc.verify_logout_token(
            token,
            keys=keys,
            settings=settings,
            audience="messenger-web",
        )
    )
    assert check.ok and check.claims.session_state == "сессия-1"


@pytest.mark.parametrize(
    "extra",
    [
        {"events": {"другое-событие": {}}},
        {"nonce": "logout-token-never-has-nonce"},
    ],
)
def test_не_logout_event_не_может_отозвать_сессию(key, keys, settings, extra):
    now = datetime.now(UTC)
    payload = {
        "iss": ISSUER,
        "aud": "messenger-web",
        "iat": int(now.timestamp()),
        "jti": "logout-invalid",
        "sid": "сессия-1",
        "events": {oidc.BACKCHANNEL_LOGOUT_EVENT: {}},
        **extra,
    }
    token = jwt.encode(payload, key, algorithm="RS256", headers={"kid": KID})
    check = asyncio.run(
        oidc.verify_logout_token(
            token, keys=keys, settings=settings, audience="messenger-web"
        )
    )
    assert check.rejection is TokenRejection.MALFORMED
