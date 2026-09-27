// Детекторы наблюдаемой поверхности ленты (B8).
//
// Проверяется не оформление, а три вещи, по которым приёмка читает состояние:
// применённая граница, тождество сообщения и его номер в беседе. Ни одну из них
// нельзя вывести из разметки: `React key` в DOM не попадает, порядок массива
// виден только коду, а «граница неизвестна» и «граница равна нулю» — разные
// состояния, различимые исключительно наличием атрибута.

import { render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  FakeIntersectionObserver,
  installObserverForJsdom,
  restoreIntersectionObserver,
} from "../../../test-support/intersectionObserver";
import type { ChatMessage, MessageDeliveryState } from "../../../shared/lib/types";
import { MessageList } from "./MessageList";

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

/**
 * Носитель границы — корень ленты. Ищется он не по тестовому идентификатору,
 * а по самому производственному атрибуту: `data-testid` был бы швом, которого
 * в проде нет, и тест проверял бы разметку, которой у приёмки не будет.
 */
function renderList(
  appliedThroughSeq: number | null,
  messages: ChatMessage[] = [],
  onVisibleThroughSeq?: (seq: number) => void,
) {
  const view = render(
    <MessageList
      messages={messages}
      conversationName="Anna Petrova"
      appliedThroughSeq={appliedThroughSeq}
      onVisibleThroughSeq={onVisibleThroughSeq}
    />,
  );

  const root = view.container.firstElementChild;

  return {
    ...view,
    boundary: root?.getAttribute("data-applied-through-seq") ?? null,
    rows: [...view.container.querySelectorAll("[data-message-seq]")],
  };
}

describe("применённая граница видна в разметке", () => {
  it("до снимка граница неизвестна — атрибута нет", () => {
    // Не «равен нулю» и не «пустая строка»: атрибут со значением «неизвестно»
    // был бы тем же нулём, только хуже — приёмке пришлось бы знать про него
    // отдельно. Отсутствие атрибута читается однозначно.
    const { boundary } = renderList(null);

    expect(boundary).toBeNull();
  });

  it("после пустого снимка граница равна нулю", () => {
    // Пустой снимок — успех, а не отсутствие ответа: следующий законный номер
    // в беседе — единица, и граница «применено по нулевое» это факт (B23).
    const { boundary } = renderList(0);

    expect(boundary).toBe("0");
  });

  it("непустая граница видна целиком", () => {
    // Край, найденный красным: реализация «атрибут есть только у нуля»
    // проходит оба теста выше и при этом теряет границу там, где она
    // непустая, — то есть ровно там, где приёмка читает сходимость
    // (`data-applied-through-seq == S + 101`). Ноль и число проверяются
    // раздельно, потому что это два разных значения одного поля.
    const { boundary } = renderList(102, [messageOf(101), messageOf(102)]);

    expect(boundary).toBe("102");
  });
});

describe("сообщение несёт и тождество, и номер", () => {
  it("каждое сообщение несёт data-message-id и data-message-seq", () => {
    // Оба атрибута нужны вместе и по отдельности бесполезны: по `id`
    // доказывается «ровно один раз», по `seq` — «строго возрастает на 1».
    // Идентификаторы уникальны, но не упорядочены, поэтому одним `id`
    // последовательность недоказуема.
    const { rows } = renderList(102, [messageOf(101), messageOf(102)]);

    expect(rows.map((row) => row.getAttribute("data-message-seq"))).toEqual(["101", "102"]);
    expect(rows.map((row) => row.getAttribute("data-message-id"))).toEqual([
      messageOf(101).id,
      messageOf(102).id,
    ]);
  });

  it("пустая лента называет беседу по имени", () => {
    const { container } = renderList(0);

    expect(container.textContent).toContain("No messages yet. Say hello to Anna Petrova.");
  });
});

describe("пустота — утверждение о снимке, а не о длине массива", () => {
  // Различие то же, что между `appliedThroughSeq === null` и `=== 0`, и стоит
  // оно ровно того же: пустой массив до снимка и пустой массив после пустого
  // снимка — разные факты. Первый значит «сервер ещё не отвечал», второй —
  // «сервер ответил, и в беседе ноль сообщений». Показывать в обоих случаях
  // «No messages yet. Say hello to …» значит выводить утверждение о беседе из
  // молчания сервера — тот же класс, что «Active now» и «Last seen» в шапке.

  it("до снимка лента не утверждает, что сообщений нет", () => {
    const { container } = renderList(null);

    expect(container.textContent).not.toContain("No messages yet");
  });

  it("до снимка лента называет загрузку", () => {
    // Не «ничего»: пустая панель без объяснения неотличима от сломанной
    // разметки, а `ConnectionStatusLine` в этот момент говорит о соединении,
    // а не об истории.
    const { container } = renderList(null);

    expect(container.textContent).toContain("Loading messages…");
  });

  it("после пустого снимка то же самое пустое состояние возвращается", () => {
    // Обратная сторона: правка не имеет права убрать утверждение там, где оно
    // правдиво. Пустой снимок — факт, и «No messages yet» ему соответствует.
    const { container } = renderList(0);

    expect(container.textContent).toContain("No messages yet. Say hello to Anna Petrova.");
  });
})

// Наблюдатель видимости строк (D13) — «прочитано» берётся по строке, видимой
// **целиком**, и это единственный источник второго числа квитанции:
// `document.visibilityState` говорит о вкладке, а не о сообщении, и лента
// `1…500` при окне `450…470` сделала бы `read_seq = 500` утверждением, которого
// человек не делал.
//
// Наблюдателя в jsdom нет вовсе, поэтому его двойник подставляется здесь, а не
// берётся готовым: тогда измеряются и опции, с которыми он заведён, и разбор
// его ответов — то, что настоящий браузер подтвердит уже на стенде.
//
// Двойник общий с панелью (`test-support/intersectionObserver.ts`): `ChatPage`
// заводит того же наблюдателя через ту же ленту, и второй экземпляр разошёлся
// бы с первым молча.

afterEach(() => {
  restoreIntersectionObserver();
});

/**
 * Лента, отрисованная при подставленном наблюдателе.
 *
 * Двойник ставится **до** рендера, а не после: компонент заводит наблюдателя
 * эффектом, и подмена задним числом не дала бы ни одного экземпляра — тест
 * проверял бы пустоту. `installObserverForJsdom` обнуляет список экземпляров,
 * поэтому `latest` здесь — наблюдатель именно этого рендера.
 */
function renderObserved(
  appliedThroughSeq: number | null,
  messages: ChatMessage[],
  onVisibleThroughSeq: (seq: number) => void,
) {
  installObserverForJsdom();
  const view = renderList(appliedThroughSeq, messages, onVisibleThroughSeq);

  return { ...view, observer: FakeIntersectionObserver.latest };
}

describe("видимость строк — «прочитано» по целиком видимой строке", () => {
  it("наблюдателя в окружении нет — лента не падает и колбэк не зовётся", () => {
    // jsdom наблюдателя не имеет вовсе, и это не «плохое окружение», а
    // названная цена (D13): компонент обязан не бросить. Квитанция в этом
    // случае просто не отправляется — и это честнее, чем падение панели.
    const seen: number[] = [];

    expect(() =>
      renderList(2, [messageOf(1), messageOf(2)], (seq) => seen.push(seq)),
    ).not.toThrow();
    expect(seen).toEqual([]);
  });

  it("наблюдатель заводится на контейнер ленты и требует полного пересечения", () => {
    // `root` — сам контейнер: без него отсчёт шёл бы от окна браузера, а лента
    // живёт в своей прокрутке. `threshold: 1.0` — «видна целиком», и опции здесь
    // не украшение: с нулём частично показанная строка объявлялась бы прочитанной.
    const seen: number[] = [];
    const { observer } = renderObserved(2, [messageOf(1), messageOf(2)], (seq) => seen.push(seq));

    expect(observer.options?.threshold).toBe(1.0);
    expect(observer.options?.root).toBeInstanceOf(HTMLElement);
    expect(observer.targets.size).toBe(2);
  });

  it("целиком видимая строка двигает границу до своего номера", () => {
    const seen: number[] = [];
    const { rows, observer } = renderObserved(
      2,
      [messageOf(1), messageOf(2)],
      (seq) => seen.push(seq),
    );

    observer.intersect(rows[0]!, 1);
    observer.intersect(rows[1]!, 1);

    // Максимум из видимых, а не первый и не последний по порядку прихода.
    expect(seen).toEqual([1, 2]);
  });

  it("частично видимая строка границу не двигает", () => {
    // Это и есть запрет D13: «прочитано» не имеет права уехать за «показано».
    // Строка, наполовину ушедшая под сгиб, человеком не прочитана, и объявить
    // её прочитанной значит утверждать о собеседнике то, чего он не делал.
    const seen: number[] = [];
    const { rows, observer } = renderObserved(
      3,
      [messageOf(1), messageOf(2), messageOf(3)],
      (seq) => seen.push(seq),
    );

    observer.intersect(rows[0]!, 0.5);
    observer.intersect(rows[1]!, 1);

    expect(seen).toEqual([2]);
  });

  it("та же граница второй раз не сообщается", () => {
    // Повтор — это лишний запрос квитанции на каждый кадр прокрутки. Движение
    // вперёд — новость, повтор прежней границы — нет.
    const seen: number[] = [];
    const { rows, observer } = renderObserved(1, [messageOf(1)], (seq) => seen.push(seq));

    observer.intersect(rows[0]!, 1);
    observer.intersect(rows[0]!, 1);

    expect(seen).toEqual([1]);
  });

  it("ушедшая из окна строка границу назад не двигает", () => {
    // Обратная сторона того же правила: номер квитанции идёт только вперёд
    // (D6а). Прокрутка вверх — не «раз-прочтение».
    const seen: number[] = [];
    const { rows, observer } = renderObserved(
      2,
      [messageOf(1), messageOf(2)],
      (seq) => seen.push(seq),
    );

    observer.intersect(rows[1]!, 1);
    observer.intersect(rows[0]!, 1);
    observer.intersect(rows[1]!, 0);

    expect(seen).toEqual([2]);
  })
})

describe("состояние доставки видно в разметке", () => {
  function mineWith(state: MessageDeliveryState): ChatMessage {
    return { ...messageOf(1), deliveryState: state };
  }

  it("своё сообщение несёт состояние квитанции", () => {
    const { rows } = renderList(1, [mineWith("read")]);

    expect(rows[0]!.getAttribute("data-message-state")).toBe("read");
  })

  it("чужое сообщение состояния не несёт: это факт о моём сообщении", () => {
    // Состояние доставки — утверждение о том, что стало с **моим** письмом.
    // У чужого сообщения такого состояния нет: оно уже здесь, и «доставлено»
    // про него сказало бы нечто, чего собеседник не делал.
    const theirs: ChatMessage = { ...messageOf(1), authorId: "user-anna", deliveryState: "read" };
    const { rows } = renderList(1, [theirs]);

    expect(rows[0]!.getAttribute("data-message-state")).toBeNull();
  })

  it("состояние без квитанции — отсутствие атрибута, а не пустое слово", () => {
    // `undefined` у поля значит «сервер о доставке не сообщал». Написать здесь
    // `data-message-state=""` значило бы завести в разметке третье состояние,
    // которого приёмке пришлось бы знать отдельно.
    const { rows } = renderList(1, [messageOf(1)]);

    expect(rows[0]!.getAttribute("data-message-state")).toBeNull();
  })
});
