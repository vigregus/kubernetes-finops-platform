import { describe, expect, it } from "vitest"

import {
  PROFILE_KEY,
  PROFILE_TTL_MS,
  loadProfile,
  rememberProfile,
  type ProfileStorage,
} from "./profileMemory"

function memory(initial: Record<string, string> = {}): ProfileStorage & { data: Record<string, string> } {
  const data = { ...initial }
  return {
    data,
    getItem: (key) => data[key] ?? null,
    setItem: (key, value) => {
      data[key] = value
    },
    removeItem: (key) => {
      delete data[key]
    },
  }
}

describe("«липкий» профиль", () => {
  it("без памяти звонок начинается в normal", () => {
    expect(loadProfile(null, 1000)).toBe("normal")
    expect(loadProfile(memory(), 1000)).toBe("normal")
  })

  it("restricted запоминается и действует до срока", () => {
    const s = memory()
    rememberProfile(s, "restricted", 1000)
    expect(loadProfile(s, 1000 + PROFILE_TTL_MS)).toBe("restricted")
    expect(loadProfile(s, 1000 + PROFILE_TTL_MS + 1)).toBe("normal")
  })

  it("успешный normal снимает запоминание: прямой путь заработал", () => {
    const s = memory()
    rememberProfile(s, "restricted", 1000)
    rememberProfile(s, "normal", 2000)
    expect(s.data[PROFILE_KEY]).toBeUndefined()
    expect(loadProfile(s, 2000)).toBe("normal")
  })

  it("мусор и время из будущего не включают restricted", () => {
    expect(loadProfile(memory({ [PROFILE_KEY]: "{не json" }), 1000)).toBe("normal")
    expect(loadProfile(memory({ [PROFILE_KEY]: JSON.stringify({ profile: "turbo", at: 1000 }) }), 1000)).toBe("normal")
    expect(loadProfile(memory({ [PROFILE_KEY]: JSON.stringify({ profile: "restricted", at: 5000 }) }), 1000)).toBe("normal")
  })

  it("недоступное хранилище не роняет звонок", () => {
    const broken: ProfileStorage = {
      getItem: () => {
        throw new Error("denied")
      },
      setItem: () => {
        throw new Error("denied")
      },
      removeItem: () => {
        throw new Error("denied")
      },
    }
    expect(loadProfile(broken, 1000)).toBe("normal")
    expect(() => rememberProfile(broken, "restricted", 1000)).not.toThrow()
  })
})
