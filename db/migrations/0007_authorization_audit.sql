-- Долговечный след привилегированных решений авторизации.
-- Разрешение возвращается только после этой записи; если аудит недоступен,
-- административная операция считается несовершённой (AUTHZ-005).
CREATE TABLE authorization_audit (
    audit_id          uuid PRIMARY KEY,
    actor_user_id     uuid        REFERENCES users (user_id),
    actor_external_id text        NOT NULL,
    resource_kind     text        NOT NULL,
    resource_id       text        NOT NULL,
    action            text        NOT NULL,
    allowed           boolean     NOT NULL,
    reason            text,
    created_at        timestamptz NOT NULL DEFAULT now()
);
