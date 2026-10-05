import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";
import { createApiClient, withUnwrappedErrors } from "./api/client";
import {
  AttachmentsApi,
  CallsApi,
  PushApi,
  AuthApi,
  ConversationsApi,
  MessagesApi,
  SendMessageRequestTypeEnum,
  TelemetryApi,
  UsersApi,
} from "./api/generated";
import { createRealtimeTicketIssuer } from "./api/realtimeToken";
import { bootStateOf, completeLogin } from "./features/auth/callback";
import { createTelemetryClient } from "./features/telemetry/telemetryClient";
import type { AttachmentClient } from "./features/attachments/attachmentUpload";
import type { PushSubscriptionApi } from "./features/notifications/pushClient";
import { toCallSignal, toCallView, type CallsOperations } from "./features/calls/callsApi";
import { ensureDeviceId, loadDeviceId, saveDeviceId } from "./features/auth/deviceId";
import { CALLBACK_PATH } from "./features/auth/session";
import { createSessionState } from "./features/auth/sessionState";
import type {
  CreateConversation,
  SearchUser,
} from "./features/conversations/components/NewConversationDialog";
import type { ResendVerificationEmail } from "./features/auth/components/EmailVerificationBanner";
import type { HistoryApi } from "./features/messages/history";
import { createOutboxStore, type OutboxStore } from "./features/messages/outbox/outboxStore";
import type { SendMessage } from "./features/messages/outbox/useOutbox";
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

// `withUnwrappedErrors` — не украшение: без неё любой отказ здесь доходил бы
// до вызывающих как `FetchError` вместо `ApiProblem`/`ServiceUnavailableError`
// (`api/client.ts`, там же и причина). Оборачивается **конструктор**, а не
// отдельные вызовы — тем же доводом, что и обёртка сама объясняет.
const authApi = withUnwrappedErrors(new AuthApi(client.configuration));
const conversationsApi = withUnwrappedErrors(new ConversationsApi(client.configuration));
const messagesApi = withUnwrappedErrors(new MessagesApi(client.configuration));
const usersApi = withUnwrappedErrors(new UsersApi(client.configuration));
const attachmentsApi = withUnwrappedErrors(new AttachmentsApi(client.configuration));
const pushApi = withUnwrappedErrors(new PushApi(client.configuration));
const callsApi = withUnwrappedErrors(new CallsApi(client.configuration));

/**
 * G3-008, одна константа на приложение — тем же доводом, что у
 * `outboxStore`: очередь телеметрии копится между рендерами и панелями,
 * второй объект завёл бы вторую, никогда не сбрасываемую очередь.
 *
 * Без `withUnwrappedErrors`: `telemetryClient.flush()` сам глотает любой
 * отказ (`telemetryClient.ts`), и разворачивать `FetchError` в `ApiProblem`
 * здесь нечему — поймать его и выбросить заново успело бы само тело
 * `try/catch` внутри `flush()`.
 */
const telemetryClient = createTelemetryClient({
  api: new TelemetryApi(client.configuration),
});

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

/**
 * Отправка сообщения (`POST /conversations/{id}/messages`) — операция рядом с
 * квитанцией и по той же причине: у очереди нет ни адреса, ни токена.
 *
 * `clientMessageId` **приходит сюда**, а не чеканится здесь, и это не
 * перекладывание работы: идентификатор принадлежит **логической** отправке, а не
 * попытке, и чеканит его тот, кто умеет повторить, — очередь (`D4`). Заведись он
 * в этой обёртке, повтор получил бы свежий UUID, и в Postgres легли бы две
 * строки вместо одной.
 *
 * `type` — литерал из сгенерированного перечисления, а не строка `"text"`:
 * сужение здесь проверяется `tsc -b`, а строку компилятор пропустил бы, и
 * расхождение с контрактом всплыло бы отказом сервера на живом стенде.
 */
const sendMessage: SendMessage = (request) =>
  messagesApi.sendMessage({
    conversationId: request.conversationId,
    sendMessageRequest: {
      clientMessageId: request.clientMessageId,
      type: SendMessageRequestTypeEnum.Text,
      payload: { text: request.text },
    },
  });

/**
 * Вложения (G4): операции рядом с остальными клиентами API, по той же причине —
 * у панели нет ни адреса, ни токена.
 *
 * `put` — **не** через клиент API, и это главное в этом блоке: ссылка в
 * хранилище уже подписана, и `Authorization` или cookie на ней — чужие для
 * него заголовки. `credentials: "omit"` назван явно, а не оставлен умолчанию:
 * `PUT` идёт на другой origin (`s3.finops.local`), и приложенная cookie была
 * бы утечкой, а не помощью.
 */
/**
 * Подписка Web Push: три операции контракта в той форме, что нужна клиенту
 * уведомлений. Ключ VAPID приходит с сервера, а не из сборки: пара создаётся
 * в кластере, и в git её нет.
 */
const pushSubscriptionApi: PushSubscriptionApi = {
  publicKey: async () => (await pushApi.getPushPublicKey()).publicKey,
  save: async (subscription) => {
    const keys = subscription.keys
    if (subscription.endpoint === undefined || keys === undefined) {
      throw new Error("подписка браузера без адреса или ключей");
    }
    await pushApi.putPushSubscription({
      pushSubscription: {
        endpoint: subscription.endpoint,
        keys: { p256dh: keys["p256dh"] ?? "", auth: keys["auth"] ?? "" },
      },
    });
  },
  remove: () => pushApi.deletePushSubscription(),
};

/**
 * Звонки: операции контракта в форме, нужной клиенту. Билет подключения — на
 * **канал звонков** (`scope=calls`): у звонка своё соединение, не зависящее от
 * открытой беседы.
 */
const issueCallsTicket = createRealtimeTicketIssuer(client.configuration, "calls");

const callsOperations: CallsOperations = {
  start: async (conversationId, kind) =>
    toCallView(await callsApi.startCall({ startCall: { conversationId, kind } })),
  current: async () => {
    const { call } = await callsApi.getCurrentCall();
    return call === null || call === undefined ? null : toCallView(call);
  },
  accept: async (callId, tabId) =>
    toCallView(await callsApi.acceptCall({ callId, acceptCall: { tabId } })),
  decline: async (callId) => toCallView(await callsApi.declineCall({ callId })),
  // `keepalive` — запрос на закрытии страницы должен дойти до сервера.
  hangup: async (callId, options) =>
    toCallView(
      await callsApi.hangupCall({ callId }, options?.unload === true ? { keepalive: true } : undefined),
    ),
  fail: async (callId) => toCallView(await callsApi.failCall({ callId })),
  keepalive: async (callId) => toCallView(await callsApi.keepAliveCall({ callId })),
  connected: async (callId, connectionType) =>
    toCallView(await callsApi.reportCallConnected({ callId, callConnected: { connectionType } })),
  signal: (callId, signal) => callsApi.sendCallSignal({ callId, callSignal: toCallSignal(signal) }),
  iceServers: async (callId) => {
    const { iceServers } = await callsApi.getCallIceServers({ callId });
    return iceServers.map((server) => ({
      urls: server.urls,
      ...(server.username === undefined ? {} : { username: server.username }),
      ...(server.credential === undefined ? {} : { credential: server.credential }),
    }));
  },
};

const attachmentClient: AttachmentClient = {
  ops: {
    create: (request) =>
      attachmentsApi.createAttachment({
        createAttachmentRequest: {
          contentType: request.contentType,
          sizeBytes: request.sizeBytes,
          fileName: request.fileName,
          ...(request.durationMs === undefined ? {} : { durationMs: request.durationMs }),
        },
      }),
    complete: (attachmentId) => attachmentsApi.completeAttachment({ attachmentId }),
    status: (attachmentId) => attachmentsApi.getAttachmentStatus({ attachmentId }),
    put: (url, headers, body) =>
      fetch(url, { method: "PUT", headers, body, credentials: "omit", mode: "cors" }),
  },
  send: (request) =>
    messagesApi.sendMessage({
      conversationId: request.conversationId,
      sendMessageRequest: {
        clientMessageId: request.clientMessageId,
        type:
          request.kind === "image"
            ? SendMessageRequestTypeEnum.Image
            : request.kind === "voice"
              ? SendMessageRequestTypeEnum.Voice
              : SendMessageRequestTypeEnum.File,
        payload: {
          ...(request.caption === "" ? {} : { text: request.caption }),
          ...(request.durationMs === undefined ? {} : { durationMs: request.durationMs }),
        },
        attachmentIds: [request.attachmentId],
      },
    }),
};

/**
 * Поиск человека по адресу (`GET /users?email=`) — операция рядом с прочими, и по
 * той же причине: у диалога нет ни адреса API, ни токена.
 *
 * Отдаётся **не** список, а объект или отказ, и это форма ответа сервера, а не
 * упрощение: совпадение точное, адрес уникален (частичный индекс
 * `users_email_live_uniq`), и «нашлось двое» — состояние, которого не бывает
 * (`D1`). Поэтому и клиент не приносит сюда массива.
 *
 * Нормализация адреса живёт **на сервере** (`normalize_email`): сделай её здесь —
 * и правило приведения адреса завелось бы вторым, а расхождение с регистрацией
 * всплыло бы тем, что человек не нашёл себя же в другом регистре.
 */
const searchUser: SearchUser = (email) => usersApi.findUserByEmail({ email });

/**
 * Создание личной беседы (`POST /conversations`) — операция оттуда же.
 *
 * `participant_id` приходит **готовым** — из ответа поиска, — и это главное, что
 * здесь есть: беседу заводит подтверждённый человек, а не строка адреса, которую
 * никто не проверял (`D2`). Заведись поле адреса в запросе, собеседника по строке
 * выбирал бы сервер, а человек подтверждал бы то, чего не видел.
 *
 * Ответ — **модель API**, а не модель интерфейса: беседа приходит без зрителя, и
 * `sender_id` в ней ещё не переведён в `"me"`. Адаптация — работа `ChatPage`, где
 * `currentUserId` под рукой; здесь её взять негде, и вторая модель строки списка
 * разошлась бы с первой.
 *
 * Маршрут идемпотентен по составу участников (`ensure_direct_conversation`,
 * `ON CONFLICT (direct_key)`): повторное подтверждение вернёт **ту же** беседу, а
 * не заведёт вторую, и контракт ради этого не меняется (`D8`).
 */
const createConversation: CreateConversation = (participantId) =>
  conversationsApi.createDirectConversation({
    createDirectConversationRequest: { participantId },
  });

/**
 * Повторная отправка письма о подтверждении (`POST /auth/verify-email/resend`)
 * — операция оттуда же, `G3-007-1a`.
 *
 * `authApi` уже обёрнут `withUnwrappedErrors` выше: `202` отдаёт `void`, отказ
 * доходит до баннера как `ApiProblem`/`ServiceUnavailableError`, а не как
 * `FetchError` — той же причиной, что и у остальных операций.
 */
const resendVerificationEmail: ResendVerificationEmail = () =>
  authApi.resendVerificationEmail();

/**
 * Хранилище очереди — одна константа на приложение, а не по объекту на панель.
 *
 * Открытие базы стоит денег, а читать её очередь обязана **один раз на
 * монтирование**: второй объект дал бы второй круг восстановления, и запись
 * восстановилась бы дважды (безопасно, но лишним запросом).
 *
 * **Названное отступление от `D7`.** Требование «выход из системы чистит очередь
 * целиком» здесь **не исполнено**, и не по недосмотру: выхода из системы в
 * приложении нет вовсе — ни кнопки, ни операции. `CurrentUserFooter` рисует
 * только настройки, `SessionsApi.revokeSession` зовётся лишь из вычеркнутой
 * страницы, а `App` не имеет даже пропа `onSignOut`. Поэтому `clearForLogout`
 * остаётся без вызова, а заведение выхода — отдельная работа (кнопка, операция,
 * возврат в Keycloak), которая в объём этого гейта не входит. Названо вслух,
 * чтобы отсутствие вызова не выглядело забывчивостью.
 */
const outboxStore: OutboxStore = createOutboxStore();

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
      sendMessage={sendMessage}
      searchUser={searchUser}
      createConversation={createConversation}
      resendVerificationEmail={resendVerificationEmail}
      outboxStore={outboxStore}
      readCentrifugoUrl={readCentrifugoUrl}
      issueTicket={issueTicket}
      telemetry={telemetryClient}
      attachments={attachmentClient}
      pushSubscriptions={pushSubscriptionApi}
      calls={callsOperations}
      issueCallsTicket={issueCallsTicket}
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
