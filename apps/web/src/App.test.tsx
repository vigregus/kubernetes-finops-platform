import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  ConversationListPageFromJSON,
  ListMessages200ResponseFromJSON,
  MeFromJSON,
  type ListMessages200Response,
} from "./api/generated";
import type { BootState } from "./api/client";
import type { SessionState } from "./features/auth/sessionState";
import type { HistoryApi } from "./features/messages/history";
import { givenFakeCentrifuge } from "./test-support/centrifuge";
import { App } from "./App";

/**
 * `App` — место, где четыре состояния загрузки становятся четырьмя разными
 * экранами, и где разница между ними перестаёт быть словами.
 *
 * Проверяются обе половины развилки `401`: у первого посетителя сессии не было
 * вовсе, и «истекла» ему говорить нечего; а после `ready` тот же `401` —
 * действительно кончившаяся сессия. Слить их — ровно то, от чего предостерегает
 * `_login_failure` (`api/main.py:307-317`).
 *
 * `503` при этом не ведёт никуда: единственное действие — повтор.
 */

/** Состояние, каким его отдаёт `client.bootstrap`. Хранилища здесь нет — есть ответ. */
function sessionOf(state: BootState): SessionState {
  return {
    getSnapshot: () => state,
    subscribe: () => () => {},
    set: () => state,
    bootstrap: async () => state,
  };
}

/**
 * `ready` собирается через конвертеры — по проводу, в snake_case.
 *
 * Не литералами: подмена регистра дала бы `display_name` в никуда, и тест
 * проверял бы собственную ошибку вместо адаптера.
 */
const READY: BootState = {
  kind: "ready",
  account: MeFromJSON({
    user_id: "user-viewer",
    display_name: "David Miller",
    email: "david.miller@example.com",
    email_verified: true,
    capabilities: ["read", "send_message"],
  }),
  conversations: ConversationListPageFromJSON({
    items: [
      {
        conversation_id: "c1",
        type: "direct",
        created_at: "2026-09-01T00:00:00Z",
        participants: [
          { user_id: "user-viewer", display_name: "David Miller" },
          { user_id: "user-anna", display_name: "Anna Petrova" },
        ],
        last_message: {
          message_id: "m1",
          conversation_id: "c1",
          seq: 1,
          sender_id: "user-anna",
          type: "text",
          created_at: new Date().toISOString(),
          payload: { text: "Hello" },
        },
      },
    ],
    next_before_activity_at: null,
    next_before_conversation_id: null,
  }),
};

/**
 * История приходит **своим** текстом, отличным от `Hello` в превью списка.
 *
 * Не придирка: `adaptConversations` несёт `last_message.payload.text` в превью
 * сайдбара, и одинаковая строка в превью и в ленте дала бы `getByText` два
 * элемента — то есть тест краснел бы на собственном совпадении, а не на
 * доехавшей истории.
 */
const HISTORY: ListMessages200Response = ListMessages200ResponseFromJSON({
  items: [
    {
      message_id: "m-history",
      conversation_id: "c1",
      seq: 1,
      sender_id: "user-anna",
      type: "text",
      created_at: "2026-09-01T00:00:00Z",
      payload: { text: "History reached the panel" },
    },
  ],
  has_more: false,
  next_after_seq: null,
  sync_to_seq: null,
});

const historyApi: HistoryApi = { listMessages: async () => HISTORY };

/**
 * Пропсы, которых у `App` не было до этого гейта, собираются **на каждый
 * вызов**, а не один раз: `createCentrifuge` записывает вызовы в свой объект, и
 * общая фабрика на два теста смешала бы их наблюдения.
 *
 * Фабрика соединений здесь не для симметрии: ветка `ready` монтирует настоящую
 * панель, а та поднимает соединение. Без шва в jsdom поехал бы настоящий
 * `centrifuge` (`App.tsx`, доккомментарий `createCentrifuge`).
 */
function shell(state: BootState, api: HistoryApi = historyApi) {
  return {
    session: sessionOf(state),
    onRetry: () => {},
    historyApi: api,
    // Сверка списка (`D11`) — операция той же природы, что `onRetry`, и живёт
    // в `main.tsx`; здесь она должна быть, но звать её эти тесты не обязаны.
    refreshConversations: async () => ({
      items: [],
      nextBeforeActivityAt: null,
      nextBeforeConversationId: null,
    }),
    // Квитанция — операция той же природы. Тесты экранов загрузки её не
    // дёргают: до ветки `ready` панели нет вовсе, а та, что монтируется в
    // последнем наборе, до отправки не доходит — строки ленты не пересекаются.
    sendReceipts: async () => ({}),
    readCentrifugoUrl: () => "wss://rt.example.test/connection/websocket",
    issueTicket: async () => "ticket-for-the-hunt",
    createCentrifuge: givenFakeCentrifuge().factory,
  };
}

describe("экраны состояния загрузки", () => {
  it("первому посетителю говорят о входе, а не об истёкшей сессии", () => {
    render(<App {...shell({ kind: "unauthenticated" })} />);

    expect(screen.getByRole("button", { name: /continue with vector id/i })).toBeTruthy();
    // Сессии не было — значит, и слова о ней быть не должно.
    expect(screen.queryByText(/session expired/i)).toBeNull();
  });

  it("после ready тот же 401 — это кончившаяся сессия", () => {
    render(<App {...shell({ kind: "session-expired" })} />);

    expect(screen.getByText(/session expired/i)).toBeTruthy();
  });

  it("недоступный сервис не ведёт на вход, а предлагает повтор", () => {
    const onRetry = vi.fn();
    render(<App {...shell({ kind: "transient-error", traceId: "trace-7" })} onRetry={onRetry} />);

    // Ни кнопки входа, ни слова об истёкшей сессии: вход в этот момент не
    // работает, и отправлять человека туда значило бы выдать недоступность
    // сервиса за его действие.
    expect(screen.queryByRole("button", { name: /continue with vector id/i })).toBeNull();
    expect(screen.queryByText(/session expired/i)).toBeNull();
    // Трасса — единственное, что человек может назвать при разборе.
    expect(screen.getByText(/trace-7/)).toBeTruthy();

    screen.getByRole("button", { name: /try again/i }).click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("ready доводит данные до списка бесед, подвала и ленты", async () => {
    render(<App {...shell(READY)} />);

    // Имя собеседника, а не зрителя: `participants` приходят вместе со
    // зрителем, и «первый в списке» показал бы Дэвиду его же имя (B9г).
    // Утверждение — о **шапке**, а не о списке: имя собеседника там ровно одно,
    // и подстановка зрителя прошла бы по списку незамеченной, а по шапке нет.
    expect(screen.getByRole("heading", { name: "Anna Petrova" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "David Miller" })).toBeNull();
    // Подвал — из `/me`, а не из фикстур.
    expect(screen.getAllByText("David Miller").length).toBeGreaterThan(0);

    // А это — та самая правка, ради которой гейт и делался. До неё здесь
    // стояло обратное утверждение: «No messages yet» отсутствует, а
    // «history isn't loaded yet» присутствует, — то есть панель
    // **признавалась**, что историю не грузит. Теперь `App` доводит историю до
    // ленты, и это видно по доехавшему тексту, а не по отсутствию заглушки.
    expect(await screen.findByText("History reached the panel")).toBeTruthy();
    expect(screen.queryByText(/history isn't loaded yet/i)).toBeNull();
    // Граница пришла **из ответа истории**, а не из `last_message` списка: она
    // читается по `max(seq)` страницы, и `1` здесь — её значение, а не
    // совпадение с номером последнего сообщения в списке бесед.
    expect(document.querySelector('[data-applied-through-seq="1"]')).toBeTruthy();
    expect(document.querySelector('[data-message-id="m-history"]')).toBeTruthy();
    expect(document.querySelector('[data-message-seq="1"]')).toBeTruthy();
  });

  it("адрес соединения не читается, пока панели нет", () => {
    const read = vi.fn(() => "wss://rt.example.test/connection/websocket");
    render(<App {...shell({ kind: "unauthenticated" })} readCentrifugoUrl={read} />);

    // Ленивость — не оптимизация, а разница между «страница не открылась» и
    // «отказ при входе». `loadRuntimeConfig()` бросает на отсутствующей
    // конфигурации; прочитанный заранее (в `App` или при загрузке модуля по
    // `main.tsx`), он уронил бы показ `LoginPage` целиком — то есть человек
    // увидел бы белый экран вместо просьбы войти, и отказ не назвал бы то, что
    // он делал.
    expect(screen.getByRole("button", { name: /continue with vector id/i })).toBeTruthy();
    expect(read).not.toHaveBeenCalled();
  });

  it("на ready адрес читается один раз на снимок, а не на рендер", async () => {
    const read = vi.fn(() => "wss://rt.example.test/connection/websocket");
    const { rerender } = render(<App {...shell(READY)} readCentrifugoUrl={read} />);
    await screen.findByText("History reached the panel");

    rerender(<App {...shell(READY)} readCentrifugoUrl={read} />);
    await act(async () => {});

    // Ровно один: адрес — константа окружения, и перечитывать его на каждый
    // рендер значило бы спрашивать одно и то же по многу раз за сессию, а при
    // ошибке конфигурации — падать на рендере, который к соединению отношения
    // не имеет.
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("повторный рендер не перечитывает хвост", async () => {
    const listMessages = vi.fn(async () => HISTORY);
    const api: HistoryApi = { listMessages };

    const { rerender } = render(<App {...shell(READY, api)} />);
    await screen.findByText("History reached the panel");
    const reads = listMessages.mock.calls.length;

    rerender(<App {...shell(READY, api)} />);
    // Даём эффектам дойти до конца: без этого «не перечитал» и «ещё не успел»
    // в тесте неотличимы.
    await act(async () => {});

    expect(reads).toBeGreaterThan(0);
    expect(listMessages.mock.calls.length).toBe(reads);
  });
});
