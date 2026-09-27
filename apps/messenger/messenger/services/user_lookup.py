"""Поиск человека по точному адресу.

Правило гейта целиком укладывается в одну фразу: **«не найден» — это один
ответ на пять разных случаев**. Адрес свободен, принадлежит стёртой учётной
записи, принадлежит заблокированному, принадлежит заблокировавшему, это сам
спрашивающий — наружу все пять выглядят одинаково (`Reason.USER_NOT_FOUND`
→ `404 resource_not_found`), и это не удобство, а условие задачи.

Отличить их значило бы отвечать на вопрос, которого спрашивающий не задавал.
«Этот адрес занят, но человек вам не отвечает» — знание, которое в беседе
закрывает маска `G3-007`; отдать его здесь, до всякой беседы, значило бы
завести второй, неохраняемый вход к тому же наблюдению. А «по этому адресу
никого нет» вместо «по этому адресу вас заблокировали» — это ещё и
приглашение перебрать адреса заново, обходя блокировку.

Точность совпадения при этом **удорожает** перебор, но не закрывает его:
пока ответы для занятого и свободного адреса различаются (а они
различаются — `200` против `404`), enumeration возможен, и стоит он
лишь знания полного адреса. Поэтому рядом стоят аутентификация и лимит,
а не одна надежда на точность. Обе половины обязательны, и обе названы
ценой, а не подразумеваются.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from messenger.adapters import ratelimit
from messenger.domain.errors import Reason
from messenger.domain.ids import UserId
from messenger.domain.user import User
from messenger.repositories import conversations, users
from messenger.telemetry import logging as logging_envelope

log = logging.getLogger(__name__)

# Тридцать поисков в минуту. Число выбрано по назначению, а не по вкусу:
# поиск — редкое действие (человек ищет собеседника, а не листает каталог),
# поэтому настоящему человеку этого хватит с запасом, а перебору —
# тридцать адресов в минуту, то есть 43 тысячи в сутки. Это дорого ровно
# настолько, чтобы перебор не был бесплатным, и дешевле любого способа
# закрыть его совсем.
LOOKUP_LIMIT = 30
LOOKUP_WINDOW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class UserLookupResult:
    """Найденный человек или причина отказа.

    Полей ровно два, и оба — то, что уходит в ответ. Присутствия здесь нет
    и не заводится: `online`/`last_seen_at` описывают активность человека,
    которого спрашивающий ещё не знает, и наблюдение за незнакомцем — та
    же угроза, ради которой в беседе заведена маска (`G3-007` D14).

    `rejection` — из закрытого словаря `Reason`; нового кода для «не найден»
    не заводится, потому что `USER_NOT_FOUND` уже отдаёт ровно то тело,
    которым отвечает несуществующая беседа. Отдельный код сделал бы эти
    два «не найдено» различимыми и вернул бы поиску роль справочника.
    """

    user_id: UserId | None = None
    display_name: str | None = None
    rejection: Reason | None = None
    # Число из решения лимитера. Заполняется только при отказе по лимиту:
    # без него клиент повторяет вслепую и упирается снова.
    retry_after_seconds: int = 0
    # Решение принято без счётчика (Redis недоступен). Отдельный признак,
    # потому что всплеск таких решений — это отказ Redis, а не поведение
    # людей, и увидеть его в журнале надо иначе.
    degraded: bool = False

    @property
    def ok(self) -> bool:
        return self.user_id is not None and self.rejection is None


async def find_by_email(
    conn: asyncpg.Connection,
    *,
    viewer: User,
    email: str,
    limiter: ratelimit.RateLimiter,
) -> UserLookupResult:
    """Ищет живого человека по адресу от имени зрителя.

    `email` приходит **нормализованным**, и это ответственность вызывающего:
    пустой адрес — это запрос без предмета, а не попытка поиска, и лимит
    на него тратиться не должен. Поэтому проверка «адрес вообще есть» стоит
    в маршруте, до этой функции; сюда пустая строка не попадает.

    Лимит с ключом по **зрителю**, а не по адресу. Довод тот же, что у
    повторной отправки письма (`services/verification.py`): адрес меняется,
    и счёт по нему обходился бы сменой одной буквы — то есть ограничение
    стоило бы ровно ничего. Ключ по зрителю считает **запросы человека**,
    а не запросы про адрес, и сменой адреса не обнуляется.

    Политика отказа — `ALLOW`, и это **противоположно** пути письма
    (`verification.py`, `OnFailure.DENY`). Там действие — рассылка почты
    наружу и необратимо, и недоступный счётчик обязан его запрещать. Здесь
    действие — чтение внутри системы, а создание беседы есть часть чата,
    и `CACHE-004` требует, чтобы при недоступном Redis чат работал. Цена
    названа прямо: при мёртвом счётчике перебор не ограничен ничем, кроме
    самой аутентификации. Это принято — и это не «лимит необязателен»:
    лимит есть, он просто не встаёт между человеком и чатом.
    """
    decision = await limiter.take(
        f"user-lookup:{viewer.user_id}",
        limit=LOOKUP_LIMIT,
        window_seconds=LOOKUP_WINDOW_SECONDS,
        on_failure=ratelimit.OnFailure.ALLOW,
    )
    if not decision.allowed:
        log.info(
            "поиск человека отклонён лимитом",
            extra={
                "event": "user_lookup_limited",
                "log_stream": logging_envelope.STREAM_SECURITY,
                "result": "failed",
                "error_code": "rate_limited",
                "degraded": decision.degraded,
                "user_id": str(viewer.user_id),
            },
        )
        return UserLookupResult(
            rejection=Reason.RATE_LIMITED,
            retry_after_seconds=decision.retry_after_seconds,
            degraded=decision.degraded,
        )

    found = await users.fetch_user_by_email(conn, email=email)
    if found is None:
        return _not_found(viewer=viewer, reason="no_such_address")

    # Себя сервер собеседником не предлагает. Отдельным кодом (`403`) это
    # не помечается: «не предлагается» — тот же исход, что «не найден»,
    # и различать их значило бы завести второй исход для одного ответа.
    # Своего адреса человек и так не скрывает — `GET /me` отдаёт его.
    if found.user_id == viewer.user_id:
        return _not_found(viewer=viewer, reason="self")

    # Блокировка — в любую сторону, и предикат берётся **тот же**, что
    # маскирует присутствие и квитанции в беседе (`G3-007` D14): второй
    # запрос той же формы однажды разошёлся бы с первым, и разошёлся бы
    # в сторону «блокировку видно только в одном направлении».
    #
    # Спрашивается после находки, а не до: если адрес свободен, ответ и так
    # «не найден», и запрос в `blocks` был бы работой ради ничего.
    blocked = await conversations.blocked_with(conn, viewer=viewer.user_id)
    if found.user_id in blocked:
        return _not_found(viewer=viewer, reason="blocked")

    return UserLookupResult(
        user_id=found.user_id,
        display_name=found.display_name,
    )


def _not_found(*, viewer: User, reason: str) -> UserLookupResult:
    """Один исход на все «не найден» — и одна запись в журнал на каждый.

    Наружу уходит одно и то же, и это правильно. Но **внутри** случаи
    различимы, и различие стоит записать: иначе отладка «почему человека
    не находят» упрётся в четыре одинаковые строки, неотличимые друг от
    друга, и различить их можно будет только чтением базы руками.

    `reason` уходит в журнал и **никогда** — в ответ. Это не деталь
    реализации: именно смешение этих двух каналов превратило бы поиск
    в канал наблюдения.
    """
    log.info(
        "человек не найден по адресу",
        extra={
            "event": "user_lookup_miss",
            "log_stream": logging_envelope.STREAM_SECURITY,
            "result": "success",
            "reason": reason,
            "user_id": str(viewer.user_id),
        },
    )
    return UserLookupResult(rejection=Reason.USER_NOT_FOUND)
