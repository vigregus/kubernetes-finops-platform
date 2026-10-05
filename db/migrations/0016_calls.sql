-- Звонки один-на-один (ADR 0007, CALL-001).
--
-- Две таблицы, и разделены они по сроку жизни строки, а не по предмету.
--
-- `calls` — запись о звонке, остаётся навсегда: итог звонка уходит в ленту
-- системным сообщением, а сама строка нужна для разбора («почему не
-- соединилось») и для метрик. Обращаются к ней по первичному ключу.
--
-- `call_participants` — **только живые** звонки: строка участника появляется
-- вместе со звонком и **удаляется** в той же транзакции, что завершает его.
-- Отсюда три следствия, ради которых таблица отдельная:
--
--   * «один активный звонок на пользователя» — обычное ограничение
--     `UNIQUE (user_id)`, а не проверка в коде и не частичный индекс. Два
--     одновременных звонка одному человеку разрешает база: вторая вставка
--     падает на ограничении. Частичный индекс (`WHERE active`) был бы тем же
--     по смыслу, но `CREATE INDEX` без CONCURRENTLY запрещён ADR 0006 даже на
--     новой таблице, а ограничение внутри `CREATE TABLE` — нет;
--   * подметальщик ищет зависшие звонки по этой таблице, а не по всей
--     истории: в ней строки только живых звонков, их порядка числа звонящих
--     в данную секунду, и сканирование не растёт со временем;
--   * «мой текущий звонок» после перезагрузки вкладки — один запрос по
--     уникальному ключу.
--
-- BEGIN/COMMIT в файле не пишутся: транзакцией владеет db/migrate.sh.

CREATE TABLE calls (
    call_id            uuid PRIMARY KEY,
    conversation_id    uuid        NOT NULL REFERENCES conversations (conversation_id),
    caller_id          uuid        NOT NULL REFERENCES users (user_id),
    callee_id          uuid        NOT NULL REFERENCES users (user_id),
    kind               text        NOT NULL CHECK (kind IN ('audio', 'video')),
    state              text        NOT NULL DEFAULT 'ringing'
                                   CHECK (state IN ('ringing', 'accepted', 'active', 'ended')),
    -- Монотонный номер состояния: клиент отбрасывает событие с номером не
    -- больше уже виденного, поэтому запоздавшее `ringing` не оживит
    -- завершённый звонок (`CALL-007`).
    version            integer     NOT NULL DEFAULT 1,
    -- Номер последнего сигнала (offer/answer/ice) в звонке: выдаёт сервер, а не
    -- клиент, — клиенту нельзя верить в порядке, по которому отбрасывается
    -- устаревший `offer`.
    signal_seq         integer     NOT NULL DEFAULT 0,
    end_reason         text        CHECK (end_reason IN (
                           'completed', 'declined', 'missed', 'cancelled',
                           'busy', 'unavailable', 'failed')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    accepted_at        timestamptz,
    active_at          timestamptz,
    ended_at           timestamptz,
    -- Последнее подтверждение жизни от любой из сторон: тишина дольше окна —
    -- звонок завершает подметальщик.
    last_keepalive_at  timestamptz NOT NULL DEFAULT now(),
    -- Каким путём пошло медиа; сообщает клиент по `getStats()`, когда звонок
    -- стал активным. Источник доли релея (допущение оценки: 15%).
    connection_type    text        CHECK (connection_type IN ('direct', 'relay')),
    -- Итог звонка в ленте беседы: пишется в той же транзакции, что завершает
    -- звонок, поэтому повторное завершение его не дублирует (`CALL-009`).
    summary_message_id uuid        REFERENCES messages (message_id),
    CHECK (caller_id <> callee_id),
    CHECK ((state = 'ended') = (end_reason IS NOT NULL)),
    CHECK ((state = 'ended') = (ended_at IS NOT NULL))
);

COMMENT ON TABLE calls IS
    'Звонки один-на-один: запись остаётся навсегда, итог — системное сообщение в беседе';

CREATE TABLE call_participants (
    call_id  uuid NOT NULL REFERENCES calls (call_id) ON DELETE CASCADE,
    user_id  uuid NOT NULL REFERENCES users (user_id),
    role     text NOT NULL CHECK (role IN ('caller', 'callee')),
    PRIMARY KEY (call_id, user_id),
    -- Один живой звонок на человека. Не проверка в коде: два одновременных
    -- звонка разрешаются базой, а не гонкой.
    CONSTRAINT call_participants_one_live_call UNIQUE (user_id)
);

COMMENT ON TABLE call_participants IS
    'Участники только ЖИВЫХ звонков: строки удаляются при завершении звонка';
