import type { CallProfile } from "./callEngine"

/**
 * «Липкий» профиль (часть 18, §5): устройство помнит, что в этой сети прямая попытка не сработала,
 * и следующий звонок начинает сразу в `restricted`, не тратя на неё `DIRECT_ATTEMPT_MS`.
 *
 * Память — удобство устройства, не состояние: её может не быть (приватное окно, очищенные данные),
 * и звонок тогда идёт обычным путём. Срок короткий: сеть меняется (дом → метро), а рекомендация
 * «relay-only» дорога (весь трафик через coturn), поэтому она устаревает сама, и возврат в `normal`
 * случается без участия человека. Сети клиент не различает, потому срок — единственная защита от
 * залипания.
 */
export const PROFILE_KEY = "messenger.call.profile"
export const PROFILE_TTL_MS = 30 * 60 * 1000

export interface ProfileStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export function loadProfile(storage: ProfileStorage | null, now: number): CallProfile {
  if (storage === null) return "normal"
  try {
    const raw = storage.getItem(PROFILE_KEY)
    if (raw === null) return "normal"
    const stored = JSON.parse(raw) as { profile?: unknown; at?: unknown }
    if (stored.profile !== "restricted" || typeof stored.at !== "number") return "normal"
    return now - stored.at <= PROFILE_TTL_MS && now >= stored.at ? "restricted" : "normal"
  } catch {
    return "normal"
  }
}

/**
 * Запоминает профиль, в котором звонок **соединился**. `restricted` продлевает срок, `normal`
 * снимает запоминание: прямой путь заработал — залипать на relay незачем.
 */
export function rememberProfile(
  storage: ProfileStorage | null,
  profile: CallProfile,
  now: number,
): void {
  if (storage === null) return
  try {
    if (profile === "restricted") {
      storage.setItem(PROFILE_KEY, JSON.stringify({ profile, at: now }))
    } else {
      storage.removeItem(PROFILE_KEY)
    }
  } catch {
    // нет места или запрещено — не повод ронять звонок
  }
}

export function browserProfileStorage(): ProfileStorage | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}
