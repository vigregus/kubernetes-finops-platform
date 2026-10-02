-- Вложения: конвейер обработки (G4-001).
--
-- Схема G0 знала четыре состояния жизни файла до сообщения (`pending`,
-- `uploaded`, `attached`, `orphaned`) и пятое от стирания (`erased`). Конвейер
-- из `03-v1-scope.md` — `uploading → processing → ready`, плюс `rejected` и
-- `failed` — другой: он описывает не «где файл относительно сообщения», а
-- «можно ли ему доверять». Эти две оси сводятся в одну колонку так:
--
--   pending     клиент получил ссылку, объект ещё не подтверждён
--   processing  `complete` принят: идёт сверка типа и проверка сканером
--   ready       проверка пройдена, к сообщению ещё не прикреплён
--   rejected    тип не совпал с заявленным или сканер счёл файл опасным
--   failed      обработка не удалась по нашей вине (хранилище, сканер)
--   attached    прикреплён к сообщению (то, что раньше значило «готов»)
--   orphaned    так и не прикреплён за сутки, ждёт удаления
--   erased      объект стёрт физически (0002)
--
-- `uploaded` исчезает: оно значило «объект лёг», а теперь это `processing`.
-- Код G0–G3 в таблицу не писал вовсе (вложения не были включены), поэтому
-- переименование строк безопасно и в окне выкатки.
--
-- BEGIN/COMMIT в файле не пишутся: транзакцией владеет db/migrate.sh.

UPDATE attachments SET state = 'processing' WHERE state = 'uploaded';

ALTER TABLE attachments
    DROP CONSTRAINT IF EXISTS attachments_state_check;

-- NOT VALID, затем VALIDATE отдельным оператором (ADR 0006): тем же приёмом,
-- что `0009_delivery_receipts.sql`, и по той же причине.
ALTER TABLE attachments ADD CONSTRAINT attachments_state_check CHECK (
    state IN ('pending', 'processing', 'ready', 'rejected', 'failed',
              'attached', 'orphaned', 'erased')
) NOT VALID;

ALTER TABLE attachments VALIDATE CONSTRAINT attachments_state_check;

-- Результат обработки. Все колонки необязательны: у `pending` их быть не
-- может, и подставлять пустые значения значило бы утверждать то, чего
-- конвейер ещё не выяснил.
--
-- `detected_content_type` — то, что показала сигнатура начала файла, а
-- `content_type` остаётся заявленным клиентом. Расхождение и есть `ATT-007`,
-- и хранить обе величины нужно именно ради него: по одной видно «отклонён»,
-- по двум — почему.
ALTER TABLE attachments ADD COLUMN file_name text;
ALTER TABLE attachments ADD COLUMN detected_content_type text;
ALTER TABLE attachments ADD COLUMN processed_at timestamptz;
ALTER TABLE attachments ADD COLUMN rejection_reason text;

-- Аренда строки обработчиком — по образцу outbox (`0001_init.sql`): воркер
-- берёт `processing` с `FOR UPDATE SKIP LOCKED`, и упавший воркер отпускает
-- строку истечением срока, а не ручной чисткой.
ALTER TABLE attachments ADD COLUMN lease_owner text;
ALTER TABLE attachments ADD COLUMN lease_until timestamptz;
ALTER TABLE attachments ADD COLUMN attempts integer NOT NULL DEFAULT 0;

-- Прикреплённое вложение обязано знать сообщение, и обратное тоже: строка
-- с `message_id` в любом другом состоянии означала бы, что сообщение
-- ссылается на файл, которому нельзя доверять.
ALTER TABLE attachments ADD CONSTRAINT attachments_attached_has_message CHECK (
    (state = 'attached') = (message_id IS NOT NULL)
    OR state = 'erased'
) NOT VALID;

ALTER TABLE attachments VALIDATE CONSTRAINT attachments_attached_has_message;

-- Причина отказа нужна только у `rejected`/`failed`; у остальных она была бы
-- устаревшей записью о прошлой попытке.
ALTER TABLE attachments ADD CONSTRAINT attachments_reason_only_on_refusal CHECK (
    rejection_reason IS NULL OR state IN ('rejected', 'failed', 'erased')
) NOT VALID;

ALTER TABLE attachments VALIDATE CONSTRAINT attachments_reason_only_on_refusal;

-- CREATE INDEX здесь нет намеренно, по той же причине, что в 0010 и 0012:
-- CONCURRENTLY внутри `psql --single-transaction` не работает, а расширять
-- GRANDFATHERED значило бы обойти правило. Воркер читает строки в
-- `processing`, которых по построению единицы (файл проходит обработку
-- секунды). Уборщик (G4-005) ходит по `attachments_orphan_idx` из G0, чей
-- предикат остался `state IN ('pending', 'uploaded')`: `uploaded` теперь
-- пуст, `pending` — главный источник сирот. Неприкреплённые `ready` за
-- сутки — редкий случай (человек выбрал файл и передумал после обработки),
-- и читаются они обычным проходом по таблице, ограниченной сутками жизни.
