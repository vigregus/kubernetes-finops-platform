/**
 * PKCE (RFC 7636) и `state` — то, что делает код авторизации одноразовым для
 * того, кто его начал.
 *
 * Модуль намеренно чистый: ни `window`, ни `sessionStorage`, ни `location`.
 * Кодирование проверяется известным ответом из RFC, а не сличением с самим
 * собой, — иначе неверный вариант base64 остался бы зелёным: он одинаково
 * «работает» и на отправке, и на приёме, и расходятся они только на сервере.
 */

/** Алфавит `unreserved` из RFC 7636, §4.1 — им ограничен `code_verifier`. */
const UNRESERVED = /^[A-Za-z0-9\-._~]+$/;

/** Границы длины `code_verifier` (RFC 7636, §4.1). */
export const VERIFIER_MIN_LENGTH = 43;
export const VERIFIER_MAX_LENGTH = 128;

/** 48 байт → ровно 64 символа base64url без выравнивания. Внутри границ. */
const VERIFIER_BYTES = 48;
/** 32 байта → 43 символа: `state` не короче `code_verifier`, но и не длиннее нужного. */
const STATE_BYTES = 32;

/**
 * Не `Uint8Array<ArrayBufferLike>`: `crypto.getRandomValues` принимает только
 * представления над `ArrayBuffer`, а `ArrayBufferLike` допускает ещё и
 * `SharedArrayBuffer`, который записать случайными числами нельзя.
 */
type RandomBytes = (bytes: Uint8Array<ArrayBuffer>) => Uint8Array<ArrayBuffer>;

const defaultRandomBytes: RandomBytes = (bytes) => crypto.getRandomValues(bytes);

/**
 * base64url по RFC 4648 §5: `+` → `-`, `/` → `_`, выравнивание `=` убрано.
 *
 * `btoa` берёт строку, а не байты, поэтому байты переводятся через
 * `String.fromCharCode` — значения 0..255, старших разрядов не теряется.
 */
export function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * `code_verifier` — случайная строка из алфавита `unreserved`.
 *
 * Не «случайные символы алфавита»: 66 символов алфавита не делят 256 нацело,
 * и выборка по остатку от деления смещала бы распределение. Здесь берутся
 * байты и кодируются base64url — его алфавит целиком лежит внутри `unreserved`,
 * а модульной арифметики нет вовсе.
 */
export function createCodeVerifier(randomBytes: RandomBytes = defaultRandomBytes): string {
  const verifier = base64UrlEncode(randomBytes(new Uint8Array(VERIFIER_BYTES)));
  // Проверка собственного выхода, а не украшение: если кодирование когда-нибудь
  // сменится на обычный base64, отсюда прилетит `+` или `/`, и сервер отвергнет
  // обмен уже после того, как человек ввёл пароль.
  if (
    verifier.length < VERIFIER_MIN_LENGTH ||
    verifier.length > VERIFIER_MAX_LENGTH ||
    !UNRESERVED.test(verifier)
  ) {
    throw new Error("code_verifier вышел за границы RFC 7636");
  }
  return verifier;
}

/**
 * `code_challenge` методом `S256` — `BASE64URL(SHA256(ASCII(verifier)))`.
 *
 * Кодируется **ASCII**-представление строки: верификатор состоит из
 * `unreserved`, то есть ASCII, и для него UTF-8 совпадает с ASCII. Для
 * произвольной строки это было бы неверно, и `TextEncoder` здесь именно
 * потому, что он кодирует UTF-8, — оба совпадают ровно на этом алфавите.
 */
export async function createCodeChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64UrlEncode(new Uint8Array(digest));
}

/** `state` — защита от подстановки чужого кода. Свойство то же, что у верификатора. */
export function createState(randomBytes: RandomBytes = defaultRandomBytes): string {
  return base64UrlEncode(randomBytes(new Uint8Array(STATE_BYTES)));
}
