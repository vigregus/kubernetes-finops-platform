import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";
import { createApiClient } from "./api/client";
import { AuthApi, ConversationsApi, MessagesApi } from "./api/generated";
import { createRealtimeTicketIssuer } from "./api/realtimeToken";
import { bootStateOf, completeLogin } from "./features/auth/callback";
import { ensureDeviceId, loadDeviceId, saveDeviceId } from "./features/auth/deviceId";
import { CALLBACK_PATH } from "./features/auth/session";
import { createSessionState } from "./features/auth/sessionState";
import type { HistoryApi } from "./features/messages/history";
import type { SendReceipt } from "./features/receipts/useReceipts";
import { loadRuntimeConfig } from "./runtime-config";

/**
 * Роутера нет — и не будет: адрес ровно один, `/callback`, и различается он по
 * `window.location.pathname`. SPA-fallback в nginx отдаёт `index.html` на любой
 * неизвестный путь, поэтому «страница» здесь — это ветка в коде, а не маршрут.
 */

const client = createApiClient({
  deviceId: () => loadDeviceId(window.localStorage),
  saveDeviceId: (deviceId) => {
    saveDeviceId(window.localStorage, deviceId);
  },
});

const session = createSessionState();

const authApi = new AuthApi(client.configuration);
const conversationsApi = new ConversationsApi(client.configuration);
const messagesApi = new MessagesApi(client.configuration);

/**
 * Клиент истории отдаётся **операцией**, а не объектом: `HistorySource`
 * собирается в `App` под зрителя, потому что `currentUserId` известен только
 * после `/me`. Обёртка нужна и технически — метод класса, отданный голой
 * ссылкой, потерял бы `this.configuration`.
 */
const historyApi: HistoryApi = {
  listMessages: (request) => messagesApi.listMessages(request),
};

/** Свежий тикет на каждую попытку соединения: он живёт 120 секунд. */
const issueTicket = createRealtimeTicketIssuer(client.configuration);

/**
 * Адрес соединения — функцией, а не значением: `loadRuntimeConfig()` бросает на
 * отсутствующей конфигурации, и прочитанное заранее значение уронило бы страницу
 * целиком вместо отказа, который называет то, что человек делал.
 */
const readCentrifugoUrl = () => loadRuntimeConfig().centrifugoUrl;

/** Что грузит загрузка. `ready` ставится обоими, а не первым из них. */
const bootstrapDeps = {
  loadAccount: () => authApi.whoAmI(),
  loadConversations: () => conversationsApi.listConversations(),
};

/**
 * Сверка: перечитать список бесед — операция, собранная рядом с загрузкой.
 *
 * Клиенту списка сегодня **нечем** его перечитать: `loadConversations` живёт
 * здесь, а панель получает уже разобранный массив. Без этого пропа сверка была
 * бы словом в докстринге: у счётчика непрочитанного событие — транспорт
 * best-effort, и после потерянной публикации истину взять неоткуда.
 *
 * Тикет здесь не перевыпускается, устройство не переспрашивается и `/me` не
 * повторяется: список бесед несёт и счётчики, и отметки квитанций, то есть всё,
 * что сверяется. Второй круг за тем, что не меняется, был бы платой за
 * симметрию (`D11`).
 *
 * Отдаётся **клиентским** типом (`ConversationListPage`), а не моделью
 * интерфейса: адаптация — работа `ReadyScreen`, и здесь её негде взять
 * (`currentUserId` известен только после `/me`).
 */
const refreshConversations = () => conversationsApi.listConversations();

/**
 * Квитанция вкладки (`POST /conversations/{id}/receipts`) — операция рядом с
 * остальными клиентами API, и по той же причине: у панели нет ни адреса, ни
 * токена.
 *
 * Тело собирает **вызывающий** (`receiptToSend` в `useReceipts`): сюда приходит
 * уже только то, что сдвинулось, и лишних полей в запросе не бывает. Это не
 * украшение, а условие законности ответа: у запроса `additionalProperties:
 * false`, и лишнее поле получило бы отказ, а не тишину.
 *
 * Обёртка, а не голая ссылка на метод: метод класса, отданный без `this`,
 * потерял бы `configuration` — та же причина, что у `historyApi` выше.
 *
 * Ссылка **устойчива** (модульная константа): её смена перезапускает дребезг в
 * `useReceipts`, и нестабильная ссылка откладывала бы отправку на каждом
 * рендере — вкладка не сообщила бы ничего и никогда.
 */
const sendReceipts: SendReceipt = (conversationId, receipt) =>
  messagesApi.setReceipts({
    conversationId,
    setReceiptsRequest: { ...receipt },
  });

async function start(): Promise<void> {
  // Идентификатор заводится **до** первого запроса — и до ветки: на `/callback`
  // первым запросом идёт обмен, и он тоже обязан нести `X-Device-Id`, иначе
  // сервер сочтёт устройство новым.
  ensureDeviceId(window.localStorage);

  if (window.location.pathname !== CALLBACK_PATH) {
    await session.bootstrap(client, bootstrapDeps);
    return;
  }

  const outcome = await completeLogin({
    client,
    store: window.sessionStorage,
    origin: window.location.origin,
    search: window.location.search,
    deviceId: loadDeviceId(window.localStorage),
  });

  // Адрес очищается **всегда** и до загрузки. Не только ради красоты: `code` и
  // `state` остались бы в истории браузера, и перезагрузка страницы повторила бы
  // заход на callback. Незавершённый вход к этому моменту снят, поэтому повтор
  // обмена не состоится — но показывать человеку адрес с чужим `code` незачем.
  window.history.replaceState(null, "", "/");

  if (outcome.kind !== "ok") {
    // `401` и `503` обмена известны уже здесь; идти за ними в `/me` значило бы
    // сходить туда за заведомым `401`.
    session.set(bootStateOf(outcome));
    return;
  }

  await session.bootstrap(client, bootstrapDeps);
}

/**
 * Повтор — та же загрузка, что и в `start`, а не «сброс в bootstrapping».
 * `bootstrap` сам решает, чем закончить; второй путь выставления состояния завёл
 * бы вторую правду о том, что значит «готово».
 */
function retry(): void {
  void session.bootstrap(client, bootstrapDeps);
}

// Обёртка и её зависимости собираются здесь, а не в `App`: у компонента нет ни
// адреса API, ни доступа к токену, и быть не должно.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App
      session={session}
      onRetry={retry}
      historyApi={historyApi}
      refreshConversations={refreshConversations}
      sendReceipts={sendReceipts}
      readCentrifugoUrl={readCentrifugoUrl}
      issueTicket={issueTicket}
    />
  </StrictMode>,
);

// Отклонение не глушится: `completeLogin` пробрасывает только те отказы,
// которые состояниями не являются (неожиданный `4xx`), и тихо превращать их в
// «что-то пошло не так» значило бы скрыть дефект.
//
// Сетевой отказ сюда **не** доходит: он состояние (`transient-error`), а не
// неожиданность, и граница проходит ровно по этому признаку. До правки среза 2
// она была проведена неверно — отказ сети покидал обмен отклонением, состояния
// не выставлялось вовсе, и загрузка оставалась в `bootstrapping` навсегда.
void start();
