/**
 * Минимальная строка состояния соединения — не штатный запас.
 *
 * `ConnectionStateBanner` (проектный запас, `FORBIDDEN`) показывает состояние
 * крупно и с действиями, которых в объёме G3-006 нет: «messages still send» —
 * про отправку, а отправки из браузера здесь нет вовсе (B3). Подключать баннер
 * значило бы притащить ветку, которая никогда не сработает, и объявить её
 * проверенной. Здесь — ровно одно: состояние названо словами.
 *
 * Зачем строка вообще, если состояние и так видно по атрибуту. Затем, что
 * `03-v1-scope.md:196` запрещает показывать старое состояние как актуальное —
 * запрет адресован **человеку**, а не приёмке, и `SYNCING` обязан быть назван
 * тем, что он значит. При `syncing` строка говорит не «limited», не
 * «connected» и не молчит, а называет догрузку: пока она идёт, лента не полна.
 *
 * `role="status"` — живой регион, а не украшение: смена состояния происходит
 * без действия человека, и объявить её экранному диктору дешевле, чем объяснять
 * молчание интерфейса.
 */
import type { ConnectionState } from "../../../shared/lib/types";

/**
 * Слова состояний. Слова те же, что в `ConnectionState`, — приёмка читает
 * состояние по атрибуту, а не по строке, и расходиться им незачем.
 */
const LABEL: Readonly<Record<ConnectionState, string>> = {
  connected: "Connected",
  connecting: "Connecting…",
  disconnected: "No connection",
  degraded: "Limited service",
  syncing: "Catching up on missed messages…",
};

interface ConnectionStatusLineProps {
  state: ConnectionState;
}

export function ConnectionStatusLine({ state }: ConnectionStatusLineProps) {
  return (
    <p role="status" className="px-6 py-1 text-xs text-text-warm-secondary">
      {LABEL[state]}
    </p>
  );
}
