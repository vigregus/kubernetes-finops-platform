/**
 * Автомат состояния соединения — чистая функция, не знающая про `centrifuge`.
 *
 * Разделение не стилистическое. Сеть, SDK и браузер приносят **факты**, а
 * `SYNCING`, `DISCONNECTED` и причина синхронизации — наши решения по этим
 * фактам. Слей их в один модуль — и каждое решение пришлось бы проверять живым
 * соединением, то есть не проверять вовсе. Здесь же переходы доказываются
 * прогоном без сети, и ровно так предъявляются красные прогоны среза.
 *
 * Пять состояний интерфейса (`03-v1-scope.md:182-193`) и три состояния SDK —
 * разные множества: у SDK есть только `connecting`, `connected` и
 * `disconnected`. `SYNCING` и `DEGRADED` наши. `DEGRADED` в этом автомате
 * **недостижим**, и это проверено тестом, а не оставлено на совесть: у него в
 * объёме G3-006 нет достижимого production-входа (B1), а состояние, в которое
 * нельзя попасть, объявленное проверенным, было бы ложью в отчёте гейта.
 */
import type { ConnectionState } from "../../shared/lib/types";

/**
 * Почему мы синхронизируемся.
 *
 * Не телеметрия: `SYNCING` после переподключения приходит и от недостижимой
 * позиции, и от локального пропуска в `seq`, и от ошибки позиции, — и без
 * причины эти три случая в разметке неотличимы друг от друга. Поле живёт
 * ровно пока состояние `syncing` и снимается при выходе из него.
 */
export type SyncReason = "recovery-miss" | "sequence-gap" | "unrecoverable-position";

export interface ConnectionMachineState {
  readonly state: ConnectionState;
  /** Заполнено только при `state === "syncing"`; иначе `null`. */
  readonly syncReason: SyncReason | null;
  /**
   * Факт браузера: `false`, пока `navigator.onLine === false`.
   *
   * Хранится в состоянии, а не читается в момент решения, потому что он **старше**
   * факта сокета: SDK, узнав о разрыве, ещё некоторое время шлёт `connected`,
   * и решение «верить ли сокету» обязано опираться на запомненный факт.
   */
  readonly browserOnline: boolean;
  /**
   * Разрешено ли возобновлять попытки после того, как браузер вернулся в сеть.
   *
   * Ортогонален `browserOnline` намеренно. Терминальный разрыв (наш `4501` —
   * отозванная сессия) означает, что новые попытки бессмысленны: клиент
   * долбил бы connect-proxy мёртвым тикетом. Но браузерное `online` наступает
   * независимо и без этого признака возобновило бы соединение, которого быть
   * не должно, — то есть признак снимается терминальным кодом и **не**
   * возвращается браузерным событием.
   */
  readonly reconnectAllowed: boolean;
}

/** Что приходит в автомат. Кто именно это принёс — SDK или браузер — видно по имени. */
export type ConnectionEvent =
  /** Браузер потерял сеть. Производственный вход, а не выдумка ради теста. */
  | { readonly type: "browser-offline" }
  | { readonly type: "browser-online" }
  /** SDK начал попытку; сюда же приходят `noPing` и закрытие транспорта. */
  | { readonly type: "sdk-connecting" }
  | { readonly type: "sdk-connected" }
  /** Разрыв с кодом из `DisconnectedContext`. */
  | { readonly type: "sdk-disconnected"; readonly code: number }
  /** Ответ на подписку: обе половины — из `ServerSubscribedContext`. */
  | {
      readonly type: "subscription-subscribed";
      readonly wasRecovering: boolean;
      readonly recovered: boolean;
    }
  /** Ошибка недостижимой позиции — приходит подпиской, а не разрывом (см. README). */
  | { readonly type: "unrecoverable-position" }
  /** Публикация с `seq > appliedThroughSeq + 1` — детектор пропуска среза 3. */
  | { readonly type: "sequence-gap" }
  /** Догрузка дошла до границы: `has_more === false` и `next_after_seq === null`. */
  | { readonly type: "sync-completed" };

/**
 * Начальное состояние.
 *
 * `browserOnline` передаётся снаружи, а не предполагается `true`: выдуманное
 * «наверное, сеть есть» — это ровно тот класс утверждений, который гейт
 * запрещает (сравни «unknown» и `0` у `appliedThroughSeq`). Состояние —
 * `connecting`, потому что соединения ещё нет, а не потому, что оно потеряно.
 */
export function initialConnectionState(browserOnline: boolean): ConnectionMachineState {
  return { state: "connecting", syncReason: null, browserOnline, reconnectAllowed: true };
}

/**
 * Терминален ли разрыв — по **измеренному** правилу SDK, а не по догадке.
 *
 * `_handleDisconnect` (`centrifuge/build/index.js:5353`) считает непереподключаемыми
 * коды `[3500, 4000)` и `[4500, 5000)`; тот же диапазон применяется к коду
 * закрытия транспорта, когда в `reason` нет разобранного `advice.reconnect`
 * (`:4506`). Повторяем это правило здесь не ради копирования, а потому, что
 * `reconnectAllowed` существует ровно затем, чтобы **не отменить** решение SDK:
 * если SDK уже прекратил попытки, браузерное `online` не должно их возобновить.
 *
 * Наш `4501` попадает сюда же — и это не совпадение: он выбран в этом диапазоне
 * именно как терминальный.
 */
function isTerminalDisconnect(code: number): boolean {
  return (code >= 3500 && code < 4000) || (code >= 4500 && code < 5000);
}

/** Состояние синхронизации с названной причиной — единственное место, где она ставится. */
function syncing(state: ConnectionMachineState, reason: SyncReason): ConnectionMachineState {
  return { ...state, state: "syncing", syncReason: reason };
}

/**
 * Переход. Единственная точка, где меняется состояние соединения.
 *
 * Порядок веток внутри каждого случая несущий: сначала проверяется факт
 * браузера, потом всё остальное.
 */
export function transition(
  state: ConnectionMachineState,
  event: ConnectionEvent,
): ConnectionMachineState {
  switch (event.type) {
    // Потеря сети — безусловный переход: пока её нет, ни одно событие SDK
    // состояния не меняет. Офлайн показывается честно (`03-v1-scope.md:188`),
    // а ждать `DISCONNECTED` от SDK здесь было бы гаданием: при потере сети
    // Centrifuge уходит скорее в `connecting`, чем в `disconnected`.
    case "browser-offline":
      return { ...state, browserOnline: false, state: "disconnected", syncReason: null };

    // Возврат в сеть возобновляет попытки **только** если они не запрещены
    // терминальным разрывом. Иначе остаёмся там же: признак потому и
    // ортогонален, что эти два события независимы.
    case "browser-online":
      return {
        ...state,
        browserOnline: true,
        state: state.reconnectAllowed ? "connecting" : "disconnected",
        syncReason: null,
      };

    // Факт сокета не перебивает факт браузера: пока сети нет, «идёт попытка»
    // и «подключено» — не то, что видит человек.
    case "sdk-connecting":
      return state.browserOnline ? { ...state, state: "connecting", syncReason: null } : state;

    case "sdk-connected":
      // Из синхронизации сокет не выводит: `connected` говорит «канал поднят»,
      // и это правда — но «всё сходится» он не говорит. Объявить здесь
      // `CONNECTED` значило бы показать старое состояние как актуальное
      // (`03-v1-scope.md:196`), а догрузка в этот момент ещё идёт.
      if (!state.browserOnline || state.state === "syncing") {
        return state;
      }
      return { ...state, state: "connected", syncReason: null };

    case "sdk-disconnected":
      // Разрыв при офлайне ничего не добавляет: состояние уже `disconnected`,
      // но признак обязан сняться — иначе терминальный код, пришедший из-за
      // офлайна, потерялся бы.
      return {
        ...state,
        state: "disconnected",
        syncReason: null,
        reconnectAllowed: isTerminalDisconnect(event.code) ? false : state.reconnectAllowed,
      };

    case "subscription-subscribed":
      // Главная защита гейта. `recovered` равно `false` и тогда, когда
      // восстанавливать было нечего, — на **первой** подписке. Без проверки
      // `wasRecovering` каждая загрузка страницы уезжала бы в `SYNCING`, то
      // есть интерфейс врал бы про расхождение там, где его нет.
      if (event.wasRecovering && !event.recovered) {
        return syncing(state, "recovery-miss");
      }
      // Первая подписка и успешное восстановление неразличимы по `recovered` и
      // различимы по `wasRecovering` — и оба ведут в `CONNECTED` мимо
      // синхронизации, потому что расхождения нет ни в том, ни в другом.
      if (!state.browserOnline || state.state === "syncing") {
        return state;
      }
      return { ...state, state: "connected", syncReason: null };

    case "unrecoverable-position":
      // Второй документированный путь к той же починке: позиция недостижима, и
      // сходить за историей нужно так же, как при неудачном восстановлении.
      // Отказом или бесконечным переподключением это быть не должно.
      return state.browserOnline ? syncing(state, "unrecoverable-position") : state;

    case "sequence-gap":
      return state.browserOnline ? syncing(state, "sequence-gap") : state;

    case "sync-completed":
      // Выход есть только из синхронизации: закончиться может то, что шло.
      if (state.state !== "syncing" || !state.browserOnline) {
        return state;
      }
      return { ...state, state: "connected", syncReason: null };
  }
}
