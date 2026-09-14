"""Проверка токена по открытым ключам реалма.

Ключи берутся с внутреннего адреса Keycloak, а издатель сверяется
с внешним. Это не небрежность, а свойство окружения, проверенное
запросом: `hostname` у Keycloak задан как `https://idp.finops.local`,
и в discovery — а значит и в токенах — стоит именно он, каким бы путём
ни пришёл запрос. Ходить за ключами по этому адресу изнутри кластера
нечем: имя не резолвится, а сертификат выписан локальным центром.
Отсюда две разные настройки вместо одной.

Сам разбор токена делает PyJWT. Единственное, чего ему нельзя доверить, —
выбор алгоритма: алгоритм, взятый из заголовка токена, позволяет
предъявить `alg: none` или подписать HS256 открытым ключом. Поэтому
список алгоритмов задаётся здесь и не зависит от того, что написано
в токене.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
import jwt
from jwt import PyJWK

from messenger.domain.identity import Claims, TokenCheck, TokenRejection

# Единственный допустимый алгоритм подписи. Список, а не строка: при
# ротации на другой алгоритм здесь окажутся оба, и это будет видно.
ALGORITHMS = ("RS256",)


@dataclass(frozen=True, slots=True)
class OidcSettings:
    """Издатель, ключи и аудитория. Три разные вещи, и путать их дорого."""

    # Что обязано стоять в `iss`. Внешний адрес — тот, что в токене.
    issuer: str
    # Откуда берутся ключи. Внутренний адрес — тот, что достижим.
    jwks_url: str
    # Кто ресурсный сервер. Без этой проверки токен, выданный любому
    # другому клиенту того же реалма, открывает наше API.
    audience: str
    # Допуск на расхождение часов. Без него узел, ушедший на секунду
    # вперёд, начинает отвергать только что выданные токены.
    leeway_seconds: int = 30
    # Как долго ключи считаются свежими.
    cache_seconds: int = 300
    # Нижний предел между обращениями за ключами. Без него токен
    # с выдуманным `kid` превращается в способ завалить Keycloak
    # запросами с нашей стороны.
    min_refresh_seconds: int = 10
    request_timeout_seconds: float = 3.0


def usable_keys(document: dict) -> dict[str, PyJWK]:
    """Оставляет из набора только то, чем можно проверить подпись.

    В реалме Keycloak лежит не один ключ: рядом с RS256 для подписи
    живёт RSA-OAEP для шифрования. Принять подпись ключом шифрования
    было бы ошибкой проверки, а не мелочью — поэтому отбор явный,
    и проверяется он отдельным тестом без сети.
    """
    keys: dict[str, PyJWK] = {}
    for entry in document.get("keys", []):
        if entry.get("use") not in (None, "sig"):
            continue
        if entry.get("alg") not in ALGORITHMS:
            continue
        kid = entry.get("kid")
        if not kid:
            continue
        try:
            keys[kid] = PyJWK.from_dict(entry)
        except (jwt.InvalidKeyError, KeyError, ValueError):
            continue
    return keys


@dataclass(slots=True)
class JwksCache:
    """Открытые ключи реалма, сложенные по `kid`.

    Кеш существует не ради скорости, а ради независимости: проверка
    токена не должна ходить в сеть на каждый запрос, иначе недоступный
    Keycloak мгновенно превращается в недоступное API, хотя все выданные
    токены ещё действительны.
    """

    settings: OidcSettings
    _keys: dict[str, PyJWK] = field(default_factory=dict)
    _fetched_at: float = 0.0
    _last_attempt: float = 0.0

    @property
    def has_keys(self) -> bool:
        """Есть ли хоть что-то, чем проверять.

        Отличает «токен подписан неизвестным ключом» от «ключей нет вовсе»:
        первое — отказ пользователю, второе — отказ системы.
        """
        return bool(self._keys)

    def _stale(self, now: float) -> bool:
        return now - self._fetched_at > self.settings.cache_seconds

    async def key_for(self, kid: str) -> PyJWK | None:
        """Ключ по `kid`, с одной попыткой обновления при промахе.

        Промах — обычное дело при ротации: новый ключ появляется в реалме
        раньше, чем истекают токены, подписанные старым. Поэтому неизвестный
        `kid` не отказ, а повод перечитать — но не чаще, чем раз в
        `min_refresh_seconds`.
        """
        now = time.monotonic()
        if kid in self._keys and not self._stale(now):
            return self._keys[kid]

        if now - self._last_attempt >= self.settings.min_refresh_seconds:
            await self._refresh(now)

        return self._keys.get(kid)

    async def _refresh(self, now: float) -> None:
        self._last_attempt = now
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as http:
                response = await http.get(self.settings.jwks_url)
                response.raise_for_status()
                document = response.json()
        except (httpx.HTTPError, ValueError):
            # Старые ключи не выбрасываются: пока Keycloak недоступен,
            # проверять уже выданные токены всё ещё можно и нужно.
            return

        keys = usable_keys(document)
        if keys:
            self._keys = keys
            self._fetched_at = now


async def verify_access_token(token: str, *, keys: JwksCache, settings: OidcSettings) -> TokenCheck:
    """Проверяет подпись, издателя, аудиторию и срок.

    Порядок проверок задан PyJWT и здесь не важен: важно, что ни одна
    из них не пропущена. Пропущенная проверка аудитории означает, что
    токен браузерного клиента любого другого приложения того же реалма
    открывает наше API; пропущенная проверка издателя — что его открывает
    токен из чужого Keycloak.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return TokenCheck.rejected(TokenRejection.MALFORMED)

    kid = header.get("kid")
    if not kid:
        return TokenCheck.rejected(TokenRejection.MALFORMED)

    key = await keys.key_for(kid)
    if key is None:
        # Ключи не удалось получить вовсе — это «проверить нечем»,
        # а не «токен плохой»; различаются они по тому, есть ли
        # в кеше хоть что-нибудь.
        if not keys.has_keys:
            return TokenCheck.rejected(TokenRejection.KEYS_UNAVAILABLE)
        return TokenCheck.rejected(TokenRejection.UNKNOWN_KEY)

    try:
        payload = jwt.decode(
            token,
            key=key,
            algorithms=list(ALGORITHMS),
            audience=settings.audience,
            issuer=settings.issuer,
            leeway=settings.leeway_seconds,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        return TokenCheck.rejected(TokenRejection.EXPIRED)
    except jwt.InvalidAudienceError:
        return TokenCheck.rejected(TokenRejection.WRONG_AUDIENCE)
    except jwt.InvalidIssuerError:
        return TokenCheck.rejected(TokenRejection.WRONG_ISSUER)
    except jwt.MissingRequiredClaimError:
        return TokenCheck.rejected(TokenRejection.MISSING_CLAIM)
    except jwt.InvalidSignatureError:
        return TokenCheck.rejected(TokenRejection.BAD_SIGNATURE)
    except jwt.PyJWTError:
        # Всё остальное — тоже отказ, и тоже без подробностей наружу.
        return TokenCheck.rejected(TokenRejection.MALFORMED)

    return TokenCheck.accepted(_to_claims(payload))


def _to_claims(payload: dict) -> Claims:
    return Claims(
        subject=payload["sub"],
        email=payload.get("email"),
        email_verified=bool(payload.get("email_verified", False)),
        # `name` есть не всегда: у служебной учётной записи его нет,
        # а имя в списке бесед нужно всё равно.
        display_name=payload.get("name") or payload.get("preferred_username"),
        session_state=payload.get("sid") or payload.get("session_state"),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
    )
