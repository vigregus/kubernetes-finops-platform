// Детекторы обвязки: два источника фактов сводятся в один автомат, а время
// жизни соединения принадлежит эффекту.
//
// Собственных решений у обвязки нет — они в автомате, и там же проверяются.
// Здесь проверяется ровно то, чего у автомата быть не может: что браузерные
// события доходят, что SDK подключён и отключён по монтированию, и — главное
// — что признак `reconnectAllowed`, снятый терминальным разрывом, доходит до
// **решения о вызове** `connect()`, а не только до состояния.

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { givenFakeCentrifuge, givenTicketIssuer } from "../../test-support/centrifuge";
import { useRealtimeConnection } from "./useRealtimeConnection";

const CENTRIFUGO = "wss://rt.finops.local/connection/websocket";
const CHANNEL = "conversation:3f6b0d1e-0f4e-4a1f-9d2b-3ad0d1e6a111";

function givenHook() {
  const fake = givenFakeCentrifuge();
  const tickets = givenTicketIssuer();

  const view = renderHook(() =>
    useRealtimeConnection({
      centrifugoUrl: CENTRIFUGO,
      channel: CHANNEL,
      issueTicket: tickets.issueTicket,
      onPublication: () => {},
      createCentrifuge: fake.factory,
    }),
  );

  return { ...view, fake };
}

/** Браузерное событие — тот самый production-вход, а не подмена состояния. */
function browserEvent(type: "offline" | "online") {
  act(() => {
    window.dispatchEvent(new Event(type));
  });
}

describe("факты браузера доходят до автомата", () => {
  it("потеря сети переводит в DISCONNECTED", () => {
    const { result } = givenHook();

    expect(result.current.state).toBe("connecting");

    browserEvent("offline");

    expect(result.current.state).toBe("disconnected");
    expect(result.current.browserOnline).toBe(false);
  });

  it("возврат сети поднимает соединение заново", () => {
    const { result } = givenHook();

    browserEvent("offline");
    browserEvent("online");

    expect(result.current.state).toBe("connecting");
    expect(result.current.browserOnline).toBe(true);
  });
});

describe("факты SDK доходят до автомата", () => {
  it("соединение и разрыв отражаются в состоянии", () => {
    const { result, fake } = givenHook();

    act(() => {
      fake.clientHandlers.connected();
    });
    expect(result.current.state).toBe("connected");

    act(() => {
      fake.clientHandlers.disconnected({ code: 3001 });
    });
    expect(result.current.state).toBe("disconnected");
  });
});

describe("соединение живёт ровно столько, сколько компонент", () => {
  it("монтирование поднимает соединение, размонтирование опускает", () => {
    const { unmount, fake } = givenHook();

    expect(fake.calls.connect).toBe(1);
    expect(fake.calls.newSubscription).toBe(0);

    unmount();

    expect(fake.calls.disconnect).toBe(1);
  });
});

describe("факт, увиденный над SDK, доходит до автомата", () => {
  // Третий источник фактов, и он не SDK и не браузер: пропуск в `seq` видит
  // детектор слияния, а завершение догрузки — протокол `sync`. Обе новости
  // обязаны попасть в **тот же** автомат, иначе `SYNCING` перестанет быть
  // одним состоянием: `data-sync-reason` нечем заполнить, а выход из
  // синхронизации неоткуда взять.
  it("пропуск переводит в SYNCING и называет причину", () => {
    const { result, fake } = givenHook();

    act(() => {
      fake.clientHandlers.connected();
    });

    act(() => {
      result.current.notify({ type: "sequence-gap" });
    });

    expect(result.current.state).toBe("syncing");
    expect(result.current.syncReason).toBe("sequence-gap");
  });

  it("завершение догрузки возвращает CONNECTED и снимает причину", () => {
    const { result, fake } = givenHook();

    act(() => {
      fake.clientHandlers.connected();
      result.current.notify({ type: "sequence-gap" });
    });
    expect(result.current.state).toBe("syncing");

    act(() => {
      result.current.notify({ type: "sync-completed" });
    });

    expect(result.current.state).toBe("connected");
    expect(result.current.syncReason).toBeNull();
  });
});

describe("терминальный разрыв не возрождается браузерным online", () => {
  it("после 4501 возврат сети не зовёт connect и оставляет DISCONNECTED", () => {
    // Проверка B16 на уровне обвязки. Состояние само по себе уже проверено
    // автоматом; здесь важно, что признак доходит до **действия** — что мы не
    // дёргаем библиотеку впустую, заставляя её идти на connect-proxy с
    // мёртвым тикетом.
    const { result, fake } = givenHook();

    act(() => {
      fake.clientHandlers.connected();
      fake.clientHandlers.disconnected({ code: 4501 });
    });
    expect(result.current.reconnectAllowed).toBe(false);

    const attemptsBefore = fake.calls.connect;

    browserEvent("offline");
    browserEvent("online");

    expect(result.current.state).toBe("disconnected");
    expect(fake.calls.connect).toBe(attemptsBefore);
  });

  it("после обычного разрыва возврат сети возобновляет попытку", () => {
    const { result, fake } = givenHook();

    act(() => {
      fake.clientHandlers.connected();
      fake.clientHandlers.disconnected({ code: 3001 });
    });

    const attemptsBefore = fake.calls.connect;

    browserEvent("offline");
    browserEvent("online");

    expect(result.current.state).toBe("connecting");
    expect(fake.calls.connect).toBe(attemptsBefore + 1);
  });
});
