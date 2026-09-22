import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { conversationOf } from "../../../test-support/fixtures";
import { ConversationListItem } from "./ConversationListItem";

/**
 * Строка списка — то, по чему приёмка среза 7 опознаёт беседу.
 *
 * Опознание по имени не годится: имя — строка, которую беседа может совпасть с
 * другой беседой, и тогда локатор нашёл бы не ту. `id` — сущность модели, но
 * `key` в разметку не попадает, поэтому идентификатор обязан быть в DOM
 * отдельным атрибутом.
 */
describe("строка списка бесед", () => {
  it("несёт идентификатор беседы в разметке", () => {
    const { container } = render(
      <ConversationListItem conversation={conversationOf({ id: "conversation-42" })} onSelect={() => {}} />,
    );

    expect(container.querySelector('[data-conversation-id="conversation-42"]')).not.toBeNull();
  });

  it("беседа без последнего сообщения не показывает выдуманного времени", () => {
    // Времени нет ровно тогда, когда нет сообщения: `last_message` не пришёл, и
    // «только что» здесь — догадка, а не факт (`types.ts`, `lastMessageTimestamp`).
    render(
      <ConversationListItem
        conversation={conversationOf({ hasMessages: false, lastMessagePreview: "", lastMessageTimestamp: undefined })}
        onSelect={() => {}}
      />,
    );

    expect(screen.getByText("No messages yet")).toBeTruthy();
    expect(screen.queryByText(/^\d{1,2}:\d{2}$/)).toBeNull();
    expect(screen.queryByText(/yesterday|sep\b/i)).toBeNull();
  });

  it("беседа с сообщением показывает время последнего", () => {
    // Положительный контроль: без него предыдущая проверка зеленела бы и на
    // строке, которая не рисует время никогда.
    render(<ConversationListItem conversation={conversationOf()} onSelect={() => {}} />);

    expect(screen.getByText("14:22")).toBeTruthy();
  });

  it("нажатие отдаёт идентификатор нажатой беседы", () => {
    const onSelect = vi.fn();
    render(<ConversationListItem conversation={conversationOf({ id: "conversation-42" })} onSelect={onSelect} />);

    screen.getByRole("button").click();
    expect(onSelect).toHaveBeenCalledWith("conversation-42");
  });
});
