import { useSyncExternalStore } from "react";
import { adaptConversations } from "./features/conversations/adapter";
import { ChatPage } from "./features/conversations/ChatPage";
import { LoginPage } from "./features/auth/LoginPage";
import { adaptMe } from "./features/auth/me-adapter";
import { TransientErrorScreen } from "./features/auth/components/TransientErrorScreen";
import type { SessionState } from "./features/auth/sessionState";

interface AppProps {
  session: SessionState;
  /**
   * Повтор загрузки. Приходит сверху, потому что обёртка над API и её
   * зависимости (`client`, `bootstrapDeps`) собираются в `main.tsx`: у `App`
   * нет ни адреса API, ни доступа к токену, и быть не должно.
   */
  onRetry: () => void;
}

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
export function App({ session, onRetry }: AppProps) {
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

    case "ready": {
      const viewer = adaptMe(state.account);
      // Зритель один на страницу, и адаптация идёт **после** `/me`: без
      // `user_id` собеседник в личной беседе неотличим от самого зрителя (B9г).
      const conversations = adaptConversations(state.conversations, viewer.userId, new Date());

      return <ChatPage conversations={conversations} currentUser={viewer.user} />;
    }
  }
}

export default App;
