// Детекторы автомата соединения.
//
// Прогон без сети и без SDK — в этом и смысл разделения: решения по фактам
// проверяются числом, а не наблюдением за живым соединением. Каждый блок ниже
// назван по дефекту, который он ловит, а не по методу, которым проверяет.

import { describe, expect, it } from "vitest";

import type { ConnectionMachineState } from "./connectionMachine";
import { initialConnectionState, transition } from "./connectionMachine";
import type { ConnectionEvent } from "./connectionMachine";

/** Соединение поднято и подтверждено — исходная точка для проверок разрыва. */
function connected(): ConnectionMachineState {
  return transition(initialConnectionState(true), { type: "sdk-connected" });
}

/** Разрыв названным кодом при живом браузере. */
function afterDisconnect(code: number, from = connected()): ConnectionMachineState {
  return transition(from, { type: "sdk-disconnected", code });
}

/**
 * Все входы автомата — для перебора, который доказывает недостижимость
 * `degraded`. Перечислены руками и намеренно: событие, забытое здесь, выпало бы
 * из перебора, и «перехода в `degraded` нет» держалось бы на неполном списке.
 */
const ALL_EVENTS: readonly ConnectionEvent[] = [
  { type: "browser-offline" },
  { type: "browser-online" },
  { type: "sdk-connecting" },
  { type: "sdk-connected" },
  { type: "sdk-disconnected", code: 2 },
  { type: "sdk-disconnected", code: 3001 },
  { type: "sdk-disconnected", code: 3500 },
  { type: "sdk-disconnected", code: 4501 },
  { type: "subscription-subscribed", wasRecovering: false, recovered: false },
  { type: "subscription-subscribed", wasRecovering: true, recovered: false },
  { type: "subscription-subscribed", wasRecovering: true, recovered: true },
  { type: "unrecoverable-position" },
  { type: "sequence-gap" },
  { type: "sync-completed" },
];

describe("переходы по фактам SDK", () => {
  it("начало — CONNECTING: соединения ещё нет", () => {
    expect(initialConnectionState(true).state).toBe("connecting");
  });

  it("sdk-connecting переводит в CONNECTING", () => {
    expect(transition(connected(), { type: "sdk-connecting" }).state).toBe("connecting");
  });

  it("sdk-connected переводит в CONNECTED", () => {
    expect(connected().state).toBe("connected");
  });

  it("разрыв переводит в DISCONNECTED", () => {
    expect(afterDisconnect(3001).state).toBe("disconnected");
  });

  it("после выхода из syncing причина снята: она живёт только при syncing", () => {
    const syncing = transition(connected(), {
      type: "subscription-subscribed",
      wasRecovering: true,
      recovered: false,
    });
    expect(syncing.state).toBe("syncing");

    const done = transition(syncing, { type: "sync-completed" });
    expect(done.state).toBe("connected");
    expect(done.syncReason).toBeNull();
  });
});

describe("детектор расхождения: защита wasRecovering", () => {
  // Главный детектор гейта. `recovered` равно `false` и на первой подписке —
  // тогда, когда восстанавливать было нечего. Без проверки `wasRecovering`
  // каждая загрузка страницы уезжала бы в SYNCING, то есть интерфейс сообщал бы
  // о расхождении, которого нет, и догружал бы историю на ровном месте.
  it("первая подписка (wasRecovering: false) остаётся CONNECTED, а не уходит в SYNCING", () => {
    const state = transition(connected(), {
      type: "subscription-subscribed",
      wasRecovering: false,
      recovered: false,
    });

    expect(state.state).toBe("connected");
    expect(state.syncReason).toBeNull();
  });

  it("wasRecovering: true, recovered: false — расхождение, SYNCING с причиной recovery-miss", () => {
    const state = transition(connected(), {
      type: "subscription-subscribed",
      wasRecovering: true,
      recovered: false,
    });

    expect(state.state).toBe("syncing");
    expect(state.syncReason).toBe("recovery-miss");
  });

  it("успешное восстановление (recovered: true) — расхождения нет", () => {
    const state = transition(connected(), {
      type: "subscription-subscribed",
      wasRecovering: true,
      recovered: true,
    });

    expect(state.state).toBe("connected");
  });

  it("sdk-connected не выводит из SYNCING: сокет поднят, но сходимость ещё не доказана", () => {
    const syncing = transition(connected(), {
      type: "subscription-subscribed",
      wasRecovering: true,
      recovered: false,
    });

    const still = transition(syncing, { type: "sdk-connected" });
    expect(still.state).toBe("syncing");
    expect(still.syncReason).toBe("recovery-miss");
  });
});

describe("второй путь к той же починке — ошибка позиции", () => {
  // Измерено у закреплённого артефакта: ошибка недостижимой позиции приходит
  // событием подписки (`unsubscribed`, ctx.code === 112), а не разрывом.
  // Поэтому у неё **своё** событие автомата: свести её к обычному разрыву
  // значило бы увести клиента в переподключение без догрузки.
  it("ведёт в тот же SYNCING, с собственной причиной", () => {
    const state = transition(connected(), { type: "unrecoverable-position" });

    expect(state.state).toBe("syncing");
    expect(state.syncReason).toBe("unrecoverable-position");
  });

  it("причина отличима от пропуска в seq — иначе три случая в разметке неразличимы", () => {
    const gap = transition(connected(), { type: "sequence-gap" });

    expect(gap.state).toBe("syncing");
    expect(gap.syncReason).toBe("sequence-gap");
  });
});

describe("терминальный разрыв и признак reconnectAllowed", () => {
  it("4501 снимает признак: попытки прекращены", () => {
    expect(afterDisconnect(4501).reconnectAllowed).toBe(false);
  });

  it("3001 признак не снимает: это не терминальный разрыв", () => {
    // Измерено: `_handleDisconnect` оставляет `reconnect = true` для 3001 —
    // SDK переподключается сам, и запрещать ему нечего.
    expect(afterDisconnect(3001).reconnectAllowed).toBe(true);
  });

  it("connected → 4501 → offline → online заканчивается DISCONNECTED", () => {
    // Последовательность, ради которой признак ортогонален офлайну. Без него
    // браузерное `online` возобновило бы попытки с мёртвым тикетом, и интерфейс
    // обещал бы соединение, которого не будет.
    let state = afterDisconnect(4501);
    state = transition(state, { type: "browser-offline" });
    state = transition(state, { type: "browser-online" });

    expect(state.state).toBe("disconnected");
    expect(state.reconnectAllowed).toBe(false);
  });

  it("online при живой сессии поднимает соединение заново", () => {
    let state = afterDisconnect(3001);
    state = transition(state, { type: "browser-offline" });
    state = transition(state, { type: "browser-online" });

    expect(state.state).toBe("connecting");
  });
});

describe("офлайн как производственный вход", () => {
  it("offline переводит в DISCONNECTED", () => {
    expect(transition(connected(), { type: "browser-offline" }).state).toBe("disconnected");
  });

  it("пока офлайн, факт сокета не выводит из DISCONNECTED", () => {
    // Факт браузера старше факта сокета: SDK, узнав о разрыве, ещё шлёт
    // `connected`, и поверить ему значило бы показать связь, которой нет.
    const offline = transition(connected(), { type: "browser-offline" });

    for (const event of [
      { type: "sdk-connected" },
      { type: "sdk-connecting" },
      { type: "sdk-disconnected", code: 3001 },
      { type: "subscription-subscribed", wasRecovering: false, recovered: false },
      { type: "unrecoverable-position" },
      { type: "sequence-gap" },
      { type: "sync-completed" },
    ] satisfies ConnectionEvent[]) {
      expect(transition(offline, event).state).toBe("disconnected");
    }
  });

  it("терминальный код, пришедший из-за офлайна, всё равно снимает признак", () => {
    const offline = transition(connected(), { type: "browser-offline" });
    const state = transition(offline, { type: "sdk-disconnected", code: 4501 });

    expect(state.reconnectAllowed).toBe(false);
    expect(transition(state, { type: "browser-online" }).state).toBe("disconnected");
  });
});

describe("DEGRADED недостижим", () => {
  // Критерий ревью B1, исполненный, а не заявленный: у `degraded` в объёме
  // G3-006 нет достижимого production-входа, и состояние, в которое нельзя
  // попасть, не может быть объявлено проверенным. Тест перебирает все
  // состояния и все входы, а не проверяет «мы туда не заходим» на глазок.
  it("ни один переход во всех состояниях и на всех входах не даёт degraded", () => {
    const start = initialConnectionState(true);
    const states: ConnectionMachineState[] = [
      start,
      transition(start, { type: "sdk-connected" }),
      transition(start, { type: "browser-offline" }),
      transition(start, { type: "sdk-disconnected", code: 4501 }),
      transition(start, {
        type: "subscription-subscribed",
        wasRecovering: true,
        recovered: false,
      }),
    ];
    // Все **достижимые** состояния представлены, и их ровно четыре: пятое,
    // `degraded`, и есть предмет проверки. Число здесь стоит затем, чтобы
    // набор стартовых состояний не сузился молча — перебор из двух состояний
    // доказывал бы недостижимость `degraded` ровно в той же мере.
    expect(new Set(states.map((s) => s.state)).size).toBe(4);

    for (const from of states) {
      for (const event of ALL_EVENTS) {
        expect(transition(from, event).state).not.toBe("degraded");
      }
    }
  });
});
