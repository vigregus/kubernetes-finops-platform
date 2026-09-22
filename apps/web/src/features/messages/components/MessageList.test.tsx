// Детекторы наблюдаемой поверхности ленты (B8).
//
// Проверяется не оформление, а три вещи, по которым приёмка читает состояние:
// применённая граница, тождество сообщения и его номер в беседе. Ни одну из них
// нельзя вывести из разметки: `React key` в DOM не попадает, порядок массива
// виден только коду, а «граница неизвестна» и «граница равна нулю» — разные
// состояния, различимые исключительно наличием атрибута.

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../../../shared/lib/types";
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
function renderList(appliedThroughSeq: number | null, messages: ChatMessage[] = []) {
  const view = render(
    <MessageList
      messages={messages}
      conversationName="Anna Petrova"
      appliedThroughSeq={appliedThroughSeq}
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
});
