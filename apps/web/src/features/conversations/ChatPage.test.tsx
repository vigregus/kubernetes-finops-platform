import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { Conversation as ConversationDto, ConversationListPage } from "../../api/generated";
import { givenFakeCentrifuge, givenTicketIssuer } from "../../test-support/centrifuge";
import { VIEWER, conversationOf } from "../../test-support/fixtures";
import {
  FakeIntersectionObserver,
  installObserverForJsdom,
  restoreIntersectionObserver,
} from "../../test-support/intersectionObserver";
import type { ChatMessage } from "../../shared/lib/types";
import type { Conversation } from "../../shared/lib/types";
import type { OutgoingReceipt } from "../receipts/receiptWatermarks";
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
const ANNA_ID = "user-anna";
const CENTRIFUGO_URL = "wss://rt.finops.local/connection/websocket";

const ANNA = conversationOf({
  id: "c1",
  name: "Anna Petrova",
  hasMessages: true,
  // Собеседник назван по имени: по нему квитанция события (`reader_id`)
  // узнаётся как его. Без него личная квитанция неотличима от собственной —
  // обе приходят в один канал беседы.
  peerUserId: ANNA_ID,
});
/**
 * Канал беседы `ANNA` — так его строит панель (`conversation:{id}`).
 *
 * Указывается в событиях SDK явно, потому что канал теперь **отбор**: сервер
 * подписывает клиента на несколько каналов сразу (`user:{id}` тем же событием),
 * и событие без имени канала не говорит, о какой подписке речь.
 */
const ANNA_CHANNEL = "conversation:c1";
/**
 * Личный канал зрителя — так его строит панель (`user:{currentUserId}`).
 *
 * Проверки счётчика адресуются ему явно: публикация без имени канала не
 * говорит, о какой подписке речь, а событие, отправленное в канал беседы,
 * проверяло бы не тот путь.
 */
const VIEWER_CHANNEL = "user:user-viewer";
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

/** Пустая страница списка — законный ответ сервера, а не заглушка. */
function emptyPage(items: ConversationListPage["items"] = []): ConversationListPage {
  return { items, nextBeforeActivityAt: null, nextBeforeConversationId: null };
}

/**
 * Ответ REST с одной личной беседой — то, чем отвечает сверка.
 *
 * Собирается по **контракту**, а не по модели: сверка адаптируется тем же
 * `adaptConversations`, что и загрузка, и тест, подавший сюда модель вместо DTO,
 * проверял бы ветку, которой в жизни не бывает. Собеседник назван тем же
 * `user_id`, что и в `ANNA`: по нему панель узнаёт квитанцию события, и
 * разошедшееся имя сделало бы «состояние собеседника в REST» неотличимым от
 * состояния любого другого участника.
 */
function directPage(
  conversationId: string,
  overrides: Partial<ConversationDto> = {},
): ConversationListPage {
  return emptyPage([
    {
      conversationId,
      type: "direct",
      participants: [
        { userId: VIEWER_ID, displayName: "David Miller" },
        { userId: ANNA_ID, displayName: "Anna Petrova" },
      ],
      createdAt: "2026-09-01T00:00:00Z",
      ...overrides,
    },
  ]);
}

/**
 * Наблюдатель возвращается в исходное состояние после каждого теста.
 *
 * Подменённый `IntersectionObserver` — глобальный: оставленный, он попал бы в
 * следующий файл прогона, и тот проверял бы двойника вместо окружения.
 */
afterEach(() => {
  restoreIntersectionObserver();
});

interface SetupOptions {
  readonly conversations: Conversation[];
  readonly tail?: (conversationId: string) => Promise<TailPage>;
  readonly loadPage?: HistorySource["loadPage"];
  /** Ответ сверки. По умолчанию — пустая страница: сверка не обязана ничего менять. */
  readonly refresh?: () => Promise<ConversationListPage>;
  /**
   * Исход отправки квитанции. По умолчанию — успех.
   *
   * Отказ задаётся здесь, а не молчанием: политика «число записывается только
   * после ответа» держится на этом `Promise`, и тест, не назвавший исход,
   * проверял бы удачный транспорт, ничего о нём не зная.
   */
  readonly sendReceipts?: (conversationId: string, receipt: OutgoingReceipt) => Promise<unknown>;
}

function setup({ conversations, tail, loadPage, refresh, sendReceipts }: SetupOptions) {
  const fake = givenFakeCentrifuge();
  const tickets = givenTicketIssuer();
  const calls = {
    tails: [] as string[],
    pages: [] as SyncPage[],
    refreshes: 0,
    /**
     * Отправленные квитанции — то, что вкладка **сообщила**, по порядку.
     *
     * Записываются, а не проверяются на месте: правило D6 читается по
     * последовательности (доставка, потом прочтение, назад — никогда), а не по
     * одному запросу.
     */
    receipts: [] as Array<{ conversationId: string; receipt: OutgoingReceipt }>,
  };

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

  const refreshConversations = () => {
    calls.refreshes += 1;
    return refresh ? refresh() : Promise.resolve(emptyPage());
  };

  // Устойчивая ссылка: смена функции перезапускала бы дребезг квитанции на
  // каждом рендере, и отправка откладывалась бы вечно (см. `useReceipts`).
  const send = (conversationId: string, receipt: OutgoingReceipt) => {
    calls.receipts.push({ conversationId, receipt });

    return sendReceipts ? sendReceipts(conversationId, receipt) : Promise.resolve();
  };

  // Собирается функцией, а не литералом на месте: повтор загрузки приносит
  // **тот же** компонент с другим списком, и собрать его вторым литералом
  // значило бы разойтись с первым на первой же правке пропсов.
  const panel = (list: Conversation[]) => (
    <ChatPage
      conversations={list}
      refreshConversations={refreshConversations}
      currentUser={VIEWER}
      currentUserId={VIEWER_ID}
      history={history}
      centrifugoUrl={CENTRIFUGO_URL}
      issueTicket={tickets.issueTicket}
      sendReceipts={send}
      createCentrifuge={fake.factory}
    />
  );

  const view = render(panel(conversations));

  const pane = view.container.querySelector("[data-connection-state]");

  return {
    ...view,
    fake,
    calls,
    /**
     * Новые данные загрузки — тем же пропсом, каким они приходят в жизни:
     * повтор чтения после отказа. Это **единственный** путь, на котором список
     * меняется без сверки, и до него состояние панели не сбрасывалось ничем.
     */
    reload: (list: Conversation[]) => view.rerender(panel(list)),
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
    /**
     * Число непрочитанного — **из разметки строки**, а не из `Badge`.
     *
     * `null` здесь значит «атрибута нет», то есть «сервер числа не назвал», и
     * это третье состояние, отличное от `"0"`: слить их в одно значило бы
     * выдавать молчание сервера за «всё прочитано».
     */
    unread: (conversationId: string) =>
      view.container
        .querySelector(`[data-conversation-id="${conversationId}"]`)
        ?.getAttribute("data-unread-count") ?? null,
    /**
     * Что вкладка **сообщила** о прочтении — и только это.
     *
     * `null` здесь значит «не сообщала»: ноль в этом атрибуте означал бы
     * «прочитано ни до чего», то есть утверждение, которого вкладка не делала.
     * Отсутствие атрибута и `"0"` — разные состояния, и сливать их нельзя.
     */
    myRead: () => pane?.getAttribute("data-my-read-seq") ?? null,
    /** Что сообщил собеседник о прочтении. `null` — он ещё ничего не сообщал. */
    peerRead: () => pane?.getAttribute("data-peer-read-seq") ?? null,
    /**
     * Состояние **своего** сообщения. Читается по производственному
     * `data-message-seq`, а не по порядку строк: порядок строк — то, что
     * проверяется отдельно, и опираться на него здесь значило бы проверять
     * одно через другое.
     */
    rowState: (seq: number) =>
      view.container
        .querySelector(`[data-message-seq="${seq}"]`)
        ?.getAttribute("data-message-state") ?? null,
  };
}

/** Публикация личного канала — тот самый вход, а не подмена состояния. */
async function publishUnread(
  fake: ReturnType<typeof givenFakeCentrifuge>,
  conversationId: string,
  unreadCount: number,
) {
  await act(async () => {
    fake.clientHandlers["publication"]?.({
      channel: VIEWER_CHANNEL,
      data: { type: "unread.changed", conversation_id: conversationId, unread_count: unreadCount },
    });
  });
}

/**
 * Публикация сообщения в канал беседы — тот же вход, каким он приходит в жизни.
 *
 * Отправитель назван собеседником: чужое сообщение — это и есть случай, ради
 * которого квитанция существует (`RCP-001` — «A видит прочитанное»), и событие
 * о собственном сообщении проверяло бы другую ветку.
 */
async function publishMessage(
  fake: ReturnType<typeof givenFakeCentrifuge>,
  seq: number,
) {
  await act(async () => {
    fake.clientHandlers["publication"]?.({
      channel: ANNA_CHANNEL,
      data: {
        type: "message.created",
        message_id: `11111111-1111-1111-1111-1111111111${String(seq).padStart(2, "0")}`,
        seq,
        sender_id: ANNA_ID,
        payload: { text: `message ${seq}` },
      },
    });
  });
}

/**
 * Публикация квитанции в канал беседы.
 *
 * Тело набирается здесь целиком — вместе с `reader_id`, потому что именно им
 * панель отличает чужую квитанцию от собственной: обе приходят на один канал.
 */
async function publishRead(
  fake: ReturnType<typeof givenFakeCentrifuge>,
  body: { reader_id: string; read_seq?: number; delivered_seq?: number },
) {
  await act(async () => {
    fake.clientHandlers["publication"]?.({
      channel: ANNA_CHANNEL,
      data: { type: "message.read", ...body },
    });
  });
}

/** Возврат вкладки — повод сверки (`D11`). */
async function returnToVisible() {
  await act(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
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
      fake.clientHandlers["subscribed"]?.({ channel: ANNA_CHANNEL, wasRecovering: true, recovered: false });
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
      fake.clientHandlers["subscribed"]?.({ channel: ANNA_CHANNEL, wasRecovering: true, recovered: false });
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
      fake.clientHandlers["subscribed"]?.({ channel: ANNA_CHANNEL, wasRecovering: false, recovered: false });
    });

    await waitFor(() => expect(state()).toBe("connected"));
    expect(screen.queryByText(/catching up on missed messages/i)).toBeNull();
  });
});

describe("число непрочитанного: событие личного канала и сверка", () => {
  /** Страница сверки с одним числом — то, что несёт ответ REST. */
  function pageWith(conversationId: string, unreadCount: number): ConversationListPage {
    return directPage(conversationId, { unreadCount });
  }

  it("событие личного канала ставит число, а не прибавляет его", async () => {
    // Абсолютность — не деталь: публикация best-effort, и повтор доставки
    // возможен (пачка Kafka приезжает второй раз). Сложение сдвинуло бы
    // счётчик вверх на каждом повторе, и исправить это на клиенте нечем.
    const { fake, unread, calls } = setup({
      conversations: [conversationOf({ id: "c1", unreadCount: 1 })],
    });

    await publishUnread(fake, "c1", 2);
    expect(unread("c1")).toBe("2");

    await publishUnread(fake, "c1", 5);
    expect(unread("c1")).toBe("5");

    // Число пришло событием, а не сверкой: круг REST здесь был бы лишним.
    expect(calls.refreshes).toBe(0);
  });

  it("число из личного канала доходит до строки списка", async () => {
    // Без правки среза 5 сюда не доходило ничего: публикации личного канала
    // выбрасывались фильтром по одному каналу, и вкладка узнавала своё число
    // только перезагрузкой.
    const { fake, unread } = setup({ conversations: [conversationOf({ id: "c1" })] });

    expect(unread("c1")).toBeNull();

    await publishUnread(fake, "c1", 3);

    expect(unread("c1")).toBe("3");
  });

  it("ноль из события — настоящее число, а не отсутствие", async () => {
    const { fake, unread } = setup({ conversations: [conversationOf({ id: "c1", unreadCount: 4 })] });

    await publishUnread(fake, "c1", 0);

    expect(unread("c1")).toBe("0");
  });

  it("возврат вкладки заменяет число ответом REST, а не правит прежнее", async () => {
    // `D11`: транспорт у события best-effort, истину приносит чтение. Замена
    // чинит потерянную публикацию целиком, накопление — не чинит вовсе.
    // Мутация, которая это краснит: оверлей оставляется поверх свежего ответа
    // (тогда здесь осталось бы «3»), либо число складывается (тогда «10»).
    const { fake, unread, calls } = setup({
      conversations: [conversationOf({ id: "c1", unreadCount: 1 })],
      refresh: () => Promise.resolve(pageWith("c1", 7)),
    });

    await publishUnread(fake, "c1", 3);
    expect(unread("c1")).toBe("3");

    await returnToVisible();

    await waitFor(() => expect(unread("c1")).toBe("7"));
    expect(calls.refreshes).toBe(1);
  });

  it("сверка берёт число из ответа, а не из пропса, оставшегося прежним", async () => {
    // Пропс не менялся вовсе: `conversations` — снимок момента загрузки, и
    // перечитанный список обязан заменить его целиком, а не «дополнить».
    const { fake, unread } = setup({
      conversations: [conversationOf({ id: "c1", unreadCount: 1 })],
      refresh: () => Promise.resolve(pageWith("c1", 9)),
    });

    await returnToVisible();

    await waitFor(() => expect(unread("c1")).toBe("9"));
    expect(fake.calls.connect).toBe(1);
  });

  it("новые данные загрузки снимают оверлей: событие не старше только что прочитанного", async () => {
    // Обратная сторона предыдущего теста. Там пропс не менялся, и ответ сверки
    // обязан был его перекрыть; здесь пришли **новые данные загрузки** (повтор
    // чтения после отказа), и наложенное поверх них прежнее событие было бы
    // утверждением старше только что полученного — тот же класс, что запрещает
    // `D11`. Оверлей снимается **вместе с данными**, а не живёт своей жизнью:
    // число берётся из пришедшего списка.
    const { fake, unread, reload } = setup({
      conversations: [conversationOf({ id: "c1", unreadCount: 1 })],
    });

    await publishUnread(fake, "c1", 5);
    expect(unread("c1")).toBe("5");

    reload([conversationOf({ id: "c1", unreadCount: 2 })]);

    expect(unread("c1")).toBe("2");
  });

  it("на монтирование сверка не ходит: повод берётся по переходу", async () => {
    // Панель пересоздаётся на каждой смене беседы (`key`), и сверка «на
    // монтирование» давала бы лишний круг REST на каждое переключение.
    const { calls } = setup({ conversations: [conversationOf({ id: "c1" })] });

    expect(calls.refreshes).toBe(0);
  });

  it("уход в фон сверку не запускает: там вкладка от событий ушла, а не пропустила их", async () => {
    // `document.visibilityState` в jsdom — `visible` (что и проверяет соседний
    // тест). Здесь он подменяется **своим** свойством `document`, а не правкой
    // прототипа: прототип принадлежит прогону целиком, и оставленный на нём
    // `hidden` сделал бы слепым следующий тест, а не убрался бы вместе с этим.
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });

    try {
      const { calls } = setup({ conversations: [conversationOf({ id: "c1" })] });

      await returnToVisible();

      expect(calls.refreshes).toBe(0);
    } finally {
      delete (document as unknown as Record<string, unknown>).visibilityState;
      expect(document.visibilityState).toBe("visible");
    }
  });

  it("подписка личного канала не сдвигает состояние соединения беседы", async () => {
    // Тот самый красный, ради которого фильтр `subscribed` оставлен. Сервер
    // выдаёт оба канала одним тикетом (`services/realtime.py:83`) и эмитит
    // `subscribed` на **каждый** из них. Пущенная в автомат, эта подписка
    // читалась бы как подписка нашей беседы: `wasRecovering: true,
    // recovered: false` увёл бы панель в `syncing`, и `data-connection-state`
    // ушёл бы из `connected` по событию о чужом канале.
    //
    // Догрузка удерживается намеренно: отпущенная, она завершилась бы в ту же
    // микротаску и вернула бы `connected` **уже после** ошибочного перехода, —
    // то есть проверка зеленела бы и на дефекте. Здесь важен именно факт
    // перехода, а не то, чем он кончился.
    const { fake, state, syncReason } = setup({
      conversations: [conversationOf({ id: "c1" })],
      loadPage: () => new Promise(() => {}),
    });

    await act(async () => {
      fake.clientHandlers["connected"]?.({});
    });
    await waitFor(() => expect(state()).toBe("connected"));

    await act(async () => {
      fake.clientHandlers["subscribed"]?.({
        channel: VIEWER_CHANNEL,
        wasRecovering: true,
        recovered: false,
      });
    });

    expect(state()).toBe("connected");
    expect(syncReason()).toBeNull();
  });

  it("выход из разрыва — второй повод сверки", async () => {
    // `disconnected` — состояние, в котором события могли не дойти. Возврат
    // из него и есть повод; «мы сейчас в connected» поводом не является
    // (см. тест выше про монтирование).
    const { fake, calls } = setup({ conversations: [conversationOf({ id: "c1" })] });

    await act(async () => {
      fake.clientHandlers["connected"]?.({});
    });
    expect(calls.refreshes).toBe(0);

    await act(async () => {
      fake.clientHandlers["disconnected"]?.({ code: 3001 });
    });
    // Разрыв сам по себе — не повод: истину берут **после** него.
    expect(calls.refreshes).toBe(0);

    await act(async () => {
      fake.clientHandlers["connected"]?.({});
    });

    await waitFor(() => expect(calls.refreshes).toBe(1));
  });

  it("число для беседы, которой нет в списке, ничего не рисует", async () => {
    // Показать её нечем, а завести беседу из одного числа значило бы выдумать
    // содержимое. Падать при этом нельзя: публикация приходит из сети.
    const { fake, unread, container } = setup({ conversations: [conversationOf({ id: "c1" })] });

    await publishUnread(fake, "c2", 3);

    expect(unread("c1")).toBeNull();
    expect(container.querySelector('[data-conversation-id="c2"]')).toBeNull();
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

/**
 * Квитанция **вкладки**: что она сообщила и когда (D6).
 *
 * Проверяется не «отправка случилась», а **что именно** ушло: два числа живут по
 * разным законам, и различие между ними — предмет гейта. `delivered` — свойство
 * устройства (публикация доходит и применяется и в свёрнутой вкладке), `read` —
 * свойство взгляда, и в фоне его не существует.
 */
describe("квитанция вкладки: что и когда уходит", () => {
  it("целиком видимая строка двигает прочтение, а применённое — доставку", async () => {
    // Две клетки таблицы D6 разом: до касания строк уходит только доставка
    // (сообщение применено, и это свойство устройства), после — оба числа.
    // Различие читается по разметке панели, а не по внутренностям хука.
    installObserverForJsdom();
    const { calls, myRead, boundary } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(101), messageOf(102)])),
    });

    await waitFor(() => expect(boundary()).toBe("102"));
    await waitFor(() =>
      expect(calls.receipts).toEqual([{ conversationId: "c1", receipt: { deliveredSeq: 102 } }]),
    );
    // Доставка ушла, прочтения нет: строку ещё никто не видел.
    expect(myRead()).toBeNull();

    FakeIntersectionObserver.latest.intersectSeq(101, 1);
    FakeIntersectionObserver.latest.intersectSeq(102, 1);

    await waitFor(() => expect(myRead()).toBe("102"));
    expect(calls.receipts.at(-1)).toEqual({
      conversationId: "c1",
      receipt: { readSeq: 102 },
    });
  });

  it("в фоне уходит доставка, прочтение — нет; при возврате уходит свёрнутое", async () => {
    // Четыре клетки D6 целиком, и утверждение названо точно: «в фоне отправок
    // нет» — **ложно** и прошло бы зелёным на дефекте, потому что запрет
    // отправок в фоне отнял бы у `RCP-001` половину смысла. Проверяется именно
    // запрет на `read_seq` в фоне и его отправка после возврата.
    //
    // Свойство подменяется у **своего** `document`, а не у прототипа: прототип
    // принадлежит прогону целиком, и оставленный на нём `hidden` сделал бы
    // слепым следующий файл, а не убрался бы вместе с этим.
    // Значение читается из переменной, а не из литерала: «вернуться» и «уйти в
    // фон» — это **два** перехода, и подмена с постоянным `hidden` проверила бы
    // только первый из них (а возврат получился бы мнимым).
    let tabState: "hidden" | "visible" = "hidden";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => tabState,
    });

    try {
      installObserverForJsdom();
      const { fake, calls, myRead, boundary } = setup({
        conversations: [ANNA],
        tail: () => Promise.resolve(tailOf([messageOf(101), messageOf(102)])),
        // Возврат вкладки будит сверку, и список обязан остаться тем же: пустой
        // ответ снял бы выбранную беседу вместе с панелью, а с ней и состояние
        // квитанции — тест проверял бы пустоту.
        refresh: () => Promise.resolve(directPage("c1")),
      });

      await waitFor(() => expect(boundary()).toBe("102"));
      await waitFor(() =>
        expect(calls.receipts.map((call) => call.receipt)).toEqual([{ deliveredSeq: 102 }]),
      );

      // Строки видит наблюдатель, а не вкладка: номер прочтения посчитан и
      // лежит в панели. В фон он при этом не уходит — и это проверяется
      // положительным утверждением, а не «ничего не пришло».
      FakeIntersectionObserver.latest.intersectSeq(102, 1);
      await publishMessage(fake, 103);

      await waitFor(() =>
        expect(calls.receipts.map((call) => call.receipt)).toEqual([
          { deliveredSeq: 102 },
          { deliveredSeq: 103 },
        ]),
      );
      expect(myRead()).toBeNull();

      tabState = "visible";
      await returnToVisible();

      // Свёрнутое прочтение — максимум видимого за всё отсутствие, а не первое
      // число после возврата.
      await waitFor(() => expect(myRead()).toBe("102"));
      expect(calls.receipts.at(-1)).toEqual({
        conversationId: "c1",
        receipt: { readSeq: 102 },
      });
    } finally {
      delete (document as unknown as Record<string, unknown>).visibilityState;
      expect(document.visibilityState).toBe("visible");
    }
  });
});

/**
 * Квитанция **собеседника**: приём события, проекция на свои сообщения и сверка.
 *
 * Обе половины `RCP-001` живут здесь: `message.read` ускоряет, но истину
 * приносит REST (`D11`), и оба пути обязаны ставить **одно и то же** число.
 */
describe("квитанция собеседника: приём и отображение", () => {
  it("отставшее событие собеседника числа не двигает", async () => {
    const { fake, peerRead, boundary } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(101), messageOf(102)])),
    });

    await waitFor(() => expect(boundary()).toBe("102"));

    await publishRead(fake, { reader_id: ANNA_ID, read_seq: 495, delivered_seq: 480 });
    expect(peerRead()).toBe("495");

    // Старое устройство доедает очередь и присылает прежний номер. Принять его
    // значило бы показать прочтение, которого не делали, и откатить уже
    // показанную отметку (`D6а`).
    await publishRead(fake, { reader_id: ANNA_ID, read_seq: 494, delivered_seq: 470 });

    expect(peerRead()).toBe("495");
  });

  it("своя квитанция собеседником не становится", async () => {
    // Своя приходит **на тот же канал беседы**, и различает их только
    // `reader_id`. Прими вкладка свою за чужую — и человек увидел бы
    // собственное прочтение как прочтение собеседника; это тот же класс, что
    // «имя из `participants[0]`» в адаптере.
    const { fake, peerRead } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(101)])),
    });

    await publishRead(fake, { reader_id: ANNA_ID, read_seq: 495 });
    expect(peerRead()).toBe("495");

    await publishRead(fake, { reader_id: VIEWER_ID, read_seq: 900 });

    expect(peerRead()).toBe("495");
  });

  it("беседе без названного собеседника квитанция не приписывается", async () => {
    // Группе состояние собеседника не положено: их там несколько, и «его»
    // номера не существует. Приписать событие любому участнику значило бы
    // назвать собеседником случайного человека.
    const { fake, peerRead } = setup({
      conversations: [conversationOf({ id: "c1", name: "Team" })],
      tail: () => Promise.resolve(tailOf([messageOf(101)])),
    });

    await publishRead(fake, { reader_id: ANNA_ID, read_seq: 495 });

    expect(peerRead()).toBeNull();
  });

  it("номера собеседника из REST проецируются на свои сообщения", async () => {
    // Три состояния одной ленты разом: собеседник прочитал до 480, его
    // устройство доехало до 495, дальше — только отправлено. Состояние
    // выводится из **его** номеров, а не из наших отправок: «доставлено» —
    // факт собеседника.
    const { rowState, boundary } = setup({
      conversations: [
        conversationOf({
          id: "c1",
          peerUserId: ANNA_ID,
          peerReadState: { readSeq: 480, deliveredSeq: 495 },
        }),
      ],
      tail: () => Promise.resolve(tailOf([messageOf(480), messageOf(495), messageOf(500)])),
    });

    await waitFor(() => expect(boundary()).toBe("500"));

    expect(rowState(480)).toBe("read");
    expect(rowState(495)).toBe("delivered");
    expect(rowState(500)).toBe("sent");
  });

  it("сверка приносит номера собеседника из read_states", async () => {
    // Ветка `D11` без события-повода: истину приносит ответ REST. До возврата
    // вкладки о собеседнике не известно **ничего**, и это не пара нулей —
    // массив `read_states` разреженный, и зритель в нём стоит первым: номер
    // обязан быть найден по имени, а не по месту.
    const { peerRead, rowState, calls } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(101)])),
      refresh: () =>
        Promise.resolve(
          directPage("c1", {
            readStates: [
              { userId: VIEWER_ID, lastReadSeq: 900, lastDeliveredSeq: 900 },
              { userId: ANNA_ID, lastReadSeq: 495, lastDeliveredSeq: 480 },
            ],
          }),
        ),
    });

    await waitFor(() => expect(calls.tails).toEqual(["c1"]));
    expect(peerRead()).toBeNull();

    await returnToVisible();

    await waitFor(() => expect(peerRead()).toBe("495"));
    expect(calls.refreshes).toBe(1);
    // Тот же ответ двигает и состояние сообщений — числа у путей одни и те же.
    expect(rowState(101)).toBe("read");
  });
});
