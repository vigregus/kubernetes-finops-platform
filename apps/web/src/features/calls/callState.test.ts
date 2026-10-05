import { describe, expect, it } from "vitest"

import {
  IDLE,
  callReducer,
  isLive,
  shouldApplySignal,
  type ApiCallView,
  type CallAction,
  type CallView,
} from "./callState"

const CALL = "11111111-1111-1111-1111-111111111111"
const CONV = "22222222-2222-2222-2222-222222222222"

function apiCall(overrides: Partial<ApiCallView> = {}): ApiCallView {
  return {
    callId: CALL,
    conversationId: CONV,
    kind: "audio",
    state: "ringing",
    endReason: null,
    version: 1,
    role: "caller",
    ...overrides,
  }
}

function run(...actions: CallAction[]): CallView {
  return actions.reduce(callReducer, IDLE)
}

const incoming: CallAction = {
  type: "incoming",
  callId: CALL,
  conversationId: CONV,
  kind: "video",
  peerName: "Alice",
  version: 1,
}

describe("исходящий звонок", () => {
  it("ответ сервера на «начать» — экран «звоним»", () => {
    const view = run({ type: "api-call", call: apiCall(), peerName: "Bob" })
    expect(view.phase).toBe("outgoing")
    expect(view.peerName).toBe("Bob")
    expect(view.role).toBe("caller")
  })

  it("принят → соединяется → медиа пошло → активен", () => {
    const view = run(
      { type: "api-call", call: apiCall(), peerName: "Bob" },
      { type: "state", callId: CALL, state: "accepted", reason: null, version: 2 },
    )
    expect(view.phase).toBe("connecting")
    expect(callReducer(view, { type: "connected" }).phase).toBe("active")
  })

  it("завершённый при создании звонок (занято) показывает причину", () => {
    const view = run({
      type: "api-call",
      call: apiCall({ state: "ended", endReason: "busy" }),
      peerName: null,
    })
    expect(view.phase).toBe("ended")
    expect(view.endReason).toBe("busy")
  })

  it("встречный звонок приходит сразу принятым", () => {
    const view = run({ type: "api-call", call: apiCall({ state: "accepted", version: 2 }), peerName: null })
    expect(view.phase).toBe("connecting")
  })
})

describe("ответ API и событие о том же звонке", () => {
  it("ответ не обнуляет применённые сигналы: тот же offer не применится дважды", () => {
    const live = run(
      incoming,
      { type: "local-accepting" },
      { type: "signal-applied", seq: 4 },
      { type: "set-muted", muted: true },
    )
    const after = callReducer(live, {
      type: "api-call",
      call: apiCall({ state: "accepted", version: 2, role: "callee" }),
      peerName: null,
    })
    expect(after.lastSignalSeq).toBe(4)
    expect(after.muted).toBe(true)
    expect(after.peerName).toBe("Alice")
  })

  it("ответ с уже виденным номером ничего не меняет", () => {
    const seen = run(incoming, { type: "state", callId: CALL, state: "accepted", reason: null, version: 2 })
    const same = callReducer(seen, {
      type: "api-call",
      call: apiCall({ state: "accepted", version: 2, role: "callee" }),
      peerName: null,
    })
    expect(same).toBe(seen)
  })
})

describe("входящий звонок", () => {
  it("событие даёт экран входящего с именем", () => {
    const view = run(incoming)
    expect(view).toMatchObject({ phase: "incoming", role: "callee", peerName: "Alice", kind: "video" })
  })

  it("при идущем звонке второй входящий игнорируется", () => {
    const busy = run({ type: "api-call", call: apiCall(), peerName: "Bob" })
    const other = callReducer(busy, { ...incoming, callId: "99999999-9999-9999-9999-999999999999" })
    expect(other).toBe(busy)
  })

  it("«принять» сразу переводит в соединение", () => {
    expect(run(incoming, { type: "local-accepting" }).phase).toBe("connecting")
  })

  it("принято в другой вкладке — экран гаснет с причиной", () => {
    const view = run(incoming, {
      type: "state", callId: CALL, state: "accepted", reason: "accepted_elsewhere", version: 2,
    })
    expect(view.phase).toBe("ended")
    expect(view.endReason).toBe("accepted_elsewhere")
  })

  it("а в принявшей вкладке то же событие ничего не гасит", () => {
    const view = run(incoming, { type: "local-accepting" }, {
      type: "state", callId: CALL, state: "accepted", reason: "accepted_elsewhere", version: 2,
    })
    expect(view.phase).toBe("connecting")
  })

  it("отмена звонящим гасит входящий", () => {
    const view = run(incoming, {
      type: "state", callId: CALL, state: "ended", reason: "cancelled", version: 2,
    })
    expect(view).toMatchObject({ phase: "ended", endReason: "cancelled" })
  })
})

describe("защита номером состояния (CALL-007)", () => {
  it("запоздавшее событие с прежним номером отбрасывается", () => {
    const ended = run(incoming, {
      type: "state", callId: CALL, state: "ended", reason: "missed", version: 3,
    })
    const late = callReducer(ended, {
      type: "state", callId: CALL, state: "ringing", reason: null, version: 1,
    })
    expect(late).toBe(ended)
  })

  it("одинаковый номер — повтор, не новое состояние", () => {
    const view = run(incoming, { type: "state", callId: CALL, state: "accepted", reason: null, version: 2 })
    const again = callReducer(view, { type: "state", callId: CALL, state: "active", reason: null, version: 2 })
    expect(again).toBe(view)
  })

  it("состояние чужого звонка не трогает этот", () => {
    const view = run(incoming)
    const other = callReducer(view, {
      type: "state", callId: "99999999-9999-9999-9999-999999999999", state: "ended", reason: "missed", version: 9,
    })
    expect(other).toBe(view)
  })

  it("завершённый звонок не оживает событием «принято»", () => {
    const ended = run(incoming, { type: "state", callId: CALL, state: "ended", reason: "missed", version: 3 })
    expect(callReducer(ended, { type: "state", callId: CALL, state: "accepted", reason: null, version: 4 }).phase)
      .toBe("ended")
  })

  it("неизвестная причина конца читается как «не соединилось»", () => {
    const view = run(incoming, { type: "state", callId: CALL, state: "ended", reason: "meteor", version: 2 })
    expect(view.endReason).toBe("failed")
  })
})

describe("защита номером сигнала (CALL-007)", () => {
  const live = run({ type: "api-call", call: apiCall({ state: "accepted", version: 2 }), peerName: null })

  it("применяется только сигнал новее виденного и только этого звонка", () => {
    expect(shouldApplySignal(live, CALL, 1)).toBe(true)
    expect(shouldApplySignal(live, "other", 1)).toBe(false)
    const seen = callReducer(live, { type: "signal-applied", seq: 3 })
    expect(shouldApplySignal(seen, CALL, 3)).toBe(false)
    expect(shouldApplySignal(seen, CALL, 2)).toBe(false)
    expect(shouldApplySignal(seen, CALL, 4)).toBe(true)
  })

  it("в завершённый звонок сигналы не применяются", () => {
    const ended = callReducer(live, { type: "local-ended", reason: "completed" })
    expect(shouldApplySignal(ended, CALL, 99)).toBe(false)
  })
})

describe("локальные действия", () => {
  it("конец звонка локально фиксирует причину, повторный — ничего не меняет", () => {
    const view = run(incoming, { type: "local-accepting" }, { type: "local-ended", reason: "media_denied" })
    expect(view).toMatchObject({ phase: "ended", endReason: "media_denied" })
    expect(callReducer(view, { type: "local-ended", reason: "failed" })).toBe(view)
  })

  it("микрофон и камера переключаются, сброс возвращает исходное", () => {
    const view = run(
      incoming, { type: "set-muted", muted: true }, { type: "set-camera-off", cameraOff: true },
    )
    expect(view.muted && view.cameraOff).toBe(true)
    expect(callReducer(view, { type: "reset" })).toBe(IDLE)
  })

  it("isLive: звонит, соединяется и активен — живой; пусто и конец — нет", () => {
    expect(isLive(IDLE)).toBe(false)
    expect(isLive(run(incoming))).toBe(true)
    expect(isLive(run(incoming, { type: "local-ended", reason: "failed" }))).toBe(false)
  })
})
