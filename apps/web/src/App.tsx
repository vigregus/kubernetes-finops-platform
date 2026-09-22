import { useMemo, useSyncExternalStore } from "react";
import { adaptConversations } from "./features/conversations/adapter";
import { ChatPage } from "./features/conversations/ChatPage";
import { LoginPage } from "./features/auth/LoginPage";
import { adaptMe } from "./features/auth/me-adapter";
import { TransientErrorScreen } from "./features/auth/components/TransientErrorScreen";
import { createHistorySource, type HistoryApi } from "./features/messages/history";
import type { CentrifugeFactory } from "./features/realtime/realtimeClient";
import type { RealtimeTicketIssuer } from "./api/realtimeToken";
import type { BootState } from "./api/client";
import type { SessionState } from "./features/auth/sessionState";

interface AppProps {
  session: SessionState;
  /**
   * Повтор загрузки. Приходит сверху, потому что обёртка над API и её
   * зависимости (`client`, `bootstrapDeps`) собираются в `main.tsx`: у `App`
   * нет ни адреса API, ни доступа к токену, и быть не должно.
   */
  onRetry: () => void;
  /**
   * Клиент истории сообщений — **операция**, а не готовая лента.
   *
   * Именно клиент, а не `HistorySource`: зритель (`currentUserId`) известен
   * только после `/me`, и переводить `sender_id` в `"me"` нечем, пока ответа
   * нет. Сборка источника — здесь, потому что `App` уже место композиции:
   * адаптация `/me` и списка бесед живёт тут же и ровно один раз.
   */
  historyApi: HistoryApi;
  /**
   * Адрес соединения — **функцией**, а не значением, и это не стилистика.
   *
   * Читает его `main.tsx` (у `App` нет ни доступа к окружению, ни адреса API),
   * но читает **в момент, когда адрес понадобился**, а не при загрузке модуля:
   * `loadRuntimeConfig()` на отсутствующей конфигурации бросает, и значение,
   * прочитанное заранее, уронило бы страницу целиком — вместо отказа, который
   * называет то, что человек делал. Ровно так же читает себя `oidcIssuer`
   * (`runtime-config.ts:83-86`).
   */
  readCentrifugoUrl: () => string;
  /** Свежий тикет на каждую попытку соединения (`POST /realtime/token`). */
  issueTicket: RealtimeTicketIssuer;
  /**
   * Подмена SDK — **тестовый шов**, транзитом в `ChatPage`; в `main.tsx` не
   * передаётся.
   *
   * Он здесь не для симметрии, а потому, что без него экраны загрузки нельзя
   * предъявить прогоном без сети: ветка `ready` монтирует настоящую панель, а
   * та поднимает соединение. Подменять модуль `centrifuge` через `vi.mock`
   * значило бы подменить заодно и константы адаптера — то есть проверять не
   * тот код, который исполняется (`realtimeClient.ts:44-53`).
   */
  createCentrifuge?: CentrifugeFactory;
}

/** Состояние готовности: данные `/me` и списка бесед в одном снимке. */
type ReadyState = Extract<BootState, { readonly kind: "ready" }>;

/**
 * Композиция состояний загрузки — целиком, без «а если данных нет, то покажем
 * что-нибудь».
 *
 * Развилка здесь не косметическая: `unauthenticated` и `session-expired` — это
 * разные сообщения человеку, а `transient-error` не ведёт никуда (B9а). Отсюда
 * и `ready`, который несёт **данные**, а не признак: адаптация к модели
 * интерфейса идёт здесь и ровно один раз, и второго понятия готовности
 * (`ChatPage` грузит сам) не заводится.
 */
export function App({
  session,
  onRetry,
  historyApi,
  readCentrifugoUrl,
  issueTicket,
  createCentrifuge,
}: AppProps) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);

  switch (state.kind) {
    case "bootstrapping":
      return (
        <div className="flex h-dvh w-full items-center justify-center bg-surface text-sm text-text-warm-secondary">
          Loading…
        </div>
      );

    case "unauthenticated":
      // У первого посетителя cookie просто нет: слова «сессия истекла» здесь
      // быть не должно — это утверждение о том, чего не было.
      return <LoginPage />;

    case "session-expired":
      // А здесь сессия **была** и кончилась — на обмене уже после `ready`.
      return <LoginPage sessionExpired />;

    case "transient-error":
      return <TransientErrorScreen traceId={state.traceId} onRetry={onRetry} />;

    case "ready":
      /**
       * Отдельный компонент, а не строки прямо здесь, — из-за порядка хуков.
       *
       * Хук поставить в этот `switch` нельзя: выше стоят ранние `return`, и на
       * `bootstrapping` порядок вызовов был бы другим. Отсюда компонент: он
       * монтируется **только** на `ready`, и его хуки живут ровно столько,
       * сколько живут данные.
       *
       * Про `useMemo` внутри — сказано там, где он стоит: **измерено**, что
       * защитой от бесконечного перечитывания хвоста он не является, и
       * приписывать ему это не следует.
       */
      return (
        <ReadyScreen
          state={state}
          historyApi={historyApi}
          readCentrifugoUrl={readCentrifugoUrl}
          issueTicket={issueTicket}
          createCentrifuge={createCentrifuge}
        />
      );
  }
}

interface ReadyScreenProps {
  state: ReadyState;
  historyApi: HistoryApi;
  readCentrifugoUrl: () => string;
  issueTicket: RealtimeTicketIssuer;
  createCentrifuge?: CentrifugeFactory;
}

function ReadyScreen({
  state,
  historyApi,
  readCentrifugoUrl,
  issueTicket,
  createCentrifuge,
}: ReadyScreenProps) {
  const viewer = useMemo(() => adaptMe(state.account), [state.account]);

  // Адрес читается здесь, а не в `App` и не при загрузке модуля: панель — это
  // первое место, где он действительно нужен, и до неё страница обязана
  // показываться целиком (см. `readCentrifugoUrl` в пропсах).
  const centrifugoUrl = useMemo(() => readCentrifugoUrl(), [readCentrifugoUrl]);

  // Зритель один на страницу, и адаптация идёт **после** `/me`: без
  // `user_id` собеседник в личной беседе неотличим от самого зрителя (B9г).
  //
  // Момент времени берётся один раз на снимок данных, а не в рендере: `new
  // Date()` в теле менял бы display-время на каждом рендере, и «14:22» уезжало
  // бы вперёд от любого нажатия клавиши.
  const conversations = useMemo(
    () => adaptConversations(state.conversations, viewer.userId, new Date()),
    [state.conversations, viewer.userId],
  );

  // Две дороги истории собираются **под зрителя**: `currentUserId` нужен, чтобы
  // `sender_id` собеседника стал `"them"`, а свой — `"me"`.
  //
  // `useMemo` здесь держит **ссылку**, а не спасает от цикла, и разница
  // измерена, а не предположена. Напрашивающееся объяснение — «без него
  // `loadTail` менял бы ссылку и эффект хвоста перезапускался бы бесконечно» —
  // неверно для этого кода: `useConversationHistory` держит эффект хвоста на
  // `[conversationId, commit, pump]`, а загрузчик берёт из `handlersRef`
  // (`:190`, `:219`), то есть смена ссылки загрузчика эффект не трогает.
  // Проверено снятием: без `useMemo` весь прогон зелёный (281 тест), и
  // «повторный рендер не перечитывает хвост» ниже тоже. Оставлен `useMemo`
  // потому, что требует план (адаптация `ready` идёт один раз) и потому, что
  // иначе `history` уходил бы новым объектом в пропсы на каждом рендере
  // `App`, — но выдавать за это несущую защиту нечего: её тут нет.
  const history = useMemo(
    () => createHistorySource({ api: historyApi, currentUserId: viewer.userId }),
    [historyApi, viewer.userId],
  );

  return (
    <ChatPage
      conversations={conversations}
      currentUser={viewer.user}
      currentUserId={viewer.userId}
      history={history}
      centrifugoUrl={centrifugoUrl}
      issueTicket={issueTicket}
      createCentrifuge={createCentrifuge}
    />
  );
}

export default App;
