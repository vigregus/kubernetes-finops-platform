# Vector — web

React + TypeScript + Vite frontend for the messenger client. Structure and naming
follow `docs/messenger/07-engineering-standard.md` Часть 6; stack decisions follow
`docs/messenger/02-delivery.md`.

## Run

```bash
npm install
npm run dev          # app at http://localhost:5173
npm run storybook    # component library at http://localhost:6006
npm run api:generate # OpenAPI client → src/api/generated/   (нужен Docker)
npm run build        # api:generate + tsc -b + vite build
npm run build:app    # tsc -b + vite build, без генерации
npm run build-storybook
```

`npm run build` зовёт `api:generate` первым: клиент не коммитится, и без него
`tsc -b` проверял бы отсутствующие модули. Внутри образа вызывается
`build:app` — у Node-стадии нет демона, генерация идёт отдельной стадией.
Подробности — в `src/api/README.md`.

## Structure

```
src/
  api/            generated OpenAPI client (src/api/generated/, not committed)
                  + thin wrappers; see api/README.md
  features/
    auth/         LoginPage, SettingsSessionsPage + components/
                  (LoginScreen, SessionsPanel, SessionRow, EmailVerificationBanner, CurrentUserFooter)
    conversations/ ChatPage + components/
                  (ConversationSidebar, ConversationListItem, ChatHeader, BlockedNotice)
    messages/     components/
                  (MessageTimeline, MessageBubble, AttachmentBubble, UnsupportedMessageNotice,
                   DeletedMessageNotice, DayDivider, ConnectionStateBanner, SyncIndicator,
                   TypingIndicator, MessageComposer, EmptyConversationState)
  shared/
    ui/           domain-agnostic building blocks: Icon, IconButton, Avatar, StatusDot,
                  Badge, TextField, SearchField, UpdateAvailableBanner, MessengerLayout
    lib/          mock-data.ts (fixtures), types.ts (stand-in for generated types)
```

Naming: components `PascalCase`, hooks `useXxx`, data fields `camelCase`, module
files `kebab-case`.

Every component has a colocated `*.stories.tsx` covering its documented states —
run `npm run storybook` and browse by feature (Auth / Conversations / Messages /
Shared UI). Component/story doc blocks cite the `docs/messenger/*.md` section and
test ID (e.g. `CLI-001`, `RT-004`) that requires the state, so the mapping from
spec to UI stays traceable.

## What's covered

Driven by `docs/messenger/{01-architecture,02-delivery,03-v1-scope,04-decisions,
07-engineering-standard,08-authorization,09-data-lifecycle,13-client-compatibility}.md`:

- **Connection states** (`ConnectionStateBanner`): connected / connecting /
  disconnected / degraded / syncing — the 5 states the client must distinguish
  because `recovered=true/false` alone isn't enough.
- **Message delivery** (`MessageBubble`): the client FSM from
  `07-engineering-standard.md` — `sending → sent → delivered → read`, plus
  `failed` and `retrying` (covers both a retried send and an offline-outbox
  item waiting for connectivity), with a specific failure reason where the
  server gives one (e.g. "you were blocked").
- **Attachments** (`AttachmentBubble`): image / file / voice, each through
  uploading → processing → ready, or rejected / failed.
- **Unsupported message types** (`UnsupportedMessageNotice`): a message kind
  from a newer client build still occupies its slot instead of being skipped.
- **Deleted messages** (`DeletedMessageNotice`): tombstone, not removed.
- **Typing & presence** (`TypingIndicator`, `ChatHeader`): online / away /
  last-seen / typing, per-conversation and in the sidebar.
- **Blocking** (`BlockedNotice`, disabled `MessageComposer`): symmetric block,
  history stays readable, composer disabled with a reason.
- **Empty states** (`EmptyConversationState`): a conversation can exist with
  zero messages.
- **Auth/session** (`LoginScreen`, `SessionsPanel`, `EmailVerificationBanner`):
  session-expired re-login prompt, multi-device session list with per-device
  and log-out-everywhere actions, gated/rate-limited email verification.
- **Client updates** (`UpdateAvailableBanner`): reload prompt for an
  incompatible Service Worker update.

## Что подключено, а что нет

G3-005 (`IMPLEMENTATION-PLAN.md`, требования `CTR-004` и `DEP-001`) превращает этот
макет в минимальный работающий клиент настоящего backend: вход через Keycloak,
`GET /me`, `GET /conversations` и статика из собственного образа, закреплённого по
digest. Клиент из `packages/contracts/openapi.yaml` — **это** G3-005, а не работа
«на будущее»; подробности в `src/api/README.md`.

В объём G3-005 **не** входит, и это не «ещё не сделано», а решение:

- TanStack Query — bootstrap это небольшой слой состояния, а не кэш запросов
- `centrifuge-js` и любой realtime: ни presence, ни «печатает», ни квитанций
- XState-FSM и основанный на нём `ConnectionStateBanner` целиком
- TanStack Virtual, IndexedDB, офлайн-очередь
- React Router — один адрес `/callback`, ветвление по `window.location.pathname`
- История сообщений: `GET /conversations/{id}/messages`, `POST /messages`, пагинация
- G3-006: tab sleep → `recovered=false` → `SYNCING` → catch-up → `CONNECTED`

Компоненты и stories перечисленного остаются в дереве проектным запасом: ни одна
`*.stories.tsx` не удаляется. Но в production-путь они не подключены — элемент,
который рисуется и ничего не делает, обещает возможность, которой нет.

`src/shared/lib/types.ts` из «заглушки вместо сгенерированных типов» становится
UI-представлением: оно ужимается до того, что интерфейс действительно рисует, и
перестаёт требовать полей, которых сервер не отдаёт.
