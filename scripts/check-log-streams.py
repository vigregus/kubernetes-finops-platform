#!/usr/bin/env python3
"""Проверка контракта журналов: каталог событий и поток каждого из них.

Контракт телеметрии (`docs/messenger/06-observability.md`, часть 4) требует
две вещи от каждой записи: машинное имя события и поток, по которому
определяется срок хранения. Обе держатся только проверкой.

Конверт заполняет обработчик, и это единственное, что не требует
дисциплины. А вот `event` и `stream` кладёт вызывающий, и без каталога
через полгода в хранилище оказывается `event: "auth_fail"` рядом
с `event: "login_rejected"` — два имени одного происшествия, ни по одному
из которых нельзя построить ни запрос, ни оповещение.

Поток важнее имени. Запись об отказе входа, ушедшая в `application`,
удаляется через неделю вместе с отладочным мусором — и разбор угона
учётной записи, начатый через месяц, начинается с пустого экрана.

Правило, по которому поток выбирается:

    security      кто получил доступ, кто не получил и почему:
                  вход и отказ входа, создание и отзыв сессии,
                  создание учётной записи, подтверждение адреса,
                  срабатывание и деградация ограничителя
    access        обращения HTTP и WebSocket
    audit         привилегированные действия support/moderator/admin
    application   работа сервиса и его зависимостей: пулы, брокеры,
                  кеши, циклы обработки
    synthetic     результаты сквозных проб
    deployment    смена версии и конфигурации

Отдельно проверяется слой `repositories`: журнала в нём нет и быть не
должно. Всё, что этот слой делает, — вызовы драйвера, а спаны драйвера
создаёт инструментация, в обход `tracing.span()`. Запись, сделанная внутри
такого спана, получит `span_id` охватывающего спана, и связь «запись ↔
спан» разойдётся ровно там, где её будут искать: по отдельности ни в
журнале, ни в трассе этого не видно. Идентификатор при этом настоящий,
поэтому и заметить расхождение не по чему.

Разбор по AST, а не поиском по тексту: `extra` бывает разложен на три
строки, а имя события — в переменной, и текстовый поиск такое либо
пропустит, либо посчитает нарушением.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "apps" / "messenger" / "messenger"

LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}
LOGGERS = {"log", "logger"}

APPLICATION = "application"

# Слой, которому журнал запрещён целиком. Причина в шапке: спаны драйвера
# создаются инструментацией в обход `tracing.span()`, и запись внутри них
# получает идентификатор не того спана.
REPOSITORIES = PACKAGE / "repositories"

# Каталог событий. Новое событие обязано появиться здесь вместе с потоком —
# иначе проверка падает, и вопрос «сколько это хранить» решается при
# написании записи, а не через год при разборе инцидента.
EVENTS: dict[str, str] = {
    # Жизненный цикл процесса и его зависимостей.
    "service_start": APPLICATION,
    "service_stop": APPLICATION,
    "db_pool_unavailable": APPLICATION,
    # Отдельно от недоступности писателя, хотя база та же: этот отказ
    # не отказ — чтение уходит на писателя, и сервис продолжает отвечать.
    # Слитое в одно событие, оно показывало бы «база недоступна» на
    # исправном сервисе ровно тогда, когда реплика не настроена, —
    # та же причина, по которой `POSTGRES_READ` отделён от `POSTGRES`
    # (services/runtime.py).
    "db_read_pool_unavailable": APPLICATION,
    "dependency_check": APPLICATION,
    "kafka_connect": APPLICATION,
    "kafka_record": APPLICATION,
    "event_cache": APPLICATION,
    "centrifugo_api_error": APPLICATION,
    "centrifugo_unavailable": APPLICATION,
    "oidc_keys_unavailable": APPLICATION,
    "admin_token": APPLICATION,
    "verify_email": APPLICATION,
    # Исход настройки экспорта трасс. Отдельным событием, а не частью
    # `service_start`: на вопрос «почему в Tempo пусто» ответ обязан быть
    # в первой же строке пода, а не после чтения чарта.
    "tracing_config": APPLICATION,
    # Конвейер сообщения.
    #
    # `message_commit_failed` - docs/messenger/06-observability.md,
    # "Определение готовности" называет его обязательным минимумом для
    # MSG-001: без него путь `message_id -> журналы` для отказавшей
    # записи не начать (сообщение может даже не получить message_id,
    # если транзакция упала раньше вставки - см. services/messages.py).
    "message_commit_failed": APPLICATION,
    "outbox_cycle": APPLICATION,
    "outbox_publish": APPLICATION,
    "outbox_unroutable": APPLICATION,
    "outbox_lease_lost": APPLICATION,
    "realtime_cycle": APPLICATION,
    "realtime_delivery": APPLICATION,
    "realtime_duplicate": APPLICATION,
    # Проекция непрочитанного. Два имени, а не одно: `unread_cycle`
    # отвечает на «жив ли потребитель», `unread_event` — на «что стало
    # с конкретным событием». Слить их значило бы потерять второе:
    # исход события не виден по циклу, а цикл не виден по событию.
    "unread_cycle": APPLICATION,
    "unread_event": APPLICATION,
    # Обращения.
    "http_request": "access",
    # Доступ: кого пустили, кого нет и почему.
    "token_exchange": "security",
    "token_rejected": "security",
    "login_rejected": "security",
    "user_created": "security",
    "device_id_rejected": "security",
    "session_revoked": "security",
    "session_revoked_access": "security",
    "sessions_revoked_all": "security",
    "rate_limit_degraded": "security",
    "verify_email_initial": "security",
    "verify_email_limited": "security",
    "verify_email_sent": "security",
}

# `security` отвечает на вопрос «кого пустили, кого нет и почему» - без
# идентификатора субъекта запись не отвечает на «кого», только на «что».
# Найдено не по коду - живым запросом к VictoriaLogs, где `user_created`
# оказался текстом "заведена учётная запись" без единого поля о том, чья
# запись. Восемь событий из двенадцати несли этот же пробел молча:
# `user_id` был доступен в коде каждого вызова и просто не передавался.
#
# Два вида исключения, а не одно:
#
#   - субъект решения по построению неизвестен (обмен токена ещё не
#     привязан к профилю, отказ случился до того, как личность
#     установлена) - требовать здесь `user_id` значило бы требовать
#     выдуманное значение;
#   - решение принято не по учётной записи, а по другому ключу
#     (`rate_limit_degraded` - лимитер общий, и субъект решения - это
#     ключ лимита, а не обязательно пользователь).
SECURITY_SUBJECT_UNKNOWN = frozenset({
    "token_exchange", "token_rejected", "login_rejected",
})
SECURITY_SUBJECT_FIELD: dict[str, str] = {
    "rate_limit_degraded": "key",
}
DEFAULT_SECURITY_SUBJECT_FIELD = "user_id"

# Значения констант потока из `telemetry/logging.py`. Проверка читает их
# оттуда же, откуда берёт код: список, переписанный сюда руками, однажды
# разойдётся с тем, что на самом деле уедет в хранилище.
STREAM_CONSTANTS: dict[str, str] = {}
for line in (PACKAGE / "telemetry" / "logging.py").read_text(encoding="utf-8").splitlines():
    if line.startswith("STREAM_"):
        name, _, value = line.partition(" = ")
        STREAM_CONSTANTS[name.strip()] = value.strip().strip('"')

# Имя поля тоже оттуда: `stream` занят меткой контейнера, и запись,
# положенная не в то поле, теряется в хранилище молча.
FIELD = STREAM_CONSTANTS.pop("STREAM_FIELD")


def logging_calls(tree: ast.AST):
    """Вызовы журнала в модуле: узел, уровень."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in LEVELS:
            continue
        if not isinstance(func.value, ast.Name) or func.value.id not in LOGGERS:
            continue
        yield node, func.attr


def literal(node: ast.expr | None) -> str | None:
    """Значение узла, если оно известно статически."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # `logging_envelope.STREAM_SECURITY` — константа, а не строка.
    if isinstance(node, ast.Attribute) and node.attr in STREAM_CONSTANTS:
        return STREAM_CONSTANTS[node.attr]
    return None


def extra_of(call: ast.Call) -> dict[str, ast.expr] | None:
    for keyword in call.keywords:
        if keyword.arg == "extra":
            if not isinstance(keyword.value, ast.Dict):
                return None
            return {
                key.value: value
                for key, value in zip(keyword.value.keys, keyword.value.values, strict=True)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    return None


def main() -> int:
    if not PACKAGE.exists():
        print(f"нет пакета {PACKAGE}", file=sys.stderr)
        return 1

    violations: list[str] = []
    seen: set[str] = set()
    checked = 0

    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(ROOT)

        for call, level in logging_calls(tree):
            checked += 1
            where = f"{rel}:{call.lineno}"

            if path.parent == REPOSITORIES:
                violations.append(
                    f"{where}: журнал в слое repositories — запись получит `span_id` "
                    f"охватывающего спана, а не того, который сейчас: спаны драйвера "
                    f"создаёт инструментация в обход `span()`"
                )
                continue

            extra = extra_of(call)

            if extra is None:
                violations.append(f"{where}: log.{level} без extra — нет ни события, ни потока")
                continue

            event = literal(extra.get("event"))
            if event is None:
                violations.append(f"{where}: нет `event` или он вычисляется — по такому имени не искать")
                continue
            seen.add(event)

            if "stream" in extra:
                violations.append(
                    f"{where}: поле `stream` занято меткой контейнера (stdout/stderr) — "
                    f"поток журнала кладётся в `{FIELD}`"
                )

            if "result" not in extra:
                violations.append(f"{where}: событие `{event}` без `result` — успех это или отказ, неизвестно")

            expected = EVENTS.get(event)
            if expected is None:
                violations.append(
                    f"{where}: событие `{event}` нет в каталоге — впишите его в "
                    f"{Path(__file__).name} и выберите поток"
                )
                continue

            stream = literal(extra.get(FIELD))
            if FIELD in extra and stream is None:
                violations.append(f"{where}: `{FIELD}` у `{event}` вычисляется — проверить нечем")
            elif expected == APPLICATION and stream is not None:
                violations.append(
                    f"{where}: `{event}` задаёт поток `{stream}` явно, "
                    f"а он умолчательный — лишняя строка, которая однажды разойдётся"
                )
            elif expected != APPLICATION and stream != expected:
                violations.append(
                    f"{where}: `{event}` уходит в `{stream or APPLICATION}`, "
                    f"а по каталогу это `{expected}` — срок хранения будет чужой"
                )

            if expected == "security" and event not in SECURITY_SUBJECT_UNKNOWN:
                subject_field = SECURITY_SUBJECT_FIELD.get(
                    event, DEFAULT_SECURITY_SUBJECT_FIELD
                )
                if subject_field not in extra:
                    violations.append(
                        f"{where}: `{event}` в security без `{subject_field}` — "
                        f"«кого пустили, кого нет и почему» без «кого»"
                    )

    stale = sorted(set(EVENTS) - seen)
    for event in stale:
        violations.append(f"каталог: событие `{event}` больше никто не пишет — уберите его")

    print(f"  проверено вызовов журнала: {checked}")
    if violations:
        print(f"  нарушений контракта журналов: {len(violations)}")
        for v in violations:
            print(f"  ✗ {v}")
        return 1
    print(f"  каталог событий: {len(EVENTS)}, поток задан у каждого")
    return 0


if __name__ == "__main__":
    sys.exit(main())
