-- Начальная схема мессенджера.
--
-- Правка, ответ и удаление не входят в первую версию, но схема обязана
-- позволить их добавить без перелома: поля есть, пути к ним нет.
--
-- BEGIN и COMMIT здесь намеренно отсутствуют: транзакцией владеет
-- исполнитель (db/migrate.sh --single-transaction), и он же одним коммитом
-- записывает версию. Собственный COMMIT в файле разорвал бы эту связь -
-- схема применилась бы, а версия не записалась.

-- ---------------------------------------------------------------------------
-- Пользователи и устройства
-- ---------------------------------------------------------------------------

CREATE TABLE users (
    user_id         uuid PRIMARY KEY,
    -- Внешний идентификатор из Keycloak. Пароли и вход живут там,
    -- здесь - только профиль и связи.
    external_id     text        NOT NULL UNIQUE,
    display_name    text        NOT NULL,
    -- text, а не citext: расширением владеет оператор, и миграция,
    -- зависящая от порядка его применения, ломается в чистом окружении.
    -- Регистронезависимость даёт индекс по lower() ниже.
    email           text,
    email_verified  boolean     NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    -- Надгробие, а не DELETE: на пользователя ссылаются сообщения,
    -- и физическое удаление обрушило бы историю чужих бесед.
    deleted_at      timestamptz
);

-- Частичный уникальный индекс: адрес уникален среди живых, но освобождается
-- после удаления учётной записи.
CREATE UNIQUE INDEX users_email_live_uniq
    ON users (lower(email)) WHERE deleted_at IS NULL AND email IS NOT NULL;

-- Два открытых браузера создают ту же задачу, что ноутбук и телефон,
-- поэтому устройство - сущность первой версии, а не будущей мобильной.
CREATE TABLE devices (
    device_id           uuid PRIMARY KEY,
    user_id             uuid        NOT NULL REFERENCES users (user_id),
    user_agent          text,
    -- Подписка Web Push. Для веба это не APNs и не FCM - другой механизм,
    -- и NULL здесь означает «уведомлений на это устройство нет».
    push_subscription   jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX devices_user_idx ON devices (user_id);

CREATE TABLE sessions (
    session_id      uuid PRIMARY KEY,
    user_id         uuid        NOT NULL REFERENCES users (user_id),
    device_id       uuid        NOT NULL REFERENCES devices (device_id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    -- Отзыв по истечении и отзыв по выходу - разные вещи. Второй обязан
    -- срабатывать немедленно, поэтому он отдельная отметка, а не «дождаться
    -- expires_at».
    revoked_at      timestamptz,
    revoked_reason  text
);

CREATE INDEX sessions_user_live_idx
    ON sessions (user_id) WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------------------
-- Беседы
-- ---------------------------------------------------------------------------

CREATE TABLE conversations (
    conversation_id uuid PRIMARY KEY,
    type            text        NOT NULL CHECK (type IN ('direct', 'group')),
    -- Упорядоченная пара участников для бесед один-на-один: 'меньший:больший'.
    -- Это и есть решение гонки встречного создания (CONV-001) - вторая
    -- транзакция падает на уникальном индексе, а не создаёт вторую беседу.
    -- Полагаться на «сначала SELECT, потом INSERT» нельзя: между ними
    -- успевает вклиниться параллельный запрос.
    direct_key      text,
    -- Номер последнего выданного сообщения. Выдаётся под блокировкой строки
    -- беседы в той же транзакции, что и вставка сообщения.
    last_seq        bigint      NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CHECK ((type = 'direct') = (direct_key IS NOT NULL))
);

CREATE UNIQUE INDEX conversations_direct_key_uniq
    ON conversations (direct_key) WHERE direct_key IS NOT NULL;

CREATE TABLE conversation_members (
    conversation_id uuid        NOT NULL REFERENCES conversations (conversation_id),
    user_id         uuid        NOT NULL REFERENCES users (user_id),
    role            text        NOT NULL DEFAULT 'member'
                                CHECK (role IN ('member', 'admin')),
    joined_at       timestamptz NOT NULL DEFAULT now(),
    -- Исключение из беседы - тоже отметка: нужно знать, что человек был
    -- участником, иначе его прошлые сообщения окажутся от постороннего.
    left_at         timestamptz,
    PRIMARY KEY (conversation_id, user_id)
);

CREATE INDEX conversation_members_user_idx
    ON conversation_members (user_id) WHERE left_at IS NULL;

-- ---------------------------------------------------------------------------
-- Сообщения
-- ---------------------------------------------------------------------------

CREATE TABLE messages (
    message_id          uuid PRIMARY KEY,
    conversation_id     uuid        NOT NULL REFERENCES conversations (conversation_id),
    -- Порядок внутри беседы. Не время создания: часы разъезжаются,
    -- а два сообщения могут прийти в одну миллисекунду.
    conversation_seq    bigint      NOT NULL,
    sender_id           uuid        NOT NULL REFERENCES users (user_id),
    -- Идемпотентность: повтор после сбоя приходит с тем же значением,
    -- и уникальный индекс ниже превращает его в отказ вместо дубля.
    client_message_id   uuid        NOT NULL,
    type                text        NOT NULL
                                    CHECK (type IN ('text', 'image', 'file', 'voice', 'system')),
    payload             jsonb       NOT NULL,
    -- Поля для правки, ответа и удаления существуют с первого дня,
    -- хотя операций над ними в первой версии нет.
    reply_to_message_id uuid        REFERENCES messages (message_id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    edited_at           timestamptz,
    -- Удаление - надгробие: строка остаётся, содержимое стирается.
    -- Физический DELETE сломал бы нумерацию, ответы на удалённое,
    -- состояние прочтения и восстановление при переподключении.
    deleted_at          timestamptz,
    UNIQUE (conversation_id, conversation_seq)
);

CREATE UNIQUE INDEX messages_idempotency_uniq
    ON messages (conversation_id, sender_id, client_message_id);

-- Пагинация идёт по ключу: WHERE conversation_seq < $1 ORDER BY ... DESC.
-- OFFSET на миллионах сообщений деградирует линейно, причём именно
-- на старых беседах - то есть у самых лояльных пользователей.
CREATE INDEX messages_conversation_seq_idx
    ON messages (conversation_id, conversation_seq DESC);

CREATE TABLE attachments (
    attachment_id   uuid PRIMARY KEY,
    message_id      uuid        REFERENCES messages (message_id),
    uploader_id     uuid        NOT NULL REFERENCES users (user_id),
    -- Вложение загружается до того, как появится сообщение, поэтому
    -- у него свой жизненный цикл и своё состояние.
    state           text        NOT NULL DEFAULT 'pending'
                                CHECK (state IN ('pending', 'uploaded', 'attached', 'orphaned')),
    bucket          text        NOT NULL,
    object_key      text        NOT NULL,
    content_type    text        NOT NULL,
    size_bytes      bigint      NOT NULL CHECK (size_bytes >= 0),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bucket, object_key)
);

-- Загруженные, но так и не прикреплённые файлы - это оплаченное место
-- за ничто. Индекс нужен уборщику, который их находит.
CREATE INDEX attachments_orphan_idx
    ON attachments (created_at) WHERE state IN ('pending', 'uploaded');

-- ---------------------------------------------------------------------------
-- Прочтение, блокировки, жалобы
-- ---------------------------------------------------------------------------

CREATE TABLE read_states (
    conversation_id uuid        NOT NULL REFERENCES conversations (conversation_id),
    user_id         uuid        NOT NULL REFERENCES users (user_id),
    -- Только вперёд. Монотонность проверяется при записи: пришедшая
    -- с опозданием квитанция не должна откатывать счётчик назад.
    last_read_seq   bigint      NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, user_id)
);

CREATE TABLE blocks (
    blocker_id  uuid        NOT NULL REFERENCES users (user_id),
    blocked_id  uuid        NOT NULL REFERENCES users (user_id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (blocker_id, blocked_id),
    CHECK (blocker_id <> blocked_id)
);

CREATE TABLE reports (
    report_id       uuid PRIMARY KEY,
    reporter_id     uuid        NOT NULL REFERENCES users (user_id),
    message_id      uuid        REFERENCES messages (message_id),
    reported_user_id uuid       REFERENCES users (user_id),
    reason          text        NOT NULL,
    state           text        NOT NULL DEFAULT 'open'
                                CHECK (state IN ('open', 'reviewing', 'resolved', 'rejected')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,
    resolved_by     uuid        REFERENCES users (user_id),
    -- Жалоба обязана указывать на что-то: без этой проверки в таблицу
    -- попадёт запись, по которой модератору нечего смотреть.
    CHECK (message_id IS NOT NULL OR reported_user_id IS NOT NULL)
);

CREATE INDEX reports_open_idx ON reports (created_at) WHERE state = 'open';

-- ---------------------------------------------------------------------------
-- Outbox
-- ---------------------------------------------------------------------------

-- Запись в Kafka не входит в транзакцию базы. Событие кладётся сюда тем же
-- коммитом, что и сообщение, а отправляет его отдельный процесс - иначе
-- падение между коммитом и публикацией теряет событие навсегда.
CREATE TABLE outbox (
    id              bigserial PRIMARY KEY,
    aggregate_type  text        NOT NULL,
    aggregate_id    uuid        NOT NULL,
    event_type      text        NOT NULL,
    event_version   int         NOT NULL DEFAULT 1,
    -- Ключ партиционирования в Kafka. Для сообщений это conversation_id:
    -- так порядок внутри беседы сохраняется, потому что все её события
    -- попадают в одну партицию.
    partition_key   text        NOT NULL,
    -- Идемпотентность на стороне потребителя: повторная доставка
    -- отсеивается по этому значению.
    event_id        uuid        NOT NULL UNIQUE,
    payload         jsonb       NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    published_at    timestamptz,
    attempts        int         NOT NULL DEFAULT 0,
    last_error      text,
    -- Аренда: отправитель помечает пачку своим идентификатором на срок,
    -- потом отпускает. Транзакция при этом не держится открытой на время
    -- сетевого вызова - иначе блокировки строк живут столько же, сколько
    -- таймаут Kafka.
    lease_owner     text,
    lease_until     timestamptz
);

-- Очередь на отправку: неопубликованные, у которых аренда свободна
-- или истекла. Именно по этому индексу идёт FOR UPDATE SKIP LOCKED.
CREATE INDEX outbox_pending_idx
    ON outbox (created_at) WHERE published_at IS NULL;
