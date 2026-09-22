/**
 * Слияние трёх источников в одну ленту — и **единственное** место, где лента
 * сортируется и дедуплицируется.
 *
 * Источников три, и порядок у них разный: хвост приходит по убыванию
 * (`openapi.yaml:218-221`), догрузка — по возрастанию, публикация — по одному
 * элементу. Три порядка не должны доезжать до модели. Второе место сортировки
 * означало бы, что два пути однажды разойдутся — и разойдутся молча, ровно на
 * том сообщении, ради которого гейт и существует.
 *
 * Модуль чистый: ни `centrifuge`, ни `fetch`, ни React. Поэтому детектор
 * пропуска (`MSG-005`) предъявляется прогоном без сети.
 */
import type { ChatMessage } from "../../shared/lib/types";

/** Состояние ленты: сообщения по возрастанию `seq` и применённая граница. */
export interface MergeState {
  readonly messages: readonly ChatMessage[];
  /**
   * Наибольший применённый `conversation_seq` беседы
   * (`07-engineering-standard.md:78`).
   *
   * `null` — **не ноль**: граница неизвестна, снимка ещё не было. Разница
   * несущая, потому что следующий законный номер — единица: считая
   * неизвестную границу нулём, пустая беседа сошлась бы никогда, а считая
   * пустой снимок неизвестностью — клиент не смог бы сказать, что беседа
   * пуста.
   */
  readonly appliedThroughSeq: number | null;
}

/**
 * Чем закончилось применение — с состоянием в каждом случае.
 *
 * Исход назван, а не выведен вызывающим из сравнения границ: «пропуск» и
 * «уже применено» требуют **разных** действий (догрузка против ничего), и
 * вызывающий, угадывающий их по числам, ошибётся тихо.
 */
export type MergeOutcome =
  | { readonly kind: "applied"; readonly state: MergeState }
  /** Это сообщение уже в ленте: тот же `message_id` пришёл вторым путём. */
  | { readonly kind: "duplicate"; readonly state: MergeState }
  /** Номер не больше применённой границы — сообщение уже показано. */
  | { readonly kind: "already-applied"; readonly state: MergeState }
  /** Номер выше `appliedThroughSeq + 1` — часть беседы не дошла. */
  | { readonly kind: "gap"; readonly state: MergeState }
  /** Граница неизвестна: применять нечего, сначала снимок. */
  | { readonly kind: "before-snapshot"; readonly state: MergeState };

export function emptyMergeState(): MergeState {
  return { messages: [], appliedThroughSeq: null };
}

/** Снимок хвоста: число ноль для пустой страницы — факт, а не догадка (B23). */
export function applySnapshot(state: MergeState, messages: readonly ChatMessage[]): MergeOutcome {
  return {
    kind: "applied",
    state: {
      messages: merge(state.messages, messages),
      appliedThroughSeq: headOf(messages),
    },
  };
}

/**
 * Публикация канала. Порядок применения — по номеру, а не по времени прихода.
 *
 * Сообщение с номером выше `appliedThroughSeq + 1` **не применяется**: дыра
 * остаётся дырой, и граница через неё не перешагивает. Применить его значило
 * бы объявить ленту полной там, где в ней пропуск, — и пропуск исчез бы из
 * вида навсегда (`MSG-005`).
 */
export function applyMessage(state: MergeState, message: ChatMessage): MergeOutcome {
  if (state.appliedThroughSeq === null) {
    return { kind: "before-snapshot", state };
  }
  if (state.messages.some((known) => known.id === message.id)) {
    return { kind: "duplicate", state };
  }
  if (message.seq <= state.appliedThroughSeq) {
    return { kind: "already-applied", state };
  }
  if (message.seq > state.appliedThroughSeq + 1) {
    return { kind: "gap", state };
  }
  return {
    kind: "applied",
    state: {
      messages: merge(state.messages, [message]),
      appliedThroughSeq: message.seq,
    },
  };
}

/**
 * Страница догрузки — та, что пришла по `after_seq`.
 *
 * Непрерывность проверяется и здесь, а не только на одиночной публикации:
 * страница, начинающаяся выше границы, — та же дыра, и граница не имеет права
 * прыгнуть через неё. Незачем: догрузка запрошена ровно от границы, и сервер
 * отдаёт от неё же.
 */
export function applyPage(state: MergeState, messages: readonly ChatMessage[]): MergeOutcome {
  if (state.appliedThroughSeq === null) {
    return { kind: "before-snapshot", state };
  }
  const fresh = messages.filter(
    (message) =>
      message.seq > state.appliedThroughSeq! &&
      !state.messages.some((known) => known.id === message.id),
  );
  if (fresh.length === 0) {
    return { kind: "applied", state };
  }
  if (!joinsBoundary(fresh, state.appliedThroughSeq)) {
    return { kind: "gap", state };
  }
  return {
    kind: "applied",
    state: {
      messages: merge(state.messages, fresh),
      appliedThroughSeq: Math.max(...fresh.map((message) => message.seq)),
    },
  };
}

/**
 * Проигрывание буфера bootstrap — по одному, в порядке номеров.
 *
 * Останавливается на первой дыре и отдаёт её наружу: дальше применять
 * бессмысленно, пока не догружено пропущенное.
 */
export function applyBuffered(state: MergeState, buffered: readonly ChatMessage[]): MergeOutcome {
  let current = state;
  for (const message of [...buffered].sort((a, b) => a.seq - b.seq)) {
    const outcome = applyMessage(current, message);
    if (outcome.kind === "gap" || outcome.kind === "before-snapshot") {
      return { kind: outcome.kind, state: current };
    }
    current = outcome.state;
  }
  return { kind: "applied", state: current };
}

function headOf(messages: readonly ChatMessage[]): number {
  return messages.length === 0 ? 0 : Math.max(...messages.map((message) => message.seq));
}

/**
 * Первый номер страницы — ровно следующий за границей, и дальше без разрывов.
 *
 * Одной непрерывности внутри страницы мало: страница `[103, 104]` непрерывна и
 * при этом не стыкуется с границей `100` — между ними дыра, через которую
 * граница не имеет права перешагнуть.
 */
function joinsBoundary(messages: readonly ChatMessage[], appliedThroughSeq: number): boolean {
  const seqs = messages.map((message) => message.seq).sort((a, b) => a - b);
  return seqs.every((seq, index) => seq === appliedThroughSeq + index + 1);
}

/**
 * Слияние и сортировка — здесь и только здесь.
 *
 * При равных `message_id` побеждает **уже лежащее** сообщение: обе копии
 * описывают одну запись, и подменять показанное на пришедшее вторым значило бы
 * перерисовывать ленту без причины.
 */
function merge(
  existing: readonly ChatMessage[],
  incoming: readonly ChatMessage[],
): readonly ChatMessage[] {
  const byId = new Map<string, ChatMessage>();
  for (const message of existing) {
    byId.set(message.id, message);
  }
  for (const message of incoming) {
    if (!byId.has(message.id)) {
      byId.set(message.id, message);
    }
  }
  return [...byId.values()].sort((a, b) => a.seq - b.seq);
}
