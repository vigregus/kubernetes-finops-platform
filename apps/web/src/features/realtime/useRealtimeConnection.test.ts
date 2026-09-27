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
/** Второй канал той же выдачи: `user:{id}` (`services/realtime.py:83`). */
const USER_CHANNEL = "user:8c1f2a34-5b6d-4e7f-8a90-1b2c3d4e5f60";

function givenHook() {
  const fake = givenFakeCentrifuge();
  const tickets = givenTicketIssuer();

  const view = renderHook(() =>
    useRealtimeConnection({
      centrifugoUrl: CENTRIFUGO,
      channel: CHANNEL,
      userChannel: USER_CHANNEL,
      issueTicket: tickets.issueTicket,
      onPublication: () => {},
      onUserPublication: () => {},
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

describe("личный канал доходит до списка, не трогая соединение", () => {
  it("публикация личного канала зовёт свой обработчик", () => {
    const fake = givenFakeCentrifuge();
    const tickets = givenTicketIssuer();
    const seen: unknown[] = [];

    renderHook(() =>
      useRealtimeConnection({
        centrifugoUrl: CENTRIFUGO,
        channel: CHANNEL,
        userChannel: USER_CHANNEL,
        issueTicket: tickets.issueTicket,
        onPublication: () => {},
        onUserPublication: (payload) => seen.push(payload),
        createCentrifuge: fake.factory,
      }),
    );

    act(() => {
      fake.clientHandlers["publication"]?.({
        channel: USER_CHANNEL,
        data: { type: "unread.changed", conversation_id: "c-1", unread_count: 3 },
      });
    });

    expect(seen).toEqual([{ type: "unread.changed", conversation_id: "c-1", unread_count: 3 }]);
  });

  it("новый обработчик списка не пересоздаёт соединение", () => {
    // Обработчик читается из ссылки (`userPublicationRef`) — по той же причине,
    // что и обработчик ленты: соединение обязано жить дольше одного рендера.
    // Без ссылки смена обработчика (а сверка меняет его вместе с `currentUserId`)
    // рвала бы сокет и поднимала новый — то есть вкладка теряла бы как раз те
    // публикации, ради которых личный канал и впущен.
    const fake = givenFakeCentrifuge();
    const tickets = givenTicketIssuer();

    const view = renderHook(
      (props: { onUserPublication: (payload: unknown) => void }) =>
        useRealtimeConnection({
          centrifugoUrl: CENTRIFUGO,
          channel: CHANNEL,
          userChannel: USER_CHANNEL,
          issueTicket: tickets.issueTicket,
          onPublication: () => {},
          onUserPublication: props.onUserPublication,
          createCentrifuge: fake.factory,
        }),
      { initialProps: { onUserPublication: () => {} } },
    );

    view.rerender({ onUserPublication: () => {} });

    expect(fake.calls.connect).toBe(1);
    expect(fake.calls.disconnect).toBe(0);
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
