import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ConversationSidebar } from "./components/ConversationSidebar"
import { ChatHeader } from "./components/ChatHeader"
import { NewConversationDialog } from "./components/NewConversationDialog"
import type { CreateConversation, SearchUser } from "./components/NewConversationDialog"
import { adaptConversation, adaptConversations, withSentPreview } from "./adapter"
import { adaptUnreadChanged, withUnreadOverlay } from "./unreadOverlay"
import type { Conversation as ConversationDto, ConversationListPage, Message } from "../../api/generated"
import {
  adaptMessage,
  adaptPublication,
  adaptReadReceipt,
  attachmentCountOf,
} from "../messages/message-adapter"
import { playIncomingMessageSound } from "../messages/notificationSound"
import { confirmedClientIds } from "../messages/eventMerge"
import { MessageComposer } from "../messages/components/MessageComposer"
import { MessageList } from "../messages/components/MessageList"
import { useOutbox } from "../messages/outbox/useOutbox"
import type { OutboxStore } from "../messages/outbox/outboxStore"
import type { SendMessage } from "../messages/outbox/useOutbox"
import { advance, deliveryStateOf } from "../receipts/receiptWatermarks"
import type { Watermarks } from "../receipts/receiptWatermarks"
import { useReceipts } from "../receipts/useReceipts"
import type { SendReceipt } from "../receipts/useReceipts"
import type { HistorySource } from "../messages/history"
import {
  useConversationHistory,
  type ConversationHistory,
} from "../messages/useConversationHistory"
import { EmailVerificationBanner } from "../auth/components/EmailVerificationBanner"
import type { ResendVerificationEmail } from "../auth/components/EmailVerificationBanner"
import { ConnectionStatusLine } from "../realtime/components/ConnectionStatusLine"
import type { CentrifugeFactory } from "../realtime/realtimeClient"
import { useRealtimeConnection } from "../realtime/useRealtimeConnection"
import type { ConnectionEvent, ConnectionMachineState } from "../realtime/connectionMachine"
import {
  INITIAL_CONNECTION_TELEMETRY_CONTEXT,
  mapConnectionEvent,
} from "../telemetry/connectionTelemetry"
import type { TelemetryClient } from "../telemetry/telemetryClient"
import type { AttachmentClient } from "../attachments/attachmentUpload"
import { useAttachmentDraft } from "../attachments/useAttachmentDraft"
import { MessengerLayout } from "../../shared/ui/MessengerLayout"
import type {
  ChatMessage,
  Conversation,
  CurrentUser,
  PendingMessage,
} from "../../shared/lib/types"

/**
 * Пустой оверлей — **одна** карта на модуль, а не новая на каждый сброс.
 *
 * Свежая карта на каждый `setOverlay(new Map())` была бы новым состоянием при
 * том же значении: React сравнивает ссылки, и каждый сброс вызывал бы лишний
 * рендер списка на ровном месте.
 */
const EMPTY_OVERLAY: ReadonlyMap<string, number> = new Map()

/**
 * Пустой список записей очереди — **одна** ссылка на модуль.
 *
 * Та же причина, что у `EMPTY_OVERLAY`: свежий `[]` — новое значение при том же
 * смысле, и получатель (`MessageList`) считал бы проп изменившимся на каждом
 * рендере беседы, которой очередь не касается вовсе.
 */
const EMPTY_PENDING: readonly PendingMessage[] = []

/**
 * Лента **одной** беседы, какой её видит очередь, — и только то, что ей нужно.
 *
 * Очередь живёт выше панели (см. довод у `useOutbox`), а лента — внутри неё, и
 * это единственное место, где две половины сходятся. Отдаётся ровно два поля, а
 * не весь `ConversationHistory`: очередь не должна знать ни про границу слияния,
 * ни про догрузку, ни про `applyMessage` — иначе она завела бы второе место,
 * знающее про ленту, ровно то, что запрещает `eventMerge.confirmedClientIds`.
 *
 * `conversationId` здесь не для красоты: очередь одна на все беседы, а ответ
 * приходит на **свою**, и вложение ответа в чужую ленту показало бы сообщение
 * там, где его не отправляли. Проверка стоит на стороне, которая это знает.
 */
interface FeedEntry {
  readonly conversationId: string
  readonly accept: (message: ChatMessage) => void
}

interface ChatPageProps {
  /** Беседы из `GET /conversations`. Фикстур здесь нет и быть не может. */
  conversations: Conversation[]
  /**
   * Перечитать список бесед — повод сверки (`D11`).
   *
   * Отдаёт **клиентский** тип: адаптация под зрителя идёт здесь, там же, где
   * лежит `currentUserId`, — тем же `adaptConversations`, что и на загрузке.
   * Второго способа собрать список не появляется.
   */
  refreshConversations: () => Promise<ConversationListPage>
  currentUser: CurrentUser
  /** Зритель в терминах домена: им `sender_id` переводится в `"me"`. */
  currentUserId: string
  /** Две дороги истории поверх клиента API — собираются в `main.tsx`. */
  history: HistorySource
  centrifugoUrl: string
  /** Свежий тикет на каждую попытку соединения (`POST /realtime/token`). */
  issueTicket: () => Promise<string>
  /**
   * Отправка квитанции (`POST /conversations/{id}/receipts`) — собирается в
   * `main.tsx`, как и остальные клиенты API.
   *
   * Обязана быть **устойчивой** (модуль или `useCallback`): её смена
   * перезапускает дребезг в `useReceipts`, и нестабильная ссылка откладывала бы
   * отправку на каждом рендере — вкладка не сообщила бы ничего и никогда.
   */
  sendReceipts: SendReceipt
  /**
   * Отправка сообщения — операция, собранная в `main.tsx`, как и квитанция.
   *
   * Устойчивость ссылки здесь нужна меньше, чем у `sendReceipts` (там её смена
   * перезапускала бы дребезг), но по той же причине: очередь держит её в
   * зависимостях попытки, и новая ссылка на каждом рендере пересобирала бы
   * замыкание попытки без всякой пользы.
   */
  sendMessage: SendMessage
  /**
   * Хранилище очереди — **на уровень списка**, а не панели беседы.
   *
   * Панель пересоздаётся на каждой смене беседы (`key` ниже), и очередь,
   * живущая в ней, обнулялась бы вместе с ней: черновик, ушедший в офлайне,
   * исчезал бы от одного клика по другой беседе. Здесь `key` нет ни у
   * `ChatPage`, ни у чего-либо выше него, поэтому очередь переживает и смену
   * беседы, и (через IndexedDB) перезагрузку.
   */
  outboxStore: OutboxStore
  /**
   * Поиск человека по адресу (`GET /users?email=`) — операция из `main.tsx`.
   *
   * Диалог получает её **готовой**: у компонента нет ни адреса API, ни токена,
   * и собирать `UsersApi` здесь значило бы завести второй способ говорить с
   * сервером рядом с тем, которым говорят все остальные.
   */
  searchUser: SearchUser
  /**
   * Создание личной беседы (`POST /conversations`) — оттуда же.
   *
   * Отдаёт модель API: беседа, которую вернул сервер, кладётся в список
   * **здесь**, тем же `adaptConversation`, что и список из `GET /conversations`.
   * Второго способа собрать строку списка не появляется.
   */
  createConversation: CreateConversation
  /**
   * `POST /auth/verify-email/resend` — операция оттуда же, `G3-007-1a`.
   *
   * Баннер получает её **готовой** по той же причине, что и `searchUser`: у
   * компонента нет ни адреса API, ни токена.
   */
  resendVerificationEmail: ResendVerificationEmail
  /** Подмена SDK — для компонентных тестов; в production не задаётся. */
  createCentrifuge?: CentrifugeFactory
  /** G3-008: приёмник best-effort телеметрии доставки. */
  telemetry: Pick<TelemetryClient, "record">
  /** G4: вложения. Без него композер остаётся прежним, только текст. */
  attachments?: AttachmentClient
}

/**
 * Главная панель: выбор беседы и **одна** дорога данных для любой из них.
 *
 * Ветки по `hasMessages` здесь больше нет, и это главная правка гейта. Раньше
 * флаг из **списка бесед** делил путь на два: с историей — заглушка «history
 * isn't loaded yet», без истории — `EmptyConversationState`, и realtime-путь не
 * запускался вовсе. Пустая беседа оставалась без соединения и без применённой
 * границы, и первое же сообщение (`seq = 1`) некуда было применить: сервер
 * подписывает клиента на канал сам, публикация приходит, а у активной беседы
 * нет ни границы, ни ленты.
 *
 * Теперь путь один: `connect → снимок → рендер`. Выбранная беседа всегда
 * проходит соединение и хвост, а пустота выясняется **из применённого пустого
 * снимка** (`{"items": []}` → граница `0`), а не из флага списка. Флаг
 * `hasMessages` говорит лишь о последнем сообщении на момент чтения списка — и
 * после первого же нового сообщения перестаёт быть правдой, тогда как снимок
 * остаётся фактом.
 *
 * `MessageComposer` (отправка, `G3-007-1`), `MessageBubble` (оформление
 * строки, тот же гейт) и `EmailVerificationBanner` (`G3-007-1a`) подключены.
 * `MessageTimeline`, `TypingIndicator`, `SyncIndicator` и
 * `ConnectionStateBanner` — по-прежнему нет, они остаются проектным запасом
 * (закрытый список `B15`). Наблюдаемость ленты не отменена оформлением:
 * `MessageList` единолично владеет DOM-маркерами приёмки и видимостью строк
 * для квитанций, `MessageBubble` рисует только содержимое узла.
 *
 * **Баннер не трогает композер.** `EmailVerificationBanner` показывается по
 * `currentUser.emailVerified === false`, но `MessageComposer` ниже не знает
 * про `emailVerified` вовсе — и это исполнение домена, а не пропуск: до
 * подтверждения почты у `Capability.SEND_MESSAGE` отказа нет
 * (`_UNVERIFIED = {READ, SEND_MESSAGE}`, `domain/user.py`), под запретом
 * только `START_CONVERSATION`. Ветка «неподтверждён, но пишет в уже
 * существующей беседе» — не то, что здесь чинится, а то, чему нельзя дать
 * сломаться: гасить композер по одному лишь `emailVerified` значило бы
 * молча сузить домен до того, что удобнее нарисовать.
 *
 * **Список бесед живёт здесь двумя половинами: базой и оверлеем.** База — то,
 * что принёс REST; оверлей — числа из `unread.changed`, пришедшие в личный
 * канал вкладки. Две половины, а не одно накопительное число, потому что
 * транспорт у них разный: событие доставляется best-effort и может не прийти
 * вовсе, а ответ REST приходит всегда. Сверка (`D11`) **заменяет** базу и
 * обнуляет оверлей, а не правит прежнее число относительно нового: потерянную
 * публикацию накопление не чинит, а замена чинит целиком. Пути к истине у
 * счётчика поэтому три: событие, сверка при возврате вкладки и перезагрузка.
 *
 * Повод сверки берётся **по переходу**, а не по факту «мы подключены»: панель
 * беседы пересоздаётся на каждой смене беседы (`key` ниже), и сверка на
 * монтирование давала бы по лишнему кругу REST на каждое переключение. Сверяем
 * на возврат видимости (`visibilitychange → visible`, здесь) и на выход из
 * `disconnected`/`degraded` (там же, где видно состояние соединения).
 */
export function ChatPage({
  conversations,
  refreshConversations,
  currentUser,
  currentUserId,
  history,
  centrifugoUrl,
  issueTicket,
  sendReceipts,
  sendMessage,
  outboxStore,
  searchUser,
  createConversation,
  resendVerificationEmail,
  createCentrifuge,
  telemetry,
  attachments,
}: ChatPageProps) {
  // Ленивая инициализация, а не `?? conversations[0]` в рендере: запасного
  // значения у настоящих данных нет, а пустой список — законный ответ сервера.
  const [activeId, setActiveId] = useState<string | null>(() => conversations[0]?.id ?? null)
  /**
   * Диалог создания беседы — состояние **списка**, а не панели.
   *
   * Панель пересоздаётся на каждой смене беседы (`key` ниже), и диалог,
   * живущий в ней, закрывался бы от создания первой же беседы — а его задача
   * как раз пережить создание: после подтверждения он открывает созданную
   * беседу и закрывается сам, по факту, а не по перерисовке.
   */
  const [creatingConversation, setCreatingConversation] = useState(false)

  /**
   * База — из пропсов, и это не дублирование состояния: пропсы приходят из
   * `BootState` и меняются загрузкой (повтор после отказа), тогда как база
   * меняется ещё и сверкой.
   */
  const [base, setBase] = useState<Conversation[]>(conversations)
  const [overlay, setOverlay] = useState<ReadonlyMap<string, number>>(EMPTY_OVERLAY)
  /** Какой ответ загрузки база с оверлеем уже приняли — по ссылке на массив. */
  const [loadedFrom, setLoadedFrom] = useState(conversations)

  if (loadedFrom !== conversations) {
    // Пришли новые данные загрузки — оверлей снимается вместе с ними: ответ
    // REST и есть истина, а наложенное поверх него число было бы утверждением
    // старше только что полученного (D11).
    //
    // Правка **при рендере**, а не эффектом, и это не вкусовщина: эффект
    // срабатывает после отрисовки, то есть кадр со старым числом успевает
    // показаться — ровно то утверждение, которое D11 и запрещает. Синхроннее
    // здесь нечего: пришли те же данные заново, и React знает об этом раньше,
    // чем о сработавшем эффекте. Сравнение по ссылке, а не по содержимому:
    // список приходит новым массивом тогда, когда его читали заново.
    setLoadedFrom(conversations)
    setBase(conversations)
    setOverlay(EMPTY_OVERLAY)
  }

  /**
   * Число из личного канала: **ставится**, а не прибавляется.
   *
   * Публикация best-effort, и повтор доставки возможен — пачка Kafka приезжает
   * второй раз. Сложение на повторе сдвинуло бы счётчик вверх ровно там, где
   * исправить его нечем.
   */
  const onUnreadPublication = useCallback((payload: unknown) => {
    const change = adaptUnreadChanged(payload)
    if (change === null) return

    setOverlay((current) => {
      const next = new Map(current)
      next.set(change.conversationId, change.unreadCount)
      return next
    })
  }, [])

  /** Сверка: круг REST за истиной. Общий на оба повода — ответ один и тот же. */
  const refreshing = useRef(false)
  const refresh = useCallback(async () => {
    // Два повода могут совпасть (возврат видимости сразу за выходом из
    // разрыва). Второй круг при этом не отменяется, а **не начинается**:
    // ответ на него был бы тем же, а первый ещё в пути.
    if (refreshing.current) return
    refreshing.current = true
    try {
      const page = await refreshConversations()
      // Момент времени — свой, а не тот, что был на загрузке: список
      // перечитывается сейчас, и display-время обязано считаться от этого
      // момента, иначе «14:22» уехало бы в прошлое от самой сверки.
      setBase(adaptConversations(page, currentUserId, new Date()))
      setOverlay(EMPTY_OVERLAY)
    } catch {
      // Отказ сверки оставлен без строки на экране, и это не «проглочено»:
      // сверка не обещает ничего нового — она заменяет уже показанное более
      // свежим. Неудача оставляет ровно то, что человек и видел, ложного
      // утверждения на экране не появляется, а следующий повод сходит за
      // истиной снова. Строка «не удалось обновить» сообщала бы о нашей
      // неудаче там, где ни одно число не стало неправдой.
    } finally {
      refreshing.current = false
    }
  }, [refreshConversations, currentUserId])

  useEffect(() => {
    // Обработчик зовётся и на уход в фон — «видимость кончилась» не повод
    // идти за списком: сверка нужна там, где вкладка могла пропустить
    // события, а не там, где она от них ушла.
    const onVisibilityChange = () => {
      if (document.visibilityState !== "visible") return
      void refresh()
    }

    document.addEventListener("visibilitychange", onVisibilityChange)
    return () => document.removeEventListener("visibilitychange", onVisibilityChange)
  }, [refresh])

  /**
   * Своё сообщение в строке списка — **от отправки**, а не от `message.created`
   * (`D13`).
   *
   * Довод тот же, что у сверки: событие доставляется best-effort, и строка,
   * ждущая его, показывала бы «ничего не отправлено» там, где отправлено.
   * Применяется к **базе**, а не к слитому списку: оверлей непрочитанного
   * говорит о числах, а превью и порядок — о самой беседе, и накладывать одно
   * на другое значило бы собирать список вторым способом.
   *
   * `pending` приходит снаружи, а не выводится из очереди: нажатие помечает
   * неподтверждённое, ответ снимает пометку — и оба состояния ставит тот, кто
   * знает, что произошло.
   */
  const touchConversation = useCallback(
    (conversationId: string, text: string, pending: boolean) => {
      setBase((current) =>
        withSentPreview(current, { conversationId, text, at: new Date(), pending }),
      )
    },
    [],
  )

  /**
   * Созданная беседа — в список и на экран, без второго круга REST (`D10`).
   *
   * Ответ `POST /conversations` **уже содержит** беседу, поэтому сверка здесь
   * была бы вторым запросом за тем, что пришло. Беседа кладётся сверху — она
   * свежая по построению.
   *
   * Уже знакомая беседа **не заменяется**: `POST` идемпотентен и умеет вернуть
   * существующую (`ensure_direct_conversation`, `D8`), и подмена показанной
   * строки ответом, который о ней ничего нового не говорит, потеряла бы то, что
   * список уже знает (например, счётчик непрочитанного, если сервер его в
   * создании не назвал). Предмет здесь — открыть беседу, а не переписать список.
   */
  const handleConversationCreated = useCallback(
    (dto: ConversationDto) => {
      const conversation = adaptConversation(dto, currentUserId, new Date())
      setBase((current) =>
        current.some((item) => item.id === conversation.id) ? current : [conversation, ...current],
      )
      setActiveId(conversation.id)
      setCreatingConversation(false)
    },
    [currentUserId],
  )

  // Слияние — на каждом рендере списка, а не при приходе события: иначе
  // пришлось бы держать согласие между двумя состояниями в руках, и число
  // отставало бы от базы ровно на один рендер.
  const merged = useMemo(() => withUnreadOverlay(base, overlay), [base, overlay])

  /**
   * Лента активной беседы — в ссылке, потому что её владелец объявлен **ниже**.
   *
   * Направление выбрано измерением, а не вкусом: очередь обязана быть выше
   * панели (иначе запись умирает на смене беседы), а лента живёт в панели, и
   * `key` пересоздаёт её при каждом переключении. Значит связь идёт **вверх** —
   * панель сообщает о себе, очередь пользуется. Обратное направление потребовало
   * бы либо поднять ленту в `ChatPage` (и потерять изоляцию хука от `key`), либо
   * положить очередь в панель (и потерять саму очередь).
   *
   * Ссылка, а не состояние: `onSent` зовётся из промиса попытки, и записанное в
   * состояние значение было бы снимком того рендера, в котором попытка началась.
   */
  const feedRef = useRef<FeedEntry | null>(null)
  const attachFeed = useCallback((entry: FeedEntry | null) => {
    feedRef.current = entry
  }, [])

  /**
   * Ответ сервера — **тем же** путём, что история и realtime (D6).
   *
   * Ни здесь, ни в очереди нет второй сборки модели: `adaptMessage` и
   * `acceptPublication` — те же две функции, которыми в ленту входит публикация.
   * Положить ответ мимо них значило бы завести второй путь слияния, и первое же
   * расхождение — разрыв номеров, который второй путь не распознает, — осталось
   * бы незамеченным.
   *
   * `new Date()` здесь на месте, в отличие от тестов адаптера: часы нужны ровно
   * затем, чтобы отформатировать отметку, а не чтобы получить проверяемое
   * значение. Запись при этом **не снимается** — снимет её `settle` по факту
   * появления сообщения в ленте (`confirmedClientIds`), потому что при разрыве
   * номеров ответа в ленте ещё нет.
   *
   * Беседа сверяется с записью: ответ на чужую ленту — не «сообщение не
   * отправилось», а «его не туда положили», и различить это здесь есть чем.
   */
  const onSent = useCallback(
    (record: PendingMessage, response: Message) => {
      // Строка списка обновляется **до** вложения в ленту и независимо от него:
      // превью и порядок — предмет списка, а лента может эту беседу и не
      // показывать (человек ушёл в другую). Помечая подтверждённым, а не
      // неподтверждённым: сервер уже ответил, и держать пометку дальше значило
      // бы утверждать «не отправлено» о принятом.
      touchConversation(record.conversationId, record.text, false)

      const entry = feedRef.current
      if (entry === null || entry.conversationId !== record.conversationId) return

      entry.accept(adaptMessage(response, currentUserId, new Date()))
    },
    [currentUserId, touchConversation],
  )

  /**
   * Очередь исходящих — **здесь**, а не в панели беседы.
   *
   * Место выбрано по измеренному свойству: панель пересоздаётся на каждой смене
   * беседы (`key` ниже), и очередь, живущая в ней, обнулялась бы вместе с ней —
   * черновик, ушедший в офлайне, исчезал бы от одного клика по другой беседе.
   * Здесь `key` нет ни у `ChatPage`, ни у чего-либо выше, поэтому запись
   * переживает смену беседы, а хранилище — ещё и перезагрузку.
   *
   * Очередь **одна на все беседы** (хранилище тоже одно), и запись помнит свою:
   * читать IndexedDB заново при каждом переключении значило бы платить за то,
   * что не менялось, и повторно запускать попытку для всех записей сразу.
   */
  const outbox = useOutbox({ store: outboxStore, send: sendMessage, onSent })

  /**
   * Записи **своей** беседы: очередь одна на все, а лента показывает одну.
   *
   * Фильтр здесь, а не в ленте. `MessageList` получает уже своё — и это не
   * размещение кода, а отказ от второго места, знающего про беседы: такое место
   * разошлось бы с тем, которое чистит очередь, и разошлось бы молча (тот же
   * довод, что у фильтра `pending` в докстринге ленты).
   */
  const pendingForActive = useMemo(
    () =>
      activeId === null
        ? EMPTY_PENDING
        : outbox.pending.filter((record) => record.conversationId === activeId),
    [outbox.pending, activeId],
  )

  const activeConversation = merged.find((c) => c.id === activeId) ?? null

  return (
    <>
      <MessengerLayout
        // `=== false`, а не `!currentUser.emailVerified`: `undefined` —
        // «сервер об этом не сообщал», а не «не подтверждён» (тот же довод,
        // что у `peerReadState`/`lastSeenAt` в `shared/lib/types.ts`), и
        // показывать баннер на одной лишь неизвестности значило бы утверждать
        // то, чего `/me` не говорил. Адрес тоже проверяется: без него банеру
        // нечего вставить в «Confirm ‹email›».
        banner={
          currentUser.emailVerified === false && currentUser.email !== undefined ? (
            <EmailVerificationBanner email={currentUser.email} resend={resendVerificationEmail} />
          ) : undefined
        }
        sidebar={
          <ConversationSidebar
            conversations={merged}
            activeConversationId={activeConversation?.id ?? null}
            currentUser={currentUser}
            onSelectConversation={setActiveId}
            // Проп передан — кнопка новой беседы **есть** (D10). До этого среза
            // он оставался непереданным, и кнопки не существовало вовсе: не
            // «спрятана», а не нарисована.
            onNewConversation={() => setCreatingConversation(true)}
          />
        }
      >
        {activeConversation === null ? (
          // Шапки нет: шапка — утверждение о выбранной беседе, а её нет.
          <div className="flex flex-1 items-center justify-center px-6 text-center">
            <p className="text-sm text-text-warm-secondary">No conversations</p>
          </div>
        ) : (
          /**
           * `key` ставит **владелец выбора беседы**, и стоит он над компонентом,
           * который держит хук, а не внутри него.
           *
           * Отступление от буквы плана названо: там `key` стоит на самом
           * `ChatPage`, а выбор беседы живёт в `App`. Вынести его туда значило бы
           * переписать `App` (состояние выбора плюс сайдбар) и не добавить ни
           * одного наблюдаемого свойства — требование («смена беседы сбрасывает
           * состояние ленты») исполнено ровно так же, потому что React
           * пересоздаёт по `key` **родительский** элемент. `key`, написанный
           * внутри самого `ChatPage`, собственный state хука не сбросил бы: тот
           * живёт в том же компоненте, который `key` не пересоздаёт.
           */
          <ConversationPane
            key={activeConversation.id}
            conversation={activeConversation}
            currentUserId={currentUserId}
            history={history}
            centrifugoUrl={centrifugoUrl}
            issueTicket={issueTicket}
            sendReceipts={sendReceipts}
            createCentrifuge={createCentrifuge}
            pending={pendingForActive}
            // Беседа известна **здесь**, а не в композере: `enqueue` требует
            // беседу, а композер о беседах не знает вовсе — он знает только имя
            // собеседника для подсказки в поле.
            //
            // Строка списка двигается **в момент нажатия**, с пометкой «ещё не
            // подтверждено» (D13): человек уже написал, и список, ждущий сервера,
            // показывал бы старое превью под только что отправленным текстом.
            onSend={(text) => {
              outbox.enqueue(activeConversation.id, text)
              touchConversation(activeConversation.id, text, true)
            }}
            onUnreadPublication={onUnreadPublication}
            onReconcile={refresh}
            // Лента сообщает о себе — очередь этим пользуется, чтобы вложить ответ
            // в **свою** беседу (довод у `FeedEntry`).
            onFeedReady={attachFeed}
            // Обратное движение: подтверждённое дошло до ленты — снять запись.
            // Снимает очередь, а решает лента: вопрос «что подтверждено» знает
            // `eventMerge`, вопрос «что снять» — `confirmedClientIds`.
            onConfirmed={outbox.settle}
            telemetry={telemetry}
            attachments={attachments}
          />
        )}
      </MessengerLayout>
      {/*
        Диалог — **вне** раскладки и вне панели беседы: панель пересоздаётся по
        `key` при смене беседы, а диалог закрывается сам, по факту создания, и
        перерисовка панели его бы закрыла раньше.
      */}
      {creatingConversation && (
        <NewConversationDialog
          searchUser={searchUser}
          createConversation={createConversation}
          onCreated={handleConversationCreated}
          onClose={() => setCreatingConversation(false)}
        />
      )}
    </>
  )
}

interface ConversationPaneProps {
  conversation: Conversation
  currentUserId: string
  history: HistorySource
  centrifugoUrl: string
  issueTicket: () => Promise<string>
  /** Транспорт квитанции — тот же, что у панели: беседа уже известна. */
  sendReceipts: SendReceipt
  createCentrifuge?: CentrifugeFactory
  /** Записи очереди **этой** беседы; отбор сделан уровнем выше. */
  pending: readonly PendingMessage[]
  /** Человек нажал Send: текст уходит в очередь, тождество чеканится там же. */
  onSend: (text: string) => void
  /** Публикация личного канала — наверх, к списку: число принадлежит не беседе. */
  onUnreadPublication: (payload: unknown) => void
  /** Выход из разрыва — повод сверки списка. */
  onReconcile: () => void
  /**
   * «Вот моя лента», и `null` — «её больше нет».
   *
   * Панель умирает по `key` при каждой смене беседы, и очередь обязана узнать об
   * этом **сама**: ссылка на мёртвую ленту вложила бы ответ сервера в беседу,
   * которой на экране нет.
   */
  onFeedReady: (entry: FeedEntry | null) => void
  /** Подтверждённое дошло до ленты — вот его `client_message_id`. */
  onConfirmed: (clientMessageIds: ReadonlySet<string>) => void
  /** G3-008: приёмник best-effort телеметрии доставки. */
  telemetry: Pick<TelemetryClient, "record">
  attachments?: AttachmentClient
}

/**
 * Панель одной беседы: соединение, снимок, лента, квитанция.
 *
 * Живёт отдельным компонентом ради `key` (см. выше) и ради порядка хуков —
 * порядок объявления здесь и есть порядок эффектов.
 *
 * ## Квитанция наблюдаема двумя числами, и каждое — о разном (D6)
 *
 * `data-my-read-seq` — то, что вкладка **сообщила** о прочтении; отсутствие
 * атрибута значит «не сообщала ничего», и это не ноль. `data-peer-read-seq` —
 * то, что сообщил собеседник: здесь ноль настоящее («прочтено ни до чего»), а
 * отсутствие — «он молчит». По этим двум числам приёмка читает обе половины
 * `RCP-001` (прочтение уехало вперёд и назад не поехало), не сканируя ленту.
 * Третье наблюдаемое место — `data-message-state` на строке: состояние **своего**
 * сообщения по номерам собеседника.
 */
function ConversationPane({
  conversation,
  currentUserId,
  history,
  centrifugoUrl,
  issueTicket,
  sendReceipts,
  createCentrifuge,
  pending,
  onSend,
  onUnreadPublication,
  onReconcile,
  onFeedReady,
  onConfirmed,
  telemetry,
  attachments,
}: ConversationPaneProps) {
  /**
   * Лента этого окна — в ссылке, потому что публикации достаются обработчику,
   * который объявлен **раньше** самого хука.
   *
   * Так сделано намеренно: соединение обязано подниматься первым (B21), а его
   * `onPublication` — единственный вход публикаций в ленту. Порядок объявления
   * хуков даёт порядок эффектов, и к моменту, когда придёт первая публикация,
   * эта ссылка уже заполнена: сеть приходит не раньше следующей задачи
   * событийного цикла, а эффекты к тому времени выполнены все. Отсюда и
   * `?.` — не «может не быть», а «не имеет права уронить соединение».
   */
  const historyRef = useRef<ConversationHistory | null>(null)

  /**
   * Номера собеседника: докуда доехало до его устройства и докуда он прочитал.
   *
   * `null` — «собеседник не сообщал ничего», и это **не пара нулей**: вывести
   * ноль из молчания значило бы объявить прочтение, которого он не делал, и
   * откатить уже показанную отметку. Живут эти числа независимо от присутствия и
   * отметки времени (`last_seen_at` приходит тем же ответом, но отвечает на
   * другой вопрос), и переживают потерю realtime-слоя — потому что истину
   * приносит REST (`D11`), а событие лишь ускоряет.
   */
  const [peer, setPeer] = useState<Watermarks | null>(null)

  /**
   * Максимум по целиком видимым строкам — единственный источник «прочитано».
   *
   * Ноль здесь — «ни одна строка ещё не видна целиком», и это честное значение:
   * беседа открыта, но человек не прочитал ничего. Своего сторожа монотонности у
   * этого состояния нет намеренно — он уже стоит у ленты (`reported` в
   * `MessageList`) и в отправке (`receiptToSend` шлёт только строго большее).
   * Второй сторож был бы неотличим от первого и не проверялся бы ничем.
   */
  const [visibleThroughSeq, setVisibleThroughSeq] = useState(0)

  const peerUserId = conversation.peerUserId

  /**
   * Состояние чтения из ответа REST — истина, на которой стоит вся ветка «без
   * единого события»: вкладка, открытая **после** квитанции, узнаёт её этим
   * путём и только им. Максимум, а не присваивание: ответ может прийти и
   * старше уже принятого события, и откатить показанное им было бы нельзя.
   * `advance` возвращает **тот же** объект, когда новостей нет, поэтому
   * повторный ответ состояния не двигает.
   */
  useEffect(() => {
    const state = conversation.peerReadState
    if (state === undefined) return

    setPeer((current) => advance(current, state))
  }, [conversation.peerReadState])

  const onPublication = useCallback(
    (payload: unknown) => {
      const message = adaptPublication(payload, currentUserId)

      if (message !== null && attachmentCountOf(payload) > 0) {
        // G4: сообщение с вложением. Голая публикация показала бы подпись
        // без файла, а ссылка на файл в канал не едет (`attachmentCountOf`),
        // поэтому за ним идёт история: тот же `startSync`, что у разрыва
        // номеров, но без смены состояния соединения — расхождения нет, есть
        // недостающее содержимое. Номер в ленту не кладётся заранее: сошедшаяся
        // догрузка положит сообщение целиком, а не дважды.
        void historyRef.current?.startSync()
        if (message.authorId !== "me") {
          playIncomingMessageSound()
          telemetry.record({
            type: "message_received",
            occurredAt: new Date(),
            messageId: message.id,
            conversationId: conversation.id,
          })
        }
        return
      }

      if (message !== null) {
        historyRef.current?.acceptPublication(message)
        // Звук — только чужому сообщению: своё уже названо отправкой (D13),
        // и звонок по нему сообщил бы человеку о том, что он только что
        // сделал сам.
        if (message.authorId !== "me") {
          playIncomingMessageSound()
          // G3-008: t6 - получено по WS. `t7` (`delivery_ack`) - двойной
          // `requestAnimationFrame`: первый кадр после коммита состояния в
          // `historyRef`, второй - после того, как браузер его отрисовал.
          // Это приближение «отрисовано», не доказательство видимости в
          // вьюпорте (та проверка - у `MessageList`, по другому поводу,
          // квитанции прочтения); честное имя для него - `message_rendered`,
          // не `message_received` второй раз.
          telemetry.record({
            type: "message_received",
            occurredAt: new Date(),
            messageId: message.id,
            conversationId: conversation.id,
          })
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              const renderedAt = new Date()
              telemetry.record({
                type: "message_rendered",
                occurredAt: renderedAt,
                messageId: message.id,
                conversationId: conversation.id,
              })
              telemetry.record({
                type: "delivery_ack",
                occurredAt: renderedAt,
                messageId: message.id,
                conversationId: conversation.id,
              })
            })
          })
        }
        return
      }

      // `null` здесь — не сбой, а измеренная форма канала: `message.read` и
      // `message.deleted` приходят по тому же каналу, но сообщениями не
      // являются. Падение внутри этого обработчика унесло бы соединение.
      const receipt = adaptReadReceipt(payload)

      // Квитанция **собеседника** — и только его. Своя приходит на тот же канал
      // беседы, и различает их один `reader_id`; прими мы свою за чужую — и
      // человек увидел бы собственное прочтение как прочтение собеседника
      // (тот же класс, что «имя из `participants[0]`» в адаптере). Беседе без
      // названного собеседника квитанция не приписывается вовсе: в группе
      // «его» номера не существует, и выбрать любого значило бы назвать
      // собеседником случайного человека.
      if (receipt === null || peerUserId === undefined || receipt.readerId !== peerUserId) return

      // Только максимум (D6а): отставшее событие со старого устройства не
      // двигает ни одно число.
      setPeer((current) => advance(current, receipt))
    },
    [currentUserId, peerUserId, conversation.id, telemetry],
  )

  // G3-008: контекст телеметрии соединения живёт здесь, не в чистом
  // мапере (`mapConnectionEvent` остаётся функцией без памяти, тем же
  // доводом, что и у `connectionMachine` самого). Ссылка, а не `useState`:
  // смена контекста не должна перерисовывать панель.
  const connectionTelemetryContext = useRef(INITIAL_CONNECTION_TELEMETRY_CONTEXT)

  const onObservedEvent = useCallback(
    (event: ConnectionEvent, before: ConnectionMachineState) => {
      const mapped = mapConnectionEvent(event, before, connectionTelemetryContext.current)
      connectionTelemetryContext.current = mapped.context
      for (const telemetryEvent of mapped.events) telemetry.record(telemetryEvent)
    },
    [telemetry],
  )

  const connection = useRealtimeConnection({
    centrifugoUrl,
    // Имя канала — `conversation:{id}`: `conversation_id` в тело публикации не
    // едет вовсе, привязка к беседе и есть подписка (`services/realtime_delivery.py`).
    channel: `conversation:${conversation.id}`,
    // Второй канал того же тикета — личный, вкладки, а не беседы. Собирается
    // он тем же правилом, что и на сервере (`services/realtime_delivery.py`,
    // `user_channel_for`), и берётся из зрителя: у одного человека он один на
    // все беседы, и смена беседы его не меняет.
    userChannel: `user:${currentUserId}`,
    issueTicket,
    onPublication,
    onUserPublication: onUnreadPublication,
    createCentrifuge,
    onObservedEvent,
  })

  const { notify } = connection

  // Три факта наружу: два — от ленты (пропуск и сходимость), третий — отказ
  // догрузки. Все три ведут в тот же автомат, а не в отдельные `useState`:
  // разложенные, они разошлись бы в момент перехода и `data-sync-reason`
  // нечем было бы заполнить.
  const onGapDetected = useCallback(() => notify({ type: "sequence-gap" }), [notify])
  const onSyncDone = useCallback(() => notify({ type: "sync-completed" }), [notify])

  /**
   * Отказ догрузки — видимая строка, а не молчание.
   *
   * `useConversationHistory` различает «догрузка не сошлась» (остаёмся в
   * `SYNCING`, `onGapDetected`) и «догрузка отказала» (`400` и подобное —
   * дефект клиента, `openapi.yaml:608-613`). Второе обязано быть видно: без
   * него отказ неотличим от долгой работы, и клиент висел бы в `SYNCING`
   * вечно, ничего не сказав. Состояние автомата при этом не подменяется:
   * сходимости не было, и `data-connection-state` остаётся `syncing`.
   */
  const [syncFailed, setSyncFailed] = useState(false)
  const onSyncFailed = useCallback(() => setSyncFailed(true), [])

  const conversationHistory = useConversationHistory({
    conversationId: conversation.id,
    loadTail: history.loadTail,
    loadPage: history.loadPage,
    onGapDetected,
    onSyncDone,
    onSyncFailed,
  })

  // Ссылка обновляется без массива зависимостей — как `handlersRef` в самой
  // обвязке: значение обязано быть свежим в тот же тик, а не после следующего
  // рендера.
  useEffect(() => {
    historyRef.current = conversationHistory
  })

  // G4: черновик вложения и отправка сообщения-вложения.
  const attachmentDraft = useAttachmentDraft(attachments?.ops)
  const draftValue = attachmentDraft.draft
  const clearDraft = attachmentDraft.clear
  // Тождество логической отправки живёт в ссылке и привязано к вложению: повтор
  // после сбоя шлёт **тот же** `client_message_id` (D3), и сервер ответит `200`
  // с тем же сообщением, а не заведёт второе.
  const attachmentSendId = useRef<{ attachmentId: string; clientMessageId: string } | null>(null)
  const sendAttachment = useCallback(
    (caption: string) => {
      if (attachments === undefined || draftValue.state !== "ready") return
      const { attachmentId, kind } = draftValue
      if (attachmentSendId.current?.attachmentId !== attachmentId) {
        attachmentSendId.current = { attachmentId, clientMessageId: crypto.randomUUID() }
      }
      void attachments
        .send({
          conversationId: conversation.id,
          clientMessageId: attachmentSendId.current.clientMessageId,
          kind,
          caption,
          attachmentId,
        })
        .then((response) => {
          clearDraft()
          // Ответ кладётся тем же путём, что и публикация (`adaptMessage` →
          // `acceptPublication`): второго способа слияния не заводится. Если
          // публикация о том же сообщении уже пришла, дубль по номеру
          // отбрасывает лента.
          historyRef.current?.acceptPublication(adaptMessage(response, currentUserId, new Date()))
        })
        .catch(() => {
          // Сбой отправки не стирает черновик: вложение остаётся `ready`, и
          // повтор нажатия «Send» уйдёт с тем же тождеством.
        })
    },
    [attachments, draftValue, conversation.id, currentUserId, clearDraft],
  )

  /**
   * Лента говорит очереди, кто она, — и умолкает, когда её больше нет.
   *
   * `acceptPublication` берётся прямо из хука, а не из `historyRef.current`: у
   * ссылки нет ни стабильности, ни гарантии, что она уже заполнена к моменту
   * первого ответа сервера, — а этот эффект исполняется до того, как вкладка
   * успеет что-либо отправить. Уборка сообщает `null`, и это не симметрия ради
   * симметрии: панель умирает по `key` при каждой смене беседы, и очередь без
   * этого вложила бы ответ в беседу, которой на экране уже нет.
   *
   * Беседа называется **панелью**, а не сверяется вызывающим: она знает своё
   * имя, а очередь нет — у неё запись помнит беседу, но ленты за ней не стоит.
   */
  useEffect(() => {
    onFeedReady({
      conversationId: conversation.id,
      accept: conversationHistory.acceptPublication,
    })
    return () => onFeedReady(null)
  }, [onFeedReady, conversation.id, conversationHistory.acceptPublication])

  /**
   * Подтверждённое дошло до ленты — снять записи очереди по их тождеству (D6).
   *
   * Здесь и только здесь решается судьба оптимистичной записи, и решается она
   * **фактом ленты**, а не исходом ответа: при разрыве номеров сообщение в
   * `messages` не попадает (`applyMessage` оставляет дыру дырой), значит снимать
   * нечего, и запись доживает до конца догрузки — вместо того чтобы исчезнуть
   * вместе с ещё не приехавшим подтверждением.
   *
   * Это верно и для порядка «событие раньше ответа»: оба входа идут одним путём
   * (`acceptPublication`), и подтверждённое попадает в `messages` одинаково —
   * независимо от того, кто его принёс. Второй точки снятия не заводится: две
   * разошлись бы ровно так же, как любые две копии правила.
   */
  useEffect(() => {
    onConfirmed(confirmedClientIds(conversationHistory.messages, pending))
  }, [onConfirmed, conversationHistory.messages, pending])

  /**
   * Квитанция вкладки: два числа из двух разных источников.
   *
   * `delivered` — применённая граница ленты: свойство **устройства**, а не
   * взгляда, поэтому растёт и у свёрнутой вкладки. `read` — максимум по целиком
   * видимым строкам, и существует он только у видимой вкладки: за это отвечает
   * сам `useReceipts`, который в фоне отправляет доставку и молчит о прочтении
   * (D6). Возвращается оттуда **отправленное и подтверждённое**, и по нему
   * читается `data-my-read-seq`.
   */
  const myReceipt = useReceipts({
    conversationId: conversation.id,
    deliveredThroughSeq: conversationHistory.appliedThroughSeq,
    visibleThroughSeq,
    send: sendReceipts,
  })

  /**
   * Состояние доставки — **своим** сообщениям, и выводится оно из номеров
   * собеседника, а не из наших отправок.
   *
   * Пол в `sent` (`deliveryStateOf` при `peer === null`) — утверждение о нашем
   * сообщении, а не о собеседнике: сообщение лежит в применённой ленте, значит
   * сервер его принял. Чужому сообщению состояния не достаётся вовсе: оно уже
   * здесь, и «доставлено» про него сказало бы то, чего собеседник не делал
   * (`MessageList` рисует атрибут только для `authorId === "me"`).
   */
  const messages = useMemo(
    () =>
      conversationHistory.messages.map((message) =>
        message.authorId === "me"
          ? { ...message, deliveryState: deliveryStateOf(message.seq, peer) }
          : message,
      ),
    [conversationHistory.messages, peer],
  )

  /**
   * «Вошли в `SYNCING` — пойти за историей».
   *
   * Решение «синхронизироваться» принимает автомат (единственная точка правды
   * о состоянии), а исполняет его этот эффект. Следствие названо честно:
   * просьба, пришедшая уже после того, как круг закончился, стоит одного
   * лишнего круга REST — `startSync()` ставит намерение до выхода в `pump()`,
   * и круг идёт от текущей границы. Данных при этом не выдумывается: лишний
   * круг возвращает то же, что уже есть.
   */
  const { startSync } = conversationHistory
  useEffect(() => {
    if (connection.state !== "syncing") return
    void startSync()
  }, [connection.state, startSync])

  /**
   * Выход из разрыва — второй повод сверки списка (`D11`).
   *
   * Повод берётся **по переходу**: сравнение с прежним состоянием, а не
   * проверка «сейчас `connected`». Разница не косметическая — состояние
   * читается и на монтировании, а панель пересоздаётся на каждой смене беседы
   * (`key` выше), поэтому проверка «сейчас `connected`» сходила бы за списком
   * на каждое переключение беседы.
   *
   * `degraded` и `disconnected` — ровно те два состояния, в которых события
   * могли не дойти: первое значит «связь есть, но данные под вопросом», второе
   * — «связи нет». Возврат в `connecting` тоже считается выходом: этого
   * достаточно, потому что за истиной мы идём **после** разрыва, а не вместо
   * восстановления соединения.
   */
  const previousConnectionState = useRef(connection.state)
  useEffect(() => {
    const previous = previousConnectionState.current
    previousConnectionState.current = connection.state
    if (previous !== connection.state && (previous === "disconnected" || previous === "degraded")) {
      onReconcile()
    }
  }, [connection.state, onReconcile])

  return (
    <div
      className="flex min-h-0 flex-1 flex-col"
      // Слова — те же, что в типе `ConnectionState`, а не человекочитаемая
      // строка: приёмка читает состояние по атрибуту, а не по тексту.
      data-connection-state={connection.state}
      // Причина — **только** пока идёт синхронизация, и это не мелочь: в
      // `connected` причина отсутствует по определению, а оставленный в
      // разметке хвост прошлого перехода читался бы как текущий.
      data-sync-reason={
        connection.state === "syncing" ? (connection.syncReason ?? undefined) : undefined
      }
      // Что вкладка **сообщила** о прочтении, а не что она видит: сюда попадает
      // только отправленное и подтверждённое транспортом (`useReceipts`).
      // Отсутствие атрибута — «не сообщала ничего», и это не ноль: ноль был бы
      // утверждением «прочитано ни до чего». По этому атрибуту приёмка читает
      // число, не сканируя ленту.
      data-my-read-seq={myReceipt?.readSeq ?? undefined}
      // Что сообщил собеседник. Ноль здесь — настоящее состояние (он прислал
      // `read_seq = 0`), отсутствие — «он ещё ничего не сообщал»; слить их
      // значило бы вывести прочтение из молчания.
      data-peer-read-seq={peer?.readSeq ?? undefined}
    >
      <ChatHeader conversation={conversation} />

      {conversationHistory.phase === "error" ? (
        // Отказ хвоста — отдельное состояние, и оно не выдаёт себя ни за
        // пустоту, ни за загрузку. Прежняя заглушка «history isn't loaded yet»
        // говорила о том, чего мы не сделали; здесь сказано то, что случилось.
        <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
          <p className="text-sm text-text-warm-secondary">Message history isn&apos;t available.</p>
        </div>
      ) : (
        <MessageList
          messages={messages}
          conversationName={conversation.name}
          appliedThroughSeq={conversationHistory.appliedThroughSeq}
          // Источник второго числа квитанции. `setState` — устойчивая ссылка, и
          // это здесь несуще: смена её перезапускала бы наблюдателя на каждом
          // рендере, то есть строка «видна целиком» объявлялась бы заново.
          onVisibleThroughSeq={setVisibleThroughSeq}
          pending={pending}
        />
      )}

      {syncFailed ? (
        <p role="status" className="px-6 py-1 text-xs text-text-warm-secondary">
          Couldn&apos;t catch up on missed messages.
        </p>
      ) : null}

      <ConnectionStatusLine state={connection.state} />

      {/*
        Композер рисуется и при отказе истории (`phase === "error"`), и это не
        упущение: отказ догрузки говорит о **хвосте**, а не о праве писать —
        сообщение уходит на сервер отдельным запросом и в ленте окажется, когда
        её перечитают. Спрятать поле ввода значило бы запретить человеку
        действие по причине, к нему не относящейся.

        `disabled` не передаётся ничем, и это названная граница: состояния
        «писать сюда нельзя» клиент не знает — признак блокировки доезжает
        отсутствием метаданных (`G3-007` D14), а не флагом на беседе. Ветка
        `blocked` композера остаётся непроизведённой, и выдавать её за
        исполняемое правило не будем.
      */}
      <MessageComposer
        recipientName={conversation.name}
        onSend={onSend}
        attachment={
          attachments === undefined
            ? undefined
            : {
                draft: draftValue,
                onPick: attachmentDraft.pick,
                onClear: clearDraft,
                onSend: sendAttachment,
              }
        }
      />
    </div>
  )
}
