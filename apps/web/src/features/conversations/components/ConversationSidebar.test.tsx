import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { VIEWER, conversationOf } from "../../../test-support/fixtures";
import { ConversationSidebar } from "./ConversationSidebar";

/**
 * Сайдбар — то место, где фикстуры были видны глазом, а не только в коде:
 * список приходил из `mock-data`, а над ним висели счётчик и поиск.
 *
 * Здесь проверяется не оформление, а три утверждения сразу: поиска нет,
 * кнопки без обработчика нет, счётчик не выдаёт себя за полное число.
 */
const CONVERSATIONS = [
  conversationOf({ id: "c1", name: "Anna Petrova" }),
  conversationOf({ id: "c2", name: "Marcus Chen" }),
];

function sidebar(props: Record<string, unknown> = {}) {
  return render(
    <ConversationSidebar
      conversations={CONVERSATIONS}
      activeConversationId={null}
      currentUser={VIEWER}
      onSelectConversation={() => {}}
      {...props}
    />,
  );
}

describe("список бесед", () => {
  it("поиска нет: он обещал бы поиск по беседам вообще", () => {
    // `SearchField` — работающий локальный фильтр по **загруженному** массиву,
    // а `GET /conversations` пагинирован, и пагинацию G3-005 намеренно не
    // реализует (B28). Поле фильтровало бы первую страницу, обещая поиск по
    // всем беседам, — тот же класс, что кнопка с `onClick={undefined}`.
    sidebar();

    expect(screen.queryByPlaceholderText(/search/i)).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("без обработчика кнопки новой беседы нет", () => {
    sidebar();

    expect(screen.queryByRole("button", { name: "New conversation" })).toBeNull();
  });

  it("с обработчиком кнопка новой беседы есть и работает", () => {
    const onNewConversation = vi.fn();
    sidebar({ onNewConversation });

    screen.getByRole("button", { name: "New conversation" }).click();
    expect(onNewConversation).toHaveBeenCalledTimes(1);
  });

  it("в счётчике нет слова Active: это число бесед, а не людей в сети", () => {
    // `{conversations.length} Active` читается как «в сети», хотя считает
    // беседы. Тот же дефект, что «Active now» в подвале и «Offline» в шапке:
    // интерфейс сообщает факт о присутствии, которого сервер не сообщал (B17).
    sidebar();

    expect(screen.queryByText(/active/i)).toBeNull();
  });

  it("число над списком подписано как число загруженных", () => {
    // Просто `2` запрещено не меньше, чем `2 Active`: `GET /conversations`
    // пагинирован, и число без подписи читается как «столько бесед всего».
    sidebar();

    expect(screen.getByText(/2 loaded/i)).toBeTruthy();
  });
});
