import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { VIEWER, conversationOf } from "../../test-support/fixtures";
import { ChatPage } from "./ChatPage";

/**
 * Главная панель знает **три** состояния, а не два, и ни одно из них не лжёт
 * (B21).
 *
 * Разделение обязательно, потому что `EmptyConversationState` говорит «No
 * messages yet. Say hello to <имя>» — и это правда **только** при отсутствии
 * `last_message`. Если `GET /conversations` вернул `last_message`, сообщения
 * есть; G3-005 их не загружает, но сказать «их нет» значило бы утверждать
 * обратное тому, что сообщил сервер. Это тот же класс, что «Active now» и
 * «Last seen».
 */
const ANNA = conversationOf({ id: "c1", name: "Anna Petrova", hasMessages: true });
const MARCUS_NO_MESSAGES = conversationOf({
  id: "c2",
  name: "Marcus Chen",
  hasMessages: false,
  lastMessagePreview: "",
  lastMessageTimestamp: undefined,
});

describe("главная панель", () => {
  it("пустой список бесед не падает, не рисует шапку и не показывает фикстур", () => {
    render(<ChatPage conversations={[]} currentUser={VIEWER} />);

    // Два утверждения в одном тесте — намеренно: падение на `conversations[0]`
    // и список из `mock-data` — это одна и та же причина (композиция берёт
    // данные не оттуда), и проверять её по половине значило бы оставить вторую
    // половину незамеченной.
    expect(screen.queryByText("Anna Petrova")).toBeNull();
    expect(screen.getByText(/no conversations/i)).toBeTruthy();
    // Шапка — это `<header>` верхнего уровня, то есть роль `banner`.
    expect(screen.queryByRole("banner")).toBeNull();
  });

  it("беседа без последнего сообщения показывает существующее пустое состояние", () => {
    render(<ChatPage conversations={[MARCUS_NO_MESSAGES]} currentUser={VIEWER} />);

    expect(screen.getByText(/no messages yet\. say hello to marcus chen/i)).toBeTruthy();
  });

  it("беседа с последним сообщением не говорит, что сообщений нет", () => {
    render(<ChatPage conversations={[ANNA]} currentUser={VIEWER} />);

    // Красный прогон этого теста приходит от нынешней композиции, где выбранная
    // беседа **всегда** идёт в `EmptyConversationState`.
    expect(screen.queryByText(/no messages yet/i)).toBeNull();
    // И вместо истории — честное «она не загружена»: третье состояние не
    // приносит историю в объём, а именно ею честно не пользуется.
    expect(screen.getByText(/history isn't loaded yet/i)).toBeTruthy();
  });

  it("истории, композера и ленты в дереве нет", () => {
    render(<ChatPage conversations={[ANNA]} currentUser={VIEWER} />);

    // `MessageComposer` и `MessageTimeline` — из закрытого списка B15: они
    // остаются проектным запасом, но в production-путь G3-005 не входят.
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByText("Today")).toBeNull();
  });

  it("выбор беседы переключает панель и никуда не ходит", () => {
    render(<ChatPage conversations={[ANNA, MARCUS_NO_MESSAGES]} currentUser={VIEWER} />);

    // Выбрана первая — история не загружена, а не «сообщений нет».
    expect(screen.getByText(/history isn't loaded yet/i)).toBeTruthy();

    // `fireEvent`, а не голый `element.click()`: прямой вызов обработчика не
    // доводит обновление состояния до разметки, и тест утверждал бы о прежнем
    // экране. Нажатие здесь — механика, утверждение остаётся о результате.
    fireEvent.click(screen.getByText("Marcus Chen").closest("button")!);

    expect(screen.getByText(/no messages yet\. say hello to marcus chen/i)).toBeTruthy();
  });

  it("имя выбранной беседы стоит в шапке", () => {
    render(<ChatPage conversations={[ANNA]} currentUser={VIEWER} />);

    expect(screen.getByRole("banner")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Anna Petrova" })).toBeTruthy();
  });
});
