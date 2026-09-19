# Vector — messenger-web

React + TypeScript + Vite frontend for the messenger client, built as an atomic-design
component library. Stack decisions follow `docs/messenger/02-delivery.md`.

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
src/design-system/
  atoms/        Icon, IconButton, Avatar, StatusDot, Badge, TextField
  molecules/    SearchField, ConversationListItem, MessageBubble, AttachmentBubble,
                UnsupportedMessageNotice, DeletedMessageNotice, DayDivider,
                ConnectionStateBanner, SyncIndicator, TypingIndicator, MessageComposer,
                EmptyConversationState, UpdateAvailableBanner, BlockedNotice,
                EmailVerificationBanner, SessionRow, CurrentUserFooter
  organisms/    ConversationSidebar, ChatHeader, MessageTimeline, SessionsPanel, LoginScreen
  templates/    MessengerLayout
src/pages/       ChatPage, LoginPage, SettingsSessionsPage
src/data/        mockData.ts — local fixtures, no backend wired yet
```

Every component has a colocated `*.stories.tsx` covering its documented states —
open `npm run storybook` and browse by folder. Component/story doc blocks cite the
`docs/messenger/*.md` section and test ID (e.g. `CLI-001`, `RT-004`) that requires
the state, so the mapping from spec to UI stays traceable.

## What's covered

Driven by `docs/messenger/{01-architecture,02-delivery,03-v1-scope,04-decisions,
08-authorization,09-data-lifecycle,13-client-compatibility}.md`:

- **Connection states** (`ConnectionStateBanner`): connected / connecting /
  disconnected / degraded / syncing — the 5 states the client must distinguish
  because `recovered=true/false` alone isn't enough.
- **Message delivery** (`MessageBubble`): sending → sent → delivered → read,
  plus pending (offline outbox) and failed (with retry and a specific reason,
  e.g. "you were blocked").
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

## Not yet wired (needs a live API contract)

Generated OpenAPI client, TanStack Query, `centrifuge-js`, an XState connection/
recovery FSM, TanStack Virtual for the timeline, IndexedDB outbox, React Router.
The components above are ready to be driven by that layer once it exists —
right now they're fed from `src/data/mockData.ts`.
