"""Проверки проводки API: проба живости, метрики, журнал обращений.

Проверяется каркас: сервис отвечает, отдаёт метрики, не создаёт временных
рядов на каждый путь и пишет журнал в общем конверте. Готовность живёт
отдельно — в `test_readiness.py`: она зависит от состояния базы, и смешивать
её с проводкой значит получить тест, падающий по двум разным причинам.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from messenger.api.main import app
from messenger.telemetry.logging import configure


@pytest.fixture
def client():
    return TestClient(app)


def test_живость_не_зависит_от_зависимостей(client):
    """Проба живости отвечает, даже когда вокруг ничего нет.

    Проба, падающая из-за недоступной базы, устраивает каскад: kubelet
    перезапускает исправные поды, те снова не видят базу — сервис
    уничтожает себя, пока база восстанавливается.
    """
    assert client.get("/livez").status_code == 200


def test_метрики_отдаются(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "messenger_build_info" in r.text


def test_версия_видна_в_метрике(client):
    r = client.get("/metrics")
    # «Что сейчас запущено» — непрерывный ряд. Из него нельзя строить
    # отметки на графиках, но знать версию по метрике необходимо.
    assert 'messenger_build_info{service=' in r.text


def test_маршрут_в_метке_шаблон_а_не_путь(client):
    """Главное правило кардинальности.

    Неизвестный путь от сканера портов не должен создавать временной ряд.
    """
    client.get("/no-such-path-12345")
    client.get("/no-such-path-67890")
    body = client.get("/metrics").text
    assert "no-such-path-12345" not in body
    assert 'route="unmatched"' in body


def test_журнал_обращений_в_общем_конверте(client, capsys):
    """Запись выбирается по событию, а не по позиции.

    Сторонние библиотеки — uvicorn, httpx — пишут через тот же корневой
    журнал и получают тот же конверт. Это правильно: единый формат на всё,
    что выходит из процесса. Но означает, что «последняя строка» может
    принадлежать не нам, и первая версия теста на этом и упала.

    configure() вызывается здесь намеренно: обработчик связывается с потоком
    вывода в момент создания, а перехват вывода в тестах подменяет поток
    позже. Без повторной настройки запись уходит мимо перехвата — это
    свойство самого журналирования, а не теста.
    """
    configure()
    client.get("/livez")
    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    ours = [r for r in records if r.get("event") == "http_request"]
    assert ours, "обращение не попало в журнал"
    record = ours[-1]
    for field in ("timestamp", "service", "environment", "version", "event", "trace_id"):
        assert field in record, f"нет поля конверта {field}"
    assert record["event"] == "http_request"
    assert record["log_stream"] == "access"


def test_трасса_начинается_здесь_если_её_не_прислали(client, capsys):
    """Запись обращения и ответ несут одну и ту же трассу.

    Без этого поддержка получает от человека номер, которого нет ни
    в одном журнале, — а искать по времени в трёх сервисах значит
    находить совпадения, а не причину.
    """
    configure()
    r = client.get("/livez")
    trace_id = r.headers["X-Trace-Id"]
    assert len(trace_id) == 32

    записи = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    наши = [x for x in записи if x.get("event") == "http_request"]
    assert наши[-1]["trace_id"] == trace_id


def test_чужая_трасса_продолжается_а_не_начинается_заново(client, capsys):
    """Запрос к нам может быть продолжением чужого.

    Разрывать цепочку на своей границе значит превращать сквозную
    трассу в две несвязанные половины ровно там, где интереснее всего.
    """
    configure()
    чужая = "4bf92f3577b34da6a3ce929d0e0e4736"
    r = client.get("/livez", headers={"traceparent": f"00-{чужая}-00f067aa0ba902b7-01"})
    assert r.headers["X-Trace-Id"] == чужая

    записи = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    наши = [x for x in записи if x.get("event") == "http_request"]
    assert наши[-1]["trace_id"] == чужая
