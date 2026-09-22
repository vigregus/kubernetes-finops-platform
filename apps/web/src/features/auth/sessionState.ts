/**
 * Слой состояния загрузки — B27 плана: «bootstrap — это небольшой слой
 * состояния плюс `useCurrentUser`».
 *
 * Хранилище крошечное намеренно: кэша, инвалидации и дедупликации запросов в
 * G3-005 нет, `TanStack Query` не вводится ради трёх запросов. Здесь только
 * то, что нужно, чтобы состояние загрузки пережило перерисовку и было
 * прочитано хуком в срезе 3.
 *
 * Состояние не выводится из наличия токена: `ready` — утверждение о данных,
 * а не о токене, и ставит его `bootstrap` (см. `api/client.ts`).
 */
import type { ApiClient, BootState, BootstrapDependencies } from "../../api/client";

export interface SessionState {
  /** Совместимо с `useSyncExternalStore`: одна и та же ссылка до изменения. */
  readonly getSnapshot: () => BootState;
  readonly subscribe: (listener: () => void) => () => void;
  /** Прогоняет bootstrap и сохраняет его исход. Исход же и возвращает. */
  readonly bootstrap: (client: ApiClient, deps: BootstrapDependencies) => Promise<BootState>;
  /**
   * Записывает состояние, добытое не загрузкой.
   *
   * Единственный такой случай — возврат из Keycloak: `401` и `503` callback
   * известны **до** bootstrap, и запускать загрузку, чтобы узнать, что входа не
   * было, значило бы сходить в `/me` за заведомым `401`. Тип принимает
   * `BootState` целиком, а не пару исходов, намеренно: отдельный узкий тип
   * позволил бы протащить сюда `ready` — состояние, которое ставит только
   * данные.
   */
  readonly set: (next: BootState) => BootState;
}

export function createSessionState(initial: BootState = { kind: "bootstrapping" }): SessionState {
  let state: BootState = initial;
  const listeners = new Set<() => void>();

  function apply(next: BootState): BootState {
    state = next;
    for (const listener of listeners) {
      listener();
    }
    return state;
  }

  return {
    getSnapshot: () => state,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    set: apply,
    bootstrap: async (client: ApiClient, deps: BootstrapDependencies) =>
      apply(await client.bootstrap(deps)),
  };
}
