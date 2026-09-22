/**
 * Время последнего сообщения — **display-строка**, а не ISO.
 *
 * Тип поля в модели — `string`, и соблазн прокинуть туда `created_at` как есть
 * велик: типы сходятся, `tsc` молчит. Но `ConversationListItem` рисует это поле
 * безусловно, и в списке оказалось бы `2026-09-20T14:22:31Z` — визуально
 * сломанный дизайн, которого никто не заказывал. Дизайн показывает `14:22`,
 * `Yesterday`, `Sep 20`, значит форматирование — часть таблицы соответствия,
 * а не украшение.
 *
 * `now` — **параметр**, а не `Date.now()` внутри. Со скрытым `Date.now()` тест
 * на «сегодня/вчера/раньше» зависел бы от момента прогона: зеленел бы в один
 * день и краснел в другой, то есть не проверял бы ничего.
 */

export interface TimestampFormatOptions {
  /** Локаль интерфейса. По умолчанию — локаль браузера. */
  locale?: string
  /** Зона, в которой считаются границы суток. По умолчанию — зона браузера. */
  timeZone?: string
}

/**
 * Ключ суток `2026-09-20` в заданной зоне.
 *
 * Собирается из частей, а не из строки локали: `en-CA` сегодня отдаёт
 * `2026-09-20`, но это её частная договорённость, и сравнение дней не должно
 * зависеть от того, как локаль решит расставить разделители.
 */
function dayKey(date: Date, timeZone?: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone,
  }).formatToParts(date)
  const part = (type: string) => parts.find((p) => p.type === type)?.value ?? ""
  return `${part("year")}-${part("month")}-${part("day")}`
}

/**
 * Предыдущие сутки по календарю, а не «минус 24 часа».
 *
 * Вычитание суток из момента врёт на переходе через летнее время: в день, где
 * сутки длятся 25 часов, `now - 24h` может остаться теми же сутками, и
 * «вчерашнее» сообщение получило бы время вместо `Yesterday`. Здесь из ключа
 * суток собирается полночь UTC, и вычитание идёт по календарю.
 */
function previousDayKey(day: string): string {
  const [year, month, date] = day.split("-").map(Number)
  return new Date(Date.UTC(year, month - 1, date) - 86_400_000).toISOString().slice(0, 10)
}

export function formatConversationTimestamp(
  createdAt: string,
  now: Date,
  options: TimestampFormatOptions = {},
): string | undefined {
  const created = new Date(createdAt)
  // Неразбираемая отметка — это отсутствие времени, а не строка `Invalid Date`
  // в списке. Второе было бы утверждением, которого сервер не делал.
  if (Number.isNaN(created.getTime())) return undefined

  const { locale, timeZone } = options
  const createdDay = dayKey(created, timeZone)

  if (createdDay === dayKey(now, timeZone)) {
    // `hourCycle: "h23"` — а не `hour12: false`: у того в ряде локалей полночь
    // печатается как `24:00`, и список показывал бы несуществующий час.
    return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone }).format(
      created,
    )
  }

  if (createdDay === previousDayKey(dayKey(now, timeZone))) return "Yesterday"

  return new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", timeZone }).format(created)
}
