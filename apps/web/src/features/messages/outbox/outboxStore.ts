/**
 * Хранилище очереди офлайна — `IndexedDB`, и это выбор, а не умолчание.
 *
 * Сообщение, написанное без сети, обязано пережить перезагрузку вкладки: иначе
 * «отправлю, когда появится связь» превращается в «отправлю, если не закрою
 * страницу». `localStorage` для этого не годится по двум причинам, и обе
 * названы в `ADR 0005`: он синхронный (запись на диск тормозит кадр) и он
 * строковый (двоичное вложение туда не поместить), а главное — в него просится
 * удостоверение. Здесь удостоверений нет: перечень полей закрыт в
 * `StoredPendingMessage`, и хранилище принимает только его.
 *
 * Чего здесь **нет** и почему: `jsdom` не имеет `IndexedDB` вовсе, поэтому
 * модульные проверки этого файла не касаются — правила очереди вынесены в
 * `pendingMessages.ts` именно затем, чтобы их можно было проверить без
 * хранилища. Работа самого хранилища доказывается живой приёмкой: перезагрузка
 * страницы с неотправленным сообщением.
 */

import type { PendingMessage } from "../../../shared/lib/types";
import { fromStored, toStored, type StoredPendingMessage } from "./pendingMessages";

const DB_NAME = "messenger-outbox";
const DB_VERSION = 1;
const STORE_NAME = "pending";

export interface OutboxStore {
  put(pending: PendingMessage): Promise<void>;
  list(): Promise<PendingMessage[]>;
  remove(clientMessageId: string): Promise<void>;
  /**
   * Стирает очередь целиком — и названо это отдельно от `remove` намеренно.
   *
   * Причина стирания здесь человеческая: чужие черновики в общем браузере —
   * данные, которых человек после выхода из системы не ожидает увидеть.
   * Отдельное имя не даёт этому вызову затеряться среди точечных удалений и
   * делает поиск причины стирания однозначным.
   */
  clearForLogout(): Promise<void>;
}

/** Обёртка вокруг запроса `IndexedDB`: событийный API в обещание. */
function promised<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("запрос IndexedDB не выполнен"));
  });
}

/**
 * Открывает базу, создавая хранилище при первом обращении.
 *
 * `keyPath: "clientMessageId"` — не удобство: это тот же ключ, по которому
 * сервер узнаёт повтор, и второй ключ здесь завёл бы расхождение между «чем
 * клиент различает записи» и «чем сервер различает отправки».
 */
function openDatabase(factory: IDBFactory): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = factory.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: "clientMessageId" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("база очереди не открылась"));
  });
}

/**
 * Хранилище поверх `IndexedDB`. Фабрика приходит параметром ради проверяемости
 * и ради одного честного случая: в браузере, где `indexedDB` выключен
 * (приватное окно, запрет данных сайта), очередь не работает — и вызывающий
 * узнаёт об этом отказом, а не молчанием.
 */
export function createOutboxStore(
  factory: IDBFactory | undefined = globalThis.indexedDB,
): OutboxStore {
  async function withStore<T>(
    mode: IDBTransactionMode,
    run: (store: IDBObjectStore) => Promise<T> | T,
  ): Promise<T> {
    if (factory === undefined) {
      throw new Error("IndexedDB недоступен: очередь офлайна работать не будет");
    }
    const db = await openDatabase(factory);
    try {
      const transaction = db.transaction(STORE_NAME, mode);
      const result = await run(transaction.objectStore(STORE_NAME));
      // Ждём фиксации: без этого запись может не дойти до диска, а `list`,
      // прочитанный сразу после `put`, вернул бы пусто.
      await new Promise<void>((resolve, reject) => {
        transaction.oncomplete = () => resolve();
        transaction.onerror = () =>
          reject(transaction.error ?? new Error("транзакция очереди не завершилась"));
        transaction.onabort = () =>
          reject(transaction.error ?? new Error("транзакция очереди отменена"));
      });
      return result;
    } finally {
      db.close();
    }
  }

  return {
    async put(pending: PendingMessage): Promise<void> {
      await withStore("readwrite", (store) => promised(store.put(toStored(pending))));
    },

    async list(): Promise<PendingMessage[]> {
      const stored = await withStore("readonly", (store) =>
        promised(store.getAll() as IDBRequest<StoredPendingMessage[]>),
      );
      // Порядок хранилища — по ключу (`clientMessageId`), то есть случайный
      // для человека. Лента показывает записи по времени написания, и
      // восстановление, отдавшее их в порядке ключа, перемешало бы беседу.
      return stored.map(fromStored).sort((left, right) => left.createdAt - right.createdAt);
    },

    async remove(clientMessageId: string): Promise<void> {
      await withStore("readwrite", (store) => promised(store.delete(clientMessageId)));
    },

    async clearForLogout(): Promise<void> {
      await withStore("readwrite", (store) => promised(store.clear()));
    },
  };
}
