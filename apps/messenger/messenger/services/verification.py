"""Подтверждение адреса: повторная отправка письма под лимитом.

Письмо собирает и отправляет Keycloak — у него шаблоны, одноразовый токен
и срок его жизни. Наше здесь только одно: не давать просить его слишком
часто.

Лимит нужен не из-за нагрузки. Точка, рассылающая письмо по чужому адресу
без ограничения, — это готовый инструмент травли: адрес указывает
регистрирующийся, а письма приходят владельцу адреса.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from messenger.adapters import keycloak, ratelimit
from messenger.domain.user import User
from messenger.telemetry import metrics

log = logging.getLogger(__name__)

# Три письма в час. Первое приходит при регистрации, поэтому три повтора —
# это «не пришло, попробую ещё» трижды, а не рассылка.
RESEND_LIMIT = 3
RESEND_WINDOW_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class ResendResult:
    sent: bool = False
    # Адрес уже подтверждён — отправлять нечего. Это не ошибка клиента:
    # так выглядит вторая вкладка, открытая до подтверждения.
    already_verified: bool = False
    limited: bool = False
    retry_after_seconds: int = 0
    # Keycloak не ответил. Отличается от лимита: 503 против 429.
    upstream_failed: bool = False
    # Решение принято без счётчика.
    degraded: bool = False


async def resend_verification(
    *,
    user: User,
    limiter: ratelimit.RateLimiter,
    admin: keycloak.AdminClient,
) -> ResendResult:
    """Просит Keycloak отправить письмо ещё раз, если можно.

    Счёт ведётся по внутреннему идентификатору, а не по адресу: адрес
    меняется, и смена адреса не должна обнулять счётчик — иначе лимит
    обходится сменой одной буквы.

    При недоступном счётчике отправка **запрещается**. Обратное решение —
    пропускать — означало бы, что отказ Redis превращается в способ
    разослать почту. Это противоположно правилу пути сообщения
    (`CACHE-004`), где при том же отказе чат обязан работать, и потому
    политика указывается на каждом вызове, а не берётся по умолчанию.
    """
    if user.email_verified:
        return ResendResult(already_verified=True)

    decision = await limiter.take(
        f"verify-email:{user.user_id}",
        limit=RESEND_LIMIT,
        window_seconds=RESEND_WINDOW_SECONDS,
        on_failure=ratelimit.OnFailure.DENY,
    )
    if not decision.allowed:
        metrics.verification_email("limited")
        log.info(
            "повторная отправка отклонена лимитом",
            extra={
                "event": "verify_email_limited",
                "result": "failed",
                "error_code": "rate_limited",
                "degraded": decision.degraded,
            },
        )
        return ResendResult(
            limited=True,
            retry_after_seconds=decision.retry_after_seconds,
            degraded=decision.degraded,
        )

    if not await admin.send_verify_email(external_user_id=user.external_id):
        metrics.verification_email("upstream_failed")
        return ResendResult(upstream_failed=True)

    metrics.verification_email("sent")
    log.info(
        "письмо о подтверждении отправлено повторно",
        extra={"event": "verify_email_sent", "result": "success"},
    )
    return ResendResult(sent=True, retry_after_seconds=RESEND_WINDOW_SECONDS)
