import { describe, expect, it } from "vitest";
import { type Me, MeFromJSON } from "../../api/generated";
import { adaptMe } from "./me-adapter";

/**
 * Что приходит по проводу на `GET /me` — **в snake_case**, как в контракте.
 *
 * Не в camelCase: поле `displayName` генератор читает как `json['display_name']`,
 * и подмена регистра дала бы `name: undefined` в модели — то есть тест проверял
 * бы собственную ошибку, а не адаптер.
 */
function wire(overrides: Record<string, unknown> = {}) {
  return {
    user_id: "user-1",
    display_name: "David Miller",
    email: "david.miller@example.com",
    email_verified: true,
    capabilities: ["read", "send_message"],
    ...overrides,
  };
}

/**
 * `Me` с заданным адресом, минуя конвертер.
 *
 * Приведение здесь названо, а не спрятано: тип поля — `string | undefined`, и
 * `null` в него не входит. Но проверяется поведение **рантайма** на входе,
 * который производит другая версия генератора, а не соответствие типу.
 */
function meWith(email: unknown): Me {
  return {
    userId: "user-1",
    displayName: "David Miller",
    email: email as string | undefined,
    emailVerified: true,
    capabilities: ["read"],
  };
}

describe("таблица соответствия /me", () => {
  it("переносит имя, адрес и подтверждение адреса", () => {
    const { user } = adaptMe(MeFromJSON(wire()));

    expect(user).toEqual({
      name: "David Miller",
      email: "david.miller@example.com",
      emailVerified: true,
    });
  });

  it("null в адресе даёт undefined, а не строку «null»", () => {
    // Подаётся `Me` напрямую, а не через `MeFromJSON`: сегодняшний генератор
    // сворачивает `null` в `undefined` сам, и через провод эта ветка не
    // наблюдалась бы вовсе — тест проверял бы генератор, а не адаптер.
    // Адаптер обязан пережить `null` независимо от того, кто его свернул:
    // `nullable: true` у `Me.email` — единственный 3.0-изм в документе 3.1, и
    // поведение генератора на нём менялось от версии к версии.
    expect(adaptMe(meWith(null)).user.email).toBeUndefined();
  });

  it("пустой адрес — тоже отсутствие адреса", () => {
    // Пустая строка не `null`: она проходит через тип и дошла бы до интерфейса
    // как «адрес есть, он пустой».
    expect(adaptMe(meWith("")).user.email).toBeUndefined();
  });

  it("зритель несёт свой user_id: иначе собеседник неотличим от него самого", () => {
    // `GET /conversations` отдаёт участников **вместе со зрителем**, поэтому
    // адаптеру бесед нужен именно этот идентификатор, а не догадка о том, кто
    // из участников «первый». И лежит он не в `user`: `CurrentUser` описывает
    // то, что показывают, а идентификатор — то, чем сравнивают.
    expect(adaptMe(MeFromJSON(wire())).userId).toBe("user-1");
  });

  it("возможность не превращается в признак модели", () => {
    const viewer = adaptMe(
      MeFromJSON(wire({ capabilities: ["read", "send_message", "start_conversation"] })),
    );

    // Возможности доехали — и остались отдельно от пользователя.
    expect(viewer.capabilities).toEqual(["read", "send_message", "start_conversation"]);
    // Несущее утверждение — не «в модели есть имя», а «в модели **больше
    // ничего нет**»: `toEqual` падает на любом лишнем поле, и именно на нём
    // ловится соблазн вывести интерфейс из авторизации — `canStartConversation`
    // рядом с `start_conversation` в списке. G3-005 не умеет начинать беседы,
    // и кнопки новой беседы не будет даже при этой возможности.
    expect(viewer.user).toEqual({
      name: "David Miller",
      email: "david.miller@example.com",
      emailVerified: true,
    });
  });
});
