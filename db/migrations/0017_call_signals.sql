-- Короткоживущее хранилище сигналов звонка (RES-004).
--
-- `Centrifugo.publish() == true` значит «принято Centrifugo», а не «получил
-- браузер»: у канала звонков нет истории, и сигнал (`offer`, `answer`, кандидаты),
-- отправленный в момент короткого обрыва WebSocket получателя, исчезал навсегда.
-- Сигнал теперь **сначала в базе**, потом в канал, и получатель после
-- переподключения забирает пропущенное: `GET /calls/{id}/signals?after=<seq>`.
--
-- Живёт столько, сколько надо для переподключения (`expires_at`, 60 с): это не
-- история переписки, а буфер установки соединения. Чистит его подметальщик.
--
-- Идемпотентность повтора — уникальность `(call_id, sender_id, signal_id)`:
-- повтор с тем же `signal_id` находит уже записанный сигнал и не заводит второй
-- номер. Несколько строк без `signal_id` допустимы (NULL в уникальности различны).
--
-- Отдельного индекса нет: ключ `(call_id, seq)` покрывает выборку, а таблица —
-- это строки последней минуты (и `CREATE INDEX` без CONCURRENTLY запрещён ADR 0006).
--
-- BEGIN/COMMIT в файле не пишутся: транзакцией владеет db/migrate.sh.

CREATE TABLE call_signals (
    call_id     uuid        NOT NULL REFERENCES calls (call_id) ON DELETE CASCADE,
    seq         integer     NOT NULL,
    sender_id   uuid        NOT NULL REFERENCES users (user_id),
    signal_id   text,
    body        jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    PRIMARY KEY (call_id, seq),
    CONSTRAINT call_signals_idempotency UNIQUE (call_id, sender_id, signal_id)
);

COMMENT ON TABLE call_signals IS
    'Сигналы звонка на последнюю минуту: повтор по signal_id и выдача пропущенного после обрыва';
