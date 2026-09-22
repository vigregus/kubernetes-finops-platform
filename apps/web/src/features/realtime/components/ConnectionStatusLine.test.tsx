// Детектор строки состояния: состояние названо словами, и слова разные.
//
// Проверяется не оформление, а запрет `03-v1-scope.md:196` — «пока идёт
// синхронизация, нельзя показывать старое состояние как актуальное». Запрет
// адресован человеку, а не приёмке, поэтому его надо предъявить на поверхности,
// которую видит человек: `SYNCING` обязан быть назван догрузкой, а не «всё
// хорошо». Само по себе наличие `data-connection-state` этого не даёт: атрибут
// читает приёмка, а строку читает человек, и разойтись они могут молча.

import { render, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ConnectionState } from "../../../shared/lib/types";
import { ConnectionStatusLine } from "./ConnectionStatusLine";

const ALL_STATES: readonly ConnectionState[] = [
  "connected",
  "connecting",
  "disconnected",
  "degraded",
  "syncing",
];

/**
 * Текст строки — тем же путём, каким его прочитает человек.
 *
 * Запрос сужен до контейнера рендера, а не идёт по `document.body`: запросы
 * `render` привязаны к `body`, а несколько рендеров подряд туда дописываются —
 * без сужения второй вызов нашёл бы два `role="status"` и упал бы на разметке
 * теста, а не на дефекте компонента. Рендер снимается сразу после чтения, чтобы
 * следующий вызов начинался с чистого документа.
 */
function labelOf(state: ConnectionState): string {
  const view = render(<ConnectionStatusLine state={state} />);
  const text = within(view.container).getByRole("status").textContent ?? "";
  view.unmount();

  return text;
}

describe("строка называет состояние словами", () => {
  it("при syncing называет догрузку, а не «состояние в порядке»", () => {
    // Слова проверяются литералом, а не импортированным `LABEL`: импорт сделал
    // бы тест тождеством — он сравнивал бы константу с самой собой и зеленел бы
    // при любом её значении, включая «Connected».
    expect(labelOf("syncing")).toBe("Catching up on missed messages…");
  });

  it("пять состояний — пять разных слов", () => {
    // Ловит слияние состояний, а не только пару `syncing`/`connected`: если
    // `LABEL` потеряет ключ, React отрисует пустоту, и это тоже видно здесь.
    // Пустая строка была бы худшим исходом из возможных — она «не врёт» ровно
    // настолько, насколько молчит.
    const labels = ALL_STATES.map(labelOf);

    expect(labels.filter((label) => label.length === 0)).toEqual([]);
    expect(new Set(labels).size).toBe(ALL_STATES.length);
  });

  it("строка объявлена живым регионом", () => {
    // Смена состояния происходит без действия человека, и объявить её
    // экранному диктору дешевле, чем объяснять молчание интерфейса.
    const view = render(<ConnectionStatusLine state="disconnected" />);

    expect(within(view.container).getByRole("status")).toBeTruthy();
  });
});
