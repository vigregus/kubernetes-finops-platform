/**
 * Буфер публикаций, пришедших **до** готовности хвоста.
 *
 * Соединение поднимается первым, снимок читается вторым — этот порядок и есть
 * причина существования буфера. При обратном порядке сообщение, опубликованное
 * между снимком и подпиской, теряется навсегда: у первой подписки
 * `wasRecovering === false`, восстанавливать ей нечего, и дыра обнаружилась бы
 * только по следующему сообщению — то есть могла бы не обнаружиться вовсе.
 *
 * **Буфер — оптимизация, а не носитель корректности.** Корректность держат
 * дедупликация по `message_id` и догрузка от применённой границы: что бы буфер
 * ни потерял, это вернёт `after_seq`. Поэтому переполнение здесь не роняет
 * приложение и не оставляет ленту неполной молча — оно **снимает с себя
 * работу** и требует догрузки.
 */
import type { ChatMessage } from "../../shared/lib/types";

/** Ёмкость на беседу. Переполнение — не ошибка, а признак «нужна догрузка». */
export const BUFFER_CAPACITY = 256;

export interface BootstrapBuffer {
  add(message: ChatMessage): void;
  /** Отдаёт накопленное и очищается; после переполнения отдаёт пустое. */
  drain(): readonly ChatMessage[];
  /**
   * Буфер переполнен: содержимое больше не накапливается, нужна догрузка.
   *
   * Признак читается вызывающим **после** применения снимка: вместе с пустым
   * `drain()` он и есть указание идти обычным путём — от применённой границы.
   */
  readonly overflowed: boolean;
  readonly size: number;
}

export function createBootstrapBuffer(capacity: number = BUFFER_CAPACITY): BootstrapBuffer {
  const buffered: ChatMessage[] = [];
  let overflowed = false;

  return {
    add(message) {
      if (overflowed) {
        // Дальше накапливать нечего: всё, что придёт, всё равно вернётся
        // догрузкой. Растить буфер сверх ёмкости значило бы держать в памяти
        // беседу целиком ради случая, который догрузка закрывает точнее.
        return;
      }
      if (buffered.length >= capacity) {
        overflowed = true;
        buffered.length = 0;
        return;
      }
      buffered.push(message);
    },
    drain() {
      if (overflowed) {
        return [];
      }
      return buffered.splice(0, buffered.length);
    },
    get overflowed() {
      return overflowed;
    },
    get size() {
      return buffered.length;
    },
  };
}
