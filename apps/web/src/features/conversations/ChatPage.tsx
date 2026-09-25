import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ConversationSidebar } from "./components/ConversationSidebar"
import { ChatHeader } from "./components/ChatHeader"
import { adaptConversations } from "./adapter"
import { adaptUnreadChanged, withUnreadOverlay } from "./unreadOverlay"
import type { ConversationListPage } from "../../api/generated"
import { adaptPublication } from "../messages/message-adapter"
import { MessageList } from "../messages/components/MessageList"
import type { HistorySource } from "../messages/history"
import {
  useConversationHistory,
  type ConversationHistory,
} from "../messages/useConversationHistory"
import { ConnectionStatusLine } from "../realtime/components/ConnectionStatusLine"
import type { CentrifugeFactory } from "../realtime/realtimeClient"
import { useRealtimeConnection } from "../realtime/useRealtimeConnection"
import { MessengerLayout } from "../../shared/ui/MessengerLayout"
import type { Conversation, CurrentUser } from "../../shared/lib/types"

/**
 * Пустой оверлей — **одна** карта на модуль, а не новая на каждый сброс.
 *
 * Свежая карта на каждый `setOverlay(new Map())` была бы новым состоянием при
 * том же значении: React сравнивает ссылки, и каждый сброс вызывал бы лишний
 * рендер списка на ровном месте.
 */
const EMPTY_OVERLAY: ReadonlyMap<string, number> = new Map()

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
  /** Подмена SDK — для компонентных тестов; в production не задаётся. */
  createCentrifuge?: CentrifugeFactory
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
 * `MessageTimeline`, `MessageBubble`, `MessageComposer`, `TypingIndicator`,
 * `SyncIndicator` и `ConnectionStateBanner` сюда не подключены: они остаются
 * проектным запасом (закрытый список B13). Лента — минимальная, её задача не
 * оформление, а наблюдаемая поверхность.
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
  createCentrifuge,
}: ChatPageProps) {
  // Ленивая инициализация, а не `?? conversations[0]` в рендере: запасного
  // значения у настоящих данных нет, а пустой список — законный ответ сервера.
  const [activeId, setActiveId] = useState<string | null>(() => conversations[0]?.id ?? null)

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

  // Слияние — на каждом рендере списка, а не при приходе события: иначе
  // пришлось бы держать согласие между двумя состояниями в руках, и число
  // отставало бы от базы ровно на один рендер.
  const merged = useMemo(() => withUnreadOverlay(base, overlay), [base, overlay])

  const activeConversation = merged.find((c) => c.id === activeId) ?? null

  return (
    <MessengerLayout
      sidebar={
        <ConversationSidebar
          conversations={merged}
          activeConversationId={activeConversation?.id ?? null}
          currentUser={currentUser}
          onSelectConversation={setActiveId}
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
          createCentrifuge={createCentrifuge}
          onUnreadPublication={onUnreadPublication}
          onReconcile={refresh}
        />
      )}
    </MessengerLayout>
  )
}

interface ConversationPaneProps {
  conversation: Conversation
  currentUserId: string
  history: HistorySource
  centrifugoUrl: string
  issueTicket: () => Promise<string>
  createCentrifuge?: CentrifugeFactory
  /** Публикация личного канала — наверх, к списку: число принадлежит не беседе. */
  onUnreadPublication: (payload: unknown) => void
  /** Выход из разрыва — повод сверки списка. */
  onReconcile: () => void
}

/**
 * Панель одной беседы: соединение, снимок, лента.
 *
 * Живёт отдельным компонентом ради `key` (см. выше) и ради порядка хуков —
 * порядок объявления здесь и есть порядок эффектов.
 */
function ConversationPane({
  conversation,
  currentUserId,
  history,
  centrifugoUrl,
  issueTicket,
  createCentrifuge,
  onUnreadPublication,
  onReconcile,
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

  const onPublication = useCallback(
    (payload: unknown) => {
      const message = adaptPublication(payload, currentUserId)

      // `null` здесь — не сбой, а измеренная форма канала: `message.read` и
      // `message.deleted` приходят по тому же каналу, но сообщениями не
      // являются. Падение внутри этого обработчика унесло бы соединение.
      if (message === null) return

      historyRef.current?.acceptPublication(message)
    },
    [currentUserId],
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
          messages={conversationHistory.messages}
          conversationName={conversation.name}
          appliedThroughSeq={conversationHistory.appliedThroughSeq}
        />
      )}

      {syncFailed ? (
        <p role="status" className="px-6 py-1 text-xs text-text-warm-secondary">
          Couldn&apos;t catch up on missed messages.
        </p>
      ) : null}

      <ConnectionStatusLine state={connection.state} />
    </div>
  )
}
