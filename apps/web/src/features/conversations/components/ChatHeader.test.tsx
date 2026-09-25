import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { conversationOf } from "../../../test-support/fixtures";
import { ChatHeader } from "./ChatHeader";

/**
 * Подзаголовок шапки — то место, где интерфейс говорит о присутствии.
 *
 * Два случая разведены намеренно, и это не формальность: «сервер о присутствии
 * не сказал» и «сервер сказал `false`» — **разные** ответы, и до `G3-007` они
 * были неразличимы, потому что сервер не говорил ни того, ни другого. Теперь
 * `online: false` означает «подтверждённой активности не было окно», и это
 * положительное утверждение — оно рисуется. Молчание не рисуется и рисоваться
 * не должно: «Offline» из отсутствия данных — утверждение, выведенное из тишины.
 *
 * Отметка времени (`lastSeenAt`) остаётся не нарисованной в обоих случаях:
 * «Last seen 16:20» читается как «сейчас офлайн» ровно так же, как слово, — а
 * отметка не сообщает о человеке ничего сегодняшнего (`03-v1-scope.md:77-83`).
 */
describe("подзаголовок шапки беседы", () => {
  it("без присутствия и печатающих подзаголовка нет вовсе", () => {
    const { container } = render(<ChatHeader conversation={conversationOf()} />);

    // Именно «нет узла», а не «нет знакомого текста»: пустой `<p>` — это место,
    // которое рано или поздно заполнят догадкой.
    expect(container.querySelector("header p")).toBeNull();
    // И атрибута тоже нет: `data-presence` приёмка читает как значение сервера,
    // и `false` на месте «сервер молчал» был бы тем же выводом из тишины.
    expect(container.querySelector("header")?.getAttribute("data-presence")).toBeNull();
  });

  it("отметка времени не превращается в утверждение об офлайне", () => {
    const { container } = render(<ChatHeader conversation={conversationOf({ lastSeenAt: "9:14 AM" })} />);

    // Ветка `lastSeenAt` ничуть не слабее «Offline»: она сообщает, когда
    // человека видели, тем же предложением, каким мессенджер сообщает, что его
    // сейчас нет. Здесь ключа `online` нет вовсе — сервер о присутствии молчал.
    expect(container.querySelector("header p")).toBeNull();
    expect(screen.queryByText(/last seen|offline|online|away/i)).toBeNull();
  });

  it("presence: offline рисуется — сервер сказал это сам", () => {
    const { container } = render(<ChatHeader conversation={conversationOf({ presence: "offline" })} />);

    // Положительный контроль к тесту выше: то же отсутствие слова было бы
    // зелёным и на шапке, которая подзаголовка не рисует никогда.
    expect(screen.getByText("Offline")).toBeTruthy();
    expect(container.querySelector("header")?.getAttribute("data-presence")).toBe("offline");
  });

  it("печатающие — то, что сервер утверждает положительно, и подзаголовок есть", () => {
    // Положительный контроль: без него «подзаголовка нет» зеленело бы и на
    // шапке, которая не рисует подзаголовок никогда.
    render(<ChatHeader conversation={conversationOf({ typingNames: ["Anna"] })} />);

    expect(screen.getByText("Anna is typing…")).toBeTruthy();
  });

  it("presence: online — тоже утверждение сервера, и подзаголовок есть", () => {
    const { container } = render(<ChatHeader conversation={conversationOf({ presence: "online" })} />);

    expect(screen.getByText("Online")).toBeTruthy();
    // `data-presence="online"` — то, чем приёмка отличает живое присутствие от
    // подзаголовка с тем же словом, нарисованного по другому поводу.
    expect(container.querySelector("header")?.getAttribute("data-presence")).toBe("online");
  });

  it("без обработчиков четырёх кнопок нет", () => {
    render(<ChatHeader conversation={conversationOf()} />);

    // Кнопка с `onClick={undefined}` выглядит рабочей и не делает ничего.
    // G3-005 не передаёт ни одного из обработчиков шапки — значит, нет и
    // элементов: «Search messages», «Start voice call», «Start video call»,
    // «Chat details».
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("с обработчиками кнопки есть и работают", () => {
    const onSearch = vi.fn();
    render(<ChatHeader conversation={conversationOf()} onSearch={onSearch} />);

    screen.getByRole("button", { name: "Search messages" }).click();
    expect(onSearch).toHaveBeenCalledTimes(1);
    // Остальные три обработчика не переданы — остальных трёх кнопок нет:
    // условность по каждому обработчику, а не «вся группа целиком».
    expect(screen.queryByRole("button", { name: "Chat details" })).toBeNull();
  });
});
