import type { CurrentUser } from "../../../shared/lib/types"
import { Avatar } from "../../../shared/ui/Avatar"
import { IconButton } from "../../../shared/ui/IconButton"

interface CurrentUserFooterProps {
  user: CurrentUser
  onOpenSettings?: () => void
}

export function CurrentUserFooter({ user, onOpenSettings }: CurrentUserFooterProps) {
  return (
    <div className="flex items-center justify-between rounded-xl bg-surface-cream p-2.5 shadow-[0_1px_4px_rgba(41,37,36,0.04)]">
      <div className="flex min-w-0 items-center gap-3">
        <Avatar name={user.name} src={user.avatarUrl} ringClassName="ring-surface-cream" />
        <div className="min-w-0">
          {/*
            Подписи о присутствии здесь нет, и её отсутствие — утверждение, а не
            пропуск. «Active now» был хардкодом: realtime в G3-005 нет, сервер
            отдаёт `last_seen_at` отметкой без признака «онлайн», и подпись
            читалась как «в сети прямо сейчас» — факт, которого не сообщали.

            Здесь стояло «когда присутствие появится (G3-007), подпись вернётся
            вместе с ним», и это оказалось неверно в обе стороны. Присутствие
            `G3-007` уже приносит — но **собеседника**, в шапке беседы
            (`data-presence`, `ChatHeader.tsx`), а не своё. Своего в подписи ниже
            нет потому, что его нет в модели: `CurrentUser` (`shared/lib/types.ts`)
            не несёт ни `online`, ни `last_seen_at`, и подставить сюда присутствие
            собеседника значило бы показать человеку чужое как своё — тот же
            класс, что `participants[0]` вместо собеседника.
          */}
          <p className="truncate text-sm font-semibold leading-snug text-on-surface">{user.name}</p>
        </div>
      </div>
      {/* Элемент без обработчика не рендерится: кнопка с `onClick={undefined}`
          выглядит рабочей и не делает ничего. G3-005 настроек не приносит. */}
      {onOpenSettings && <IconButton icon="settings" label="Settings" onClick={onOpenSettings} />}
    </div>
  )
}
