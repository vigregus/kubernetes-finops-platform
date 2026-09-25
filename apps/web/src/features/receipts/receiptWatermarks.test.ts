// Квитанция — три правила, и все три чистые: приём (номер только вперёд),
// отправка (не ниже уже отправленного) и отображение (состояние своего
// сообщения по числу собеседника).
//
// Здесь же измеряется то, что в компоненте не измерить: у `null` («квитанции
// не было вовсе») и `0` («квитанция до нуля») разные судьбы, а `advance`
// приводит пару к тому же виду, что сервер (`read ≤ delivered`).

import { describe, expect, it } from "vitest"

import { advance, deliveryStateOf, receiptToSend } from "./receiptWatermarks"

describe("advance: номер идёт только вперёд", () => {
  it("отставшее событие не двигает ни одно число", () => {
    // Старое устройство доедает свою очередь и присылает прежний номер: это
    // не откат, но и не новость. Двинуть число назад значило бы показать
    // собеседнику прочтение, которого он не делал.
    expect(advance({ readSeq: 5, deliveredSeq: 9 }, { readSeq: 2, deliveredSeq: 1 })).toEqual({
      readSeq: 5,
      deliveredSeq: 9,
    })
  })

  it("продвижение двигает поле, а соседнее оставляет прежним", () => {
    expect(advance({ readSeq: 5, deliveredSeq: 9 }, { deliveredSeq: 12 })).toEqual({
      readSeq: 5,
      deliveredSeq: 12,
    })
  })

  it("отсутствие поля — не ноль: соседнее число не откатывается", () => {
    // `{readSeq: 5}` без доставки не означает «доставлено до нуля»: сервер
    // шлёт поля независимо (запрос разрешает любое одно из двух), и чтение
    // отсутствующего как нуля откатило бы показанное.
    expect(advance({ readSeq: 5, deliveredSeq: 9 }, { readSeq: 6 })).toEqual({
      readSeq: 6,
      deliveredSeq: 9,
    })
  })

  it("первое известие заводит оба числа, а не одно", () => {
    expect(advance(null, { deliveredSeq: 4 })).toEqual({ readSeq: 0, deliveredSeq: 4 })
  })

  it("прочтение поднимает и доставку: прочитать неполученное нельзя", () => {
    // Та же нормализация, что у сервера (`Conversation.read_states`: «пара
    // нормализуется на входе, а не проверяется на выходе»). Клиент, у которого
    // `read > delivered`, показал бы состояние, которого не бывает, и отправил
    // бы такой же запрос.
    expect(advance(null, { readSeq: 3 })).toEqual({ readSeq: 3, deliveredSeq: 3 })
    expect(advance({ readSeq: 1, deliveredSeq: 1 }, { readSeq: 6 })).toEqual({
      readSeq: 6,
      deliveredSeq: 6,
    })
  })

  it("ни одного числа в событии — не новость вовсе", () => {
    // Такого события не публикует никто, и разбор его уже отбрасывает. Здесь
    // проверяется, что и на этом уровне пустое событие не заводит состояние:
    // «событие без чисел» и «известие» — разные вещи.
    expect(advance(null, {})).toBeNull()
    expect(advance({ readSeq: 2, deliveredSeq: 2 }, {})).toEqual({ readSeq: 2, deliveredSeq: 2 })
  })
})

describe("receiptToSend: отправляется только то, что выше уже отправленного", () => {
  it("новое число уходит обоими полями", () => {
    expect(receiptToSend({ readSeq: 0, deliveredSeq: 0 }, { readSeq: 2, deliveredSeq: 4 })).toEqual({
      readSeq: 2,
      deliveredSeq: 4,
    })
  })

  it("повтор не уходит: то же число второй раз — это не сдвиг", () => {
    expect(receiptToSend({ readSeq: 2, deliveredSeq: 4 }, { readSeq: 2, deliveredSeq: 4 })).toBeNull()
  })

  it("отставка не уходит — ни полем, ни целым запросом", () => {
    expect(receiptToSend({ readSeq: 7, deliveredSeq: 9 }, { readSeq: 5, deliveredSeq: 3 })).toBeNull()
  })

  it("уходит только сдвинувшееся поле", () => {
    expect(receiptToSend({ readSeq: 2, deliveredSeq: 4 }, { readSeq: 2, deliveredSeq: 7 })).toEqual({
      deliveredSeq: 7,
    })
  })

  it("прочтение без доставки уходит: запрос разрешает одно поле из двух", () => {
    // `openapi.yaml:427-433` — `anyOf` по двум полям, и это не лазейка:
    // доставлено сообщает устройство, прочитано — взгляд, и второго может не
    // быть в том же круге.
    expect(receiptToSend({ readSeq: 2, deliveredSeq: 4 }, { readSeq: 5, deliveredSeq: 4 })).toEqual({
      readSeq: 5,
    })
  })
})

describe("deliveryStateOf: состояние своего сообщения по квитанции собеседника", () => {
  it("квитанции не было вовсе — sent, а не delivered", () => {
    // `null` — «собеседник ничего не сообщал», и это не то же, что «сообщил
    // ноль»: вывести доставку из молчания значило бы утверждать о собеседнике
    // то, чего он не делал.
    expect(deliveryStateOf(7, null)).toBe("sent")
  })

  it("номер не покрыт — sent", () => {
    expect(deliveryStateOf(7, { readSeq: 5, deliveredSeq: 6 })).toBe("sent")
  })

  it("покрыт доставкой — delivered", () => {
    expect(deliveryStateOf(7, { readSeq: 5, deliveredSeq: 7 })).toBe("delivered")
  })

  it("покрыт прочтением — read", () => {
    expect(deliveryStateOf(7, { readSeq: 7, deliveredSeq: 9 })).toBe("read")
  })
})
