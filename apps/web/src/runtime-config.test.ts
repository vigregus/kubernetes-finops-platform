// Детекторы конфигурации окружения.
//
// Проверяется не чтение полей, а **отказы**: конфигурация приходит томом из
// ConfigMap, и ошибка в ней обязана быть громкой при загрузке, а не молчаливым
// «realtime не подключился». Первая половина файла — про неполноту, вторая —
// про адрес, который браузеру не разрешить.
//
// Значения здесь настоящие по форме: `wss://` и публичное имя. Литерал
// `https://example.test` прошёл бы проверки, ничего не проверив, — а проверка
// публичности имени и есть то, ради чего вторая половина существует.

import { afterEach, describe, expect, it } from "vitest"

import { loadRuntimeConfig, RuntimeConfigMissingError } from "./runtime-config"

const ISSUER = "https://idp.finops.local/realms/messenger"
const CENTRIFUGO = "wss://rt.finops.local/connection/websocket"

/** Конфигурация стенда — то, что кладёт в окно файл из ConfigMap. */
function givenConfig(value: unknown): void {
  window.__MESSENGER_RUNTIME_CONFIG__ = value
}

afterEach(() => {
  delete window.__MESSENGER_RUNTIME_CONFIG__
})

describe("полная конфигурация", () => {
  it("отдаёт оба поля", () => {
    givenConfig({ oidcIssuer: ISSUER, centrifugoUrl: CENTRIFUGO })

    expect(loadRuntimeConfig()).toEqual({ oidcIssuer: ISSUER, centrifugoUrl: CENTRIFUGO })
  })
})

describe("конфигурации нет вовсе", () => {
  it("отсутствие файла названо по имени типа", () => {
    expect(() => loadRuntimeConfig()).toThrow(RuntimeConfigMissingError)
  })

  it("не-объект в окне — та же ошибка, а не падение на разборе полей", () => {
    givenConfig("https://idp.finops.local/realms/messenger")

    expect(() => loadRuntimeConfig()).toThrow(RuntimeConfigMissingError)
  })
})

describe("первое поле", () => {
  it("пустой oidcIssuer — отказ", () => {
    givenConfig({ oidcIssuer: "", centrifugoUrl: CENTRIFUGO })

    expect(() => loadRuntimeConfig()).toThrow(RuntimeConfigMissingError)
  })
})

describe("второе поле", () => {
  // Собственное имя, а не `toThrow()` без аргумента: отказ обязан быть про
  // centrifugoUrl, иначе отсутствие поля прошло бы проверку по ошибке в
  // соседнем поле — то есть тест зеленел бы на чужой причине.
  const missing = /centrifugoUrl/

  it("отсутствующий centrifugoUrl — отказ", () => {
    givenConfig({ oidcIssuer: ISSUER })

    expect(() => loadRuntimeConfig()).toThrow(missing)
  })

  it("пустой centrifugoUrl — отказ", () => {
    givenConfig({ oidcIssuer: ISSUER, centrifugoUrl: "" })

    expect(() => loadRuntimeConfig()).toThrow(missing)
  })

  it("кластерное имя — отказ: браузер его не разрешает", () => {
    givenConfig({
      oidcIssuer: ISSUER,
      centrifugoUrl: "wss://messenger-centrifugo.messenger.svc.cluster.local/connection/websocket",
    })

    expect(() => loadRuntimeConfig()).toThrow(/кластерное имя/)
  })

  it("публичное имя кластерным не считается", () => {
    // Обратная сторона предыдущего: проверка не должна краснеть на стенде,
    // который настроен верно. Различие здесь ровно в суффиксе `.svc.cluster.local`,
    // и оно проверяется с обеих сторон.
    givenConfig({ oidcIssuer: ISSUER, centrifugoUrl: CENTRIFUGO })

    expect(loadRuntimeConfig().centrifugoUrl).toBe(CENTRIFUGO)
  })
})
