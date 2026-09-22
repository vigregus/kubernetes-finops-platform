import { webcrypto } from "node:crypto";

/**
 * Даёт jsdom то, чего у него нет: `crypto.subtle`.
 *
 * Замер: под jsdom 30.1.0 `crypto.getRandomValues` и `crypto.randomUUID` есть,
 * а `crypto.subtle` — `undefined`. Присваиванием его не подменить: свойство
 * только для чтения, и обычная запись падает на `TypeError: Cannot assign to
 * read only property 'subtle' of object '#<Crypto>'`.
 *
 * Живёт отдельным модулем, а не `setupFiles`, по одной причине: подмена должна
 * быть видна там, где она нужна (PKCE и всё, что считает S256), и не выглядеть
 * настройкой проекта. В браузере `crypto.subtle` настоящий, и этот файл в
 * собранное приложение не попадает — его импортируют только тесты.
 */
export function installSubtleForJsdom(): void {
  if (globalThis.crypto.subtle !== undefined) {
    return;
  }
  Object.defineProperty(globalThis.crypto, "subtle", {
    value: webcrypto.subtle,
    configurable: true,
  });
}
