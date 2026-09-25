"""Запись квитанции: порядок шагов, отказ против исключения, пересчёт проекции.

Одна правка этого файла в `G3-003` называется вслух, потому что выглядит
иначе: соединение **получило** `transaction()`, а тест `test_транзакции_нет`,
стороживший её отсутствие, стал `test_квитанция_пишется_в_транзакции`.
Прежний тест ошибкой не был: при одной записи обёртка не давала ничего и
выглядела гарантией, которой не является. С `G3-003` у квитанции появляется
вторая запись — проекция непрочитанного — и **второй писатель той же
строки**, поэтому граница согласованности появилась, а вместе с ней и замок
беседы, который обёртка держит.

Вторая правка — `G3-007`, и она тоже называется вслух: у результата
появились **прежнее состояние** и **множество заблокированных**, а порядок
шагов вырос на два чтения. Прежнее читается **до** записи (после неё его
не достать), блокировка — после пересчёта, и оба шага в том же тесте
порядка, что и раньше. Сдвиг выводится из прежнего **свойством**, а не
полем: два поля — «прежнее» и «сдвинулось» — разошлись бы при первой
правке, а здесь истина одна.
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
    """Подменённая половина: чем ответил замок, что спросил пересчёт,
    каким было прежнее состояние и с кем у читателя блокировка."""

    def __init__(self) -> None:
        self.locks: list[ConversationId] = []
        self.recounts: list[dict[str, object]] = []
        self.previous_reads: list[UserId] = []
        self.block_reads: list[dict[str, object]] = []


def _projection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    watermark: int = 0,
    count: int = 0,
    gone: bool = False,
    previous: ReadState | None = None,
    blocked: frozenset[UserId] = frozenset(),
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

    async def _прежнее(
        conn: Connection,
        *,
        conversation_id: ConversationId,
        user_id: UserId,
    ):
        conn.calls.append("fetch_read_state")
        projection.previous_reads.append(user_id)
        return previous

    async def _блокировка(
        conn: Connection,
        *,
        viewer: UserId,
        conversation_id: ConversationId | None = None,
    ):
        conn.calls.append("blocked_with")
        projection.block_reads.append(
            {"viewer": viewer, "conversation_id": conversation_id}
        )
        return blocked

    monkeypatch.setattr(service, "lock_offsets", _lock)
    monkeypatch.setattr(service, "recount_unread", _recount)
    monkeypatch.setattr(service.read_states, "fetch_read_state", _прежнее)
    monkeypatch.setattr(service.conversations, "blocked_with", _блокировка)
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


def test_порядок_шагов_голова_замок_прежнее_запись_пересчёт(monkeypatch):
    """Ронит любую перестановку из шести шагов.

    Голова — до транзакции: она односторонняя граница присланного, а не
    вход пересчёта, и держать на ней замок беседы значило бы платить за
    лишний запрос. Замок — до записи: он и есть то, что делает пересчёт
    согласованным с приращением потребителя. Прежнее — **до** записи:
    после неё его не достать, а именно оно отвечает на вопрос «сдвинулось
    ли». Пересчёт — после записи: нижняя граница берётся из записанной
    строки. Блокировка читается последней и **под тем же замком**:
    соединение уже есть, а второй поход в базу за ней был бы отдельной
    транзакцией ради чтения, которое ничего не пишет. Пути отказа до неё
    не доходят — при пустом ответе замка беседы нет и спрашивать не о чем.
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
        "fetch_read_state",
        "upsert_read_state",
        "recount_unread",
        "blocked_with",
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


# ---------------------------------------------------------------------------
# Сдвиг: по нему решается, уходит ли событие собеседнику
# ---------------------------------------------------------------------------


def _состояние(delivered: int, read: int) -> ReadState:
    return ReadState(
        delivered_seq=ConversationSeq(delivered), read_seq=ConversationSeq(read)
    )


def test_продвижение_от_прежнего_это_сдвиг(monkeypatch):
    """Первый конец пары: состояние выросло — событие обязано уйти.

    Прежнее `(7, 7)`, записанное `(9, 9)`: `GREATEST` сдвинул строку, и
    собеседник обязан узнать об этом, не дожидаясь перезагрузки страницы.
    Сравнение с **присланным** здесь молчит: присланное нормализуется в то
    же `(9, 9)`, и «ничего не изменилось» оказалось бы ответом на
    настоящий сдвиг.
    """
    _allow(monkeypatch)
    _head(monkeypatch, 9)
    _store(monkeypatch, delivered=9, read=9)
    _projection(monkeypatch, watermark=9, previous=_состояние(7, 7))

    result = _set(Connection(), receipts=Receipts(read_seq=9))

    assert result.ok
    assert result.advanced


def test_отставшая_квитанция_не_сдвиг(monkeypatch):
    """Второй конец пары: состояние не изменилось — события нет.

    Второе устройство того же человека опоздало: в строке уже девять, а
    оно сообщает два. Клиенту вернётся **текущее** состояние (этим
    «применено» и отличается от «проигнорировано»), но рассылать событие
    о неподвижном состоянии нельзя: подписчики получили бы
    `message.read {read_seq: 2}` — утверждение, откатывающее отметку
    назад. Дефект, который ронит эта пара, — сравнение записанного с
    **присланным**: оно краснит ровно наоборот, молчит на продвижении и
    срабатывает на отставке.
    """
    _allow(monkeypatch)
    _head(monkeypatch, 9)
    _store(monkeypatch, delivered=9, read=9)
    _projection(monkeypatch, watermark=9, previous=_состояние(9, 9))

    result = _set(Connection(), receipts=Receipts(read_seq=2))

    assert result.ok
    assert result.state is not None and result.state.read_seq == 9
    assert not result.advanced


def test_первый_нулевой_запрос_не_сдвиг(monkeypatch):
    """Ронит сравнение с прежним **без приведения** (D2).

    Оба поля разрешают ноль, поэтому первый же запрос `{read_seq: 0}`
    законен: строки ещё нет, а `upsert` её **вставит** — парой `(0, 0)`.
    Сравнение `stored != previous` при `previous is None` истинно при
    watermark, не сдвинувшемся ни на шаг, и в канал ушло бы
    `message.read {read_seq: 0}` — утверждение, которого никто не делал.
    Приводится только **сравниваемое**: в ответе отсутствие строки
    остаётся отсутствием (`result.previous is None`), а не парой нулей.
    """
    _allow(monkeypatch)
    _head(monkeypatch, 9)
    _store(monkeypatch, delivered=0, read=0)
    _projection(monkeypatch, watermark=9, previous=None)

    result = _set(Connection(), receipts=Receipts(read_seq=0))

    assert result.ok
    assert not result.advanced
    # Отсутствие — не ноль: то же правило, что у `unread_count` в теле
    # беседы. Подставь здесь `ReadState(0, 0)` — и REST сказал бы
    # «состояние есть», соврав о строке, которой не было.
    assert result.previous is None


def test_прежнее_читается_про_того_же_человека(monkeypatch):
    """Ронит чтение прежнего по одному лишь номеру беседы.

    Читается состояние **смотрящего**, а не «состояние беседы»: строка
    `read_states` ключуется парой, и запрос без второго ключа вернул бы
    чужое прочтение — например, более позднее, — а с ним и ложный ответ
    «продвижения нет» на настоящее продвижение. `upsert` ниже пишет ту же
    пару, и разойтись эти два ключа не могут по построению; проверка
    называет это вслух.
    """
    записанное: dict[str, object] = {}

    async def _записать(conn, **kwargs):
        записанное.update(kwargs)
        return _состояние(9, 9)

    _allow(monkeypatch)
    _head(monkeypatch, 9)
    monkeypatch.setattr(service.read_states, "upsert_read_state", _записать)
    projection = _projection(monkeypatch, watermark=9, previous=_состояние(7, 7))

    _set(Connection(), receipts=Receipts(read_seq=9))

    assert projection.previous_reads == [VIEWER_ID]
    assert записанное["user_id"] == VIEWER_ID


def test_блокировка_едет_в_результат_фактом(monkeypatch):
    """Ронит чтение предиката без беседы (D14).

    С двумя аргументами множество значит «кто из **этой** беседы
    заблокирован с читателем»; без второго — «с кем читатель заблокирован
    вообще», и тогда одна блокировка в посторонней беседе погасила бы
    живую квитанцию здесь. Сервис только **называет** факт: по нему
    молчит публикация, и решает это обработчик (`api/main.py`), у
    которого есть чем публиковать. В базу `api` ходить не вправе
    (`scripts/check-layers.py`) — поэтому и здесь.
    """
    peer = UserId(uuid.UUID("44444444-4444-4444-4444-444444444444"))
    _allow(monkeypatch)
    _head(monkeypatch, 9)
    _store(monkeypatch, delivered=9, read=9)
    projection = _projection(
        monkeypatch, watermark=9, previous=_состояние(7, 7), blocked=frozenset({peer})
    )

    result = _set(Connection(), receipts=Receipts(read_seq=9))

    assert result.blocked_with == frozenset({peer})
    assert projection.block_reads == [
        {"viewer": VIEWER_ID, "conversation_id": CONVERSATION}
    ]


def test_без_блокировки_множество_пусто_и_это_не_отсутствие(monkeypatch):
    """Ронит `None` вместо пустого множества.

    Пустое множество — положительный ответ «блокировок нет», и обработчик
    читает его как `not result.blocked_with`. `None` на его месте дал бы
    тот же ответ случайно, но `frozenset` в типе поля объявлен не зря:
    значение, отданное вызывающему, должно отвечать на вопрос, а не
    обозначать «не спрашивали».
    """
    _allow(monkeypatch)
    _head(monkeypatch, 9)
    _store(monkeypatch, delivered=9, read=9)
    _projection(monkeypatch, watermark=9, previous=_состояние(7, 7))

    result = _set(Connection(), receipts=Receipts(read_seq=9))

    assert result.blocked_with == frozenset()
    assert result.blocked_with is not None


# ---------------------------------------------------------------------------
# Публикация: канал, тело и исход
# ---------------------------------------------------------------------------


class FakeRealtime:
    """Записывает публикации, отвечая успехом, как настроено."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel, data):
        self.published.append((channel, data))
        return self.ok


def test_событие_уходит_в_канал_беседы_и_несёт_номера():
    """Ронит переиспользование `seq` под номер квитанции и чужой канал.

    `seq` — номер **сообщения**, и разрыв в нём клиент читает как «пропустил
    события, догружай историю»; второе значение в том же поле сломало бы
    этот детектор у клиента, который о квитанциях не знает вовсе, — то
    есть ровно то, что запрещает `CTR-003`. Поэтому номер квитанции едет
    своим именем, и это проверяется буквально: в теле нет ключа `seq`.

    Канал берётся из `realtime_delivery.channel_for`, а не собирается на
    месте: имя канала — часть контракта, и вторая его сборка разошлась бы
    с первой молча.
    """
    realtime = FakeRealtime()

    published = asyncio.run(
        service.announce_read(
            realtime=realtime,
            conversation_id=CONVERSATION,
            reader_id=VIEWER_ID,
            state=_состояние(9, 7),
        )
    )

    assert published is True
    assert realtime.published == [
        (
            f"conversation:{CONVERSATION}",
            {
                "type": "message.read",
                "reader_id": str(VIEWER_ID),
                "read_seq": 7,
                "delivered_seq": 9,
            },
        )
    ]


def test_без_клиента_событие_не_уходит_и_это_не_ошибка():
    """Ронит падение на необязательном Centrifugo.

    `runtime.centrifugo_client_from_env` отдаёт `None`, когда стенд поднят
    без realtime-слоя, и квитанция обязана остаться рабочей: её предмет —
    запись состояния, а публикация лишь ускоряет доставку. Падение здесь
    сделало бы realtime условием записи, чего решение не принимало.
    """
    assert (
        asyncio.run(
            service.announce_read(
                realtime=None,
                conversation_id=CONVERSATION,
                reader_id=VIEWER_ID,
                state=_состояние(1, 1),
            )
        )
        is False
    )


def test_отказ_публикации_виден_исходом_а_не_успехом(monkeypatch):
    """Ронит `return True` независимо от ответа Centrifugo.

    Исход считается метрикой, и «ok» на неудавшейся публикации скрыла бы
    ровно тот случай, ради которого метрика заведена: коммит прошёл,
    событие не ушло, а клиент узнает об этом только сверкой. Образец —
    `services/session_management.py`, где тот же разрыв соединения тоже
    ускорение, а не условие.
    """
    realtime = FakeRealtime(ok=False)
    measured: list[str] = []
    monkeypatch.setattr(service.metrics, "realtime_published", measured.append)

    published = asyncio.run(
        service.announce_read(
            realtime=realtime,
            conversation_id=CONVERSATION,
            reader_id=VIEWER_ID,
            state=_состояние(1, 1),
        )
    )

    assert published is False
    assert measured == ["failed"]
