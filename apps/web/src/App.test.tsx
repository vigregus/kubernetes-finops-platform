import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ConversationListPageFromJSON, MeFromJSON } from "./api/generated";
import type { BootState } from "./api/client";
import type { SessionState } from "./features/auth/sessionState";
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

describe("экраны состояния загрузки", () => {
  it("первому посетителю говорят о входе, а не об истёкшей сессии", () => {
    render(<App session={sessionOf({ kind: "unauthenticated" })} onRetry={() => {}} />);

    expect(screen.getByRole("button", { name: /continue with vector id/i })).toBeTruthy();
    // Сессии не было — значит, и слова о ней быть не должно.
    expect(screen.queryByText(/session expired/i)).toBeNull();
  });

  it("после ready тот же 401 — это кончившаяся сессия", () => {
    render(<App session={sessionOf({ kind: "session-expired" })} onRetry={() => {}} />);

    expect(screen.getByText(/session expired/i)).toBeTruthy();
  });

  it("недоступный сервис не ведёт на вход, а предлагает повтор", () => {
    const onRetry = vi.fn();
    render(<App session={sessionOf({ kind: "transient-error", traceId: "trace-7" })} onRetry={onRetry} />);

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

  it("ready доводит данные до списка бесед и до подвала", () => {
    render(<App session={sessionOf(READY)} onRetry={() => {}} />);

    // Имя собеседника, а не зрителя: `participants` приходят вместе со
    // зрителем, и «первый в списке» показал бы Дэвиду его же имя (B9г).
    // Утверждение — о **шапке**, а не о списке: имя собеседника там ровно одно,
    // и подстановка зрителя прошла бы по списку незамеченной, а по шапке нет.
    expect(screen.getByRole("heading", { name: "Anna Petrova" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "David Miller" })).toBeNull();
    // Подвал — из `/me`, а не из фикстур.
    expect(screen.getAllByText("David Miller").length).toBeGreaterThan(0);
    // История не загружена: `last_message` есть, значит «No messages yet» —
    // утверждение, которого сервер не делал (B21).
    expect(screen.queryByText(/no messages yet/i)).toBeNull();
    expect(screen.getByText(/history isn't loaded yet/i)).toBeTruthy();
  });
});
