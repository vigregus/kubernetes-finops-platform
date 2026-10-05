/**
 * Сигнал о входящем звонке: звук, заголовок вкладки и, когда вкладка не на виду,
 * системное уведомление с кнопками.
 *
 * Экран вызова (`CallOverlay`) виден, только пока вкладка перед глазами; человек
 * в другой вкладке или другой программе звонок не услышал бы и не увидел. Поэтому
 * три независимых сигнала: **рингтон** (WebAudio, без звуковых файлов), **мигающий
 * заголовок** (виден в панели вкладок) и **уведомление**, если разрешение на
 * уведомления уже выдано. Разрешение здесь **не запрашивается**: просить его без
 * действия человека — значит получить запрет навсегда; его выдаёт баннер
 * уведомлений.
 *
 * Среда приходит параметром (`AlertEnv`), поэтому каждый исход предъявляется
 * прогоном без браузера.
 */
import type { CallKind } from "./callState"

export interface IncomingInfo {
  readonly callId: string
  readonly name: string
  readonly kind: CallKind
}

export interface RingtoneOutput {
  /** Один тон: частота (Гц) и длительность (мс). */
  tone(frequency: number, durationMs: number): void
  close(): void
}

export interface AlertEnv {
  readonly pageHidden: () => boolean
  /** Подписка на смену видимости вкладки; возвращает отписку. */
  readonly onVisibilityChange: (listener: () => void) => () => void
  readonly getTitle: () => string
  readonly setTitle: (title: string) => void
  /** `null` — звука нет (нет WebAudio): остаются заголовок и уведомление. */
  readonly openRingtone: () => RingtoneOutput | null
  /** Показывает уведомление, если оно разрешено; иначе молча ничего. */
  readonly notify: (info: IncomingInfo) => void
  readonly closeNotification: (callId: string) => void
  readonly setInterval: (fn: () => void, ms: number) => unknown
  readonly clearInterval: (id: unknown) => void
}

/** Рингтон: два тона и пауза, раз в две секунды. */
export const RING_PERIOD_MS = 2000
export const TITLE_BLINK_MS = 1000

export interface IncomingAlert {
  start(info: IncomingInfo): void
  stop(): void
}

export function createIncomingAlert(env: AlertEnv): IncomingAlert {
  let ringtone: RingtoneOutput | null = null
  let ringTimer: unknown = null
  let blinkTimer: unknown = null
  let unsubscribe: (() => void) | null = null
  let original = ""
  let notified = false
  let current: IncomingInfo | null = null

  const ring = () => {
    ringtone?.tone(880, 400)
    ringtone?.tone(660, 400)
  }

  const notifyOnce = () => {
    if (notified || current === null || !env.pageHidden()) return
    notified = true
    env.notify(current)
  }

  return {
    start(info) {
      if (current !== null) return
      current = info
      notified = false

      ringtone = env.openRingtone()
      ring()
      ringTimer = env.setInterval(ring, RING_PERIOD_MS)

      original = env.getTitle()
      let on = true
      env.setTitle(`📞 Incoming ${info.kind} call`)
      blinkTimer = env.setInterval(() => {
        on = !on
        env.setTitle(on ? `📞 Incoming ${info.kind} call` : original)
      }, TITLE_BLINK_MS)

      notifyOnce()
      // Человек мог уйти из вкладки уже после начала звонка.
      unsubscribe = env.onVisibilityChange(notifyOnce)
    },

    stop() {
      if (current === null) return
      const callId = current.callId
      current = null
      if (ringTimer !== null) env.clearInterval(ringTimer)
      if (blinkTimer !== null) env.clearInterval(blinkTimer)
      ringTimer = null
      blinkTimer = null
      ringtone?.close()
      ringtone = null
      env.setTitle(original)
      unsubscribe?.()
      unsubscribe = null
      if (notified) env.closeNotification(callId)
      notified = false
    },
  }
}

/** Настоящая среда браузера. Недоступное (нет WebAudio, нет разрешения) — молчание, не ошибка. */
export function browserAlertEnv(): AlertEnv {
  return {
    pageHidden: () => document.hidden,
    onVisibilityChange: (listener) => {
      document.addEventListener("visibilitychange", listener)
      return () => document.removeEventListener("visibilitychange", listener)
    },
    getTitle: () => document.title,
    setTitle: (title) => {
      document.title = title
    },
    openRingtone: () => {
      const Context =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (Context === undefined) return null
      let context: AudioContext
      try {
        context = new Context()
      } catch {
        return null
      }
      // Без действия человека на странице браузер держит звук выключенным; это
      // не ошибка, а политика автовоспроизведения: остаются заголовок и уведомление.
      void context.resume().catch(() => undefined)
      let offsetMs = 0
      return {
        tone(frequency, durationMs) {
          const oscillator = context.createOscillator()
          const gain = context.createGain()
          const start = context.currentTime + offsetMs / 1000
          oscillator.frequency.value = frequency
          // Огибающая: без неё тон щёлкает на начале и конце.
          gain.gain.setValueAtTime(0, start)
          gain.gain.linearRampToValueAtTime(0.18, start + 0.02)
          gain.gain.linearRampToValueAtTime(0, start + durationMs / 1000)
          oscillator.connect(gain).connect(context.destination)
          oscillator.start(start)
          oscillator.stop(start + durationMs / 1000 + 0.05)
          offsetMs = (offsetMs + durationMs + 50) % RING_PERIOD_MS
        },
        close() {
          void context.close().catch(() => undefined)
        },
      }
    },
    notify: (info) => {
      if (typeof Notification === "undefined" || Notification.permission !== "granted") return
      const title = `Incoming ${info.kind} call`
      const options: NotificationOptions & { actions?: { action: string; title: string }[] } = {
        body: info.name,
        tag: `call:${info.callId}`,
        icon: "/favicon.svg",
        requireInteraction: true,
        data: { callId: info.callId },
        actions: [
          { action: "accept", title: "Accept" },
          { action: "decline", title: "Decline" },
        ],
      }
      void (async () => {
        // Через Service Worker: только он умеет кнопки и нажатие, переживающее
        // вкладку. Не зарегистрирован — обычное уведомление без кнопок.
        const registration = await navigator.serviceWorker?.getRegistration()
        if (registration !== undefined) {
          await registration.showNotification(title, options)
          return
        }
        const { actions: _actions, ...plain } = options
        void _actions
        const shown = new Notification(title, plain)
        shown.onclick = () => window.focus()
      })().catch(() => undefined)
    },
    closeNotification: (callId) => {
      void (async () => {
        const registration = await navigator.serviceWorker?.getRegistration()
        const list = (await registration?.getNotifications({ tag: `call:${callId}` })) ?? []
        list.forEach((notification) => notification.close())
      })().catch(() => undefined)
    },
    setInterval: (fn, ms) => window.setInterval(fn, ms),
    clearInterval: (id) => window.clearInterval(id as number),
  }
}
