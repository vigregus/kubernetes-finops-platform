-- Стирание содержимого при сохранении конверта.
--
-- Начальная схема делала стирание невозможным: payload, display_name и
-- sender_id объявлены NOT NULL, то есть убрать содержимое, не удаляя строку,
-- нельзя. А удаление строки ломает нумерацию в беседе, ответы на удалённое,
-- состояние прочтения и восстановление при переподключении.
--
-- Миграция снимает эти ограничения и переносит проверку целостности туда,
-- где ей место: у живого сообщения содержимое есть, у стёртого — нет.
--
-- Файл 0001 при этом не правится. Уже применённая миграция неизменяема, и
-- исполнитель это проверяет по контрольной сумме: иначе схема в базе и схема
-- в git расходятся молча, а отладка идёт по коду, которого нет.

-- ---------------------------------------------------------------------------
-- Сообщения: конверт остаётся, содержимое уходит
-- ---------------------------------------------------------------------------

ALTER TABLE messages ALTER COLUMN payload DROP NOT NULL;

-- Отправитель стёрт — позиция в беседе цела. Собеседник видит «сообщение
-- удалено» на своём месте, а не дыру в нумерации.
ALTER TABLE messages ALTER COLUMN sender_id DROP NOT NULL;

-- Ограничение вместо NOT NULL: связь между состоянием и содержимым теперь
-- выражена явно, а не подразумевается.
--
--   живое      deleted_at IS NULL     → payload обязан быть
--   надгробие  deleted_at IS NOT NULL → payload обязан отсутствовать
--
-- Второе направление не менее важно первого: надгробие с сохранившимся
-- содержимым — это удаление, которого не произошло.
ALTER TABLE messages ADD CONSTRAINT messages_payload_matches_state CHECK (
    (deleted_at IS NULL     AND payload IS NOT NULL) OR
    (deleted_at IS NOT NULL AND payload IS NULL)
);

-- ---------------------------------------------------------------------------
-- Профиль: скелет остаётся
-- ---------------------------------------------------------------------------

ALTER TABLE users ALTER COLUMN display_name DROP NOT NULL;

-- Стёртая учётная запись и удалённая — разные вещи, и различать их надо.
-- deleted_at означает «ушёл», erased_at — «данные уничтожены по требованию».
-- Второе накладывает обязательства, первое нет.
ALTER TABLE users ADD COLUMN erased_at timestamptz;

ALTER TABLE users ADD CONSTRAINT users_erased_has_no_profile CHECK (
    erased_at IS NULL OR (display_name IS NULL AND email IS NULL)
);

-- ---------------------------------------------------------------------------
-- Вложения: объект удалён физически, строка помнит, что он был
-- ---------------------------------------------------------------------------

ALTER TABLE attachments ADD COLUMN erased_at timestamptz;

ALTER TABLE attachments
    DROP CONSTRAINT IF EXISTS attachments_state_check;

ALTER TABLE attachments ADD CONSTRAINT attachments_state_check CHECK (
    state IN ('pending', 'uploaded', 'attached', 'orphaned', 'erased')
);

-- ---------------------------------------------------------------------------
-- Журнал стираний
-- ---------------------------------------------------------------------------

-- Восстановление из резервной копии воскрешает всё, что было стёрто после
-- даты копии. Без этого журнала нарушение происходит ровно в момент аварии —
-- когда проверять его никто не будет.
--
-- Порядок восстановления: развернуть копию → повторно применить стирания
-- за период после её даты → и только потом пускать трафик.
CREATE TABLE erasure_log (
    erasure_id    uuid PRIMARY KEY,
    subject_type  text        NOT NULL CHECK (subject_type IN ('user', 'message', 'attachment')),
    subject_id    uuid        NOT NULL,
    requested_at  timestamptz NOT NULL,
    completed_at  timestamptz,
    -- Кто инициировал: сам пользователь, администратор, срок хранения.
    actor         text        NOT NULL,
    reason        text
    -- Содержимого здесь нет и быть не может: журнал, хранящий то, что
    -- он велел уничтожить, сам становится хранилищем стёртого.
);

CREATE INDEX erasure_log_replay_idx ON erasure_log (requested_at);
CREATE INDEX erasure_log_subject_idx ON erasure_log (subject_type, subject_id);
