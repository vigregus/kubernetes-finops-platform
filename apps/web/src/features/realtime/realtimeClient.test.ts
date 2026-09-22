// Детекторы адаптера: свежий тикет на каждую попытку, разбор событий SDK
// и изоляция зависимости.
//
// SDK подменяется **параметром фабрики**, а не `vi.mock` модуля: подмена
// модуля целиком подменила бы и константу `UNRECOVERABLE_POSITION`, и тест
// проверял бы не тот код, который исполняется.

import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

import { givenFakeCentrifuge, givenTicketIssuer } from "../../test-support/centrifuge";
import type { ConnectionEvent } from "./connectionMachine";
import { createRealtimeClient } from "./realtimeClient";

const CENTRIFUGO = "wss://rt.finops.local/connection/websocket";
const CHANNEL = "conversation:3f6b0d1e-0f4e-4a1f-9d2b-3ad0d1e6a111";

/** Адаптер с двойником и записью всего, что он отдаёт наружу. */
function givenClient() {
  const events: ConnectionEvent[] = [];
  const publications: unknown[] = [];
  const fake = givenFakeCentrifuge();
  const tickets = givenTicketIssuer();

  const client = createRealtimeClient({
    centrifugoUrl: CENTRIFUGO,
    channel: CHANNEL,
    issueTicket: tickets.issueTicket,
    onEvent: (event) => events.push(event),
    onPublication: (payload) => publications.push(payload),
    createCentrifuge: fake.factory,
  });

  return { client, fake, tickets, events, publications };
}

describe("свежий тикет на каждую попытку соединения", () => {
  // Детектор B5/B15. Тикет живёт 120 секунд, а переподключение после окна
  // офлайна случается позже. Статическое `data` прошло бы все прочие
  // проверки этого файла и умерло бы ровно в сценарии приёмки.
  it("getData возвращает новый тикет при каждом вызове", async () => {
    const { fake } = givenClient();

    expect(typeof fake.getData).toBe("function");

    expect(await fake.getData!()).toEqual({ ticket: "ticket-1" });
    expect(await fake.getData!()).toEqual({ ticket: "ticket-2" });
  });
});

describe("адрес и канал приходят снаружи", () => {
  it("адрес соединения — публичный, из конфигурации", () => {
    const { fake } = givenClient();

    expect(fake.endpoint).toBe(CENTRIFUGO);
  });

  it("канал беседы назван по контракту", () => {
    const { client, fake } = givenClient();
    client.start();

    expect(fake.channel).toBe(CHANNEL);
  });
});

describe("факты клиента доходят до автомата", () => {
  it("connecting, connected и disconnected с кодом", () => {
    const { client, fake, events } = givenClient();
    client.start();

    fake.clientHandlers.connecting();
    fake.clientHandlers.connected();
    fake.clientHandlers.disconnected({ code: 3001 });

    expect(events).toEqual([
      { type: "sdk-connecting" },
      { type: "sdk-connected" },
      { type: "sdk-disconnected", code: 3001 },
    ]);
  });
});

describe("факты подписки доходят до автомата", () => {
  it("обе половины ответа на подписку передаются как есть", () => {
    // `recovered: false` при `wasRecovering: true` и при `wasRecovering: false` —
    // разные факты, и различить их может только автомат. Адаптер обязан
    // донести обе половины, ничего не решив по дороге.
    const { client, fake, events } = givenClient();
    client.start();

    fake.subscriptionHandlers.subscribed({ wasRecovering: false, recovered: false });
    fake.subscriptionHandlers.subscribed({ wasRecovering: true, recovered: false });
    fake.subscriptionHandlers.subscribed({ wasRecovering: true, recovered: true });

    expect(events).toEqual([
      { type: "subscription-subscribed", wasRecovering: false, recovered: false },
      { type: "subscription-subscribed", wasRecovering: true, recovered: false },
      { type: "subscription-subscribed", wasRecovering: true, recovered: true },
    ]);
  });

  it("ошибка позиции приходит подпиской и названа своим событием", () => {
    // Измерено у закреплённого артефакта: код 112 уходит путём
    // `_setUnsubscribed`, то есть событием `unsubscribed`, а не `disconnected`.
    const { client, fake, events } = givenClient();
    client.start();

    fake.subscriptionHandlers.unsubscribed({ code: 112 });

    expect(events).toEqual([{ type: "unrecoverable-position" }]);
  });

  it("прочие коды снятия подписки автомату не докладываются", () => {
    // Обычный разрыв приходит событием `disconnected` и имеет свой код.
    // Доложить о нём ещё и отсюда значило бы послать автомату два разных
    // факта об одном событии.
    const { client, fake, events } = givenClient();
    client.start();

    fake.subscriptionHandlers.unsubscribed({ code: 3001 });

    expect(events).toEqual([]);
  });

  it("публикация доходит как есть, без разбора", () => {
    const { client, fake, publications } = givenClient();
    client.start();

    const payload = {
      type: "message.created",
      message_id: "m-1",
      seq: 42,
      sender_id: "u-1",
      payload: { text: "привет" },
    };
    fake.subscriptionHandlers.publication({ data: payload });

    expect(publications).toEqual([payload]);
  });
});

describe("жизненный цикл", () => {
  it("start поднимает подписку и соединение, stop опускает соединение", () => {
    const { client, fake } = givenClient();

    client.start();
    expect(fake.calls).toEqual({ connect: 1, disconnect: 0, subscribe: 1 });

    client.stop();
    expect(fake.calls.disconnect).toBe(1);
  });

  it("connect возобновляет попытки, не пересоздавая подписку", () => {
    // Возврат браузера в сеть не должен заводить вторую подписку: канал
    // один, и вторая подписка на тот же канал — ошибка SDK, а не
    // переподключение.
    const { client, fake } = givenClient();

    client.start();
    client.connect();

    expect(fake.calls.connect).toBe(2);
    expect(fake.calls.subscribe).toBe(1);
  });
});

describe("SDK изолирован в одном модуле", () => {
  // Матрица F: «centrifuge импортирован ровно в одном модуле». Проверка
  // статическая и точная: не «не больше одного», а «ровно этот файл».
  // Иначе шов мог бы переехать в другой модуль и остаться единственным —
  // то есть проверка зеленела бы на переезде.
  const IMPORTING_MODULES = ["features/realtime/realtimeClient.ts"];

  function sourceFiles(directory: string): string[] {
    return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) {
        // `generated/` — чужой сгенерированный код; тесты и stories не модули
        // production-графа, и подмена SDK в них — это и есть их работа.
        return entry.name === "generated" ? [] : sourceFiles(path);
      }
      if (!/\.tsx?$/.test(entry.name)) return [];
      if (/\.(test|stories)\.tsx?$/.test(entry.name)) return [];
      return [path];
    });
  }

  it("ни один production-модуль, кроме адаптера, не импортирует centrifuge", () => {
    // `process.cwd()` — корень приложения (`apps/web`): его задаёт
    // `root` в `vite.config.ts`, и оттуда же виден `src`. От `import.meta.url`
    // пришлось отказаться: под vite он приходит не `file:`-схемой, и
    // `fileURLToPath` падает — то есть проверка краснела бы всегда, а
    // красный, который нельзя позеленить, детектором не является.
    const srcRoot = join(process.cwd(), "src");

    const importing = sourceFiles(srcRoot)
      .filter((path) => /from\s+"centrifuge"/.test(readFileSync(path, "utf8")))
      .map((path) => relative(srcRoot, path).split("\\").join("/"));

    expect(importing).toEqual(IMPORTING_MODULES);
  });
});
