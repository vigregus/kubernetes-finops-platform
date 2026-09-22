// Детекторы буфера bootstrap: сообщение между снимком и подпиской не теряется,
// переполнение приводит к догрузке, а не к потере.
//
// Гонка здесь **не** разыгрывается живым прогоном: момент публикации задаётся
// вручную — сообщение кладётся в буфер до применения снимка и проигрывается
// после. Mutation «снять барьер» в живом E2E иногда зеленеет, а детектор обязан
// падать от дефекта, а не от проигранной гонки.

import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../../shared/lib/types";
import { BUFFER_CAPACITY, createBootstrapBuffer } from "./bootstrapBuffer";
import { applyBuffered, applyMessage, applySnapshot, emptyMergeState } from "./eventMerge";

let counter = 0;

function message(seq: number): ChatMessage {
  counter += 1;
  return { id: `m-${counter}`, seq, authorId: "u-1", kind: "text", text: `#${seq}`, timestamp: "10:00" };
}

describe("публикация до готовности хвоста", () => {
  it("накопленное отдаётся, а не теряется", () => {
    const buffer = createBootstrapBuffer();

    buffer.add(message(101));
    buffer.add(message(102));

    expect(buffer.drain().map((it) => it.seq)).toEqual([101, 102]);
  });

  it("drain очищает буфер", () => {
    const buffer = createBootstrapBuffer();
    buffer.add(message(101));

    buffer.drain();

    expect(buffer.drain()).toEqual([]);
    expect(buffer.size).toBe(0);
  });

  it("сообщение, накопленное до снимка, применяется после него", () => {
    // Прямая проверка порядка «соединение → буфер → снимок → проигрывание».
    const buffer = createBootstrapBuffer();
    buffer.add(message(1));

    const snapshot = applySnapshot(emptyMergeState(), []);
    const outcome = applyBuffered(snapshot.state, buffer.drain());

    expect(outcome.kind).toBe("applied");
    expect(outcome.state.appliedThroughSeq).toBe(1);
    expect(outcome.state.messages.map((it) => it.seq)).toEqual([1]);
  });

  it("без буфера то же сообщение применить некуда", () => {
    // Контраст, ради которого буфер и существует: до снимка граница неизвестна,
    // и одиночная публикация отбрасывается. Порядок «снимок → подписка» терял
    // бы это сообщение молча — ровно тот дефект, который закрыт буфером.
    const outcome = applyMessage(emptyMergeState(), message(1));

    expect(outcome.kind).toBe("before-snapshot");
    expect(outcome.state.messages).toEqual([]);
  });
});

describe("переполнение", () => {
  it("включает признак догрузки и не отдаёт неполное накопленное", () => {
    const buffer = createBootstrapBuffer();

    for (let seq = 1; seq <= BUFFER_CAPACITY; seq += 1) {
      buffer.add(message(seq));
    }
    expect(buffer.overflowed).toBe(false);

    buffer.add(message(BUFFER_CAPACITY + 1));

    expect(buffer.overflowed).toBe(true);
    expect(buffer.drain()).toEqual([]);
    expect(buffer.size).toBe(0);
  });

  it("после переполнения буфер не растёт", () => {
    const buffer = createBootstrapBuffer(2);

    buffer.add(message(1));
    buffer.add(message(2));
    buffer.add(message(3));
    buffer.add(message(4));

    expect(buffer.overflowed).toBe(true);
    expect(buffer.size).toBe(0);
  });

  it("ёмкость задаётся, а по умолчанию равна контрактной", () => {
    expect(BUFFER_CAPACITY).toBe(256);
    expect(createBootstrapBuffer(1).size).toBe(0);
  });
});
