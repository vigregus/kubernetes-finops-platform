"""Диагностика сети по событию (`RES-012`).

Две ручки: выдать сессию (идентификатор, список проб, **диагностические данные TURN** на
45 секунд) и принять результат. Идентификатор — подписанный токен без хранилища:
`{случайная часть}.{срок}.{подпись}`, где подпись привязана к человеку. Чужой, просроченный
и подделанный идентификатор неразличимы (одно и то же `404`). Принять результат можно раз на
фазу (`failure`, `recovery`) — повтор не накручивает метрики.

**Диагностические данные не превращают TURN в публичный relay**: выдаёт только
аутентифицированному человеку, срок 45 секунд, не чаще шести сессий в час, назначение —
проверка (малая квота у coturn на пользователя `diag:…`), а обычные данные звонка по-прежнему
только участнику принятого звонка.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

from messenger.adapters.ratelimit import OnFailure, RateLimiter
from messenger.adapters.turn import TurnProvider
from messenger.domain import call as call_domain
from messenger.domain import diagnostics as domain
from messenger.domain.errors import Reason
from messenger.domain.ids import UserId
from messenger.services import calls as calls_service
from messenger.telemetry import metrics
from messenger.telemetry.network import NetworkContext

log = logging.getLogger(__name__)


def enabled() -> bool:
    """Выключатель диагностики: свой флаг **и** включённые звонки.

    Диагностика выдаёт данные TURN вне живого звонка, поэтому она не вправе пережить выключатель
    звонков (`CALLS_ENABLED`, ADR 0007) и включается отдельно: `NETWORK_DIAGNOSTICS_ENABLED`
    (по умолчанию выключено) — вместе с готовностью защиты от злоупотреблений (`CALL-017`).
    """
    flag = os.getenv("NETWORK_DIAGNOSTICS_ENABLED", "false").strip().lower()
    return calls_service.enabled() and flag in ("1", "true", "yes", "on")


def subject(secret: bytes, user_id: UserId) -> str:
    """Псевдоним человека для ключей лимитера: UUID не попадает в журнал при сбое Redis."""
    return hmac.new(secret, f"subject:{user_id}".encode(), hashlib.sha256).hexdigest()[:20]


def secret_from_env() -> bytes:
    """Секрет подписи идентификаторов.

    `DIAGNOSTICS_SECRET`, иначе производный от секрета coturn (общий на все реплики), иначе
    случайный на процесс: идентификаторы тогда живут до перезапуска — для сессии в десять
    минут это допустимо, но с несколькими репликами нужен общий секрет (иначе токен,
    выданный одной репликой, другая отвергнет как чужой), поэтому процесс об этом предупреждает.
    """
    explicit = os.getenv("DIAGNOSTICS_SECRET", "")
    if explicit:
        return explicit.encode()
    coturn = os.getenv("TURN_COTURN_SECRET", "")
    if coturn:
        return hashlib.sha256(b"network-diagnostics:" + coturn.encode()).digest()
    log.warning(
        "секрет диагностики случаен на процесс: с несколькими репликами задайте DIAGNOSTICS_SECRET",
        extra={"event": "diagnostics_secret_ephemeral", "result": "degraded"},
    )
    return secrets.token_bytes(32)


def _sign(secret: bytes, nonce: str, expires: int, user_id: UserId) -> str:
    mac = hmac.new(secret, f"{nonce}.{expires}.{user_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:16]).decode().rstrip("=")


def issue_id(secret: bytes, user_id: UserId, expires: int) -> str:
    nonce = secrets.token_urlsafe(9)
    return f"{nonce}.{expires}.{_sign(secret, nonce, expires, user_id)}"


def verify_id(secret: bytes, user_id: UserId, diagnostic_id: str, now: float) -> bool:
    parts = diagnostic_id.split(".")
    if len(parts) != 3:
        return False
    nonce, raw_expires, signature = parts
    if not raw_expires.isdigit() or len(diagnostic_id) > 200:
        return False
    expires = int(raw_expires)
    if expires < now:
        return False
    return hmac.compare_digest(signature, _sign(secret, nonce, expires, user_id))


@dataclass(slots=True)
class SessionResult:
    diagnostic_id: str = ""
    ttl_seconds: int = 0
    report_within_seconds: int = 0
    servers: list[dict[str, object]] = field(default_factory=list)
    rejection: Reason | None = None
    retry_after_seconds: int | None = None


async def start_session(
    *,
    turn: TurnProvider,
    limiter: RateLimiter,
    secret: bytes,
    user_id: UserId,
    now: float | None = None,
) -> SessionResult:
    if not enabled():
        return SessionResult(rejection=Reason.CALLS_UNAVAILABLE)
    moment = now if now is not None else time.time()
    # Fail-closed: данные TURN вне звонка дороже отказа. Упал счётчик — диагностики нет, а не
    # неограниченная выдача relay-доступа любому аккаунту.
    decision = await limiter.take(
        f"diag-session:{subject(secret, user_id)}",
        limit=domain.SESSIONS_PER_HOUR,
        window_seconds=3600,
        on_failure=OnFailure.DENY,
    )
    if not decision.allowed:
        return SessionResult(
            rejection=Reason.RATE_LIMITED, retry_after_seconds=decision.retry_after_seconds
        )
    ttl = domain.SESSION_TTL_SECONDS
    servers = await turn.ice_servers(
        ttl_seconds=ttl, expires_at=int(moment) + ttl, subject=f"diag:{user_id}"
    )
    return SessionResult(
        diagnostic_id=issue_id(secret, user_id, int(moment) + domain.REPORT_WINDOW_SECONDS),
        ttl_seconds=ttl,
        report_within_seconds=domain.REPORT_WINDOW_SECONDS,
        servers=[server.as_dict() for server in servers or []],
    )


@dataclass(frozen=True, slots=True)
class Probe:
    probe: str
    result: str


@dataclass(frozen=True, slots=True)
class Report:
    phase: str
    trigger: str
    setup_result: str
    probes: tuple[Probe, ...] = ()
    ice_gathering_ms: int | None = None
    ice_connection_ms: int | None = None
    selected_candidate: str | None = None
    selected_relay_protocol: str | None = None
    failure_reason: str | None = None


@dataclass(slots=True)
class ReportResult:
    rejection: Reason | None = None


async def accept_report(
    *,
    limiter: RateLimiter,
    secret: bytes,
    user_id: UserId,
    diagnostic_id: str,
    report: Report,
    network: NetworkContext | None = None,
    now: float | None = None,
) -> ReportResult:
    if not enabled():
        return ReportResult(rejection=Reason.CALLS_UNAVAILABLE)
    moment = now if now is not None else time.time()
    if not verify_id(secret, user_id, diagnostic_id, moment):
        return ReportResult(rejection=Reason.DIAGNOSTIC_NOT_FOUND)
    # Одна фаза — один результат. Fail-closed: эти данные станут основой рекомендаций
    # (`RES-013`), и повтор при упавшем счётчике был бы примитивом отравления метрик.
    taken = await limiter.take(
        f"diag-report:{diagnostic_id}:{report.phase}",
        limit=1,
        window_seconds=domain.REPORT_WINDOW_SECONDS,
        on_failure=OnFailure.DENY,
    )
    if not taken.allowed:
        return ReportResult(rejection=Reason.DIAGNOSTIC_REPORTED)
    net = network or NetworkContext()
    seen: set[str] = set()
    for item in report.probes:
        # Повтор одной пробы в отчёте считается один раз: отчёт не накручивает серию.
        if item.probe in seen:
            continue
        seen.add(item.probe)
        metrics.network_probe(report.phase, item.probe, item.result, net.country, net.net_class)
    known = report.failure_reason in call_domain.FAILURE_REASONS
    reason = report.failure_reason if known and report.failure_reason else "none"
    metrics.network_diagnostic(report.phase, report.trigger, report.setup_result, reason)
    if report.ice_gathering_ms is not None:
        metrics.network_ice("gathering", report.ice_gathering_ms / 1000)
    if report.ice_connection_ms is not None:
        metrics.network_ice("connection", report.ice_connection_ms / 1000)
    if report.phase == "recovery" and report.setup_result == "connected":
        # Что спасло звонок: транспорт релея либо прямой путь. Путь неизвестен — `unknown`, а не
        # «direct»: иначе главная метрика говорила бы противоположное реальности.
        if report.selected_candidate == "relay":
            rescued = (
                report.selected_relay_protocol
                if report.selected_relay_protocol in call_domain.TURN_TRANSPORTS
                else "relay"
            )
        elif report.selected_candidate in ("host", "srflx", "prflx"):
            rescued = "direct"
        else:
            rescued = "unknown"
        metrics.network_rescue(rescued, net.country, net.net_class)
    return ReportResult()
