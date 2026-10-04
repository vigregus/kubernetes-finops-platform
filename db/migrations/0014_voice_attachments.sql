-- Голосовые сообщения (G4, ATT-004): длительность и битрейт вложения.
--
-- Расширение без сужения (ADR 0006): две необязательные колонки. Старый код
-- их не читает и не пишет, новый — заполняет только у голосовых.

ALTER TABLE attachments ADD COLUMN duration_ms integer;
ALTER TABLE attachments ADD COLUMN bitrate_kbps integer;

ALTER TABLE attachments ADD CONSTRAINT attachments_voice_numbers_positive CHECK (
    (duration_ms IS NULL OR duration_ms > 0)
    AND (bitrate_kbps IS NULL OR bitrate_kbps > 0)
) NOT VALID;

ALTER TABLE attachments VALIDATE CONSTRAINT attachments_voice_numbers_positive;
