import { describe, expect, it } from "vitest"

import {
  RING_PERIOD_MS,
  TITLE_BLINK_MS,
  createIncomingAlert,
  type AlertEnv,
  type IncomingInfo,
} from "./incomingAlert"

const INFO: IncomingInfo = { callId: "call-1", name: "Alice", kind: "video" }

function rig(options: { hidden?: boolean; ringtone?: boolean } = {}) {
  const log = {
    tones: [] as Array<[number, number]>,
    ringtoneClosed: 0,
    titles: [] as string[],
    notified: [] as IncomingInfo[],
    closed: [] as string[],
  }
  let hidden = options.hidden ?? false
  let title = "Messenger"
  const visibility: Array<() => void> = []
  const timers: Array<{ id: number; fn: () => void; ms: number; cleared: boolean }> = []

  const env: AlertEnv = {
    pageHidden: () => hidden,
    onVisibilityChange: (listener) => {
      visibility.push(listener)
      return () => visibility.splice(visibility.indexOf(listener), 1)
    },
    getTitle: () => title,
    setTitle: (next) => {
      title = next
      log.titles.push(next)
    },
    openRingtone: () =>
      options.ringtone === false
        ? null
        : {
            tone: (frequency, ms) => log.tones.push([frequency, ms]),
            close: () => {
              log.ringtoneClosed += 1
            },
          },
    notify: (info) => log.notified.push(info),
    closeNotification: (callId) => log.closed.push(callId),
    setInterval: (fn, ms) => {
      const entry = { id: timers.length + 1, fn, ms, cleared: false }
      timers.push(entry)
      return entry.id
    },
    clearInterval: (id) => {
      const entry = timers.find((t) => t.id === id)
      if (entry) entry.cleared = true
    },
  }
  const tick = (ms: number) => timers.filter((t) => t.ms === ms && !t.cleared).forEach((t) => t.fn())
  return {
    alert: createIncomingAlert(env),
    log,
    tick,
    title: () => title,
    hide: () => {
      hidden = true
      visibility.slice().forEach((listener) => listener())
    },
    timers,
  }
}

describe("звук", () => {
  it("рингтон звучит сразу и повторяется, пока звонок звонит", () => {
    const r = rig()
    r.alert.start(INFO)
    expect(r.log.tones).toEqual([[880, 400], [660, 400]])
    r.tick(RING_PERIOD_MS)
    expect(r.log.tones).toHaveLength(4)
  })

  it("конец звонка гасит звук и повторы", () => {
    const r = rig()
    r.alert.start(INFO)
    r.alert.stop()
    expect(r.log.ringtoneClosed).toBe(1)
    const before = r.log.tones.length
    r.tick(RING_PERIOD_MS)
    expect(r.log.tones).toHaveLength(before)
  })

  it("нет WebAudio — остаются заголовок и уведомление, ошибки нет", () => {
    const r = rig({ ringtone: false, hidden: true })
    expect(() => r.alert.start(INFO)).not.toThrow()
    expect(r.log.tones).toEqual([])
    expect(r.log.notified).toHaveLength(1)
  })
})

describe("заголовок вкладки", () => {
  it("мигает между сообщением о звонке и прежним заголовком", () => {
    const r = rig()
    r.alert.start(INFO)
    expect(r.title()).toBe("📞 Incoming video call")
    r.tick(TITLE_BLINK_MS)
    expect(r.title()).toBe("Messenger")
    r.tick(TITLE_BLINK_MS)
    expect(r.title()).toBe("📞 Incoming video call")
  })

  it("по окончании возвращает прежний заголовок, в каком бы состоянии мигание ни было", () => {
    const r = rig()
    r.alert.start(INFO)
    r.alert.stop()
    expect(r.title()).toBe("Messenger")
  })
})

describe("уведомление", () => {
  it("вкладка на виду — уведомления нет: экран вызова уже перед глазами", () => {
    const r = rig({ hidden: false })
    r.alert.start(INFO)
    expect(r.log.notified).toEqual([])
  })

  it("вкладка не на виду — уведомление с данными звонка", () => {
    const r = rig({ hidden: true })
    r.alert.start(INFO)
    expect(r.log.notified).toEqual([INFO])
  })

  it("человек ушёл из вкладки уже во время звонка — уведомление появляется тогда", () => {
    const r = rig({ hidden: false })
    r.alert.start(INFO)
    expect(r.log.notified).toEqual([])
    r.hide()
    expect(r.log.notified).toEqual([INFO])
  })

  it("уведомление одно на звонок, сколько бы раз вкладка ни скрывалась", () => {
    const r = rig({ hidden: false })
    r.alert.start(INFO)
    r.hide()
    r.hide()
    expect(r.log.notified).toHaveLength(1)
  })

  it("конец звонка закрывает показанное уведомление, а непоказанное не трогает", () => {
    const shown = rig({ hidden: true })
    shown.alert.start(INFO)
    shown.alert.stop()
    expect(shown.log.closed).toEqual(["call-1"])

    const unshown = rig({ hidden: false })
    unshown.alert.start(INFO)
    unshown.alert.stop()
    expect(unshown.log.closed).toEqual([])
  })

  it("подписка на видимость снимается с концом звонка", () => {
    const r = rig({ hidden: false })
    r.alert.start(INFO)
    r.alert.stop()
    r.hide()
    expect(r.log.notified).toEqual([])
  })
})

describe("повторы", () => {
  it("второй start при идущем сигнале ничего не удваивает", () => {
    const r = rig({ hidden: true })
    r.alert.start(INFO)
    r.alert.start({ ...INFO, callId: "call-2" })
    expect(r.log.tones).toHaveLength(2)
    expect(r.log.notified).toHaveLength(1)
  })

  it("stop без start безвреден", () => {
    const r = rig()
    expect(() => r.alert.stop()).not.toThrow()
    expect(r.log.titles).toEqual([])
  })
})
