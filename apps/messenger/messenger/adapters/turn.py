"""Доступ к TURN для звонков: интерфейс и два способа его выдать.

Звонок идёт напрямую между браузерами; релей TURN нужен там, где NAT не
пускает (≈ 15% звонков, допущение оценки). Выдача спрятана за интерфейсом —
по образцу `Scanner` и `ObjectStore`: выбор между управляемым сервисом и
своим coturn — настройка, а не переписывание (ADR 0007, п. 4).

Постоянных учётных данных TURN в клиенте нет и быть не может: иначе он
становится бесплатным прокси для кого угодно. Выдаётся краткоживущая пара
(`TURN_TTL_SECONDS`) и только участнику живого звонка — это проверяет сервис,
а не провайдер.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IceServer:
    """Один элемент `RTCConfiguration.iceServers`."""

    urls: tuple[str, ...]
    username: str | None = None
    credential: str | None = None

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {"urls": list(self.urls)}
        if self.username is not None:
            body["username"] = self.username
        if self.credential is not None:
            body["credential"] = self.credential
        return body


class TurnProvider(Protocol):
    async def ice_servers(
        self,
        *,
        ttl_seconds: int,
        expires_at: int | None = None,
        subject: str | None = None,
    ) -> list[IceServer] | None:
        """Список для клиента или `None`, если провайдер недоступен.

        `expires_at` (unix-время) и `subject` (кому: человек) нужны тем, кто
        умеет выдавать **стабильные** данные: один и тот же субъект в один и тот же
        срок получает одно и то же имя пользователя. Остальные их игнорируют.

        `None`, а не исключение: недоступный TURN не должен ронять звонок — у
        большинства людей прямой путь работает и без него.
        """
        ...


@dataclass(frozen=True, slots=True)
class StaticProvider:
    """Фиксированный список: STUN и, на стенде, ничего больше.

    Без релея соединение устанавливается там, где позволяет NAT; для стенда на
    одной машине этого достаточно, для людей — нет, поэтому настоящая среда
    использует управляемый TURN.
    """

    servers: tuple[IceServer, ...] = ()

    async def ice_servers(
        self,
        *,
        ttl_seconds: int,
        expires_at: int | None = None,
        subject: str | None = None,
    ) -> list[IceServer] | None:
        return list(self.servers)


@dataclass(slots=True)
class CoturnProvider:
    """Свой coturn: краткоживущие данные считаются **локально**, без внешнего API.

    Схема `use-auth-secret` самого coturn: имя пользователя — `{срок}:{метка}`
    (срок — unix-время окончания), пароль — `base64(HMAC-SHA1(секрет, имя))`.
    Сервер проверяет подпись и срок сам, поэтому ему не нужна ни база, ни связь с
    API: общий секрет — единственное, что их связывает. Сеть не участвует вовсе, и
    недоступного провайдера, как у управляемого сервиса, не бывает.

    **Данные стабильны** для одного субъекта (человека) и окна срока: метка — это
    HMAC от субъекта тем же секретом, а срок задаёт вызывающий (`expires_at`,
    округлённый до границы окна). Запросы внутри окна получают то же имя
    пользователя, и поэтому `--user-quota` у coturn ограничивает **одного
    человека**: со случайной меткой каждый запрос рождал бы нового пользователя со
    свежей квотой, и десяток запросов выедал весь пул релейных портов.

    Метка не обратима в идентификатор человека (ключевой хеш), а не `user_id`:
    имя пользователя попадает в журналы TURN, и `user_id` там означал бы утечку
    «кто с кем созванивается» в систему с иным кругом доступа.
    """

    secret: str
    urls: tuple[str, ...]
    now: Callable[[], float] = field(default=time.time, repr=False)

    async def ice_servers(
        self,
        *,
        ttl_seconds: int,
        expires_at: int | None = None,
        subject: str | None = None,
    ) -> list[IceServer] | None:
        expires = expires_at if expires_at is not None else int(self.now()) + ttl_seconds
        if subject is None:
            label = secrets.token_hex(8)
        else:
            keyed = hmac.new(self.secret.encode(), subject.encode(), hashlib.sha256)
            label = keyed.hexdigest()[:16]
        username = f"{expires}:{label}"
        # SHA-1 здесь не выбор, а схема самого coturn (`use-auth-secret`).
        mac = hmac.new(self.secret.encode(), username.encode(), hashlib.sha1)  # noqa: S324
        digest = mac.digest()
        return [
            IceServer(
                urls=self.urls,
                username=username,
                credential=base64.b64encode(digest).decode(),
            )
        ]


@dataclass(slots=True)
class CloudflareProvider:
    """Управляемый TURN Cloudflare: учётные данные создаются по REST."""

    key_id: str
    api_token: str
    base_url: str = "https://rtc.live.cloudflare.com"
    timeout_seconds: float = 3.0
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def ice_servers(
        self,
        *,
        ttl_seconds: int,
        expires_at: int | None = None,
        subject: str | None = None,
    ) -> list[IceServer] | None:
        url = f"{self.base_url}/v1/turn/keys/{self.key_id}/credentials/generate-ice-servers"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, transport=self.transport
            ) as http:
                response = await http.post(
                    url,
                    json={"ttl": ttl_seconds},
                    headers={"Authorization": f"Bearer {self.api_token}"},
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Ключ и токен в журнал не попадают: пишется только тип отказа.
            log.warning(
                "TURN недоступен",
                extra={
                    "event": "turn_unavailable",
                    "result": "failed",
                    "error_code": type(exc).__name__,
                    "dependency": "turn",
                },
            )
            return None
        return _parse_ice_servers(body)


def _parse_ice_servers(body: object) -> list[IceServer] | None:
    items = body.get("iceServers") if isinstance(body, dict) else None
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return None
    result: list[IceServer] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        urls = item.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            continue
        username = item.get("username")
        credential = item.get("credential")
        result.append(
            IceServer(
                urls=tuple(urls),
                username=username if isinstance(username, str) else None,
                credential=credential if isinstance(credential, str) else None,
            )
        )
    return result or None


TURN_ENDPOINT_TIMEOUT_SECONDS = 3.0


def restricted_only(servers: list[IceServer]) -> list[IceServer]:
    """Профиль `RESTRICTED` (`RES-011`): только `turns:` (TLS), без UDP, TCP 3478 и STUN.

    STUN в этом профиле бесполезен (`iceTransportPolicy=relay` не использует srflx) и
    выдаёт адрес клиента внешнему серверу; `turn:` по UDP или TCP 3478 — это именно то, что
    простая фильтрация режет. Серверы без единого `turns:` отбрасываются целиком.
    """
    result: list[IceServer] = []
    for server in servers:
        urls = tuple(url for url in server.urls if url.startswith("turns:"))
        if urls:
            result.append(
                IceServer(urls=urls, username=server.username, credential=server.credential)
            )
    return result


@dataclass(slots=True)
class CompositeTurnProvider:
    """Несколько TURN (`RES-006`): список клиента — объединение того, что выдали провайдеры.

    Отказ или медленный ответ одного не лишает звонок остальных: каждый опрашивается
    параллельно под своим сроком, а недоступный пропускается. Все недоступны — `None`,
    как у одного провайдера (звонок идёт без релея). Браузер выбирает рабочий TURN сам
    (ICE проверяет кандидаты со всех серверов), поэтому порядок серверов не обещает
    приоритета; важно, что они **разных площадок** — иначе второй TURN ничего не даёт.

    Исход по каждому провайдеру (`ok`, `unavailable`, `timeout`) уходит в `observe`:
    из него строится здоровье точек (`RES-014`).
    """

    providers: tuple[tuple[str, TurnProvider], ...]
    timeout_seconds: float = TURN_ENDPOINT_TIMEOUT_SECONDS
    observe: Callable[[str, str], None] = field(default=lambda name, result: None, repr=False)

    async def _one(
        self,
        name: str,
        provider: TurnProvider,
        ttl_seconds: int,
        expires_at: int | None,
        subject: str | None,
    ) -> list[IceServer] | None:
        try:
            servers = await asyncio.wait_for(
                provider.ice_servers(
                    ttl_seconds=ttl_seconds, expires_at=expires_at, subject=subject
                ),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            self.observe(name, "timeout")
            return None
        except Exception as exc:  # провайдер не должен ронять выдачу остальных
            log.warning(
                "TURN провайдер упал",
                extra={
                    "event": "turn_unavailable",
                    "result": "failed",
                    "error_code": type(exc).__name__,
                    "dependency": name,
                },
            )
            self.observe(name, "unavailable")
            return None
        self.observe(name, "ok" if servers is not None else "unavailable")
        return servers

    async def ice_servers(
        self,
        *,
        ttl_seconds: int,
        expires_at: int | None = None,
        subject: str | None = None,
    ) -> list[IceServer] | None:
        results = await asyncio.gather(
            *(
                self._one(name, provider, ttl_seconds, expires_at, subject)
                for name, provider in self.providers
            )
        )
        merged = [server for servers in results if servers for server in servers]
        return merged or None


def _coturn_from_env(prefix: str) -> CoturnProvider | None:
    secret = os.getenv(f"{prefix}_SECRET", "")
    urls = tuple(url.strip() for url in os.getenv(f"{prefix}_URLS", "").split(",") if url.strip())
    return CoturnProvider(secret=secret, urls=urls) if secret and urls else None


def provider_from_env(
    observe: Callable[[str, str], None] | None = None,
) -> TurnProvider:
    """`TURN_PROVIDER`: `coturn`, `coturn-<имя>`, `cloudflare` (через запятую) или STUN.

    `coturn` читает `TURN_COTURN_SECRET`/`TURN_COTURN_URLS`, `coturn-b` — `TURN_COTURN_B_SECRET`/
    `TURN_COTURN_B_URLS`. Несколько настроенных провайдеров собираются в `CompositeTurnProvider`
    (`RES-006`); ненастроенный пропускается с предупреждением, остальные работают. Ни одного
    настроенного — запасной список STUN.
    """
    kinds = [
        kind.strip().lower()
        for kind in os.getenv("TURN_PROVIDER", "static").split(",")
        if kind.strip()
    ] or ["static"]
    built: list[tuple[str, TurnProvider]] = []
    for kind in kinds:
        provider: TurnProvider | None = None
        if kind == "coturn" or kind.startswith("coturn-"):
            suffix = kind.removeprefix("coturn").removeprefix("-").upper().replace("-", "_")
            prefix = f"TURN_COTURN_{suffix}" if suffix else "TURN_COTURN"
            provider = _coturn_from_env(prefix)
        elif kind == "cloudflare":
            key_id = os.getenv("TURN_CLOUDFLARE_KEY_ID", "")
            token = os.getenv("TURN_CLOUDFLARE_API_TOKEN", "")
            if key_id and token:
                provider = CloudflareProvider(key_id=key_id, api_token=token)
        elif kind != "static":
            log.warning(
                "неизвестный провайдер TURN",
                extra={"event": "turn_unconfigured", "result": "failed", "dependency": kind},
            )
            continue
        if provider is None:
            if kind != "static":
                log.warning(
                    "TURN не настроен, пропущен",
                    extra={"event": "turn_unconfigured", "result": "failed", "dependency": kind},
                )
            continue
        built.append((kind, provider))
    if len(built) == 1 and len(kinds) == 1:
        return built[0][1]
    if built:
        return CompositeTurnProvider(
            providers=tuple(built), observe=observe or (lambda name, result: None)
        )
    urls = tuple(
        url.strip() for url in os.getenv("TURN_STUN_URLS", "").split(",") if url.strip()
    )
    return StaticProvider(servers=(IceServer(urls=urls),) if urls else ())
