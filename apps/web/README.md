# Vector — web

React + TypeScript + Vite frontend for the messenger client. Structure and naming
follow `docs/messenger/07-engineering-standard.md` Часть 6; stack decisions follow
`docs/messenger/02-delivery.md`.

## Run

```bash
npm install
npm run dev          # app at http://localhost:5173
npm run storybook    # component library at http://localhost:6006
npm run build         # type-check + production build
npm run build-storybook
```

## Structure

```
src/
  api/            generated OpenAPI client + thin wrappers (not wired yet, see below)
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

## Not yet wired — this is not G3-005/006

This is a component/state inventory built against the docs, not the gated
deliverable in `IMPLEMENTATION-PLAN.md` (G3 — "согласованность клиента"), which
requires a running backend, a generated OpenAPI client, and browser E2E against
a real cluster. Missing before it can count as that gate:

- Client generated from `packages/contracts/openapi.yaml` into `api/generated/`
  (`src/shared/lib/types.ts` is a hand-written stand-in, deleted once this lands)
- TanStack Query for REST state, `centrifuge-js` for realtime
- An XState connection/recovery FSM driving `ConnectionStateBanner` for real
- TanStack Virtual for the message timeline, IndexedDB for the offline outbox
- React Router
- The G3-006 browser E2E: tab sleep → `recovered=false` → `SYNCING` → catch-up → `CONNECTED`

Right now every feature reads fixtures from `shared/lib/mock-data.ts`.
