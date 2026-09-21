/**
 * Возврат из Keycloak: сверка `state` и обмен кода.
 *
 * Исходов у callback **три**, а не два, и они не сводятся друг к другу:
 * `200` — вход удался, дальше обычная загрузка; `401` — вход не удался (сессия
 * при этом не кончалась, она не начиналась); `503` — транзитное состояние.
 * Последнее сервер обрабатывает тем же `_login_failure(result.upstream_failed)`
 * (`api/main.py:342`), что и refresh, а тот отдаёт `503` при недоступном
 * Keycloak. Выдать это за «сессия истекла» значило бы отправить человека
 * входить заново в момент, когда вход не работает вовсе.
 */
import type { ApiClient, BootState, LoginOutcome } from "../../api/client";
import { redirectUri, type PendingLoginStore } from "./session";
import { consumePendingLogin } from "./session";

/**
 * Почему вход не состоялся **до** обращения к серверу.
 *
 * Отдельным полем, а не четвёртым видом исхода: для интерфейса все три — одно
 * и то же состояние («войдите»), а различать их нужно в разборе, а не на
 * экране. Заводить четвёртое состояние значило бы требовать от UI того, чего
 * он не делает.
 *
 * `missing-params` — Keycloak вернул не то, что обещал; `no-pending-login` —
 * возврат без начатого входа (или повторный: вход уже потреблён);
 * `state-mismatch` — код принадлежит не этому браузеру.
 */
export type CallbackRefusal = "missing-params" | "no-pending-login" | "state-mismatch";

export type CallbackOutcome =
  | { readonly kind: "ok"; readonly deviceId: string | undefined }
  | { readonly kind: "unauthenticated"; readonly refusal?: CallbackRefusal }
  | { readonly kind: "transient-error"; readonly traceId: string | undefined };

export interface CompleteLoginDeps {
  /** Нужен ровно один метод: callback не читает и не грузит ничего сверх обмена. */
  readonly client: Pick<ApiClient, "exchangeAuthorizationCode">;
  readonly store: PendingLoginStore;
  readonly origin: string;
  /** `window.location.search` целиком. */
  readonly search: string;
  /** Текущий идентификатор устройства — он же уедет в теле обмена. */
  readonly deviceId: string | null;
}

/**
 * Сверяет `state`, обменивает код и возвращает исход.
 *
 * Порядок здесь — предмет проверки, а не стилистика: **сверка `state` идёт до
 * любого запроса**, и на несовпадении запроса не происходит вовсе. Обмен
 * «на всякий случай, а потом сверим» отправил бы чужой код на сервер, и
 * единственной защитой остался бы `code_verifier` — то есть ровно то, что
 * `state` и должен беречь: привязку кода к начавшему вход браузеру.
 *
 * Незавершённый вход снимается **до** сверки и при любом исходе — в том числе
 * когда он не понадобился. Оставленный, он пережил бы использование, а на
 * повторном заходе на `/callback` дал бы второй обмен по тому же коду.
 */
export async function completeLogin(deps: CompleteLoginDeps): Promise<CallbackOutcome> {
  const params = new URLSearchParams(deps.search);
  const code = params.get("code");
  const state = params.get("state");

  const pending = consumePendingLogin(deps.store);

  if (code === null || code === "" || state === null || state === "") {
    return { kind: "unauthenticated", refusal: "missing-params" };
  }
  if (pending === null) {
    return { kind: "unauthenticated", refusal: "no-pending-login" };
  }
  if (pending.state !== state) {
    return { kind: "unauthenticated", refusal: "state-mismatch" };
  }

  const outcome: LoginOutcome = await deps.client.exchangeAuthorizationCode({
    code,
    codeVerifier: pending.codeVerifier,
    redirectUri: redirectUri(deps.origin),
    deviceId: deps.deviceId ?? undefined,
  });

  if (outcome.kind === "ok") {
    return { kind: "ok", deviceId: outcome.deviceId };
  }
  if (outcome.kind === "unauthenticated") {
    return { kind: "unauthenticated" };
  }
  return { kind: "transient-error", traceId: outcome.traceId };
}

/**
 * Исход callback как состояние загрузки — для тех, кто не грузит данные.
 *
 * `ok` сюда попадать не должен: после удачного обмена идёт обычная загрузка
 * (`/me` и `/conversations`), и `ready` ставится ею, а не обменом. Отдельная
 * функция, а не ветка внутри `completeLogin`, ровно затем, чтобы это было
 * видно в типе: `ok` требует продолжения.
 */
export function bootStateOf(outcome: Exclude<CallbackOutcome, { kind: "ok" }>): BootState {
  if (outcome.kind === "unauthenticated") {
    // `401` от callback — «вход не удался», а не «сессия истекла»: сессии не
    // было. Слово об истечении здесь было бы утверждением о том, чего нет.
    return { kind: "unauthenticated" };
  }
  return { kind: "transient-error", traceId: outcome.traceId };
}
