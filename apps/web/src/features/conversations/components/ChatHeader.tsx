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
 * Ветви `lastSeenAt` здесь нет, и это то же правило, что и раньше.
 * `users.last_seen_at` — «когда человек последний раз был подтверждён в сети»
 * (`03-v1-scope.md:77-83`), и «Last seen 16:20» в обычной семантике мессенджера
 * читается как «сейчас офлайн» — то есть как факт, которого отметка не
 * сообщает: человек может быть в сети прямо сейчас. Отметка и признак — два
 * разных ответа о человеке, и второй не выводится из первого.
 *
 * Ветка `offline` при этом **есть**, и она не противоречит сказанному: слово
 * рисуется ровно тогда, когда сервер сказал `online: false` сам, — то есть
 * когда за ним стоит предикат по времени ответа (`domain/presence.py`, окно
 * 180 с), а не молчание. «Offline» из отсутствия данных — по-прежнему ложь;
 * «Offline» из утверждения сервера — правда, хотя и приблизительная: сервер
 * говорит «активности не подтверждено окно», а не «соединения нет».
 *
 * Ветки `away` больше нет: производителя у неё не осталось. Адаптер — единственный,
 * кто выставляет `presence` из ответа сервера, и он отдаёт только `online` и
 * `offline` (`conversations/adapter.ts::presenceOf`). Ветка, в которую нечего
 * прийти, — тот же указатель в никуда, что маркер на чужой гейт. Сам словарь
 * `PresenceStatus` не сужен: `away` остаётся его частью и рисуется `StatusDot`
 * и `Avatar` — просто не от этого сервера.
 */
function subtitle(conversation: Conversation) {
  if (conversation.typingNames?.length) {
    return {
      text: conversation.typingNames.length === 1 ? `${conversation.typingNames[0]} is typing…` : "Several people are typing…",
      dotClass: "bg-accent-terracotta animate-pulse",
    }
  }
  if (conversation.presence === "online") return { text: "Online", dotClass: "bg-status-success" }
  // Цвет тот же, что у `StatusDot` для этого слова (`shared/ui/StatusDot.tsx:7`):
  // одно значение словаря — один цвет во всех местах, где оно появляется.
  if (conversation.presence === "offline") return { text: "Offline", dotClass: "bg-text-warm-muted" }
  return undefined
}

export function ChatHeader({ conversation, onSearch, onOpenDetails, onVoiceCall, onVideoCall }: ChatHeaderProps) {
  const subtitleValue = subtitle(conversation)
  const blocked = conversation.blockedByMe || conversation.blockedMe
  const text = blocked ? "Unavailable" : subtitleValue?.text

  return (
    // `data-presence` — то, чем приёмка читает присутствие, не разбирая текст
    // подзаголовка: значение словаря, а не слово интерфейса. Присутствия нет —
    // нет и атрибута (React опускает `undefined`), и это различает «сервер
    // сказал `false`» (`data-presence="offline"`) и «сервер не сказал ничего»
    // (атрибута нет) — то самое различие, ради которого ветвей три, а не две.
    <header
      data-presence={blocked ? undefined : conversation.presence}
      className="z-20 flex h-20 flex-shrink-0 items-center justify-between bg-surface/90 px-6 shadow-[0_1px_8px_rgba(41,37,36,0.04)] backdrop-blur-md"
    >
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
