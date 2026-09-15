-- Одна прикладная сессия живёт сразу в нескольких вкладках и потому имеет
-- несколько соединений Centrifugo. Колонка sessions.realtime_client_id из
-- 0005 остаётся на время совместимой выкатки, но новый код её не использует.

CREATE TABLE realtime_connections (
    client_id   text PRIMARY KEY,
    session_id  uuid        NOT NULL REFERENCES sessions (session_id),
    user_id     uuid        NOT NULL REFERENCES users (user_id),
    connected_at timestamptz NOT NULL DEFAULT now(),
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, client_id)
);

COMMENT ON TABLE realtime_connections IS
    'Соединения Centrifugo: одна сессия может иметь несколько вкладок';
