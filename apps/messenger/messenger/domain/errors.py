"""Таксономия ошибок: внутренняя причина и внешний ответ — разные вещи.

Прямое отображение `NOT_A_MEMBER → 403` нарушает инвариант И-2 модели
авторизации: `403` подтверждает, что беседа существует, и перебором
идентификаторов выясняется, кто с кем переписывается, без единого
прочитанного сообщения.

Поэтому отображение двухступенчатое, и делает его **одна** функция. Разложенное
по сорока обработчикам, оно однажды вернёт `403` там, где нельзя, — и заметит
это не ревью, а тот, кто перебирал.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Reason(str, Enum):
    """Что произошло на самом деле. Идёт в журнал и в метку `error_code`."""

    NOT_A_MEMBER = "not_a_member"
    CONVERSATION_NOT_FOUND = "conversation_not_found"
    USER_NOT_FOUND = "user_not_found"
    MESSAGE_NOT_FOUND = "message_not_found"
    BLOCKED = "blocked"
    EMAIL_UNVERIFIED = "email_unverified"
    SELF_CONVERSATION = "self_conversation"
    DUPLICATE_MESSAGE = "duplicate_message"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    ATTACHMENT_NOT_READY = "attachment_not_ready"
    ATTACHMENT_NOT_FOUND = "attachment_not_found"
    ATTACHMENT_UPLOAD_MISSING = "attachment_upload_missing"
    ATTACHMENTS_UNAVAILABLE = "attachments_unavailable"
    INVALID_VOICE = "invalid_voice"
    INVALID_SUBSCRIPTION = "invalid_subscription"
    PUSH_UNAVAILABLE = "push_unavailable"
    CALLS_UNAVAILABLE = "calls_unavailable"
    CALL_NOT_FOUND = "call_not_found"
    CALL_ENDED = "call_ended"
    CALL_NOT_READY = "call_not_ready"
    CALL_TAKEN = "call_taken"
    REALTIME_UNAVAILABLE = "realtime_unavailable"
    ALREADY_IN_CALL = "already_in_call"
    INVALID_SIGNAL = "invalid_signal"
    RATE_LIMITED = "rate_limited"
    UNAUTHENTICATED = "unauthenticated"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    INTERNAL = "internal"


class Visibility(str, Enum):
    """Знает ли субъект о существовании ресурса.

    От этого зависит, можно ли признать, что ресурс есть. Значение выбирает
    вызывающий, и это единственный случай, когда ему позволено влиять
    на публичный ответ: только он знает, пришёл ли субъект по своей ссылке
    или подобрал идентификатор.
    """

    # Субъект не должен узнать даже о существовании: чужая беседа, чужое
    # вложение, чужой пользователь.
    HIDDEN = "hidden"
    # Субъект заведомо знает, что ресурс есть: своя беседа, из которой
    # он вышел; своё сообщение, которое поздно править.
    KNOWN = "known"


@dataclass(frozen=True, slots=True)
class Problem:
    """Ответ по RFC 9457. Один формат на весь API."""

    status: int
    code: str
    title: str


# Публичный код и заголовок не повторяют внутреннюю причину дословно:
# наружу уходит то, что субъекту позволено узнать.
_HIDDEN_NOT_FOUND = Problem(404, "resource_not_found", "Ресурс не найден")

_PUBLIC: dict[Reason, Problem] = {
    Reason.CONVERSATION_NOT_FOUND: _HIDDEN_NOT_FOUND,
    Reason.USER_NOT_FOUND: _HIDDEN_NOT_FOUND,
    Reason.MESSAGE_NOT_FOUND: _HIDDEN_NOT_FOUND,
    # Чужой и несуществующий звонок неразличимы: по идентификатору звонка нельзя
    # выяснять, кто с кем разговаривает.
    Reason.CALL_NOT_FOUND: _HIDDEN_NOT_FOUND,
    Reason.CALLS_UNAVAILABLE: Problem(503, "calls_unavailable", "Звонки временно недоступны"),
    Reason.CALL_ENDED: Problem(409, "call_ended", "Звонок уже завершён"),
    Reason.CALL_NOT_READY: Problem(409, "call_not_ready", "Звонок ещё не принят"),
    Reason.CALL_TAKEN: Problem(409, "call_taken", "Звонок принят в другой вкладке"),
    # Сигнал не удалось **передать** собеседнику: канал звонков без истории, и
    # молчаливый успех оставил бы звонящего уверенным, что `offer` доставлен.
    Reason.REALTIME_UNAVAILABLE: Problem(
        503, "realtime_unavailable", "Сигнал не доставлен, повторите"
    ),
    Reason.ALREADY_IN_CALL: Problem(409, "already_in_call", "У вас уже идёт звонок"),
    Reason.INVALID_SIGNAL: Problem(400, "invalid_signal", "Сигнал не принят"),
    # Чужое и несуществующее вложение неразличимы — по тому же доводу, что и
    # чужая беседа: UUID не должен позволять перечислять чужие файлы.
    Reason.ATTACHMENT_NOT_FOUND: _HIDDEN_NOT_FOUND,
    Reason.ATTACHMENT_UPLOAD_MISSING: Problem(
        409, "attachment_upload_missing", "Файл в хранилище не найден"
    ),
    Reason.ATTACHMENTS_UNAVAILABLE: Problem(
        503, "attachments_unavailable", "Вложения временно недоступны"
    ),
    Reason.INVALID_SUBSCRIPTION: Problem(400, "invalid_subscription", "Подписка не принята"),
    Reason.PUSH_UNAVAILABLE: Problem(503, "push_unavailable", "Уведомления недоступны"),
    Reason.INVALID_VOICE: Problem(400, "invalid_voice", "Недопустимая длительность голосового"),
    Reason.BLOCKED: Problem(403, "forbidden", "Действие недоступно"),
    Reason.EMAIL_UNVERIFIED: Problem(403, "forbidden", "Действие недоступно"),
    Reason.SELF_CONVERSATION: Problem(403, "forbidden", "Действие недоступно"),
    Reason.DUPLICATE_MESSAGE: Problem(200, "duplicate_message", "Сообщение уже принято"),
    Reason.PAYLOAD_TOO_LARGE: Problem(413, "payload_too_large", "Содержимое слишком велико"),
    Reason.UNSUPPORTED_MEDIA_TYPE: Problem(415, "unsupported_media_type", "Тип не разрешён"),
    Reason.ATTACHMENT_NOT_READY: Problem(409, "attachment_not_ready", "Вложение не готово"),
    Reason.RATE_LIMITED: Problem(429, "rate_limited", "Слишком часто"),
    Reason.UNAUTHENTICATED: Problem(401, "unauthenticated", "Требуется вход"),
    Reason.UPSTREAM_UNAVAILABLE: Problem(503, "upstream_unavailable", "Временно недоступно"),
    Reason.INTERNAL: Problem(500, "internal", "Внутренняя ошибка"),
}


def to_problem(reason: Reason, visibility: Visibility = Visibility.HIDDEN) -> Problem:
    """Переводит внутреннюю причину в публичный ответ.

    Отсутствие права по умолчанию неотличимо от отсутствия ресурса. `403`
    отдаётся только тогда, когда субъект и так знает о существовании
    ресурса, — и это приходится указать явно, потому что умолчание
    в другую сторону стоит утечки графа общения.
    """
    if reason is Reason.NOT_A_MEMBER:
        if visibility is Visibility.KNOWN:
            return Problem(403, "forbidden", "Действие недоступно")
        return _HIDDEN_NOT_FOUND

    problem = _PUBLIC.get(reason)
    if problem is None:
        # Неизвестная причина не должна превращаться в успех или
        # в подробное сообщение: наружу уходит самое общее.
        return _PUBLIC[Reason.INTERNAL]
    return problem


class DomainError(Exception):
    """Неожиданный отказ.

    Ожидаемый отказ — возвращаемое значение, а не исключение: граница
    по тому, описан ли он в OpenAPI как `4xx`. Это исключение — для
    случаев «система сломана».
    """

    def __init__(self, reason: Reason = Reason.INTERNAL) -> None:
        super().__init__(reason.value)
        self.reason = reason
