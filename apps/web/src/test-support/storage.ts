/**
 * Даёт jsdom то, чего у него не осталось: `localStorage`.
 *
 * Замер, а не догадка. Под Node 24 `globalThis.localStorage` — это **его**
 * свойство-доступ: геттер, возвращающий `undefined`, пока процессу не передан
 * `--localstorage-file` (об этом же предупреждает сам Node:
 * `ExperimentalWarning: localStorage is not available because
 * --localstorage-file was not provided`). `populateGlobal` в vitest кладёт
 * окружение поверх `globalThis` и jsdom-овский `localStorage` не перезаписывает
 * — свойство уже занято. Доступ при этом `configurable: true`, поэтому
 * переопределение возможно, и оно здесь.
 *
 * `sessionStorage` подменять не нужно: Node его не трогает, и до теста доезжает
 * настоящий jsdom-овский — тесты `session.test.ts` работают на нём.
 *
 * Реализация повторяет `Storage` целиком, включая неочевидное свойство
 * браузера: **ключи выставляются собственными перечисляемыми свойствами**.
 * Поэтому `Object.keys(localStorage)` показывает именно ключи, и проверка
 * «в хранилище ровно один ключ» остаётся буквальной, а `length`, `key()`,
 * `getItem` и прочее живут на прототипе и в ключи не попадают — как в браузере.
 */
class MemoryStorage implements Storage {
  readonly #items = new Map<string, string>();

  get length(): number {
    return this.#items.size;
  }

  key(index: number): string | null {
    return [...this.#items.keys()][index] ?? null;
  }

  getItem(key: string): string | null {
    return this.#items.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.#items.set(key, value);
    Object.defineProperty(this, key, {
      value,
      writable: true,
      enumerable: true,
      configurable: true,
    });
  }

  removeItem(key: string): void {
    this.#items.delete(key);
    Reflect.deleteProperty(this, key);
  }

  clear(): void {
    for (const key of [...this.#items.keys()]) {
      Reflect.deleteProperty(this, key);
    }
    this.#items.clear();
  }
}

/** Ставит `localStorage`, если его нет. В браузере не делает ничего. */
export function installStorageForJsdom(): void {
  if (globalThis.localStorage !== undefined && globalThis.localStorage !== null) {
    return;
  }
  Object.defineProperty(globalThis, "localStorage", {
    value: new MemoryStorage(),
    configurable: true,
  });
}
