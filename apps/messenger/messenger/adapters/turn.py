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

import logging
import os
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
    async def ice_servers(self, *, ttl_seconds: int) -> list[IceServer] | None:
        """Список для клиента или `None`, если провайдер недоступен.

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

    async def ice_servers(self, *, ttl_seconds: int) -> list[IceServer] | None:
        return list(self.servers)


@dataclass(slots=True)
class CloudflareProvider:
    """Управляемый TURN Cloudflare: учётные данные создаются по REST."""

    key_id: str
    api_token: str
    base_url: str = "https://rtc.live.cloudflare.com"
    timeout_seconds: float = 3.0
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def ice_servers(self, *, ttl_seconds: int) -> list[IceServer] | None:
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


def provider_from_env() -> TurnProvider:
    """`TURN_PROVIDER`: `cloudflare` или (по умолчанию) фиксированный список STUN."""
    kind = os.getenv("TURN_PROVIDER", "static").strip().lower()
    if kind == "cloudflare":
        key_id = os.getenv("TURN_CLOUDFLARE_KEY_ID", "")
        token = os.getenv("TURN_CLOUDFLARE_API_TOKEN", "")
        if key_id and token:
            return CloudflareProvider(key_id=key_id, api_token=token)
        log.warning(
            "TURN Cloudflare не настроен, звонки идут без релея",
            extra={"event": "turn_unconfigured", "result": "failed"},
        )
    urls = tuple(
        url.strip() for url in os.getenv("TURN_STUN_URLS", "").split(",") if url.strip()
    )
    return StaticProvider(servers=(IceServer(urls=urls),) if urls else ())
