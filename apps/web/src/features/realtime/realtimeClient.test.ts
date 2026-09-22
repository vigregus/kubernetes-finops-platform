// Детекторы адаптера: свежий тикет на каждую попытку, режим server-side
// подписки и изоляция зависимости.
//
// SDK подменяется **параметром фабрики**, а не `vi.mock` модуля: подмена
// модуля целиком подменила бы и решения самого адаптера, и тест проверял бы
// не тот код, который исполняется.

import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

import { givenFakeCentrifuge, givenTicketIssuer } from "../../test-support/centrifuge";
import type { ConnectionEvent } from "./connectionMachine";
import { createRealtimeClient } from "./realtimeClient";

const CENTRIFUGO = "wss://rt.finops.local/connection/websocket";
const CHANNEL = "conversation:3f6b0d1e-0f4e-4a1f-9d2b-3ad0d1e6a111";
/** Второй канал той же выдачи сервера: он подписывает клиента и на `user:{id}`. */
const OTHER_CHANNEL = "user:8c1f2a34-5b6d-4e7f-8a90-1b2c3d4e5f60";

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

describe("холодное рукопожатие укладывается в таймаут соединения", () => {
  // Детектор живого дефекта приёмки. SDK обрывает попытку соединения по
  // собственному `timeout` (`build/index.js:4444-4447`: `connectTimeout =
  // setTimeout(() => transport.close(), this._config.timeout)`, снимается
  // только в `onOpen`), а дефолт у закреплённого артефакта — 5000 мс
  // (`:3635`).
  //
  // Измерено на стенде: первое соединение браузера с `rt.finops.local`
  // открывается за 9124 и 9382 мс, второе и третье — за 19 и 20 мс. Это
  // резолвер macOS: домен `local` у него обслуживает отдельный резолвер
  // (`scutil --dns`: `domain: local`, `options: mdns`, `timeout: 5`), и запрос
  // AAAA ждёт его таймаут, потому что в `/etc/hosts` для `*.finops.local` есть
  // только IPv4-строки. Замер на одном имени: A — 9 мс, AAAA — 5015 мс,
  // полный `getaddrinfo` — 5010 мс, без кэша. Chromium платит два таких
  // резолва (9382, 10019, 10065, 10145, 9420 мс), `curl` — один (5.038 с).
  //
  // Оборванная попытка кэш резолвера не греет: с дефолтным таймаутом восемь
  // попыток подряд за 45 секунд умерли на 5001–5005 мс, тогда как с тридцатью
  // секундами тот же холодный контекст получил от сервера настоящий ответ по
  // протоколу. Поэтому проверка идёт не против дефолта, а против
  // **измеренной** холодной стоимости: таймаут, который вмещает её впритык,
  // зеленел бы и не работал.
  const COLD_HANDSHAKE_MS = 9_382;

  it("таймаут соединения вмещает холодное рукопожатие с запасом", () => {
    const { fake } = givenClient();

    // Двойник записывает опции так, как их увидел бы SDK. Снятая опция даёт
    // `undefined` — то есть дефолт 5000 мс, и это и есть возврат дефекта.
    // Тип проверяется отдельной строкой, чтобы этот возврат краснел
    // утверждением с именем дефекта, а не падением сравнения на `undefined`.
    expect(typeof fake.options.timeout).toBe("number");
    expect(fake.options.timeout).toBeGreaterThanOrEqual(2 * COLD_HANDSHAKE_MS);
  });
});

describe("адрес соединения приходит снаружи", () => {
  it("адрес соединения — публичный, из конфигурации", () => {
    const { fake } = givenClient();

    expect(fake.endpoint).toBe(CENTRIFUGO);
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

describe("server-side подписка: канал выдаёт сервер, клиент его не выбирает", () => {
  // Измерено у закреплённого артефакта `centrifuge` 5.7.4. Сервер подписывает
  // клиента сам — `channels` приходят в connect-data (`services/realtime.py:83`,
  // `api/main.py:593`), и «Клиент не выбирает канал сам»
  // (`channels.json:5`). В этом режиме SDK эмитит события **клиента** —
  // `client.on("subscribed" | "publication")` — с полем `ctx.channel`
  // (`build/types.d.ts:20-45`, обработка в `_processServerSubs`,
  // `build/index.js:5149`). Объекта подписки, созданного клиентом, здесь нет
  // вовсе, поэтому двойник и даёт эти два события верхним уровнем.

  it("подписки, заведённой клиентом, не заводится вовсе", () => {
    // Клиентская подписка — **другой** режим: в нём события приходят объекту
    // подписки, а сервер канал не выдаёт. Двойник считает её вызовы именно
    // затем, чтобы этот счётчик краснел числом, а не падением внутри двойника.
    const { client, fake } = givenClient();

    client.start();

    expect(fake.calls.newSubscription).toBe(0);
  });

  it("start поднимает только соединение", () => {
    const { client, fake } = givenClient();

    client.start();

    expect(fake.calls).toEqual({ connect: 1, disconnect: 0, newSubscription: 0 });
  });

  it("обе половины ответа на подписку передаются как есть", () => {
    // `recovered: false` при `wasRecovering: true` и при `wasRecovering: false` —
    // разные факты, и различить их может только автомат. Адаптер обязан
    // донести обе половины, ничего не решив по дороге.
    const { client, fake, events } = givenClient();
    client.start();

    const subscribed = (wasRecovering: boolean, recovered: boolean) =>
      fake.clientHandlers["subscribed"]?.({ channel: CHANNEL, wasRecovering, recovered });

    subscribed(false, false);
    subscribed(true, false);
    subscribed(true, true);

    expect(events).toEqual([
      { type: "subscription-subscribed", wasRecovering: false, recovered: false },
      { type: "subscription-subscribed", wasRecovering: true, recovered: false },
      { type: "subscription-subscribed", wasRecovering: true, recovered: true },
    ]);
  });

  it("чужой канал за подписку этой беседы не принимается", () => {
    // Тем же событием приходят все каналы выдачи, включая `user:{id}`
    // (`services/realtime.py:83`). Доложить о чужом автомату беседы значило бы
    // принять чужую подписку за подписку этой — и, например, уехать в SYNCING
    // по чужому `recovered: false`.
    const { client, fake, events } = givenClient();
    client.start();

    fake.clientHandlers["subscribed"]?.({
      channel: OTHER_CHANNEL,
      wasRecovering: true,
      recovered: false,
    });

    expect(events).toEqual([]);
  });

  it("ответ на подписку нашего канала называет расхождение", () => {
    const { client, fake, events } = givenClient();
    client.start();

    fake.clientHandlers["subscribed"]?.({
      channel: CHANNEL,
      wasRecovering: true,
      recovered: false,
    });

    expect(events).toEqual([
      { type: "subscription-subscribed", wasRecovering: true, recovered: false },
    ]);
  });

  it("публикация нашего канала доходит как есть, без разбора", () => {
    const { client, fake, publications } = givenClient();
    client.start();

    const payload = {
      type: "message.created",
      message_id: "m-1",
      seq: 42,
      sender_id: "u-1",
      payload: { text: "привет" },
    };
    fake.clientHandlers["publication"]?.({ channel: CHANNEL, data: payload });

    expect(publications).toEqual([payload]);
  });

  it("публикация чужого канала в ленту не попадает", () => {
    const { client, fake, publications } = givenClient();
    client.start();

    fake.clientHandlers["publication"]?.({ channel: OTHER_CHANNEL, data: { seq: 42 } });

    expect(publications).toEqual([]);
  });

  it("снятия подписки адаптер не слушает вовсе", () => {
    // Измерено: `unsubscribed` верхнего уровня **не несёт кода** —
    // `_handleUnsubscribe` выбрасывает `unsubscribe.code` для серверных подписок
    // (`build/index.js:5321-5326`), а доходит код только путём клиентской
    // подписки, которой здесь нет. Докладывать автомату нечего, поэтому
    // обработчика нет, и это проверяется наличием самого обработчика, а не
    // пустым списком событий: пустой список получился бы и от вызова
    // отсутствующего обработчика, то есть был бы тождеством, а не детектором.
    const { client, fake } = givenClient();

    client.start();

    expect(fake.clientHandlers["unsubscribed"]).toBeUndefined();
  });
});

describe("жизненный цикл", () => {
  it("start поднимает соединение, stop опускает", () => {
    const { client, fake } = givenClient();

    client.start();
    expect(fake.calls).toEqual({ connect: 1, disconnect: 0, newSubscription: 0 });

    client.stop();
    expect(fake.calls.disconnect).toBe(1);
  });

  it("connect возобновляет попытки, подписки при этом не появляется", () => {
    // Возврат браузера в сеть не должен ничего подписывать: сервер подписал
    // клиента сам, и выбора канала на клиенте нет вовсе.
    const { client, fake } = givenClient();

    client.start();
    client.connect();

    expect(fake.calls.connect).toBe(2);
    expect(fake.calls.newSubscription).toBe(0);
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
