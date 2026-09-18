"""Проверки конверта журналов и вычистки секретов.

OBS-SEC-001 на уровне модуля. Настоящая проверка — канарейка через живой
кластер и поиск в VictoriaLogs — идёт в G5, но ловить это на уровне модуля
дешевле: здесь отказ виден за миллисекунды и до слияния.
"""
from __future__ import annotations

import io
import json
import logging

import pytest

from messenger.telemetry.logging import ENVELOPE, JsonFormatter, configure, scrub


@pytest.fixture
def capture():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("api", "local", "sha256:abc"))
    log = logging.getLogger("test")
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False

    def emit(*args, **kwargs):
        stream.seek(0)
        stream.truncate()
        log.info(*args, **kwargs)
        return json.loads(stream.getvalue())

    return emit


def test_конверт_заполнен_целиком(capture):
    record = capture("сообщение принято", extra={"event": "message_accepted"})
    for field in ENVELOPE:
        assert field in record, f"нет обязательного поля {field}"
    assert record["service"] == "api"
    assert record["version"] == "sha256:abc"


def test_одна_строка_на_запись(capture):
    record = capture("первая\nвторая")
    # Перевод строки внутри текста не должен разрывать запись: построчные
    # приёмники прочитали бы вторую половину как отдельную битую строку.
    assert "\n" not in json.dumps(record)


@pytest.mark.parametrize("key", ["text", "password", "authorization", "presigned_url"])
def test_запрещённые_ключи_вычищаются(capture, key):
    record = capture("отправка", extra={key: "MY_SECRET_CANARY_123"})
    assert "MY_SECRET_CANARY_123" not in json.dumps(record, ensure_ascii=False)


def test_вложенный_словарь_тоже_вычищается(capture):
    record = capture("запрос", extra={"ctx": {"user": {"password": "hunter2"}}})
    assert "hunter2" not in json.dumps(record)


def test_jwt_узнаётся_по_виду_а_не_по_имени_поля():
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    # Ключ называется безобидно — и именно так секрет и попадает в журнал.
    assert jwt not in json.dumps(scrub({"note": f"токен {jwt}"}))


def test_подписанная_ссылка_вычищается():
    url = "https://s3.finops.local/messenger-attachments/a.jpg?X-Amz-Signature=deadbeef"
    assert "deadbeef" not in json.dumps(scrub({"link": url}))


def test_строка_подключения_с_паролем_вычищается():
    dsn = "postgresql://messenger:s3cr3t@messenger-db-pool:5432/messenger"
    assert "s3cr3t" not in json.dumps(scrub({"detail": f"подключение {dsn}"}))


def test_исключение_проходит_вычистку(capture):
    """Текст исключения — такой же неконтролируемый источник, как и extra.

    Важная граница: вычистка ловит секреты **узнаваемой формы** — JWT,
    подписанную ссылку, строку подключения с паролем. Произвольное слово
    в свободном тексте не поймает никто: отличить `hunter2` от обычного
    слова нечем.

    Отсюда два следствия, и оба записаны в стандарте журналов. Правило
    «не класть секреты в текст исключения» остаётся правилом для человека.
    А OBS-SEC-001 — канарейка через живой конвейер с поиском в
    VictoriaLogs — нужен именно потому, что этот модуль такую утечку
    не увидит.
    """
    dsn = "postgresql://messenger:s3cr3t@messenger-db-pool:5432/messenger"
    log = logging.getLogger("test")
    stream = log.handlers[0].stream
    stream.seek(0), stream.truncate()
    try:
        raise ValueError(f"не удалось подключиться: {dsn}")
    except ValueError:
        log.exception("сбой", extra={"event": "db_connect_failed"})
    payload = stream.getvalue()
    assert "exception" in payload
    assert "s3cr3t" not in payload


def test_обычные_поля_не_страдают(capture):
    record = capture("готово", extra={"duration_ms": 37, "result": "success"})
    assert record["duration_ms"] == 37
    assert record["result"] == "success"


def test_у_каждой_записи_есть_поток(capsys):
    """Запись без потока хранить правильно нельзя.

    У потоков разные сроки: `access` средний, `audit` длинный,
    `application` короткий. Один срок на всё - это либо дорогой
    отладочный мусор, либо потерянный аудит.
    """
    log = configure()
    log.info("что-то произошло", extra={"event": "нечто"})
    запись = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert запись["log_stream"] == "application"


def test_поток_можно_указать_явно(capsys):
    log = configure()
    log.info("вход", extra={"event": "login", "log_stream": "security"})
    запись = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert запись["log_stream"] == "security"


def test_идентификатор_обращения_подставляется_из_контекста(capsys):
    """Сервисный слой не знает про HTTP и не может положить его сам,
    а без него строки одного запроса нечем связать."""
    from messenger.telemetry.logging import REQUEST_ID

    log = configure()
    token = REQUEST_ID.set("запрос-1")
    try:
        log.info("шаг", extra={"event": "нечто"})
    finally:
        REQUEST_ID.reset(token)
    запись = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert запись["request_id"] == "запрос-1"


def test_библиотеки_не_пишут_на_info(capsys):
    """httpx печатает строку на каждый исходящий запрос, и в общем потоке
    это выглядит как событие приложения с `event: httpx`."""
    import logging as _logging

    configure()
    _logging.getLogger("httpx").info("HTTP Request: POST http://... 200 OK")
    вывод = capsys.readouterr().out
    assert "HTTP Request" not in вывод

    # Предупреждение библиотеки пропускается: это уже про нас.
    _logging.getLogger("httpx").warning("соединение оборвано")
    assert "соединение оборвано" in capsys.readouterr().out
