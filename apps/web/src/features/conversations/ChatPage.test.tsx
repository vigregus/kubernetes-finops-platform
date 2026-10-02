import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// Автомок: `playIncomingMessageSound` бьёт по `AudioContext`, которого в
// jsdom нет, — сам модуль на это рассчитан (см. его докстринг, «молчание
// честнее» уже встроено), но проверить **вызов** без подмены нечем: у
// jsdom-стаба нет своей точки наблюдения, в отличие от `fake.clientHandlers`.
vi.mock("../messages/notificationSound");
import { playIncomingMessageSound } from "../messages/notificationSound";

import { MessageFromJSON } from "../../api/generated";
import type {
  Conversation as ConversationDto,
  ConversationListPage,
  Message,
} from "../../api/generated";
import { ApiProblem } from "../../api/problems";
import { givenFakeCentrifuge, givenTicketIssuer } from "../../test-support/centrifuge";
import { VIEWER, conversationOf } from "../../test-support/fixtures";
import {
  FakeIntersectionObserver,
  installObserverForJsdom,
  restoreIntersectionObserver,
} from "../../test-support/intersectionObserver";
import type { ChatMessage, CurrentUser } from "../../shared/lib/types";
import type { Conversation } from "../../shared/lib/types";
import { createOutboxStore } from "../messages/outbox/outboxStore";
import type { SendMessage, SendMessageRequest } from "../messages/outbox/useOutbox";
import type { OutgoingReceipt } from "../receipts/receiptWatermarks";
import type { HistorySource, TailPage } from "../messages/history";
import type { SyncPage, SyncPageResult } from "../messages/sync";
import { ChatPage } from "./ChatPage";
import type { CreateConversation, SearchUser } from "./components/NewConversationDialog";
import type { ResendVerificationEmail } from "../auth/components/EmailVerificationBanner";

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
  /**
   * Исход отправки сообщения. Не задан — отправки нет вовсе: попытка падает, и
   * это **названный** отказ, а не пустой успех.
   *
   * Пустой успех был бы хуже молчания: он снял бы запись из очереди и оставил
   * бы ленту без подтверждения — то есть тест, не назвавший исход, показывал бы
   * состояние, которого при таком сервере не бывает.
   */
  readonly sendMessage?: SendMessage;
  /**
   * Исход поиска человека. Не задан — отказ, по тому же доводу, что у
   * `sendMessage`: удачный ответ в подарок показал бы диалог, дошедший до
   * «нашёлся кто-то безымянный», то есть состояние, которого тест не называл.
   */
  readonly searchUser?: SearchUser;
  /** Исход создания беседы. Не задан — отказ, ровно как у поиска. */
  readonly createConversation?: CreateConversation;
  /** Исход повторной отправки письма. Не задан — отказ, ровно как у поиска. */
  readonly resendVerificationEmail?: ResendVerificationEmail;
  /** Зритель. Не задан — `VIEWER` (`emailVerified: true`, баннер скрыт). */
  readonly currentUser?: CurrentUser;
}

function setup({
  conversations,
  tail,
  loadPage,
  refresh,
  sendReceipts,
  sendMessage,
  searchUser,
  createConversation,
  resendVerificationEmail,
  currentUser,
}: SetupOptions) {
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
    /**
     * Ушедшие попытки отправки — вместе с `clientMessageId`, которым их послали.
     *
     * Идентификатор здесь, а не только текст: он и есть предмет проверки повтора
     * (D4) — «тот же самый на второй попытке» видно только по нему.
     */
    sent: [] as SendMessageRequest[],
    /**
     * Набранные адреса, ушедшие в поиск, и подтверждённые `user_id`, ушедшие в
     * создание, — порознь.
     *
     * Разделены не для симметрии: мутация «диалог создаёт беседу до
     * подтверждения» различима **только** по тому, что второго списка коснулись
     * без нажатия на подтверждение, — а первый при этом заполнен законно.
     */
    searches: [] as string[],
    created: [] as string[],
    resends: 0,
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

  // Отправка — устойчивой ссылкой, по тому же доводу, что и квитанция выше:
  // смена функции на рендере пересобирала бы замыкание попытки, и запись
  // очереди оставалась бы неотправленной.
  //
  // Исход по умолчанию — отказ: тест, не назвавший транспорта, не должен
  // получать удачную отправку в подарок (см. `SetupOptions.sendMessage`).
  const submit: SendMessage = (request) => {
    calls.sent.push(request);

    return sendMessage
      ? sendMessage(request)
      : Promise.reject(new Error("эти тесты не отправляют сообщений"));
  };

  /**
   * Хранилище очереди — настоящее, а не заглушка: в jsdom `IndexedDB` нет,
   * `list()` отказывает, и `useOutbox` ловит отказ, оставляя очередь пустой.
   *
   * Это и есть та самая названная деградация, которую `useOutbox` описывает для
   * браузера с выключенным хранилищем, — то есть тест идёт по живому пути, а не
   * мимо него. Заглушка-пустышка показала бы, что очередь «работает» там, где
   * она не работала бы вовсе.
   */
  const outboxStore = createOutboxStore();

  // Поиск и создание — устойчивыми ссылками, как `submit` выше: диалог живёт
  // ровно столько, сколько его показывают, но ссылка на операцию успевает
  // попасть в замыкание `search()`, и смена её на рендере дала бы вторую попытку
  // по тому же адресу.
  //
  // Исход по умолчанию — отказ: молчаливый успех вернул бы `UserLookup` без
  // полей, и диалог показал бы «Start chat with undefined?».
  const lookup: SearchUser = (email) => {
    calls.searches.push(email);

    return searchUser ? searchUser(email) : Promise.reject(new Error("эти тесты не ищут людей"));
  };

  const startChat: CreateConversation = (participantId) => {
    calls.created.push(participantId);

    return createConversation
      ? createConversation(participantId)
      : Promise.reject(new Error("эти тесты не создают бесед"));
  };

  const resend: ResendVerificationEmail = () => {
    calls.resends += 1;

    return resendVerificationEmail
      ? resendVerificationEmail()
      : Promise.reject(new Error("эти тесты не отправляют письмо подтверждения повторно"));
  };

  // Собирается функцией, а не литералом на месте: повтор загрузки приносит
  // **тот же** компонент с другим списком, и собрать его вторым литералом
  // значило бы разойтись с первым на первой же правке пропсов.
  const panel = (list: Conversation[]) => (
    <ChatPage
      conversations={list}
      refreshConversations={refreshConversations}
      currentUser={currentUser ?? VIEWER}
      currentUserId={VIEWER_ID}
      history={history}
      centrifugoUrl={CENTRIFUGO_URL}
      issueTicket={tickets.issueTicket}
      sendReceipts={send}
      sendMessage={submit}
      searchUser={lookup}
      createConversation={startChat}
      resendVerificationEmail={resend}
      outboxStore={outboxStore}
      createCentrifuge={fake.factory}
      // G3-008: эти тесты не проверяют телеметрию - `record` молча ничего
      // не делает, настоящий приёмник собирается в `main.tsx`.
      telemetry={{ record: () => {} }}
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

/**
 * Обратная сторона той же границы, снятой этим гейтом.
 *
 * Здесь стояло утверждение «композера в дереве нет: отправки из браузера в этом
 * гейте нет» — оно было верным для `G3-005`, где композер вычеркивался
 * статическим гейтом, и стало ложным ровно в тот момент, когда `G3-007-1`
 * вернул его в production-граф. Тест не удалён, а **перевёрнут**: не «поля нет»
 * (`queryByRole("textbox")` — `null`), а «человек производит сообщение полем», и
 * следствие нажатия доезжает до транспорта тем идентификатором, который
 * зачеканила очередь.
 *
 * Проверяется не «нажатие сработало», а **путь**: `client_message_id` здесь
 * рождается в очереди, а не в обёртке над API (`D4`), и тест читает его из двух
 * мест сразу — из записи в ленте и из ушедшего запроса. Совпали они или нет,
 * решает не глаз, а сравнение.
 */
describe("композер в дереве: человек производит сообщение полем", () => {
  it("нажатие отправки заводит запись очереди, и её же идентификатор уходит в транспорт", async () => {
    const { container, calls } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(1)])),
    });

    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());

    // Поле и кнопка ищутся **производственными** атрибутами, а не текстом
    // кнопки: тот же адрес, по которому пойдёт приёмочный сценарий, — и он не
    // поедет от правки подписи на кнопке.
    const input = container.querySelector("[data-composer-input]");
    const send = container.querySelector("[data-composer-send]");
    expect(input).toBeTruthy();
    expect(send).toBeTruthy();

    fireEvent.change(input!, { target: { value: "Hello Anna" } });
    fireEvent.click(send!);

    // Запись появилась в ленте — рядом с подтверждёнными, но без их номера.
    await waitFor(() =>
      expect(container.querySelector("[data-pending-client-id]")).toBeTruthy(),
    );

    const record = container.querySelector("[data-pending-client-id]")!;
    const queuedId = record.getAttribute("data-pending-client-id");

    expect(calls.sent).toHaveLength(1);
    expect(calls.sent[0]).toEqual({
      // Беседа берётся из активной, а не из поля ввода: человек пишет в ту,
      // которая открыта, и второго источника беседы у запроса быть не должно.
      conversationId: ANNA.id,
      clientMessageId: queuedId,
      text: "Hello Anna",
    });
  });

  it("пустое поле сообщения не производит: нажатие без текста никуда не идёт", async () => {
    const { container, calls } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(1)])),
    });

    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());

    // Пробелы — не текст: сообщение из пробелов существует в очереди и уходит
    // собеседнику пустой строкой, то есть человек отправил бы то, чего не писал.
    fireEvent.change(container.querySelector("[data-composer-input]")!, {
      target: { value: "   " },
    });
    fireEvent.click(container.querySelector("[data-composer-send]")!);

    expect(calls.sent).toEqual([]);
    expect(container.querySelector("[data-pending-client-id]")).toBeNull();
  });
});

/**
 * `G3-007-1a`: до подтверждения почты отказано только в `START_CONVERSATION`
 * (`_UNVERIFIED = {READ, SEND_MESSAGE}`, `domain/user.py`) — не в отправке.
 *
 * Баннер и композер проверяются в **одном** тесте намеренно: раздельные
 * тесты доказали бы каждый факт по отдельности, но не то, что баннер
 * появился **вместо** отключения композера, а не вместе с ним — а именно
 * эта связка и есть предмет гейта («не гасить композер по одному лишь
 * `emailVerified`»).
 */
describe("неподтверждённая почта не гасит композер в уже существующей беседе", () => {
  it("баннер виден, а отправка в открытую беседу проходит как обычно", async () => {
    const { container, calls } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(1)])),
      currentUser: { ...VIEWER, emailVerified: false },
    });

    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());

    expect(container.querySelector("[data-verification-banner-state]")).toBeTruthy();

    const input = container.querySelector("[data-composer-input]");
    const send = container.querySelector("[data-composer-send]");
    expect(input).toBeTruthy();
    expect(send).toBeTruthy();

    fireEvent.change(input!, { target: { value: "Hello Anna" } });
    fireEvent.click(send!);

    await waitFor(() =>
      expect(container.querySelector("[data-pending-client-id]")).toBeTruthy(),
    );
    expect(calls.sent).toHaveLength(1);
    expect(calls.sent[0]?.text).toBe("Hello Anna");
  });
});

/**
 * Окончательный отказ (`403`/`404`/`400`/`422`) — не сетевой сбой, и очередь
 * это уважает: `classify`/`nextAttempt` (`pendingMessages.ts`) относят такой
 * код к `final`, а не к `retry`, и не ставят таймер вовсе. Здесь проверяется
 * не сама классификация — она уже доказана без хранилища и без панели, — а
 * то, что **до DOM** она доезжает ровно так же: одна попытка транспорта, а
 * не первая из бесконечной серии.
 */
describe("окончательный отказ не крутит очередь вечно", () => {
  it("403 — одна попытка транспорта, запись помечена failed, повтора нет", async () => {
    const { container, calls } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(1)])),
      sendMessage: () =>
        Promise.reject(new ApiProblem({ status: 403, code: "blocked" })),
    });

    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());

    fireEvent.change(container.querySelector("[data-composer-input]")!, {
      target: { value: "Hello Anna" },
    });
    fireEvent.click(container.querySelector("[data-composer-send]")!);

    await waitFor(() =>
      expect(
        container.querySelector("[data-pending-client-id]")?.getAttribute("data-message-state"),
      ).toBe("failed"),
    );
    expect(calls.sent).toHaveLength(1);

    // Секунда с четвертью — заведомо больше первой паузы `retry`-веток
    // (`baseMs=1000`), но здесь таймера нет вовсе: `final` его не ставит.
    // Если бы классификация где-то по пути потерялась, `send` позвали бы
    // второй раз именно в этом окне.
    await new Promise((resolve) => setTimeout(resolve, 1250));
    expect(calls.sent).toHaveLength(1);
    expect(
      container.querySelector("[data-pending-client-id]")?.getAttribute("data-message-state"),
    ).toBe("failed");
  });
});

/**
 * Очередь исходящих живёт **над** панелью беседы (`useOutbox` в `ChatPage`,
 * не в `ConversationPane` — докстринг `MessageList.tsx`), и переключение
 * беседы пересоздаёт только панель (`key={activeConversation.id}`). Предмет
 * здесь — не то, что очередь **переживает** размонтирование (это следствие
 * места в дереве), а то, что запись **не течёт** в чужую беседу и не
 * теряется при уходе и возврате.
 */
describe("переключение беседы не роняет и не путает pending-запись", () => {
  it("запись из Anna не видна у Marcus и остаётся на месте после возврата", async () => {
    const pending = deferred<Message>();
    const { container, calls } = setup({
      conversations: [ANNA, MARCUS_NO_MESSAGES],
      tail: (conversationId) =>
        conversationId === "c1"
          ? Promise.resolve(tailOf([messageOf(1)]))
          : Promise.resolve(tailOf([])),
      sendMessage: () => pending.promise,
    });

    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());

    fireEvent.change(container.querySelector("[data-composer-input]")!, {
      target: { value: "Hello Anna" },
    });
    fireEvent.click(container.querySelector("[data-composer-send]")!);

    await waitFor(() =>
      expect(container.querySelector("[data-pending-client-id]")).toBeTruthy(),
    );
    const queuedId = container
      .querySelector("[data-pending-client-id]")!
      .getAttribute("data-pending-client-id");

    fireEvent.click(screen.getByText("Marcus Chen").closest("button")!);

    // Панель Marcus пуста и своего chat`а не видит: беседа фильтрует записи
    // очереди по `conversationId` (`ChatPage.tsx::pendingForActive`), и утечка
    // сюда была бы нарушением этого фильтра, а не просто лишней строкой.
    await waitFor(() => expect(screen.queryByText("message 1")).toBeNull());
    expect(container.querySelector("[data-pending-client-id]")).toBeNull();

    fireEvent.click(screen.getByText("Anna Petrova").closest("button")!);

    // Панель Anna ремонтируется целиком (`key`), но очередь — нет: та же
    // запись, тот же идентификатор, то же состояние «идёт попытка», ни
    // повторной отправки, ни дубля.
    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());
    await waitFor(() =>
      expect(container.querySelector("[data-pending-client-id]")).toBeTruthy(),
    );
    expect(
      container.querySelector("[data-pending-client-id]")?.getAttribute("data-pending-client-id"),
    ).toBe(queuedId);
    expect(calls.sent).toHaveLength(1);

    // Ответ приходит уже после возврата — панель Anna снова смонтирована, и
    // снятие обязано дойти до неё, а не потеряться в размонтированной.
    await act(async () => {
      pending.resolve(replyOf(ANNA.id, 2, queuedId!, "Hello Anna"));
    });
    await waitFor(() =>
      expect(container.querySelector("[data-pending-client-id]")).toBeNull(),
    );
  });
});

/**
 * Время суток в строке списка — «14:22».
 *
 * Значение **не пинуется**, и это не небрежность: часы здесь настоящие —
 * `ChatPage` зовёт `withSentPreview` без опций форматирования (`new Date()` в
 * момент отправки), в отличие от юнита адаптера, где и момент, и зона приходят
 * параметрами. Проверяемое здесь свойство — **появление** отметки у строки, у
 * которой её не было, а не её значение.
 */
const TIME_OF_DAY = /\d{1,2}:\d{2}/;

/**
 * Строка списка отражает **своё** сообщение — от отправки, а не от `message.created`.
 *
 * Предмет — не «превью обновилось», а **чем** оно обновилось и **когда**: событие
 * доставляется best-effort (та же посылка, что у сверки), и строка, ждущая его,
 * показывала бы старое превью под только что отправленным текстом. Поэтому
 * порядок, превью и время читаются **до** ответа сервера — в окне между нажатием
 * и ответом, которое здесь открывается отложенным промисом, — а не после.
 *
 * Второй предмет — то, чего строка **не** делает: своё сообщение числа
 * непрочитанного не заводит и не двигает. Проверяется в двух точках, и они не
 * повторяют друг друга: у беседы без числа отправка не должна родить ноль, а у
 * беседы с числом — ни сбросить его, ни прибавить единицу.
 */
describe("строка списка обновляется отправкой, а не событием", () => {
  it("превью, время и порядок двигаются в момент нажатия, с пометкой до ответа", async () => {
    // Беседа для отправки — **без сообщений**: у неё нет ни превью, ни времени,
    // и оба появляются ровно от отправки. На беседе с историей то же утверждение
    // неотличимо от «превью было и осталось прежним».
    const answer = deferred<Message>();
    const { container, calls, unread } = setup({
      conversations: [ANNA, MARCUS_NO_MESSAGES],
      sendMessage: () => answer.promise,
    });

    /** Порядок строк — по производственному атрибуту: он и есть то, что видит человек. */
    const order = () =>
      [...container.querySelectorAll("[data-conversation-id]")].map((item) =>
        item.getAttribute("data-conversation-id"),
      );
    const row = (id: string) =>
      container.querySelector(`[data-conversation-id="${id}"]`) as HTMLElement;
    /**
     * Пометка «ещё не подтверждено» читается **по наличию атрибута**, а не по его
     * значению: снятая пометка убирает ключ из модели, и React тогда атрибута не
     * пишет вовсе. `"false"` означало бы, что пометку не сняли, а переставили, —
     * третье состояние, которого в модели нет.
     */
    const pendingMark = (id: string) =>
      row(id).querySelector("[data-preview-pending]")?.getAttribute("data-preview-pending") ??
      null;

    // Отправляем во **вторую** беседу: у первой строка уже наверху, и «уехала
    // наверх» на ней неотличимо от «осталась на месте».
    fireEvent.click(row("c2"));
    await waitFor(() =>
      expect(container.querySelector("[data-composer-input]")).toBeTruthy(),
    );

    expect(order()).toEqual(["c1", "c2"]);
    expect(pendingMark("c2")).toBeNull();
    expect(within(row("c2")).getByText("No messages yet")).toBeTruthy();
    expect(row("c2").textContent).not.toMatch(TIME_OF_DAY);
    expect(unread("c2")).toBeNull();

    fireEvent.change(container.querySelector("[data-composer-input]")!, {
      target: { value: "Hello Marcus" },
    });
    fireEvent.click(container.querySelector("[data-composer-send]")!);

    await waitFor(() => expect(calls.sent).toHaveLength(1));

    // Ответ ещё в пути — а строка уже обновлена: строка уехала наверх, превью
    // сменилось, время появилось. Ждать `message.created` (или ответа) значило
    // бы показывать человеку старое превью под его же отправленным текстом.
    expect(order()).toEqual(["c2", "c1"]);
    expect(pendingMark("c2")).toBe("true");
    expect(within(row("c2")).getByText("Hello Marcus")).toBeTruthy();
    expect(row("c2").textContent).toMatch(TIME_OF_DAY);
    // Числа непрочитанного отправка не завела: ни единицы, ни нуля. Сервер его
    // не называл, а своё сообщение непрочитанным не бывает — `0` здесь был бы
    // утверждением, которого никто не делал.
    expect(unread("c2")).toBeNull();

    await act(async () => {
      answer.resolve(replyOf("c2", 1, calls.sent[0].clientMessageId, "Hello Marcus"));
    });

    // Ответ снимает **пометку**, а не строку: превью на месте, но оно уже
    // подтверждено — и порядок от снятия пометки не поехал.
    expect(pendingMark("c2")).toBeNull();
    expect(within(row("c2")).getByText("Hello Marcus")).toBeTruthy();
    expect(order()).toEqual(["c2", "c1"]);
  });

  it("число непрочитанного отправка не двигает: 3 остаётся 3", async () => {
    // Строка **с числом** — вторая точка того же утверждения, и она не
    // повторение первой: там числа не было вовсе, и «отправка не тронула число»
    // на ней неотличимо от «отправка сбросила его в ноль». Здесь сброс виден.
    const answer = deferred<Message>();
    const { container, calls, unread } = setup({
      conversations: [conversationOf({ ...ANNA, unreadCount: 3 })],
      sendMessage: () => answer.promise,
    });

    const row = (id: string) =>
      container.querySelector(`[data-conversation-id="${id}"]`) as HTMLElement;
    const pendingMark = () =>
      row("c1").querySelector("[data-preview-pending]")?.getAttribute("data-preview-pending") ??
      null;

    await waitFor(() =>
      expect(container.querySelector("[data-composer-input]")).toBeTruthy(),
    );
    expect(unread("c1")).toBe("3");

    fireEvent.change(container.querySelector("[data-composer-input]")!, {
      target: { value: "Hello Anna" },
    });
    fireEvent.click(container.querySelector("[data-composer-send]")!);

    await waitFor(() => expect(calls.sent).toHaveLength(1));

    // Своё сообщение непрочитанным не бывает: число остаётся тем, каким его
    // назвал сервер, — не растёт и не гаснет.
    expect(pendingMark()).toBe("true");
    expect(unread("c1")).toBe("3");

    await act(async () => {
      answer.resolve(replyOf("c1", 1, calls.sent[0].clientMessageId, "Hello Anna"));
    });

    expect(pendingMark()).toBeNull();
    expect(unread("c1")).toBe("3");
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

/**
 * Идентификатор подтверждённого сообщения — **один** на ответ и на событие.
 *
 * Обе дороги описывают одну и ту же запись, и разошедшиеся здесь
 * идентификаторы сделали бы «событие сняло запись» неотличимым от «событие
 * завело вторую». Префикс отличается от `messageOf` намеренно: снимок хвоста и
 * подтверждённое — разные сообщения, и совпади они, `applyMessage` назвал бы
 * применение `duplicate` по совсем другой причине.
 */
function confirmedIdOf(seq: number): string {
  return `22222222-2222-2222-2222-2222222222${String(seq).padStart(2, "0")}`;
}

/**
 * Ответ сервера на отправку — **тем же контрактом**, каким отвечает `POST`.
 *
 * Собирается `MessageFromJSON`, а не литералом: литерал типа `Message` разошёлся
 * бы с контрактом молча, а стенд на то и стенд, чтобы идти тем же путём, что
 * сервер, — разбор ответа обязан быть производственным.
 *
 * `client_message_id` кладётся **из запроса**, а не выдумывается: сведение идёт
 * именно им (`confirmedClientIds`), и тест, подставивший сюда своё значение,
 * проверял бы совпадение с самим собой.
 */
function replyOf(
  conversationId: string,
  seq: number,
  clientMessageId: string,
  text: string,
): Message {
  return MessageFromJSON({
    message_id: confirmedIdOf(seq),
    conversation_id: conversationId,
    seq,
    sender_id: VIEWER_ID,
    client_message_id: clientMessageId,
    type: "text",
    payload: { text },
    created_at: "2026-09-27T14:22:31Z",
  });
}

/**
 * Событие `message.created` о **своём** сообщении — путь, которым приходит
 * подтверждение, когда ответ `POST` потерян.
 *
 * Отправитель здесь зритель, а не собеседник: событие о чужом сообщении
 * проверяло бы приём (это делает `publishMessage` выше), а запись очереди к
 * чужому сообщению не относится вовсе. Тождество кладётся тем же, что ушло в
 * запрос: свести событие с записью **этим** полем и есть предмет проверки.
 */
async function publishOwnMessage(
  fake: ReturnType<typeof givenFakeCentrifuge>,
  seq: number,
  clientMessageId: string,
  text: string,
) {
  await act(async () => {
    fake.clientHandlers["publication"]?.({
      channel: ANNA_CHANNEL,
      data: {
        type: "message.created",
        message_id: confirmedIdOf(seq),
        seq,
        sender_id: VIEWER_ID,
        client_message_id: clientMessageId,
        payload: { text },
      },
    });
  });
}

/**
 * Сведение отправленного: ответ `POST`, событие канала и разрыв номеров (D6).
 *
 * Предмет — **ровно одна** запись об отправленном: одна подтверждённая и ни
 * одной висящей. Держат это **две** точки, и обе здесь названы: ответ `POST`
 * идёт тем же путём, что история и realtime (`adaptMessage` →
 * `acceptPublication`), а снимает запись не ответ, а **появление
 * подтверждённого в ленте** (`confirmedClientIds` → `settle`). Красный на одной
 * точке не закрывает вторую, поэтому порядков три, а не один.
 *
 * Транспорт везде **отложенный промис**, а не немедленный ответ: при мгновенном
 * резолве запись успела бы появиться и сняться в одной микрозадаче, и проверка
 * «сначала висит, потом снята» стала бы гонкой. Здесь окно между нажатием и
 * ответом открывается явно, и состояние записи в нём видно.
 */
describe("сведение отправленного: ответ, событие и разрыв номеров", () => {
  it("ответ доходит до ленты и снимает запись — realtime молчит", async () => {
    // Порядок (i) целиком без единого события. Мутация «снятие только по
    // событию» краснит ровно здесь: события не будет вовсе, а сообщение уже
    // отправлено — запись провисела бы вечно.
    const answer = deferred<Message>();
    const { container, calls } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(1)])),
      sendMessage: () => answer.promise,
    });

    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());

    fireEvent.change(container.querySelector("[data-composer-input]")!, {
      target: { value: "Hello Anna" },
    });
    fireEvent.click(container.querySelector("[data-composer-send]")!);

    // Запись на месте, ответа ещё нет — состояние «идёт попытка», а не «ушло».
    await waitFor(() =>
      expect(
        container.querySelector("[data-pending-client-id]")?.getAttribute("data-message-state"),
      ).toBe("sending"),
    );
    expect(calls.sent).toHaveLength(1);

    await act(async () => {
      answer.resolve(replyOf(ANNA.id, 2, calls.sent[0].clientMessageId, "Hello Anna"));
    });

    // Запись снята **появлением подтверждённого в ленте**: ни события, ни
    // второго пути слияния здесь нет.
    await waitFor(() =>
      expect(container.querySelector("[data-pending-client-id]")).toBeNull(),
    );

    expect(container.querySelectorAll(`[data-message-id="${confirmedIdOf(2)}"]`)).toHaveLength(1);
    expect(container.querySelectorAll("[data-message-seq]")).toHaveLength(2);
    // `toContain`, не `toBe`: строка теперь несёт и `MessageBubble` — время и
    // статус доставки сидят в том же узле. Предмет проверки не изменился —
    // текст сообщения на месте, а не стёрт слиянием.
    expect(container.querySelector('[data-message-seq="2"]')?.textContent).toContain("Hello Anna");
  });

  it("событие приходит раньше ответа: запись снята, и подтверждённая — одна", async () => {
    // Порядок (ii). Утверждение точное: запись обязана сняться **событием**, не
    // дождавшись ответа, — ответ может не прийти вовсе, а сообщение уже создано.
    // Мутация «снятие только по ответу» краснит здесь: до ответа запись стояла
    // бы рядом с подтверждённой, то есть об одном сообщении было бы две строки.
    const answer = deferred<Message>();
    const { container, calls, fake } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(1)])),
      sendMessage: () => answer.promise,
    });

    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());

    fireEvent.change(container.querySelector("[data-composer-input]")!, {
      target: { value: "Hello Anna" },
    });
    fireEvent.click(container.querySelector("[data-composer-send]")!);

    await waitFor(() => expect(calls.sent).toHaveLength(1));

    await publishOwnMessage(fake, 2, calls.sent[0].clientMessageId, "Hello Anna");

    await waitFor(() =>
      expect(container.querySelector("[data-pending-client-id]")).toBeNull(),
    );
    expect(container.querySelectorAll("[data-message-seq]")).toHaveLength(2);

    // Ответ приходит **вторым** — и второго сообщения не заводит: запись с тем
    // же `message_id` уже лежит, и `applyMessage` называет это `duplicate`.
    await act(async () => {
      answer.resolve(replyOf(ANNA.id, 2, calls.sent[0].clientMessageId, "Hello Anna"));
    });

    expect(container.querySelectorAll("[data-message-seq]")).toHaveLength(2);
    // `toContain` по той же причине, что выше: узел несёт ещё и статус
    // доставки от `MessageBubble`, а не только текст сообщения.
    expect(container.querySelector('[data-message-seq="2"]')?.textContent).toContain("Hello Anna");
  });

  it("ответ потерян, событие доехало: запись снята, дубля нет", async () => {
    // Порядок (iii): транспорта нет вовсе — `sendMessage` не назван, и попытка
    // отвергается (исход по умолчанию в стенде). Так и выглядит потерянный
    // ответ: для клиента он неотличим от «не дошло», и запись уходит в повтор.
    // Мутация «снятие только по ответу» краснит: ответа не было и не будет.
    const { container, calls, fake } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(1)])),
    });

    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());

    fireEvent.change(container.querySelector("[data-composer-input]")!, {
      target: { value: "Hello Anna" },
    });
    fireEvent.click(container.querySelector("[data-composer-send]")!);

    // Запись не снята и повторяется — исход отказа, а не молчание.
    await waitFor(() =>
      expect(
        container.querySelector("[data-pending-client-id]")?.getAttribute("data-message-state"),
      ).toBe("retrying"),
    );
    expect(calls.sent).toHaveLength(1);

    await publishOwnMessage(fake, 2, calls.sent[0].clientMessageId, "Hello Anna");

    await waitFor(() =>
      expect(container.querySelector("[data-pending-client-id]")).toBeNull(),
    );
    // Одна подтверждённая и ни одной висящей — ровно то, ради чего тождество
    // отправки доезжает до события (D3).
    expect(container.querySelectorAll("[data-message-seq]")).toHaveLength(2);
  });

  it("ответ с номером через пропуск: запись остаётся, а лента уходит в догрузку", async () => {
    // Разрыв номеров: граница 1, ответ принёс 3. `applyMessage` называет это
    // `gap` и сообщение в ленту **не** кладёт (`G3-006`) — значит снимать
    // нечего, и запись обязана дожить до конца круга: сними её по факту ответа,
    // и на экране не осталось бы **ни одной** записи об этом сообщении, пока
    // идёт REST (D6).
    //
    // Здесь же краснит и мутация «второй путь слияния»: ответ, положенный мимо
    // `applyMessage`, разрыва не увидел бы вовсе, и `calls.pages` остался бы пуст.
    const answer = deferred<Message>();
    const { container, calls } = setup({
      conversations: [ANNA],
      tail: () => Promise.resolve(tailOf([messageOf(1)])),
      sendMessage: () => answer.promise,
    });

    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());

    fireEvent.change(container.querySelector("[data-composer-input]")!, {
      target: { value: "Hello Anna" },
    });
    fireEvent.click(container.querySelector("[data-composer-send]")!);

    await waitFor(() => expect(calls.sent).toHaveLength(1));

    await act(async () => {
      answer.resolve(replyOf(ANNA.id, 3, calls.sent[0].clientMessageId, "Hello Anna"));
    });

    // Догрузка пошла — это и есть признак того, что разрыв распознан.
    await waitFor(() => expect(calls.pages.length).toBeGreaterThan(0));

    // А запись на месте: подтверждённого в ленте нет, снимать нечего.
    expect(container.querySelector("[data-pending-client-id]")).toBeTruthy();
    expect(container.querySelector('[data-message-seq="3"]')).toBeNull();
    expect(container.querySelectorAll("[data-message-seq]")).toHaveLength(1);
  });
});

describe("звук нового сообщения", () => {
  it("играет на чужую публикацию и молчит на своё же эхо", async () => {
    installObserverForJsdom();
    try {
      vi.mocked(playIncomingMessageSound).mockClear();

      const { fake } = setup({
        conversations: [ANNA],
        tail: () => Promise.resolve(tailOf([messageOf(1)])),
      });

      await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());
      expect(playIncomingMessageSound).not.toHaveBeenCalled();

      await publishMessage(fake, 2);
      expect(playIncomingMessageSound).toHaveBeenCalledTimes(1);

      // Своё эхо — то же самое сообщение, каким его увидело бы **другое**
      // устройство того же человека: звук по нему не звонит, отправка уже
      // названа собственным действием (D13).
      await publishOwnMessage(fake, 3, "cm-echo", "own");
      expect(playIncomingMessageSound).toHaveBeenCalledTimes(1);
    } finally {
      restoreIntersectionObserver();
    }
  });
});
