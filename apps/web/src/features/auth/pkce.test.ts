import { describe, expect, it } from "vitest";
import { installSubtleForJsdom } from "../../test-support/webcrypto";
import {
  VERIFIER_MAX_LENGTH,
  VERIFIER_MIN_LENGTH,
  base64UrlEncode,
  createCodeChallenge,
  createCodeVerifier,
  createState,
} from "./pkce";

// jsdom не даёт `crypto.subtle`; без него S256 не считается вовсе.
installSubtleForJsdom();

/** Вектор из RFC 7636, Приложение B. */
const RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
const RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";

describe("base64UrlEncode", () => {
  it("совпадает с известным ответом RFC 7636, а не сам с собой", async () => {
    // Проверка не «нашкодировали и получили то же»: ожидание взято из RFC.
    // Неверный вариант base64 (обычный, с `+` и `/`, либо с выравниванием)
    // прошёл бы любую проверку самосогласия и разошёлся бы только на сервере.
    await expect(createCodeChallenge(RFC_VERIFIER)).resolves.toBe(RFC_CHALLENGE);
  });

  it("не содержит символов вне алфавита base64url", async () => {
    const challenge = await createCodeChallenge(RFC_VERIFIER);
    expect(challenge).not.toMatch(/[+/=]/);
  });

  it("выравнивание снято, а не заменено", () => {
    // 1 байт → 2 символа base64 без `=`, хотя обычный base64 дал бы `AA==`.
    expect(base64UrlEncode(new Uint8Array([0]))).toBe("AA");
    // 3 байта `fb ff` дают `+` и `/` — ровно те символы, что заменяются.
    expect(base64UrlEncode(new Uint8Array([0xfb, 0xff]))).toBe("-_8");
  });
});

describe("createCodeVerifier", () => {
  it("укладывается в границы RFC 7636 по длине и алфавиту", () => {
    const verifier = createCodeVerifier();
    expect(verifier.length).toBeGreaterThanOrEqual(VERIFIER_MIN_LENGTH);
    expect(verifier.length).toBeLessThanOrEqual(VERIFIER_MAX_LENGTH);
    expect(verifier).toMatch(/^[A-Za-z0-9\-._~]+$/);
  });

  it("длина следует из числа байт, а не подобрана", () => {
    // 48 нулевых байт → 64 символа `A`: кодирование base64url без выравнивания.
    expect(createCodeVerifier((bytes) => bytes)).toBe("A".repeat(64));
  });

  it("два вызова не совпадают", () => {
    expect(createCodeVerifier()).not.toBe(createCodeVerifier());
  });
});

describe("createState", () => {
  it("не совпадает с верификатором и от вызова к вызову различается", () => {
    const state = createState();
    expect(state).not.toBe(createCodeVerifier());
    expect(state).not.toBe(createState());
  });

  it("пригоден для сравнения строк: без выравнивания и внеалфавитных символов", () => {
    expect(createState()).toMatch(/^[A-Za-z0-9\-_]+$/);
  });
});
