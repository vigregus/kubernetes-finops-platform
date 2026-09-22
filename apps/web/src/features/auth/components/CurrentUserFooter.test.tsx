import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CurrentUser } from "../../../shared/lib/types";
import { CurrentUserFooter } from "./CurrentUserFooter";

/**
 * Пользователь, каким его отдаёт адаптер `/me`. Полей `handle` и `presence`
 * здесь нет намеренно: сервер не отдаёт ни того, ни другого, и подвал обязан
 * собираться из того, что есть.
 */
const USER: CurrentUser = {
  name: "David Miller",
  email: "david.miller@example.com",
  emailVerified: true,
};

describe("подвал текущего пользователя", () => {
  it("не утверждает присутствие", () => {
    render(<CurrentUserFooter user={USER} />);

    // «Active now» — хардкод, а не факт: realtime в G3-005 нет, а `users.last_seen_at`
    // наружу отдаётся отметкой, без признака «онлайн» (`03-v1-scope.md:77-83`,
    // `README.md:78`). Подпись под именем читается как «в сети прямо сейчас»,
    // то есть сервер такого не сообщал.
    expect(screen.queryByText(/active now/i)).toBeNull();
    expect(screen.queryByText(/online|away|offline|last seen/i)).toBeNull();
  });

  it("без обработчика кнопки настроек нет", () => {
    render(<CurrentUserFooter user={USER} />);

    // Кнопка с `onClick={undefined}` выглядит рабочей и не делает ничего.
    // Элемент без обработчика не рендерится вовсе.
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("с обработчиком кнопка настроек есть и работает", () => {
    const onOpenSettings = vi.fn();
    render(<CurrentUserFooter user={USER} onOpenSettings={onOpenSettings} />);

    screen.getByRole("button", { name: "Settings" }).click();
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });
});
