-- 0007 успела примениться в локальном окружении до добавления SET NULL.
-- Аудит должен переживать удаление профиля: внешний идентификатор остаётся,
-- а ссылка на уже удалённую внутреннюю запись обнуляется.
ALTER TABLE authorization_audit
    DROP CONSTRAINT authorization_audit_actor_user_id_fkey;

ALTER TABLE authorization_audit
    ADD CONSTRAINT authorization_audit_actor_user_id_fkey
    FOREIGN KEY (actor_user_id) REFERENCES users (user_id) ON DELETE SET NULL;
