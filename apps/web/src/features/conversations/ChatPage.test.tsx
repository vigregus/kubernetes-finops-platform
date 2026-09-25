import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ConversationListPage } from "../../api/generated";
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

interface SetupOptions {
  readonly conversations: Conversation[];
  readonly tail?: (conversationId: string) => Promise<TailPage>;
  readonly loadPage?: HistorySource["loadPage"];
  /** Ответ сверки. По умолчанию — пустая страница: сверка не обязана ничего менять. */
  readonly refresh?: () => Promise<ConversationListPage>;
}

function setup({ conversations, tail, loadPage, refresh }: SetupOptions) {
  const fake = givenFakeCentrifuge();
  const tickets = givenTicketIssuer();
  const calls = { tails: [] as string[], pages: [] as SyncPage[], refreshes: 0 };

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
    return emptyPage([
      {
        conversationId,
        type: "direct",
        participants: [
          { userId: VIEWER_ID, displayName: "David Miller" },
          { userId: "user-anna", displayName: "Anna Petrova" },
        ],
        createdAt: "2026-09-01T00:00:00Z",
        unreadCount,
      },
    ]);
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
