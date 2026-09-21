import { describe, expect, it } from "vitest"
import { formatConversationTimestamp } from "./formatTimestamp"

/**
 * Локаль и зона заданы здесь явно, а не взяты у машины: иначе прогон зависел бы
 * от настроек того, кто его запустил, и «зелено у меня» ничего не значило бы.
 * Сама возможность их задать — тоже часть проверки: форматтер обязан считаться
 * с переданной зоной, а не с той, в которой оказался процесс.
 */
const EN = { locale: "en-US", timeZone: "UTC" } as const

const NOW = new Date("2026-09-20T18:00:00Z")

describe("время последнего сообщения в списке бесед", () => {
  it("сегодняшнее — временем", () => {
    expect(formatConversationTimestamp("2026-09-20T14:22:31Z", NOW, EN)).toBe("14:22")
  })

  it("вчерашнее — словом", () => {
    expect(formatConversationTimestamp("2026-09-19T09:14:00Z", NOW, EN)).toBe("Yesterday")
  })

  it("более раннее — датой", () => {
    expect(formatConversationTimestamp("2026-09-18T09:14:00Z", NOW, EN)).toBe("Sep 18")
  })

  it("полночь — нулевым часом, а не двадцать четвёртым", () => {
    // `hour12: false` в ряде локалей печатает полночь как `24:05`, и список
    // показывал бы час, которого в сутках нет.
    expect(formatConversationTimestamp("2026-09-20T00:05:00Z", NOW, EN)).toBe("00:05")
  })

  it("граница суток считается в переданной зоне, а не в зоне процесса", () => {
    // Один и тот же момент: в UTC это ещё вчера, в Токио — уже сегодня.
    const createdAt = "2026-09-20T23:30:00Z"
    const now = new Date("2026-09-21T10:00:00Z")

    expect(formatConversationTimestamp(createdAt, now, EN)).toBe("Yesterday")
    expect(formatConversationTimestamp(createdAt, now, { locale: "en-US", timeZone: "Asia/Tokyo" })).toBe("08:30")
  })

  it("неразбираемая отметка — отсутствие времени, а не строка Invalid Date", () => {
    expect(formatConversationTimestamp("не дата", NOW, EN)).toBeUndefined()
  })
})
