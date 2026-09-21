"""Правило присутствия: окно живости, его границы и один такт уборки.

Правило живёт в двух местах — Python (`domain/presence.py`) и SQL
(`repositories/sessions.py`) — и разойтись они могут молча. Поэтому здесь
проверяется не «функция считает то же, что считает она же», а тождества,
которые обязаны держаться между частями:

  * окно живости больше каденции продления, иначе живой клиент мигает
    офлайн между продлениями (это отказ, выглядящий как флапающая сеть,
    а не как неверная константа);
  * граница окна включающая, то есть окно ровно объявленной длины;
  * отметка «был в сети» не едет назад;
  * такт уборки — одна транзакция (одна граница `now()`) и порядок
    «убрать, потом считать».

Интеграционная половина — в `tests/integration/presence_check.py`: там
проверяется, что то же правило видно через базу и REST на двух устройствах
одного пользователя.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from messenger.adapters.centrifugo import CentrifugoSettings
from messenger.domain import presence
from messenger.domain.ids import UserId
from messenger.services import presence as service

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
USER_ID = UserId(uuid.UUID("15283722-3214-438d-bd2a-04f0f22f19c4"))


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class Connection:
    """Соединение, умеющее транзакцию, и журнал вызовов.

    Журнал ведётся, потому что предмет проверки — **порядок**: уборка идёт
    до счёта, а не после. Обратный порядок дал бы `online` по строкам,
    которые тут же удаляются, то есть число, заведомо большее правды.
    """

    def __init__(self) -> None:
        self.transactions = 0
        self.calls: list[str] = []

    def transaction(self) -> _Transaction:
        self.transactions += 1
        self.calls.append("transaction")
        return _Transaction()


# --- окно живости -----------------------------------------------------------


def test_окно_живости_больше_каденции_продления():
    """Тождество между двумя числами, живущими в разных модулях.

    Каденция берётся из настроек Centrifugo, а не из числа, переписанного
    сюда: переписанное разошлось бы с настоящим молча, и тест подтверждал бы
    сам себя. Ронится эта проверка уменьшением окна (`ONLINE_WINDOW_SECONDS`
    до минуты) — то есть ровно той правкой, которая в живом прогоне выглядит
    как нестабильная сеть.
    """
    settings = CentrifugoSettings(
        api_url="http://centrifugo:9000/api",
        api_key="key",
        token_hmac_secret_key="secret",
    )
    assert presence.ONLINE_WINDOW_SECONDS > settings.token_ttl_seconds, (
        "окно живости не больше каденции продления: живой клиент будет "
        "попадать в офлайн между продлениями"
    )


def test_граница_окна_включающая():
    cutoff = presence.online_cutoff(NOW)
    # Граница включающая: окно ровно объявленной длины, а не на мгновение
    # короче. Лишнее мгновение здесь стоило бы строки, удалённой уборщиком
    # из-под живого соединения.
    assert presence.is_online(cutoff, now=NOW)
    assert not presence.is_online(cutoff - timedelta(microseconds=1), now=NOW)
    assert presence.is_online(NOW, now=NOW)


def test_соединения_нет_это_не_живо():
    """`None` — не «давно», а «соединения нет»; живым он не считается."""
    assert not presence.is_online(None, now=NOW)


# --- отметка «был в сети» ---------------------------------------------------


def test_отметка_не_едет_назад():
    """Монотонность: присваивание вместо максимума роняет этот тест.

    Случай не выдуманный: два устройства одного человека продлеваются
    независимо, и опоздавшая транзакция принесла бы более старое время —
    `GREATEST` в SQL и `max` здесь держат одно и то же правило.
    """
    earlier = NOW - timedelta(hours=1)
    assert presence.last_seen_of(NOW, earlier) == NOW
    assert presence.last_seen_of(NOW, NOW + timedelta(hours=1)) == NOW + timedelta(hours=1)


def test_первое_подтверждение_это_и_есть_отметка():
    """«Ни разу не был в сети» — не особый случай: максимум из ничего."""
    assert presence.last_seen_of(None, NOW) == NOW


# --- доля онлайн ------------------------------------------------------------


def test_доля_на_пустой_базе_это_ноль():
    """Деление на ноль уронило бы такт уборки там, где смотреть не на что."""
    assert presence.online_share(online=0, registered=0) == 0.0
    assert presence.online_share(online=3, registered=12) == 0.25


def test_доля_берётся_из_одного_такта():
    result = service.SweepResult(deleted=2, online_users=3, registered_users=12)
    assert result.share == 0.25


# --- такт уборки ------------------------------------------------------------


def _stand(monkeypatch, *, deleted: int = 0, online: int = 1, registered: int = 4):
    """Подменяет репозитории такта: проверяется порядок, не SQL."""
    conn = Connection()
    seen: dict[str, object] = {}

    async def _delete(inner, *, window):
        seen["delete_window"] = window
        conn.calls.append("delete_stale_realtime_connections")
        return deleted

    async def _online(inner, *, window):
        seen["online_window"] = window
        conn.calls.append("count_online_users")
        return online

    async def _registered_users(inner):
        conn.calls.append("count_users")
        return registered

    monkeypatch.setattr(service.sessions, "delete_stale_realtime_connections", _delete)
    monkeypatch.setattr(service.sessions, "count_online_users", _online)
    monkeypatch.setattr(service.users, "count_users", _registered_users)
    return conn, seen


def test_такт_сначала_убирает_потом_считает(monkeypatch):
    """Порядок шагов и одна транзакция на весь такт.

    Ронится он двумя правками, и обе содержательны: перестановкой счёта
    вперёд (тогда `online` считается по строкам, которые тут же удаляются)
    и удалением обёртки `conn.transaction()` — тогда `now()` перестаёт быть
    одной границей, и доля собирается из двух состояний системы.
    """
    conn, seen = _stand(monkeypatch, deleted=2, online=1, registered=4)

    result = asyncio.run(service.sweep(conn))

    assert conn.calls == [
        "transaction",
        "delete_stale_realtime_connections",
        "count_online_users",
        "count_users",
    ]
    assert conn.transactions == 1
    assert result.deleted == 2
    assert result.online_users == 1
    assert result.registered_users == 4


def test_окно_уезжает_в_sql_длительностью(monkeypatch):
    """Границу считает Postgres, поэтому наружу уходит длительность.

    Секунды окна, посчитанные в Python, означали бы вторую границу рядом
    с `now()` базы: у часов пода и сервера базы нет причины идти одинаково.
    """
    conn, seen = _stand(monkeypatch)

    asyncio.run(service.sweep(conn))

    окно = timedelta(seconds=presence.ONLINE_WINDOW_SECONDS)
    assert seen["delete_window"] == окно
    assert seen["online_window"] == окно
