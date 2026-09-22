import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { givenFakeCentrifuge, givenTicketIssuer } from "../../test-support/centrifuge";
import { VIEWER, conversationOf } from "../../test-support/fixtures";
import type { ChatMessage } from "../../shared/lib/types";
import type { Conversation } from "../../shared/lib/types";
import type { HistorySource, TailPage } from "../messages/history";
import type { SyncPage, SyncPageResult } from "../messages/sync";
import { ChatPage } from "./ChatPage";

/**
 * Главная панель: **одна** дорога данных для любой беседы.
 *
 * Прежняя композиция ветвилась по `hasMessages` из списка бесед: с историей —
 * заглушка, без истории — `EmptyConversationState`, и realtime-путь не
 * запускался вовсе. Пустая беседа оставалась без соединения и без применённой
 * границы, и первое же сообщение (`seq = 1`) некуда было применить — сервер
 * подписывает клиента на канал сам, публикация приходит, а у активной беседы
 * нет ни границы, ни ленты.
 *
 * Поэтому здесь проверяется не число состояний, а **путь**: у любой выбранной
 * беседы запрошен хвост и выставлена граница. Флаг `hasMessages` говорит лишь о
 * последнем сообщении на момент чтения списка; снимок — факт.
 */

const VIEWER_ID = "user-viewer";
const CENTRIFUGO_URL = "wss://rt.finops.local/connection/websocket";

const ANNA = conversationOf({ id: "c1", name: "Anna Petrova", hasMessages: true });
const MARCUS_NO_MESSAGES = conversationOf({
  id: "c2",
  name: "Marcus Chen",
  hasMessages: false,
  lastMessagePreview: "",
  lastMessageTimestamp: undefined,
});

function messageOf(seq: number): ChatMessage {
  return {
    id: `11111111-1111-1111-1111-1111111111${String(seq).padStart(2, "0")}`,
    seq,
    authorId: "me",
    kind: "text",
    text: `message ${seq}`,
    timestamp: "14:22",
  };
}

function tailOf(items: ChatMessage[]): TailPage {
  return { items, hasMore: false, nextBeforeSeq: null, syncToSeq: null };
}

/** Отложенный ответ: им предъявляется «снимок ещё в пути». */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });

  return { promise, resolve };
}

interface SetupOptions {
  readonly conversations: Conversation[];
  readonly tail?: (conversationId: string) => Promise<TailPage>;
  readonly loadPage?: HistorySource["loadPage"];
}

function setup({ conversations, tail, loadPage }: SetupOptions) {
  const fake = givenFakeCentrifuge();
  const tickets = givenTicketIssuer();
  const calls = { tails: [] as string[], pages: [] as SyncPage[] };

  const history: HistorySource = {
    loadTail: (conversationId) => {
      calls.tails.push(conversationId);
      return tail ? tail(conversationId) : Promise.resolve(tailOf([]));
    },
    loadPage: (conversationId, page) => {
      calls.pages.push(page);
      return loadPage
        ? loadPage(conversationId, page)
        : Promise.resolve({ items: [], hasMore: false, nextAfterSeq: null, syncToSeq: null });
    },
  };

  const view = render(
    <ChatPage
      conversations={conversations}
      currentUser={VIEWER}
      currentUserId={VIEWER_ID}
      history={history}
      centrifugoUrl={CENTRIFUGO_URL}
      issueTicket={tickets.issueTicket}
      createCentrifuge={fake.factory}
    />,
  );

  const pane = view.container.querySelector("[data-connection-state]");

  return {
    ...view,
    fake,
    calls,
    state: () => pane?.getAttribute("data-connection-state") ?? null,
    syncReason: () => pane?.getAttribute("data-sync-reason") ?? null,
    /**
     * Граница читается по **отсутствию атрибута**, а не по значению: `null` —
     * «снимка ещё не было», `0` — «подтверждённый пустой снимок». Слить их в
     * одно значило бы выдать неизвестное за факт.
     */
    boundary: () =>
      view.container
        .querySelector("[data-applied-through-seq]")
        ?.getAttribute("data-applied-through-seq") ?? null,
  };
}

describe("панель не ветвится по hasMessages", () => {
  it("беседа без последнего сообщения идёт тем же путём: соединение и снимок", async () => {
    // `hasMessages: false` — флаг из **списка**, а не приговор к пустоте. До
    // правки эта беседа не проходила realtime-путь вовсе: соединения не было,
    // границы не было, и первое сообщение некуда было применить.
    const { calls, boundary } = setup({ conversations: [MARCUS_NO_MESSAGES] });

    await waitFor(() => expect(boundary()).toBe("0"));
    expect(calls.tails).toEqual(["c2"]);
    expect(screen.getByText(/no messages yet\. say hello to marcus chen/i)).toBeTruthy();
  });

  it("беседа с последним сообщением показывает снимок, а не пустоту", async () => {
    const { boundary } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(101), messageOf(102)])),
    });

    await waitFor(() => expect(boundary()).toBe("102"));
    expect(screen.getByText("message 101")).toBeTruthy();
    expect(screen.queryByText(/no messages yet/i)).toBeNull();
  });

  it("пустой список бесед не падает, не рисует шапку и не показывает фикстур", () => {
    const { state } = setup({ conversations: [] });

    // Панели нет вовсе: состояние соединения — утверждение о выбранной беседе,
    // а её нет.
    expect(state()).toBeNull();
    expect(screen.queryByText("Anna Petrova")).toBeNull();
    expect(screen.getByText(/no conversations/i)).toBeTruthy();
    // Шапка — это `<header>` верхнего уровня, то есть роль `banner`.
    expect(screen.queryByRole("banner")).toBeNull();
  });

  it("имя выбранной беседы стоит в шапке", async () => {
    setup({ conversations: [ANNA], tail: () => Promise.resolve(tailOf([messageOf(1)])) });

    expect(screen.getByRole("banner")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Anna Petrova" })).toBeTruthy();
  });
});

describe("граница неизвестна до снимка и равна нулю после пустого", () => {
  it("до снимка атрибута нет, и лента не утверждает пустоту беседы", async () => {
    // Второе утверждение здесь не украшение: пустая лента до снимка — это
    // «сервер ещё не отвечал», а не «сообщений нет». Ложь тут правдоподобна
    // ровно до тех пор, пока беседа не окажется непустой.
    const pending = deferred<TailPage>();
    const { boundary, state, container } = setup({
      conversations: [ANNA],
      tail: () => pending.promise,
    });

    // Соединение при этом уже объявлено — оно идёт первым (B21).
    expect(state()).toBe("connecting");
    expect(boundary()).toBeNull();
    expect(container.textContent).not.toContain("No messages yet");
    expect(container.textContent).not.toContain("history isn't loaded yet");

    await act(async () => {
      pending.resolve(tailOf([]));
    });

    await waitFor(() => expect(boundary()).toBe("0"));
  });

  it("отказ хвоста назван отказом, а не пустотой и не загрузкой", async () => {
    const { container } = setup({
      conversations: [ANNA],
      tail: () => Promise.reject(new Error("boom")),
    });

    await waitFor(() => expect(container.textContent).toContain("Message history isn't available."));
    expect(container.textContent).not.toContain("No messages yet");
  });
});

describe("состояние соединения наблюдаемо и названо причиной", () => {
  it("при syncing атрибут состояния равен syncing, причина названа, строка называет догрузку", async () => {
    // `wasRecovering: true, recovered: false` — настоящий `recovered = false`
    // (RT-004). Догрузка здесь намеренно не отвечает: проверяется состояние
    // **во время** синхронизации, а не её завершение.
    const { fake, state, syncReason } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(101), messageOf(102)])),
      loadPage: () => new Promise(() => {}),
    });

    await waitFor(() => expect(screen.getByText("message 102")).toBeTruthy());

    await act(async () => {
      fake.subscriptionHandlers["subscribed"]?.({ wasRecovering: true, recovered: false });
    });

    await waitFor(() => expect(state()).toBe("syncing"));
    expect(syncReason()).toBe("recovery-miss");
    // `03-v1-scope.md:196`: пока идёт синхронизация, старое не выдаётся за
    // актуальное — строка обязана назвать догрузку, а не «всё хорошо».
    expect(screen.getByText(/catching up on missed messages/i)).toBeTruthy();
  });

  it("в connected причины нет: она принадлежит синхронизации, а не состоянию вообще", async () => {
    const { fake, state, syncReason } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(1)])),
    });

    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());

    await act(async () => {
      fake.clientHandlers["connected"]?.({});
    });

    await waitFor(() => expect(state()).toBe("connected"));
    expect(syncReason()).toBeNull();
  });

  it("после сходимости причина снимается, а не остаётся хвостом прошлого перехода", async () => {
    // Настоящая проверка «причина — только при `syncing`»: здесь она **была**
    // поставлена и обязана исчезнуть. Проверять отсутствие причины в
    // `connected`, куда синхронизация не заходила, бессмысленно — там её никто
    // и не ставил; атрибут, оставшийся от прошлого перехода, читался бы как
    // текущая причина, и приёмка назвала бы догрузкой то, что уже сошлось.
    //
    // Атрибут именно **отсутствует**, а не пуст: `data-sync-reason=""` — это
    // названная причина с пустым значением, то есть тот же хвост, только хуже.
    const closing = deferred<SyncPageResult>();
    const { fake, state, syncReason, container } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(101), messageOf(102)])),
      // Ответ догрузки удерживается вручную: иначе сходимость наступает в ту же
      // микротаску, что и переход, и наблюдать причину было бы нечего — тест
      // ловил бы уже `connected`.
      loadPage: () => closing.promise,
    });

    await waitFor(() => expect(screen.getByText("message 102")).toBeTruthy());

    await act(async () => {
      fake.subscriptionHandlers["subscribed"]?.({ wasRecovering: true, recovered: false });
    });

    await waitFor(() => expect(syncReason()).toBe("recovery-miss"));

    // Догрузка сошлась: пустая завершённая страница объявляет конец.
    await act(async () => {
      closing.resolve({ items: [], hasMore: false, nextAfterSeq: null, syncToSeq: null });
    });

    await waitFor(() => expect(state()).toBe("connected"));
    expect(syncReason()).toBeNull();
    expect(container.textContent).not.toContain("Catching up on missed messages");
  });

  it("первая подписка не уводит в syncing: recovered false при wasRecovering false — не расхождение", async () => {
    // Без защиты `wasRecovering` каждая загрузка страницы уезжала бы в SYNCING.
    // Проверяется не «не syncing», а именем состояния: `recovered: false`
    // приходит и здесь, и различает случаи только `wasRecovering`.
    const { fake, state } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(1)])),
    });

    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());

    await act(async () => {
      fake.subscriptionHandlers["subscribed"]?.({ wasRecovering: false, recovered: false });
    });

    await waitFor(() => expect(state()).toBe("connected"));
    expect(screen.queryByText(/catching up on missed messages/i)).toBeNull();
  });
});

describe("смена беседы сбрасывает состояние ремоунтом", () => {
  it("граница прежней беседы не остаётся в разметке новой", async () => {
    // `key` стоит над компонентом, который держит хук. Снятый или перенесённый
    // внутрь `ChatPage`, он оставил бы в разметке **число прежней беседы**:
    // эффект хвоста перезапустился бы, но применил бы снимок лишь через
    // мгновение, а до тех пор панель утверждала бы чужую границу.
    const pending = deferred<TailPage>();
    const { boundary, calls } = setup({
      conversations: [ANNA, MARCUS_NO_MESSAGES],
      tail: (conversationId) =>
        conversationId === "c1" ? Promise.resolve(tailOf([messageOf(102)])) : pending.promise,
    });

    await waitFor(() => expect(boundary()).toBe("102"));

    fireEvent.click(screen.getByText("Marcus Chen").closest("button")!);

    expect(calls.tails).toEqual(["c1", "c2"]);
    // Сразу после переключения, пока снимок новой беседы ещё в пути.
    expect(boundary()).toBeNull();

    await act(async () => {
      pending.resolve(tailOf([]));
    });

    await waitFor(() => expect(boundary()).toBe("0"));
  });
});

describe("штатного запаса в дереве нет", () => {
  it("композера в дереве нет: отправки из браузера в этом гейте нет", async () => {
    const { container } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(1)])),
    });

    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(container.querySelector("[data-message-id]")).toBeTruthy();
  });
});
