/**
 * Подписка Web Push: разрешение, Service Worker, подписка браузера, сервер.
 *
 * Среда браузера (`Notification`, `navigator.serviceWorker`, `PushManager`)
 * приходит параметром, поэтому каждый исход — запрет, отказ сервера, смена
 * подписки браузером — предъявляется прогоном без браузера.
 *
 * Приложение работает **и без** уведомлений (`NTF-005`): запрет разрешения или
 * отсутствие поддержки — штатные состояния, а не ошибки, и подписка при них
 * не создаётся.
 */

/** Что клиент знает про состояние на этом устройстве. */
export type PushState =
  | { readonly kind: "checking" }
  /** Браузер не умеет Web Push (или страница не в безопасном контексте). */
  | { readonly kind: "unsupported" }
  /** Разрешения не спрашивали и подписки нет: можно предложить. */
  | { readonly kind: "off" }
  /** Человек запретил уведомления в браузере (`NTF-005`). */
  | { readonly kind: "denied" }
  | { readonly kind: "working" }
  | { readonly kind: "on" }
  /** Сервер уведомления не настроил или выключил (`503`). */
  | { readonly kind: "unavailable" }
  | { readonly kind: "error"; readonly message: string }

/** Серверная сторона подписки: три операции контракта. */
export interface PushSubscriptionApi {
  publicKey(): Promise<string>
  save(subscription: PushSubscriptionJSON): Promise<void>
  remove(): Promise<void>
}

/** Подписка браузера в той части, что нужна клиенту. */
export interface BrowserSubscription {
  toJSON(): PushSubscriptionJSON
  unsubscribe(): Promise<boolean>
}

export interface BrowserRegistration {
  readonly pushManager: {
    getSubscription(): Promise<BrowserSubscription | null>
    subscribe(options: {
      userVisibleOnly: boolean
      applicationServerKey: Uint8Array<ArrayBuffer>
    }): Promise<BrowserSubscription>
  }
}

export interface PushEnv {
  supported(): boolean
  permission(): NotificationPermission
  requestPermission(): Promise<NotificationPermission>
  /** Регистрирует (или находит) Service Worker и ждёт его готовности. */
  register(): Promise<BrowserRegistration>
}

/** Ключ VAPID приходит base64url, а `subscribe` ждёт байты. */
export function urlBase64ToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = value + "=".repeat((4 - (value.length % 4)) % 4)
  const raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"))
  const bytes = new Uint8Array(new ArrayBuffer(raw.length))
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i)
  return bytes
}

function statusOf(error: unknown): number | undefined {
  return typeof error === "object" && error !== null && "status" in error
    ? ((error as { status?: unknown }).status as number | undefined)
    : undefined
}

/**
 * Состояние при загрузке страницы.
 *
 * Разрешение уже выдано и подписка в браузере есть — **заново отправляется на
 * сервер**: браузер мог сменить подписку (`NTF-007`), а сервер хранит старую.
 * Повтор безопасен — сервер заменяет, а не дублирует.
 */
export async function detectState(env: PushEnv, api: PushSubscriptionApi): Promise<PushState> {
  if (!env.supported()) return { kind: "unsupported" }
  const permission = env.permission()
  if (permission === "denied") return { kind: "denied" }
  if (permission === "default") return { kind: "off" }

  try {
    const registration = await env.register()
    const existing = await registration.pushManager.getSubscription()
    if (existing === null) return { kind: "off" }
    await api.save(existing.toJSON())
    return { kind: "on" }
  } catch (error) {
    if (statusOf(error) === 503) return { kind: "unavailable" }
    // Не удалось зарегистрировать worker или спросить подписку при загрузке —
    // чаще всего страница на сертификате, которому браузер не доверяет (локальный
    // CA без доверия: worker на такой странице не регистрируется). Это не
    // сообщение человеку: уведомлений у него просто нет, как у браузера без
    // поддержки. Ошибка показывается только на явное нажатие «Turn on».
    return { kind: "unsupported" }
  }
}

/** Включение: разрешение → worker → подписка → сервер. */
export async function enable(env: PushEnv, api: PushSubscriptionApi): Promise<PushState> {
  if (!env.supported()) return { kind: "unsupported" }
  const permission = await env.requestPermission()
  if (permission === "denied") return { kind: "denied" }
  if (permission !== "granted") return { kind: "off" }

  try {
    const registration = await env.register()
    // Ключ берётся у сервера **до** подписки: без него подписка оформилась бы
    // на ключ, которого сервер не знает, и все push отвергались бы.
    const key = await api.publicKey()
    const subscription =
      (await registration.pushManager.getSubscription()) ??
      (await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToBytes(key),
      }))
    await api.save(subscription.toJSON())
    return { kind: "on" }
  } catch (error) {
    if (statusOf(error) === 503) return { kind: "unavailable" }
    return { kind: "error", message: "Couldn't turn on notifications. Try again." }
  }
}

/** Выключение: подписка браузера и подписка на сервере. */
export async function disable(env: PushEnv, api: PushSubscriptionApi): Promise<PushState> {
  try {
    const registration = await env.register()
    const existing = await registration.pushManager.getSubscription()
    await existing?.unsubscribe()
    await api.remove()
    return { kind: "off" }
  } catch {
    return { kind: "error", message: "Couldn't turn off notifications. Try again." }
  }
}

/** Настоящая среда браузера. Вне браузера и в небезопасном контексте — «не поддерживается». */
export function browserEnv(): PushEnv | null {
  if (
    typeof window === "undefined" ||
    !("serviceWorker" in navigator) ||
    !("PushManager" in window) ||
    !("Notification" in window)
  ) {
    return null
  }
  return {
    supported: () => window.isSecureContext,
    permission: () => Notification.permission,
    requestPermission: () => Notification.requestPermission(),
    register: async () => {
      await navigator.serviceWorker.register("/sw.js")
      return (await navigator.serviceWorker.ready) as unknown as BrowserRegistration
    },
  }
}
