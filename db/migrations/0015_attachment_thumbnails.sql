-- Миниатюры изображений (G4): ключ производного объекта и размеры оригинала.
--
-- Расширение без сужения (ADR 0006): три необязательные колонки. Размеры
-- оригинала нужны клиенту, чтобы зарезервировать место в ленте до загрузки
-- картинки и не прыгать при её появлении; ключ — чтобы удалить миниатюру
-- вместе с вложением (производные данные стираются вместе с источником).

ALTER TABLE attachments ADD COLUMN thumbnail_key text;
ALTER TABLE attachments ADD COLUMN width integer;
ALTER TABLE attachments ADD COLUMN height integer;

ALTER TABLE attachments ADD CONSTRAINT attachments_image_dimensions_positive CHECK (
    (width IS NULL OR width > 0) AND (height IS NULL OR height > 0)
) NOT VALID;

ALTER TABLE attachments VALIDATE CONSTRAINT attachments_image_dimensions_positive;
