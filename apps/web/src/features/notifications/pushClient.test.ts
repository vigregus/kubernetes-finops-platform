import { describe, expect, it, vi } from "vitest"

import {
  detectState,
  disable,
  enable,
  urlBase64ToBytes,
  type BrowserSubscription,
  type PushSubscriptionApi,
  type PushEnv,
} from "./pushClient"

function subscription(endpoint = "https://fcm.googleapis.com/x"): BrowserSubscription & { unsubscribed: boolean } {
  return {
    unsubscribed: false,
    toJSON: () => ({ endpoint, keys: { p256dh: "p", auth: "a" } }),
    async unsubscribe() {
      this.unsubscribed = true
      return true
    },
  }
}

function setup(over: {
  supported?: boolean
  permission?: NotificationPermission
  asked?: NotificationPermission
  existing?: ReturnType<typeof subscription> | null
  apiError?: unknown
} = {}) {
  const created = subscription()
  const calls = { subscribe: vi.fn(), register: vi.fn() }
  const env: PushEnv = {
    supported: () => over.supported ?? true,
    permission: async () => over.permission ?? "default",
    requestPermission: async () => over.asked ?? "granted",
    register: async () => {
      calls.register()
      return {
        pushManager: {
          getSubscription: async () => over.existing ?? null,
          subscribe: async (options) => {
            calls.subscribe(options)
            return created
          },
        },
      }
    },
  }
  const api: PushSubscriptionApi = {
    publicKey: vi.fn(async () => "BPublicKeyBase64Url"),
    save: vi.fn(async () => {
      if (over.apiError) throw over.apiError
    }),
    remove: vi.fn(async () => {}),
  }
  return { env, api, calls, created }
}

describe("состояние при загрузке", () => {
  it("браузер без Web Push — «не поддерживается», и ничего не просим", async () => {
    const { env, api, calls } = setup({ supported: false })
    expect(await detectState(env, api)).toEqual({ kind: "unsupported" })
    expect(calls.register).not.toHaveBeenCalled()
  })

  it("разрешения не спрашивали — можно предложить", async () => {
    const { env, api } = setup({ permission: "default" })
    expect(await detectState(env, api)).toEqual({ kind: "off" })
  })

  it("запрет в браузере — отдельное состояние, подписки не создаётся (NTF-005)", async () => {
    const { env, api, calls } = setup({ permission: "denied" })
    expect(await detectState(env, api)).toEqual({ kind: "denied" })
    expect(calls.subscribe).not.toHaveBeenCalled()
    expect(api.save).not.toHaveBeenCalled()
  })

  it("подписка в браузере есть — заново отправляется на сервер: он мог хранить старую (NTF-007)", async () => {
    const existing = subscription("https://updates.push.services.mozilla.com/new")
    const { env, api } = setup({ permission: "granted", existing })
    expect(await detectState(env, api)).toEqual({ kind: "on" })
    expect(api.save).toHaveBeenCalledWith(existing.toJSON())
  })

  it("разрешено, но подписки нет — снова предлагаем", async () => {
    const { env, api } = setup({ permission: "granted", existing: null })
    expect(await detectState(env, api)).toEqual({ kind: "off" })
  })

  it("worker не регистрируется (недоверенный сертификат) — тишина, а не ошибка на каждой загрузке", async () => {
    const { env, api } = setup({ permission: "granted" })
    env.register = async () => {
      throw new Error("SecurityError: An SSL certificate error occurred")
    }
    expect(await detectState(env, api)).toEqual({ kind: "unsupported" })
  })

  it("сервер не настроил уведомления (503) — «недоступно», а не ошибка", async () => {
    const { env, api } = setup({
      permission: "granted", existing: subscription(), apiError: { status: 503 },
    })
    expect(await detectState(env, api)).toEqual({ kind: "unavailable" })
  })
})

describe("включение", () => {
  it("разрешение → ключ сервера → подписка → сервер", async () => {
    const { env, api, calls, created } = setup()
    expect(await enable(env, api)).toEqual({ kind: "on" })
    const options = calls.subscribe.mock.calls[0][0]
    expect(options.userVisibleOnly).toBe(true)
    expect(options.applicationServerKey).toBeInstanceOf(Uint8Array)
    expect(api.save).toHaveBeenCalledWith(created.toJSON())
  })

  it("запрет при запросе — «denied» без подписки и без обращения к серверу", async () => {
    const { env, api, calls } = setup({ asked: "denied" })
    expect(await enable(env, api)).toEqual({ kind: "denied" })
    expect(calls.subscribe).not.toHaveBeenCalled()
    expect(api.publicKey).not.toHaveBeenCalled()
  })

  it("закрыли окно запроса — остаётся «off»", async () => {
    const { env, api } = setup({ asked: "default" })
    expect(await enable(env, api)).toEqual({ kind: "off" })
  })

  it("уже есть подписка браузера — не создаётся вторая, а прежняя уходит на сервер", async () => {
    const existing = subscription("https://fcm.googleapis.com/old")
    const { env, api, calls } = setup({ existing })
    await enable(env, api)
    expect(calls.subscribe).not.toHaveBeenCalled()
    expect(api.save).toHaveBeenCalledWith(existing.toJSON())
  })

  it("сбой сервера называется ошибкой с повтором, 503 — «недоступно»", async () => {
    const failing = setup({ apiError: new Error("x") })
    expect((await enable(failing.env, failing.api)).kind).toBe("error")
    const unavailable = setup({ apiError: { status: 503 } })
    expect(await enable(unavailable.env, unavailable.api)).toEqual({ kind: "unavailable" })
  })
})

describe("выключение", () => {
  it("снимает подписку и в браузере, и на сервере", async () => {
    const existing = subscription()
    const { env, api } = setup({ permission: "granted", existing })
    expect(await disable(env, api)).toEqual({ kind: "off" })
    expect(existing.unsubscribed).toBe(true)
    expect(api.remove).toHaveBeenCalled()
  })
})

describe("urlBase64ToBytes", () => {
  it("разбирает base64url без дополнения", () => {
    // 65 байт несжатой точки → 87 знаков base64url.
    const bytes = urlBase64ToBytes("BMEHof8LUgWP-A6qQFsYnGQduo3XX0Mk4Tov_HVpKaXwn4KLmqjMIPi1EwzaINld4CyEond3SL2FuxVvxzxeg3E")
    expect(bytes.length).toBe(65)
    expect(bytes[0]).toBe(4)
  })
})
