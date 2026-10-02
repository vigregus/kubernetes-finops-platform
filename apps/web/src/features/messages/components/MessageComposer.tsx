/**
 * Композер — единственный путь, которым человек производит сообщение.
 *
 * ## Чего здесь **нет** и почему это изменение, а не пропуск
 *
 * Здесь стояли два `IconButton` — `attach_file` («Attach file») и
 * `sentiment_satisfied` («Add emoji») — **без обработчиков**. Это не «ещё не
 * доделано»: в production-дереве нажатие на них есть, а следствия нет вовсе, и
 * кнопка без следствия — тот же указатель в никуда, что маркер на чужой гейт.
 * Гейт G3-007-1 возвращает композер в дерево (он был вычеркнут как «вне объёма
 * G3-005»), и вернуть его **вместе с мёртвыми кнопками** значило бы протащить
 * мимо проверки именно то, что композер и вычёркивало. Поэтому кнопки удалены,
 * а вложения и эмодзи названы тем, чем являются, — объёмом `G4`.
 *
 * ## Разметка, по которой приёмка находит человека за работой
 *
 * `data-composer-state` (`ready` | `blocked`) — по нему видно, доступна ли
 * отправка вообще, и доступна ли она **потому**, что беседа такова, а не
 * потому, что разметка не доросла. `data-composer-input` и
 * `data-composer-send` — адреса поля и кнопки: приёмочный сценарий обязан
 * набирать текст и нажимать **по-настоящему** (D10 плана), а поиск по
 * placeholder'у или по тексту кнопки — это адрес, который меняется от первой же
 * правки копирайтинга.
 */
import { useRef, useState, type KeyboardEvent } from "react"
import { Icon } from "../../../shared/ui/Icon"
import { TextField } from "../../../shared/ui/TextField"
import type { AttachmentDraft } from "../../attachments/useAttachmentDraft"

/**
 * Типы, которые принимает сервер (`domain/attachment.py`, `ALLOWED`). Список
 * здесь только подсказка диалогу выбора файла: решает сервер, и файл вне
 * списка всё равно получит отказ `415` до выдачи ссылки.
 */
const ACCEPT =
  "image/jpeg,image/png,image/gif,image/webp,application/pdf,application/zip,text/plain," +
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document," +
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

/**
 * Вложение в композере (G4). Необязательное целиком: без него композер —
 * прежний, только текст.
 */
export interface ComposerAttachment {
  draft: AttachmentDraft
  onPick: (file: File) => void
  onClear: () => void
  /** Отправка сообщения-вложения; подпись может быть пустой. */
  onSend: (caption: string) => void
}

interface MessageComposerProps {
  recipientName: string
  onSend: (text: string) => void
  disabled?: boolean
  disabledReason?: string
  attachment?: ComposerAttachment
}

const DRAFT_LABEL: Record<AttachmentDraft["state"], string> = {
  empty: "",
  uploading: "Uploading…",
  processing: "Checking file…",
  ready: "Ready to send",
  failed: "",
}

export function MessageComposer({
  recipientName,
  onSend,
  disabled,
  disabledReason,
  attachment,
}: MessageComposerProps) {
  const [value, setValue] = useState("")
  const fileInput = useRef<HTMLInputElement>(null)
  const draft = attachment?.draft

  function submit() {
    if (disabled) return
    const text = value.trim()
    if (attachment !== undefined && draft?.state === "ready") {
      attachment.onSend(text)
      setValue("")
      return
    }
    // Файл ещё в пути: текст не уходит отдельным сообщением, пока человек
    // не решил, что делать с вложением, — иначе подпись к картинке
    // отправилась бы раньше самой картинки.
    if (draft?.state === "uploading" || draft?.state === "processing") return
    if (!text) return
    onSend(text)
    setValue("")
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  if (disabled) {
    return (
      <div
        data-composer-state="blocked"
        className="flex-shrink-0 bg-surface/90 p-4 shadow-[0_-2px_12px_rgba(41,37,36,0.03)] backdrop-blur-md"
      >
        <div className="mx-auto flex max-w-4xl items-center justify-center gap-2 rounded-2xl bg-surface-container-low p-3.5 text-sm text-text-warm-muted">
          <Icon name="block" size={16} />
          {disabledReason ?? "You can't send messages here."}
        </div>
      </div>
    )
  }

  return (
    <div
      data-composer-state="ready"
      className="flex-shrink-0 bg-surface/90 p-4 shadow-[0_-2px_12px_rgba(41,37,36,0.03)] backdrop-blur-md"
    >
      <div className="mx-auto flex max-w-4xl flex-col gap-2">
        {draft !== undefined && draft.state !== "empty" && (
          <div
            data-attachment-draft={draft.state}
            className="flex items-center gap-2 rounded-xl bg-surface-container-low px-3 py-2 text-sm text-text-charcoal"
          >
            <Icon name="attach_file" size={16} />
            <span className="min-w-0 flex-1 truncate">{draft.fileName}</span>
            <span
              className={
                draft.state === "failed" ? "text-status-error" : "text-text-warm-muted"
              }
            >
              {draft.state === "failed" ? draft.message : DRAFT_LABEL[draft.state]}
            </span>
            <button
              type="button"
              aria-label="Remove attachment"
              data-attachment-clear
              onClick={attachment?.onClear}
              className="flex h-6 w-6 items-center justify-center rounded-full text-text-warm-muted hover:bg-surface-cream"
            >
              <Icon name="close" size={16} />
            </button>
          </div>
        )}
        <div className="flex items-center gap-2 rounded-2xl bg-surface-cream p-2 shadow-[0_2px_8px_rgba(41,37,36,0.05)] transition-all focus-within:shadow-[0_0_0_2px_rgba(234,88,12,0.25)]">
          {attachment !== undefined && (
            <>
              <input
                ref={fileInput}
                type="file"
                accept={ACCEPT}
                hidden
                data-composer-file
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  // Сброс значения: тот же файл, выбранный второй раз после
                  // отказа, иначе не породил бы `change`.
                  event.target.value = ""
                  if (file !== undefined) attachment.onPick(file)
                }}
              />
              <button
                type="button"
                aria-label="Attach file"
                data-composer-attach
                onClick={() => fileInput.current?.click()}
                className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl text-text-warm-muted hover:bg-surface-container-low"
              >
                <Icon name="attach_file" size={20} />
              </button>
            </>
          )}
          <TextField
            value={value}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={`Message ${recipientName}...`}
            data-composer-input
            className="px-1 py-2 text-sm text-text-charcoal"
          />
          <button
            type="button"
            onClick={submit}
            data-composer-send
            className="flex h-10 flex-shrink-0 items-center justify-center gap-1.5 rounded-xl bg-accent-terracotta px-4 text-sm font-semibold text-on-primary shadow-[0_2px_6px_rgba(234,88,12,0.25)] transition-all hover:bg-status-error active:scale-95"
          >
            Send
            <Icon name="send" size={18} />
          </button>
        </div>
        <div className="flex items-center justify-between px-2 text-xs text-text-warm-muted">
          <span>Enter to send, Shift + Enter for new line</span>
          <span className="flex items-center gap-1">
            <span className="h-1.5 w-1.5 rounded-full bg-status-success" />
            All messages protected
          </span>
        </div>
      </div>
    </div>
  )
}
