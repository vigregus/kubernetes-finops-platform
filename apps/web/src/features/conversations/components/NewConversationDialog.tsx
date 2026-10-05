/**
 * Создание беседы: адрес → поиск → подтверждение → создание (D2).
 *
 * ## Порядок здесь не косметика, а сам предмет
 *
 * Диалог **не** создаёт беседу по набранной строке. Адрес сначала спрашивается
 * у сервера (`GET /users?email=`), и подтверждается **найденный человек**
 * (`display_name`), а не строка, которую набрали: `POST /conversations` уходит с
 * `participant_id` из ответа поиска. Смешать два шага — отправить адрес в
 * создание — значило бы, что собеседника по строке выбирает сервер, а человек
 * подтверждает то, чего не видел.
 *
 * Отсюда же и то, чего здесь **нет**: поля `participant_email` в контракте не
 * существует, и заводить его незачем — человек подтверждает найденного.
 *
 * ## Отказ поиска — ответ, а не поломка
 *
 * `404` — не ошибка приложения: сервер отвечает так и на свободный адрес, и на
 * стёртую учётную запись, и на заблокированного (D1), — поэтому «No one found at
 * this address» это утверждение о мире, а не извинение. Кнопка подтверждения при
 * нём **не активна**: путь в отказ был бы кнопкой, которая ничего не может.
 *
 * `429` показывается тем, чем является, — ожиданием с числом из `Retry-After`.
 * Показать его как «не найден» значило бы сказать человеку неправду о человеке:
 * поиск не состоялся, и «никого нет» — вывод, которого никто не делал.
 *
 * ## Разметка, по которой приёмка идёт через интерфейс
 *
 * `role="dialog"` и `data-dialog-state` — состояние диалога одним словом
 * (`idle` | `searching` | `found` | `not-found` | `rate-limited` | `failed` |
 * `creating` | `create-failed`), по нему сценарий отличает «ищу» от «не нашёл»,
 * не читая текст. `data-new-conversation-email` — поле, `data-new-conversation-search`
 * — кнопка поиска, `data-new-conversation-start` — подтверждение,
 * `data-found-user-id` — кого нашли. Без них приёмке пришлось бы искать элементы
 * по подписям, а подпись меняется от первой же правки копирайтинга.
 */
import { useState, type KeyboardEvent } from "react"
import { ApiProblem } from "../../../api/problems"
import type { Conversation as ConversationDto, UserLookup } from "../../../api/generated"
import { Icon } from "../../../shared/ui/Icon"
import { TextField } from "../../../shared/ui/TextField"

/** Поиск человека по адресу — операция, собранная в `main.tsx` (`D1`). */
export type SearchUser = (email: string) => Promise<UserLookup>

/**
 * Создание личной беседы — тоже операция оттуда же.
 *
 * Отдаёт **модель API**, а не модель интерфейса: адаптировать её под зрителя
 * умеет только `ChatPage` — там, где лежит `currentUserId`, — и сборка второй
 * модели здесь дала бы второе место, знающее, как выглядит беседа.
 */
export type CreateConversation = (participantId: string) => Promise<ConversationDto>

/**
 * Состояние диалога — **одним** значением, а не тремя флагами.
 *
 * Флаги дали бы достижимые сочетания, которых не бывает («идёт поиск» и
 * «найден» одновременно), и разметка показывала бы то одно, то другое в
 * зависимости от порядка проверок. Здесь состояние одно, и `data-dialog-state`
 * — его же имя.
 */
type DialogState =
  | { readonly kind: "idle" }
  | { readonly kind: "searching" }
  | { readonly kind: "found"; readonly userId: string; readonly displayName: string }
  | { readonly kind: "not-found" }
  /** Числа может не быть: `Retry-After` — необязательный заголовок. */
  | { readonly kind: "rate-limited"; readonly retryAfterSeconds?: number }
  | { readonly kind: "failed" }
  | { readonly kind: "creating"; readonly userId: string; readonly displayName: string }
  | {
      readonly kind: "create-failed"
      readonly userId: string
      readonly displayName: string
      readonly message: string
    }

/**
 * Отказ поиска — разбором ответа сервера, а не по тексту ошибки.
 *
 * `404` и `429` различаются **кодом**, и это единственный способ различить их
 * честно: у обоих тело — `Problem`, а `Problem.title` — проза, которая меняется
 * независимо от смысла. Здесь же рядом стоит и запрет: `0` вместо отсутствия
 * `Retry-After` не подставляется — «сервер не сказал, когда повторять» и
 * «сервер сказал: немедленно» это разные утверждения.
 */
function lookupFailure(error: unknown): DialogState {
  if (error instanceof ApiProblem) {
    if (error.status === 404) return { kind: "not-found" }
    if (error.status === 429) {
      return error.retryAfterSeconds === undefined
        ? { kind: "rate-limited" }
        : { kind: "rate-limited", retryAfterSeconds: error.retryAfterSeconds }
    }
  }
  return { kind: "failed" }
}

interface NewConversationDialogProps {
  readonly searchUser: SearchUser
  readonly createConversation: CreateConversation
  /** Готовая беседа — наружу. Открывает её `ChatPage`, а не диалог. */
  readonly onCreated: (conversation: ConversationDto) => void
  readonly onClose: () => void
}

export function NewConversationDialog({
  searchUser,
  createConversation,
  onCreated,
  onClose,
}: NewConversationDialogProps) {
  const [email, setEmail] = useState("")
  const [state, setState] = useState<DialogState>({ kind: "idle" })

  const busy = state.kind === "searching" || state.kind === "creating"

  async function search(): Promise<void> {
    const address = email.trim()
    // Пустой адрес — запрос без предмета: сервер ответил бы на него `400`, и
    // тратить на это попытку лимита незачем.
    if (address === "" || busy) return

    setState({ kind: "searching" })
    try {
      const found = await searchUser(address)
      setState({ kind: "found", userId: found.userId, displayName: found.displayName })
    } catch (error) {
      setState(lookupFailure(error))
    }
  }

  async function confirm(): Promise<void> {
    // Создание — **только** из найденного (D2). Проверка здесь, а не в
    // `disabled` кнопки: кнопку рисует разметка, а беседу создаёт эта функция, и
    // вторая проверка на пути, который создаёт, — не дублирование, а место, где
    // ошибка останавливается, если разметку поправят.
    if (state.kind !== "found") return
    const { userId, displayName } = state

    setState({ kind: "creating", userId, displayName })
    try {
      onCreated(await createConversation(userId))
    } catch (error) {
      setState({
        kind: "create-failed",
        userId,
        displayName,
        message:
          error instanceof ApiProblem
            ? `Could not start the chat (${error.status}).`
            : "Could not start the chat. Try again.",
      })
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === "Escape") onClose()
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="New conversation"
      data-dialog-state={state.kind}
      onKeyDown={handleKeyDown}
      className="fixed inset-0 z-50 flex items-center justify-center bg-on-surface/30 px-[max(1rem,env(safe-area-inset-left,0px))] pb-[calc(1rem+env(safe-area-inset-bottom,0px))] pt-[calc(1rem+env(safe-area-inset-top,0px))]"
    >
      <div className="w-full max-w-md rounded-2xl bg-surface p-6 shadow-[0_12px_40px_rgba(41,37,36,0.18)]">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold tracking-tight text-on-surface">New conversation</h2>
          <button
            type="button"
            aria-label="Close"
            title="Close"
            onClick={onClose}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-text-warm-secondary hover:bg-surface-container-low hover:text-on-surface"
          >
            <Icon name="close" size={18} />
          </button>
        </div>

        <label
          htmlFor="new-conversation-email"
          className="mb-1 block text-xs font-semibold uppercase tracking-wider text-text-warm-muted"
        >
          Email address
        </label>
        <div className="flex items-center gap-2 rounded-xl bg-surface-cream p-2 shadow-[0_2px_8px_rgba(41,37,36,0.05)]">
          <TextField
            id="new-conversation-email"
            aria-label="Email address"
            data-new-conversation-email
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="name@example.com"
            className="px-1 py-2 text-sm text-text-charcoal"
          />
          <button
            type="button"
            data-new-conversation-search
            onClick={() => void search()}
            disabled={busy || email.trim() === ""}
            className="flex h-10 flex-shrink-0 items-center justify-center rounded-xl bg-surface-warm-subtle px-4 text-sm font-semibold text-on-surface transition-all hover:bg-surface-container-low disabled:cursor-not-allowed disabled:opacity-50"
          >
            Search
          </button>
        </div>

        {/*
          Строка состояния — **одна** на все исходы, и это не экономия: два
          места для «не найден» и «слишком часто» разошлись бы, и `429` однажды
          показался бы как «никого нет» — утверждение, которого никто не делал.
        */}
        <p data-new-conversation-status className="mt-3 min-h-5 text-sm text-text-warm-secondary">
          {statusText(state)}
        </p>

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-xl px-4 py-2 text-sm font-medium text-text-warm-secondary hover:bg-surface-container-low hover:text-on-surface"
          >
            Cancel
          </button>
          <button
            type="button"
            data-new-conversation-start
            // Активна **только** на найденном: при `404` путь вёл бы в отказ, а
            // при `429` — в повтор, которого сервер не разрешил.
            disabled={state.kind !== "found"}
            data-found-user-id={state.kind === "found" ? state.userId : undefined}
            onClick={() => void confirm()}
            className="rounded-xl bg-accent-terracotta px-4 py-2 text-sm font-semibold text-on-primary shadow-[0_2px_6px_rgba(234,88,12,0.25)] transition-all hover:bg-status-error active:scale-95 disabled:cursor-not-allowed disabled:opacity-50 disabled:active:scale-100"
          >
            Start chat
          </button>
        </div>
      </div>
    </div>
  )
}

/**
 * Текст строки состояния — по исходу, и ни один исход не подменяет другой.
 *
 * Здесь и живёт запрет «`429` не показывается как «не найден»»: у ожидания своя
 * строка с числом из `Retry-After`, и это число — не украшение. Без него
 * человеку остаётся повторять вслепую, ровно то, от чего `Retry-After` и
 * заведён.
 */
function statusText(state: DialogState): string {
  switch (state.kind) {
    case "idle":
      return ""
    case "searching":
      return "Searching…"
    case "found":
      return `Start chat with ${state.displayName}?`
    case "not-found":
      return "No one found at this address"
    case "rate-limited":
      return state.retryAfterSeconds === undefined
        ? "Too many searches. Try again later."
        : `Too many searches. Try again in ${state.retryAfterSeconds} seconds.`
    case "failed":
      return "Search failed. Try again."
    case "creating":
      return `Starting chat with ${state.displayName}…`
    case "create-failed":
      return state.message
  }
}
