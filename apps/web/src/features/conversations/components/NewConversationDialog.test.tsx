import { act, fireEvent, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiProblem } from "../../../api/problems";
import {
  ConversationFromJSON,
  UserLookupFromJSON,
  type Conversation as ConversationDto,
} from "../../../api/generated";
import { NewConversationDialog } from "./NewConversationDialog";
import type { CreateConversation, SearchUser } from "./NewConversationDialog";

/**
 * Диалог создания беседы: три состояния и то, чего в них **нельзя** сделать.
 *
 * Предмет здесь — не форма компонента, а запрет, ради которого он заведён
 * (`D2`): беседа создаётся **по подтверждённому человеку**, а не по набранной
 * строке. Поэтому первое утверждение каждого теста — про то, ушёл ли `POST`, а
 * второе — про то, что видит человек. Состояние диалога читается по
 * `data-dialog-state` (одно слово), а не по тексту: текст меняется от правки
 * подписи, а состояние — предмет.
 *
 * Три состояния названы планом среза: «ищу», «не найден», «найден, подтвердите».
 * Сверх них закрыты два исхода, которые легко слить с «не найден» и на этом
 * потерять смысл: `429` (поиск **не состоялся**) и `429` без `Retry-After`
 * (сервер не сказал, когда повторять, — и нуля вместо молчания не появляется).
 */

const VIEWER_ID = "user-viewer";
const MARIA_ID = "user-maria";

/** Отложенный ответ: им предъявляется состояние «ищу», которое иначе неуловимо. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });

  return { promise, resolve };
}

/**
 * Найденный человек — **по контракту**, а не литералом модели.
 *
 * Литерал типа `UserLookup` разошёлся бы с проводом молча, и тест, подавший в
 * диалог `{userId, displayName}` там, где сервер отвечает `user_id`, проверял бы
 * согласие с самим собой. `UserLookupFromJSON` — та же функция, которой ответ
 * разбирает клиент.
 */
function found(userId: string, displayName: string) {
  return UserLookupFromJSON({ user_id: userId, display_name: displayName });
}

/** Ответ `POST /conversations` — тем же контрактом, каким отвечает маршрут. */
const CREATED: ConversationDto = ConversationFromJSON({
  conversation_id: "c9",
  type: "direct",
  participants: [
    { user_id: VIEWER_ID, display_name: "David Miller" },
    { user_id: MARIA_ID, display_name: "Maria Petrova" },
  ],
  created_at: "2026-09-27T14:22:31Z",
});

/**
 * Стенд: **записи** вызовов, а не подмена состояния.
 *
 * Оба транспорта по умолчанию отказывают: тест, не назвавший исход, не должен
 * получать удачный поиск в подарок — «нашёлся кто-то безымянный» и «беседа
 * создалась сама» это состояния, которых он не называл.
 */
function setup(options: {
  readonly searchUser: SearchUser;
  readonly createConversation?: CreateConversation;
}) {
  const calls = {
    searches: [] as string[],
    created: [] as string[],
    opened: [] as ConversationDto[],
    closed: 0,
  };

  const view = render(
    <NewConversationDialog
      searchUser={(email) => {
        calls.searches.push(email);

        return options.searchUser(email);
      }}
      createConversation={(participantId) => {
        calls.created.push(participantId);

        return options.createConversation
          ? options.createConversation(participantId)
          : Promise.reject(new Error("эти тесты не создают бесед"));
      }}
      onCreated={(conversation) => calls.opened.push(conversation)}
      onClose={() => {
        calls.closed += 1;
      }}
    />,
  );

  const dialog = () => view.container.querySelector('[role="dialog"]');
  const email = () => view.container.querySelector("[data-new-conversation-email]") as HTMLInputElement;
  const start = () =>
    view.container.querySelector("[data-new-conversation-start]") as HTMLButtonElement;

  return {
    ...view,
    calls,
    /** Состояние диалога — одним словом, как его читает приёмка. */
    state: () => dialog()?.getAttribute("data-dialog-state") ?? null,
    /** Строка состояния: по ней видно, что именно сказано человеку. */
    status: () =>
      view.container.querySelector("[data-new-conversation-status]")?.textContent ?? null,
    start,
    /** Кого нашли — тем же атрибутом, которым это увидит приёмочный сценарий. */
    foundUserId: () => start().getAttribute("data-found-user-id"),
    typeAddress: (value: string) => {
      fireEvent.change(email(), { target: { value } });
    },
    pressSearch: () => {
      fireEvent.click(
        view.container.querySelector("[data-new-conversation-search]") as HTMLButtonElement,
      );
    },
    pressStart: () => {
      fireEvent.click(start());
    },
  };
}

/** Поиск, отвечающий найденным человеком — общий вход трёх тестов ниже. */
const findsMaria: SearchUser = async () => found(MARIA_ID, "Maria Petrova");

describe("диалог создания беседы: состояние одно, и оно наблюдаемо", () => {
  it("«ищу» → «найден, подтвердите»: беседа создаётся найденным, а не набранным", async () => {
    // Мутация (а) среза — «диалог создаёт беседу до подтверждения» — краснит
    // **дважды**: на `calls.created` сразу после поиска (там ещё пусто) и на
    // самом `POST`, ушедшем на набранный, но не подтверждённый адрес.
    const lookup = deferred<ReturnType<typeof found>>();
    const stand = setup({ searchUser: () => lookup.promise, createConversation: async () => CREATED });

    stand.typeAddress("maria@example.com");
    stand.pressSearch();

    // «Ищу» — настоящее состояние, а не мгновение: ответ ещё в пути, и кнопка
    // подтверждения при нём не активна (ещё нечего подтверждать).
    expect(stand.state()).toBe("searching");
    expect(stand.status()).toBe("Searching…");
    expect(stand.start().disabled).toBe(true);

    await act(async () => {
      lookup.resolve(found(MARIA_ID, "Maria Petrova"));
    });

    // Подтверждается **человек**, а не строка: в подписи его имя, в атрибуте —
    // его идентификатор из ответа поиска.
    expect(stand.state()).toBe("found");
    expect(stand.status()).toBe("Start chat with Maria Petrova?");
    expect(stand.foundUserId()).toBe(MARIA_ID);
    expect(stand.start().disabled).toBe(false);

    // До подтверждения не создано ничего — и это то, ради чего диалог заведён.
    expect(stand.calls.created).toEqual([]);

    await act(async () => {
      stand.pressStart();
    });

    // Ушёл идентификатор из ответа поиска, а не набранный адрес: поля
    // `participant_email` в контракте нет вовсе (D2).
    expect(stand.calls.created).toEqual([MARIA_ID]);
    expect(stand.calls.opened).toEqual([CREATED]);
    // Диалог закрывается **сам**, по факту создания: открытие беседы — работа
    // `ChatPage`, а не диалога.
    expect(stand.state()).toBe("creating");
  });

  it("404 — «никто не найден», и подтверждение при нём не активно", async () => {
    // `404` здесь — **ответ**, а не поломка: сервер отвечает так и на свободный
    // адрес, и на стёртую запись, и на заблокированного (D1). Мутация (б) среза —
    // «кнопка подтверждения активна при 404» — краснит на `disabled` ниже, и
    // только на нём: нажатие по неактивной кнопке React не доставляет вовсе, так
    // что `calls.created` остался бы пуст и при снятом запрете.
    const stand = setup({
      searchUser: async () => {
        throw new ApiProblem({ status: 404, title: "Ресурс не найден" });
      },
    });

    stand.typeAddress("nobody@example.com");
    stand.pressSearch();

    await act(async () => {});

    expect(stand.state()).toBe("not-found");
    expect(stand.status()).toBe("No one found at this address");
    expect(stand.start().disabled).toBe(true);

    await act(async () => {
      stand.pressStart();
    });

    expect(stand.calls.created).toEqual([]);
    expect(stand.calls.opened).toEqual([]);
  });

  it("429 показан ожиданием с числом из Retry-After, а не «никого нет»", async () => {
    // Мутация (в) среза — «429 показывается как „не найден“» — краснит здесь, и
    // это не придирка к формулировке: при 429 поиск **не состоялся**, и «никого
    // нет по этому адресу» было бы утверждением о человеке, которого никто не
    // делал. Число берётся из заголовка — оно посчитано сервером, и без него
    // человеку остаётся повторять вслепую.
    const stand = setup({
      searchUser: async () => {
        throw new ApiProblem({
          status: 429,
          title: "Слишком часто",
          retryAfterSeconds: 7,
        });
      },
    });

    stand.typeAddress("maria@example.com");
    stand.pressSearch();

    await act(async () => {});

    expect(stand.state()).toBe("rate-limited");
    expect(stand.status()).toBe("Too many searches. Try again in 7 seconds.");
    expect(stand.status()).not.toContain("No one found");
    expect(stand.start().disabled).toBe(true);
    expect(stand.calls.created).toEqual([]);
  });

  it("429 без Retry-After не выдумывает нуля секунд", async () => {
    // Числа может не быть: заголовок необязателен, и «сервер не сказал, когда
    // повторять» — не то же, что «сервер сказал: немедленно». Подстановка нуля
    // пригласила бы человека повторить сейчас же, то есть в тот самый момент,
    // когда сервер этого не разрешил.
    const stand = setup({
      searchUser: async () => {
        throw new ApiProblem({ status: 429, title: "Слишком часто" });
      },
    });

    stand.typeAddress("maria@example.com");
    stand.pressSearch();

    await act(async () => {});

    expect(stand.state()).toBe("rate-limited");
    expect(stand.status()).toBe("Too many searches. Try again later.");
    expect(stand.status()).not.toContain("0 seconds");
  });

  it("незнакомый отказ поиска назван отказом, а не «никого нет»", async () => {
    // Отказ сети и `5xx` — не ответ о человеке. Свести их к «не найден» значило
    // бы выдать нашу неудачу за утверждение о мире — тот же класс, что у 429.
    const stand = setup({
      searchUser: async () => {
        throw new Error("сеть");
      },
    });

    stand.typeAddress("maria@example.com");
    stand.pressSearch();

    await act(async () => {});

    expect(stand.state()).toBe("failed");
    expect(stand.status()).toBe("Search failed. Try again.");
  });

  it("пустой адрес не тратит попытку: запрос без предмета сервер отверг бы и сам", async () => {
    // Пробелы — не адрес: `GET /users?email=` без значения сервер отвечает
    // `400`, и попытка лимита ушла бы на запрос, которого человек не делал.
    const stand = setup({ searchUser: findsMaria });

    stand.typeAddress("   ");
    stand.pressSearch();

    await act(async () => {});

    expect(stand.calls.searches).toEqual([]);
    expect(stand.state()).toBe("idle");
  });
});
