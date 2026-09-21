import type { Conversation } from "../../../shared/lib/types"
import { Avatar } from "../../../shared/ui/Avatar"
import { IconButton } from "../../../shared/ui/IconButton"

interface ChatHeaderProps {
  conversation: Conversation
  /**
   * Обработчики четырёх элементов шапки. Необязателен каждый по отдельности, и
   * это не удобство, а правило: элемент без обработчика не рендерится вовсе.
   */
  onSearch?: () => void
  onOpenDetails?: () => void
  onVoiceCall?: () => void
  onVideoCall?: () => void
}

/**
 * Подзаголовок говорит только то, что сервер утверждал **положительно**.
 *
 * Ветвей `lastSeenAt` и «Offline» здесь нет, и обе убраны по одной причине.
 * `users.last_seen_at` — «когда человек последний раз был подтверждён в сети»
 * (`03-v1-scope.md:77-83`), и наружу он отдаётся отметкой, без признака
 * «онлайн» (`README.md:78`). «Last seen 16:20» в обычной семантике мессенджера
 * читается как «сейчас офлайн» — то есть как факт, которого сервер не сообщал, а
 * отметка не означает и момента перехода в офлайн: человек может быть в сети
 * прямо сейчас. «Offline» при отсутствии всяких данных — то же утверждение,
 * только короче: оно выводится из молчания сервера, а молчание не факт.
 *
 * `presence` адаптер G3-005 не выставляет никогда (realtime вне объёма), поэтому
 * до G3-007 шапка показывает только имя. Ветви `online`/`away` оставлены: они
 * рисуются ровно тогда, когда присутствие действительно пришло, и это и есть
 * положительное утверждение сервера.
 */
function subtitle(conversation: Conversation) {
  if (conversation.typingNames?.length) {
    return {
      text: conversation.typingNames.length === 1 ? `${conversation.typingNames[0]} is typing…` : "Several people are typing…",
      dotClass: "bg-accent-terracotta animate-pulse",
    }
  }
  if (conversation.presence === "online") return { text: "Online", dotClass: "bg-status-success" }
  if (conversation.presence === "away") return { text: "Away", dotClass: "bg-status-warning" }
  return undefined
}

export function ChatHeader({ conversation, onSearch, onOpenDetails, onVoiceCall, onVideoCall }: ChatHeaderProps) {
  const subtitleValue = subtitle(conversation)
  const blocked = conversation.blockedByMe || conversation.blockedMe
  const text = blocked ? "Unavailable" : subtitleValue?.text

  return (
    <header className="z-20 flex h-20 flex-shrink-0 items-center justify-between bg-surface/90 px-6 shadow-[0_1px_8px_rgba(41,37,36,0.04)] backdrop-blur-md">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <Avatar name={conversation.name} src={conversation.avatarUrl} presence={blocked ? undefined : conversation.presence} />
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg font-semibold tracking-tight text-on-surface">{conversation.name}</h1>
          {/*
            Нет подзаголовка — нет и узла. Пустой `<p>` был бы местом, которое
            рано или поздно заполнят догадкой; отсутствие узла такого места не
            оставляет.
          */}
          {text && (
            <div className="flex items-center gap-1.5">
              {!blocked && subtitleValue && (
                <span className={`inline-block h-2 w-2 flex-shrink-0 rounded-full ${subtitleValue.dotClass}`} />
              )}
              <p className="truncate text-xs font-medium text-text-warm-secondary">{text}</p>
            </div>
          )}
        </div>
      </div>

      <div className="flex flex-shrink-0 items-center gap-1">
        {onSearch && <IconButton icon="search" label="Search messages" onClick={onSearch} />}
        {!blocked && onVoiceCall && <IconButton icon="call" label="Start voice call" onClick={onVoiceCall} />}
        {!blocked && onVideoCall && <IconButton icon="videocam" label="Start video call" onClick={onVideoCall} />}
        {onOpenDetails && (
          <>
            <div className="mx-1 h-6 w-px bg-surface-container-high" />
            <IconButton icon="info" label="Chat details" onClick={onOpenDetails} />
          </>
        )}
      </div>
    </header>
  )
}
