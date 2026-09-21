// Детекторы слоя состояния загрузки.
//
// Проверяется ровно то, ради чего он существует: `ready` не выводится из
// наличия токена, а приходит исходом bootstrap; смена состояния уведомляет
// подписчика; отписка действительно отписывает.

import { describe, expect, it } from "vitest"

import type { ApiClient, BootState } from "../../api/client"
import { createSessionState } from "./sessionState"

/** Подделка зависимости: хранилищу нужен один метод, а не весь клиент. */
function clientReturning(state: BootState): ApiClient {
  return { bootstrap: async () => state } as unknown as ApiClient
}

const NEVER_CALLED = {
  loadAccount: () => Promise.resolve(undefined),
  loadConversations: () => Promise.resolve(undefined),
}

describe("состояние загрузки", () => {
  it("до bootstrap состояние — bootstrapping", () => {
    expect(createSessionState().getSnapshot()).toEqual({ kind: "bootstrapping" })
  })

  it("исход bootstrap сохраняется", async () => {
    const state = createSessionState()

    await state.bootstrap(clientReturning({ kind: "unauthenticated" }), NEVER_CALLED)

    expect(state.getSnapshot()).toEqual({ kind: "unauthenticated" })
  })

  it("исход bootstrap возвращается вызывающему", async () => {
    const ready: BootState = { kind: "ready" }

    await expect(createSessionState().bootstrap(clientReturning(ready), NEVER_CALLED)).resolves.toEqual(ready)
  })

  it("смена состояния уведомляет подписчика", async () => {
    const state = createSessionState()
    let notified = 0
    state.subscribe(() => {
      notified += 1
    })

    await state.bootstrap(clientReturning({ kind: "ready" }), NEVER_CALLED)

    expect(notified).toBe(1)
  })

  it("отписанный подписчик не уведомляется", async () => {
    const state = createSessionState()
    let notified = 0
    const unsubscribe = state.subscribe(() => {
      notified += 1
    })

    unsubscribe()
    await state.bootstrap(clientReturning({ kind: "ready" }), NEVER_CALLED)

    expect(notified).toBe(0)
  })

  it("ссылка на состояние не меняется без изменения состояния", () => {
    const state = createSessionState()

    expect(state.getSnapshot()).toBe(state.getSnapshot())
  })
})
