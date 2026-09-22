import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { conversationOf } from "../../../test-support/fixtures";
import { ChatHeader } from "./ChatHeader";

/**
 * Подзаголовок шапки — то место, где интерфейс говорит о присутствии, и в
 * G3-005 говорить о нём нечего.
 *
 * `users.last_seen_at` — «когда человек последний раз был подтверждён в сети»
 * (`03-v1-scope.md:77-83`), и наружу он отдаётся **отметкой**, без признака
 * «онлайн» (`README.md:78`). В обычной семантике мессенджера «Last seen 16:20»
 * читается как «сейчас офлайн» — то есть как факт, которого сервер не сообщал.
 * «Offline» при отсутствии всяких данных — то же самое утверждение, только
 * короче: оно выводится из молчания сервера, а молчание не факт.
 *
 * Тесты ниже различают два случая намеренно: `presence: "offline"` с отметкой
 * времени — это **не** защита от «Offline», а отдельная ветка `ChatHeader.tsx`,
 * и сведение её к одной означало бы, что половина дефекта осталась.
 */
describe("подзаголовок шапки беседы", () => {
  it("без присутствия и печатающих подзаголовка нет вовсе", () => {
    const { container } = render(<ChatHeader conversation={conversationOf()} />);

    // Именно «нет узла», а не «нет знакомого текста»: пустой `<p>` — это место,
    // которое рано или поздно заполнят догадкой.
    expect(container.querySelector("header p")).toBeNull();
  });

  it("отметка времени не превращается в утверждение об офлайне", () => {
    render(<ChatHeader conversation={conversationOf({ presence: "offline", lastSeenAt: "9:14 AM" })} />);

    // Ветка `lastSeenAt` ничуть не слабее «Offline»: она сообщает, когда
    // человека видели, тем же предложением, каким мессенджер сообщает, что его
    // сейчас нет.
    expect(screen.queryByText(/last seen/i)).toBeNull();
    expect(screen.queryByText(/offline|online|away/i)).toBeNull();
  });

  it("печатающие — то, что сервер утверждает положительно, и подзаголовок есть", () => {
    // Положительный контроль: без него «подзаголовка нет» зеленело бы и на
    // шапке, которая не рисует подзаголовок никогда.
    render(<ChatHeader conversation={conversationOf({ typingNames: ["Anna"] })} />);

    expect(screen.getByText("Anna is typing…")).toBeTruthy();
  });

  it("presence: online — тоже утверждение сервера, и подзаголовок есть", () => {
    render(<ChatHeader conversation={conversationOf({ presence: "online" })} />);

    expect(screen.getByText("Online")).toBeTruthy();
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
