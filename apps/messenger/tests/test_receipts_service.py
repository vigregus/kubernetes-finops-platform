"""Запись квитанции: порядок шагов, отказ против исключения, пересчёт проекции.

Одна правка этого файла в `G3-003` называется вслух, потому что выглядит
иначе: соединение **получило** `transaction()`, а тест `test_транзакции_нет`,
стороживший её отсутствие, стал `test_квитанция_пишется_в_транзакции`.
Прежний тест ошибкой не был: при одной записи обёртка не давала ничего и
выглядела гарантией, которой не является. С `G3-003` у квитанции появляется
вторая запись — проекция непрочитанного — и **второй писатель той же
строки**, поэтому граница согласованности появилась, а вместе с ней и замок
беседы, который обёртка держит.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from messenger.domain.authorization import Action, Decision, ResourceRef
from messenger.domain.errors import Reason, Visibility
from messenger.domain.ids import ConversationId, ConversationSeq, UserId
from messenger.domain.receipts import InvalidReceipt, ReadState, Receipts
from messenger.domain.unread import UnreadCount
from messenger.domain.user import User
from messenger.services import receipts as service

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
VIEWER_ID = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
CONVERSATION = ConversationId(uuid.UUID("33333333-3333-3333-3333-333333333333"))


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class Connection:
    """Соединение с транзакцией и журналом вызовов.

    Журнал ведётся потому, что здесь проверяется **порядок**, а не набор:
    голова читается до транзакции (она односторонняя граница присланного,
    а не вход пересчёта), замок занимается первым из того, что под
    транзакцией, а пересчёт идёт после записи состояния — считать по
    строке, в которую ещё не записали, значило бы считать по прошлому
    ответу другого устройства.
    """

    def __init__(self) -> None:
        self.transactions = 0
        self.calls: list[str] = []

    def transaction(self) -> _Transaction:
        self.transactions += 1
        self.calls.append("transaction")
        return _Transaction()


class Projection:
    """Подменённая половина непрочитанного: чем ответил замок и что спросил пересчёт."""

    def __init__(self) -> None:
        self.locks: list[ConversationId] = []
        self.recounts: list[dict[str, object]] = []


def _projection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    watermark: int = 0,
    count: int = 0,
    gone: bool = False,
) -> Projection:
    projection = Projection()

    async def _lock(conn: Connection, *, conversation_id: ConversationId):
        conn.calls.append("lock_offsets")
        projection.locks.append(conversation_id)
        # `gone` — это живой ответ замка, а не подстановка: чекпойнт
        # удалённой беседы уносит каскад `0011`, поэтому «строки нет» —
        # ровно «беседы нет».
        if gone:
            return None
        return ConversationSeq(watermark)

    async def _recount(
        conn: Connection,
        *,
        conversation_id: ConversationId,
        user_id: UserId,
        above_seq: ConversationSeq,
        through_seq: ConversationSeq,
    ):
        conn.calls.append("recount_unread")
        projection.recounts.append(
            {
                "conversation_id": conversation_id,
                "user_id": user_id,
                "above_seq": above_seq,
                "through_seq": through_seq,
            }
        )
        return UnreadCount(count)

    monkeypatch.setattr(service, "lock_offsets", _lock)
    monkeypatch.setattr(service, "recount_unread", _recount)
    return projection


def _user() -> User:
    return User(
        user_id=VIEWER_ID,
        external_id="kc-1",
        display_name="Аня",
        email="anya@example.org",
        email_verified=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _set(conn, **overrides):
    values = {
        "viewer": _user(),
        "conversation_id": CONVERSATION,
        "receipts": Receipts(read_seq=3),
    }
    return asyncio.run(service.set_receipts(conn, **{**values, **overrides}))


def _allow(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _разрешить(conn, **kwargs):
        return Decision.allow()

    monkeypatch.setattr(service.authorization, "authorize", _разрешить)


def _head(monkeypatch: pytest.MonkeyPatch, value: int | None, *, conn=None) -> None:
    async def _голова(conn_, *, conversation_id):
        if conn is not None:
            conn.calls.append("fetch_last_seq")
        return None if value is None else ConversationSeq(value)

    monkeypatch.setattr(service.conversations, "fetch_last_seq", _голова)


def _store(monkeypatch: pytest.MonkeyPatch, *, delivered: int, read: int, conn=None) -> None:
    async def _записать(conn_, **kwargs):
        if conn is not None:
            conn.calls.append("upsert_read_state")
        return ReadState(
            delivered_seq=ConversationSeq(delivered), read_seq=ConversationSeq(read)
        )

    monkeypatch.setattr(service.read_states, "upsert_read_state", _записать)


# ---------------------------------------------------------------------------
# Порядок: до работы, до замка, под замком
# ---------------------------------------------------------------------------


def test_пустая_квитанция_отвергается_до_всякой_работы(monkeypatch):
    # Порядок здесь и есть предмет проверки: проверка пределов отвечает
    # без базы, поэтому обязана идти до авторизации и до соединения.
    async def _must_not_run(*args, **kwargs):
        raise AssertionError("негодная квитанция дошла до работы")

    monkeypatch.setattr(service.authorization, "authorize", _must_not_run)
    monkeypatch.setattr(service.conversations, "fetch_last_seq", _must_not_run)
    monkeypatch.setattr(service.read_states, "upsert_read_state", _must_not_run)

    with pytest.raises(InvalidReceipt):
        _set(Connection(), receipts=Receipts())


def test_посторонний_не_читает_голову_беседы(monkeypatch):
    # Не только «отказ вместо записи»: неучастник не должен получить
    # прочитанную за него голову. Поэтому `fetch_last_seq` здесь падает,
    # а не возвращает число, — иначе проверка была бы про запись, а не
    # про порядок.
    async def _отказать(conn, **kwargs):
        return Decision.deny(Reason.NOT_A_MEMBER)

    async def _must_not_run(*args, **kwargs):
        raise AssertionError("голова беседы прочитана для постороннего")

    monkeypatch.setattr(service.authorization, "authorize", _отказать)
    monkeypatch.setattr(service.conversations, "fetch_last_seq", _must_not_run)
    monkeypatch.setattr(service.read_states, "upsert_read_state", _must_not_run)

    result = _set(Connection())
    assert result.rejection is Reason.NOT_A_MEMBER
    assert not result.ok


def test_отказ_несёт_видимость_решения(monkeypatch):
    # `KNOWN` сегодня недостижим в бою, но сервис обязан донести значение,
    # а не заменить его умолчанием: иначе объявленный `403` станет
    # недостижимым навсегда, и заметят это при первом разборе чужой беседы.
    async def _отказать(conn, **kwargs):
        return Decision.deny(Reason.NOT_A_MEMBER, visibility=Visibility.KNOWN)

    monkeypatch.setattr(service.authorization, "authorize", _отказать)

    result = _set(Connection())
    assert result.rejection is Reason.NOT_A_MEMBER
    assert result.visibility is Visibility.KNOWN


def test_номер_выше_головы_отвергается_и_не_пишется(monkeypatch):
    async def _must_not_run(*args, **kwargs):
        raise AssertionError("отвергнутая квитанция дошла до записи")

    _allow(monkeypatch)
    _head(monkeypatch, 5)
    # Проекция подменена, хотя сюда дело и не доходит: иначе тест краснел
    # бы на любом изменении того, **где** стоит проверка границы, а он про
    # другое — про то, что отвергнутая квитанция не пишется.
    _projection(monkeypatch)
    monkeypatch.setattr(service.read_states, "upsert_read_state", _must_not_run)

    with pytest.raises(InvalidReceipt):
        _set(Connection(), receipts=Receipts(delivered_seq=6))


def test_пропавшая_беседа_не_роняет_сервис(monkeypatch):
    """Ронит сравнение `None` с числом вместо ответа.

    Между решением о праве и чтением головы беседу могли удалить — и это
    не гипотеза: беседу удаляет уборка интеграционной проверки, а с нею
    уходят и её производные. `None`, дошедший до сравнения с числом,
    дал бы `500` вместо честного `404`.
    """
    _allow(monkeypatch)
    _head(monkeypatch, None)
    # По той же причине, что выше: тест про `404`, а не про место замка.
    _projection(monkeypatch)

    result = _set(Connection())
    assert result.rejection is Reason.CONVERSATION_NOT_FOUND


def test_пропавшая_беседа_не_занимает_замок(monkeypatch):
    """Ронит перенос чтения головы под замок.

    Голова читается до транзакции, и здесь это видно как ноль открытых
    транзакций — замок не просто отпущен, он не брался. Довод — не
    «вставка упала бы на несуществующей беседе» (она больше не падает:
    `lock_offsets` спрашивает существование тем же оператором), а цена
    лишнего шага: замок на беседе, которой нет, — это запись чекпойнта,
    которой не бывает, и место в очереди к строке, которой никто
    не касается. Ровно тот же порядок нужен и на живом пути: голова —
    односторонняя граница присланного, а не вход пересчёта.
    """
    conn = Connection()
    _allow(monkeypatch)
    _head(monkeypatch, None)

    result = _set(conn)

    assert result.rejection is Reason.CONVERSATION_NOT_FOUND
    assert conn.transactions == 0


def test_беседа_исчезла_под_замком_и_это_отказ(monkeypatch):
    """Ронит падение на беседе, удалённой между головой и замком.

    Та же гонка, что выше, но на шаг позже: голова прочитана, беседа
    жива, а к моменту замка её уже нет. Чекпойнта у неё тоже нет —
    каскад `0011` унёс его вместе с беседой, — поэтому пустой ответ
    замка значит «беседы нет», а не «замок не взялся». Ответ тот же
    `404`, и он **не исключение**: падение здесь стояло бы не на гонке,
    а на каждом удалении беседы, чью квитанцию клиент успел отправить.

    Проверяется и цена: ни состояния, ни пересчёта. Записывать квитанцию
    беседе, которой нет, некуда, а пересчёт по ней вернул бы ноль —
    то есть «всё прочитано» про удалённое, и вернул бы уверенно.
    """
    conn = Connection()
    _allow(monkeypatch)
    _head(monkeypatch, 9, conn=conn)
    _store(monkeypatch, delivered=3, read=3, conn=conn)
    _projection(monkeypatch, gone=True)

    result = _set(conn)

    assert result.rejection is Reason.CONVERSATION_NOT_FOUND
    assert result.state is None
    assert result.unread_count is None
    assert conn.calls == ["fetch_last_seq", "transaction", "lock_offsets"]


def test_порядок_шагов_голова_замок_запись_пересчёт(monkeypatch):
    """Ронит любую перестановку из четырёх шагов.

    Голова — до транзакции: она односторонняя граница присланного, а не
    вход пересчёта, и держать на ней замок беседы значило бы платить за
    лишний запрос. Замок — до записи: он и есть то, что делает пересчёт
    согласованным с приращением потребителя. Пересчёт — после записи:
    нижняя граница берётся из записанной строки.
    """
    conn = Connection()
    _allow(monkeypatch)
    _head(monkeypatch, 9, conn=conn)
    _store(monkeypatch, delivered=3, read=3, conn=conn)
    _projection(monkeypatch, watermark=4, count=3)

    _set(conn)

    assert conn.calls == [
        "fetch_last_seq",
        "transaction",
        "lock_offsets",
        "upsert_read_state",
        "recount_unread",
    ]


# ---------------------------------------------------------------------------
# Транзакция и замок
# ---------------------------------------------------------------------------


def test_квитанция_пишется_в_транзакции(monkeypatch):
    """Ронит удаление обёртки `conn.transaction()`.

    Без неё замок беседы отпускается сразу после чтения чекпойнта, и между
    чтением и абсолютной записью помещается приращение потребителя: он
    применит следующее событие и закоммитит `+1`, квитанция запишет своё
    число, посчитанное по прежнему чекпойнту, и вклад события потеряется
    навсегда. Тест называет свойство вслух — иначе «обёртки нет» снова
    выглядит как недосмотр.
    """
    conn = Connection()
    _allow(monkeypatch)
    _head(monkeypatch, 9)
    _store(monkeypatch, delivered=3, read=3)
    _projection(monkeypatch, watermark=4)

    _set(conn)

    assert conn.transactions == 1


def test_право_запрашивается_на_чтение_и_на_свою_беседу(monkeypatch):
    # Квитанция подтверждает **читаемое** состояние, а не изменяет беседу.
    # Разница не косметическая: по записанной модели
    # (docs/messenger/08-authorization.md) блокировка запрещает `write`
    # обеим сторонам, но оставляет `read` старой истории. Спроси сервис
    # `WRITE_CONVERSATION` — и в тот день, когда появится блокировка,
    # заблокированный не сможет сдвинуть собственный `last_read_seq`:
    # квитанция отвергнется, непрочитанное зависнет, а на нём стоит `G3-003`.
    #
    # Проверять это обязательно **здесь**: подмены в этом файле принимают
    # `**kwargs` и не видят ни действия, ни ресурса. Поэтому неверный
    # `Action` — и точно так же право, спрошенное про чужую беседу, —
    # проходят мимо всех остальных проверок файла, оставаясь зелёными.
    #
    # `membership_cache` в вызове быть не должно: ветка кеша в
    # `authorization._conversation` требует **обоих** условий — действия
    # `READ` и непустого кеша, — и свежесть членства держится именно вторым.
    # Проверка идёт соединением писателя, поэтому чтение членства из Postgres
    # здесь не ослабляется.
    seen: dict[str, object] = {}

    async def _разрешить(conn, **kwargs):
        seen.update(kwargs)
        return Decision.allow()

    monkeypatch.setattr(service.authorization, "authorize", _разрешить)
    _head(monkeypatch, 9)
    _store(monkeypatch, delivered=3, read=3)
    _projection(monkeypatch, watermark=4)

    result = _set(Connection())
    assert result.ok
    assert seen["action"] is Action.READ_CONVERSATION
    assert seen["resource"] == ResourceRef.conversation(CONVERSATION)
    assert "membership_cache" not in seen


# ---------------------------------------------------------------------------
# Что уезжает в SQL
# ---------------------------------------------------------------------------


def test_запись_получает_нормализованную_пару(monkeypatch):
    # Предмет проверки — что уезжает в SQL. Присланное «прочитано 5»
    # без доставки обязано стать парой (5, 5): доставленное поднимается
    # до прочитанного, и это `RCP-004`.
    записанное: dict[str, object] = {}

    async def _записать(conn, **kwargs):
        записанное.update(kwargs)
        return ReadState(
            delivered_seq=ConversationSeq(5), read_seq=ConversationSeq(5)
        )

    _allow(monkeypatch)
    _head(monkeypatch, 9)
    monkeypatch.setattr(service.read_states, "upsert_read_state", _записать)
    _projection(monkeypatch, watermark=9)

    result = _set(Connection(), receipts=Receipts(read_seq=5))
    assert result.ok
    assert записанное["delivered_seq"] == 5
    assert записанное["read_seq"] == 5
    assert записанное["user_id"] == VIEWER_ID
    assert записанное["conversation_id"] == CONVERSATION


def test_сервис_возвращает_состояние_из_базы_а_не_присланное(monkeypatch):
    # Ответ на отставшую квитанцию несёт текущее значение. Присланное
    # клиент счёл бы применённым — именно этим «применено» и отличается
    # от «проигнорировано».
    _allow(monkeypatch)
    _head(monkeypatch, 9)
    _store(monkeypatch, delivered=9, read=9)
    _projection(monkeypatch, watermark=9)

    result = _set(Connection(), receipts=Receipts(read_seq=2))
    assert result.state is not None
    assert result.state.read_seq == 9


def test_пересчёт_ограничен_чекпойнтом_а_не_головой_беседы(monkeypatch):
    """Ронит подстановку головы беседы в верхнюю границу пересчёта.

    Голова — последний номер в беседе, чекпойнт — последний номер,
    который применил потребитель. Между ними лежит уже записанное, но
    ещё не применённое сообщение; в счёт оно входить не должно, иначе
    квитанция обнулит счётчик на нём, а потребитель, догнав, прибавит
    `+1` по устаревшему `last_read_seq` — и счёт разойдётся с источником
    истины навсегда.
    """
    projection = _projection(monkeypatch, watermark=4, count=1)
    _allow(monkeypatch)
    _head(monkeypatch, 9)
    _store(monkeypatch, delivered=3, read=3)

    _set(Connection())

    assert projection.recounts[0]["through_seq"] == 4


def test_нижняя_граница_берётся_из_строки_а_не_из_присланного(monkeypatch):
    """Ронит подстановку присланного `read_seq` в нижнюю границу.

    Два устройства одного пользователя пишут в одну строку, и проигравшее
    обязано пересчитаться по тому, что в ней лежит, а не по своему
    запоздалому «прочитано до двух». Подставив присланное, квитанция вернула
    бы счётчик к числу, включающему уже прочитанное.
    """
    projection = _projection(monkeypatch, watermark=9, count=0)
    _allow(monkeypatch)
    _head(monkeypatch, 9)
    _store(monkeypatch, delivered=7, read=7)

    _set(Connection(), receipts=Receipts(read_seq=2))

    assert projection.recounts[0]["above_seq"] == 7


def test_пересчёт_приходит_из_запроса_а_не_вычитанием(monkeypatch):
    """Ронит замену пересчёта на «минус единица».

    Квитанция перескакивает через несколько сообщений сразу, поэтому
    прежнее число знать недостаточно: оно могло быть и потеряно. Проверка
    тут ровно одна — что сервис **спросил** источник истины и вернул его
    число, а не преобразовал своё.
    """
    projection = _projection(monkeypatch, watermark=9, count=4)
    _allow(monkeypatch)
    _head(monkeypatch, 9)
    _store(monkeypatch, delivered=9, read=9)

    result = _set(Connection())

    assert len(projection.recounts) == 1
    assert projection.recounts[0]["user_id"] == VIEWER_ID
    assert projection.recounts[0]["conversation_id"] == CONVERSATION
    assert result.unread_count == 4


def test_число_приходит_даже_когда_читать_нечего(monkeypatch):
    """Ронит `if count:` на месте возврата.

    Ноль — значение, а не отсутствие: «всё прочитано» и «проекции нет» —
    разные ответы, и во втором сервис не побывал вовсе. Ноль, проглоченный
    как «неизвестно», превратил бы пустой счётчик в утраченную проекцию,
    то есть в худший из двух ответов.
    """
    _projection(monkeypatch, watermark=9, count=0)
    _allow(monkeypatch)
    _head(monkeypatch, 9)
    _store(monkeypatch, delivered=9, read=9)

    result = _set(Connection())

    assert result.unread_count == 0
    assert result.unread_count is not None
