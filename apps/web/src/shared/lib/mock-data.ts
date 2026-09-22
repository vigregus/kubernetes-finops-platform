import type { ChatMessage, Conversation, CurrentUser, DeviceSession } from "./types"

// Фикстура приведена к модели, а не наоборот: `handle` и `presence` из
// `CurrentUser` ушли, потому что `/me` их не отдаёт, — и оставлять их здесь
// значило бы держать у догадки законное место.
export const currentUser: CurrentUser = {
  name: "David Miller",
  email: "david.miller@example.com",
  emailVerified: true,
  avatarUrl:
    "https://lh3.googleusercontent.com/aida-public/AB6AXuBt061pWtdxBlMqSkLbBb286z673SNeLN4BjrVxnSQ4MgTBsIiH1ZxPM5GAyk9xoC_RxKn_tIehLkK09ZWW0pJW9OGOmX7Jmt11Ujf0EQV8k_pH6jXnaRzyiqB8EHJvcYrLMYQHUiUo-8st-_nnUu_cxIcAjMOpbMdffaihKx3LAfN_OdqpPisRLmGJkY_7VoKFFzu9wsGnR6WvTkpADVIBqHheFMs1KvHfv7M2LOxTS1M--tY0pFJp",
}

const annaAvatar =
  "https://lh3.googleusercontent.com/aida-public/AB6AXuCc-RlBVkGKNtHrJPjcLNZYrvLDbVi2BLJodsCPX6Tg-AYMPAbmQZry3FYxK-BgUuMEhzqFKGw4NtfHctyZU_KZjd0_a3sn-53RucMw_zP-msw0K9qCb7zL9Leg4G12477hs3y8MJgzpSN0KB86USeqgu0AP9Qb5i3x21WmLTgdHL17DMd8OhY1p0tXkrecwqG-kkM9OLbFHWezsXMYY5Y4WiS-yN4lqCCkZMXc7nZYFbjzrkRgVVT4"
const marcusAvatar =
  "https://lh3.googleusercontent.com/aida-public/AB6AXuD_LKYu3oqyhEO5t7XGMOj4PC5VOEDQYd5SM4UascS9jrG49MVh63Pj4rI0DTuPv7LbWYVRNIkejdTJXHzbkXqi_JjPnHONq95L5O6rhDifg-LWmVU0tZva8rJJneuxmT90N4lfnHw_ueLR-zB-dbt_mxvIq6TwL3kOdivGAKO6OUsgxSkXS1OPYesVypkn05caXHGyCxZ5CnHCKZrwh3aFkVx3_BeusacOBfzvKmoYo-f78k2vjuDK"
const danielAvatar =
  "https://lh3.googleusercontent.com/aida-public/AB6AXuC6Drr4Gvh9KZ8jJ9kp7Z5EfFMKB2JhrPiHcTzVnsf4ym-FgQC23yAvUXrEaxOEBMzFqzQ3MyNWREdtsqdJ75Q2E-U5qCkoLJfVAdBj1HwTfz26lwGStRvoAdm3GnwL0KeqC7Gm4NQpdGhHO8e9KZBggUKBMLNT43Jum8gtMdQ_povhkZvg9Eo4OMPPwGP2ISxc845yrcR9YD33YT1__6EdY4XuEtV8F3KMklLL-9iTVT3jOVN_F4xT"
const sofiaAvatar =
  "https://lh3.googleusercontent.com/aida-public/AB6AXuATsjrz3gIL5qwHmjDyXv8rxBELuZUNWYOTE3zcSN6ft3O4c_RsRnhEBtZlioheHDpDcUtgnrJsYT2SvEA_jwmWz-FEi2IrsU9DqIBtHK_f8ecImUOXthOgS49n_QtKmwDmCi2CSPUx8WHOTnF3-dGWVHrHtAnW0XwvBkUyRcSgqxpDkUSJKBNI6EHXgQ5C0bw35pFrMqbSoq2wFIemSoOQt1RtTjBQkhQ0G4QkVhQafDWlDOyYvzCG"

// `hasMessages` проставлен у каждого литерала: это признак «сервер сообщил
// последнее сообщение», и вывести его из пустоты превью значило бы держать
// утверждение «сообщений нет» на договорённости таблицы превью, а не на факте.
// У последней беседы `last_message` нет — там превью пустое **и** признак ложен;
// у остальных они согласованы, потому что фикстура изображает то же, что ответ.
export const conversations: Conversation[] = [
  {
    id: "anna-petrova",
    name: "Anna Petrova",
    avatarUrl: annaAvatar,
    presence: "online",
    lastMessagePreview: "That works! Let's review the finalized slides tomorrow.",
    lastMessageTimestamp: "14:22",
    hasMessages: true,
  },
  {
    id: "marcus-chen",
    name: "Marcus Chen",
    avatarUrl: marcusAvatar,
    presence: "away",
    lastMessagePreview: "I've sent the updated draft. Take a look when you have a moment.",
    lastMessageTimestamp: "13:58",
    unreadCount: 2,
    typingNames: ["Marcus"],
    hasMessages: true,
  },
  {
    id: "daniel-kim",
    name: "Daniel Kim",
    avatarUrl: danielAvatar,
    lastMessagePreview: "Message deleted",
    lastMessageTimestamp: "Yesterday",
    previewDeleted: true,
    blockedByMe: true,
    hasMessages: true,
  },
  {
    id: "sofia-rossi",
    name: "Sofia Rossi",
    avatarUrl: sofiaAvatar,
    presence: "offline",
    lastSeenAt: "9:14 AM",
    lastMessagePreview: "Let's grab coffee before the project kickoff.",
    lastMessageTimestamp: "Sep 20",
    blockedMe: true,
    hasMessages: true,
  },
  {
    id: "elena-rostova",
    name: "Elena Rostova",
    initials: "ER",
    lastMessagePreview: "Thanks for checking in, talk soon!",
    lastMessageTimestamp: "Sep 18",
    hasMessages: true,
  },
  {
    id: "new-hire-project",
    name: "New Hire Onboarding",
    initials: "NH",
    lastMessagePreview: "",
    lastMessageTimestamp: "",
    hasMessages: false,
  },
]

export const activeConversationId = "anna-petrova"

export const messagesByConversation: Record<string, ChatMessage[]> = {
  "anna-petrova": [
    {
      id: "m1",
      seq: 1,
      authorId: "anna-petrova",
      authorName: "Anna Petrova",
      avatarUrl: annaAvatar,
      kind: "text",
      text: "Hi David! Did you get a chance to look over the design revisions for the homepage?",
      timestamp: "10:30 AM",
    },
    {
      id: "m2",
      seq: 2,
      authorId: "me",
      kind: "text",
      text: "Yes, just reviewed them. The typography hierarchy looks much cleaner now, especially on mobile.",
      timestamp: "10:32 AM",
      deliveryState: "read",
    },
    {
      id: "m3",
      seq: 3,
      authorId: "anna-petrova",
      authorName: "Anna Petrova",
      avatarUrl: annaAvatar,
      kind: "attachment",
      timestamp: "10:34 AM",
      attachment: {
        kind: "image",
        name: "homepage-hero-v3.png",
        state: "ready",
        previewUrl: "https://images.unsplash.com/photo-1618005198919-d3d4b5a92ead?w=600&q=60",
      },
    },
    {
      id: "m3b",
      seq: 4,
      authorId: "anna-petrova",
      authorName: "Anna Petrova",
      avatarUrl: annaAvatar,
      kind: "text",
      text: "Glad you like it. I also updated the color contrast on the secondary buttons to meet AA guidelines.",
      timestamp: "10:35 AM",
    },
    {
      id: "m4",
      seq: 5,
      authorId: "anna-petrova",
      kind: "text",
      text: "",
      timestamp: "10:38 AM",
      deleted: true,
    },
    {
      id: "m4b",
      seq: 6,
      authorId: "anna-petrova",
      authorName: "Anna Petrova",
      avatarUrl: annaAvatar,
      kind: "unsupported",
      timestamp: "10:39 AM",
    },
    {
      id: "m5",
      seq: 7,
      authorId: "me",
      kind: "text",
      text: "Perfect. I'll merge the assets into Figma so the engineering team can reference them.",
      timestamp: "10:40 AM",
      deliveryState: "delivered",
    },
    {
      id: "m6",
      seq: 8,
      authorId: "anna-petrova",
      authorName: "Anna Petrova",
      avatarUrl: annaAvatar,
      kind: "text",
      text: "Awesome! Let me know if you need any extra export formats.",
      timestamp: "10:41 AM",
    },
    {
      id: "m6b",
      seq: 9,
      authorId: "me",
      kind: "attachment",
      timestamp: "10:42 AM",
      deliveryState: "sent",
      attachment: { kind: "voice", name: "Voice message", state: "ready", durationSeconds: 14 },
    },
    {
      id: "m7",
      seq: 10,
      authorId: "me",
      kind: "text",
      text: "Will do. Let's do a quick sync after lunch.",
      timestamp: "10:42 AM",
      deliveryState: "retrying",
    },
    {
      id: "m7b",
      seq: 11,
      authorId: "me",
      kind: "attachment",
      timestamp: "10:43 AM",
      deliveryState: "sending",
      attachment: { kind: "file", name: "motion-specs-draft.pdf", state: "uploading" },
    },
    {
      id: "m8",
      seq: 12,
      authorId: "me",
      kind: "text",
      text: "I can also prepare the motion specs if helpful.",
      timestamp: "10:43 AM",
      deliveryState: "failed",
    },
  ],
  "marcus-chen": [
    {
      id: "mc1",
      seq: 1,
      authorId: "marcus-chen",
      authorName: "Marcus Chen",
      avatarUrl: marcusAvatar,
      kind: "text",
      text: "I've sent the updated draft. Take a look when you have a moment.",
      timestamp: "13:58",
    },
  ],
  "daniel-kim": [
    {
      id: "dk1",
      seq: 1,
      authorId: "daniel-kim",
      authorName: "Daniel Kim",
      avatarUrl: danielAvatar,
      kind: "text",
      text: "Hey, are we still on for the review?",
      timestamp: "Yesterday",
    },
    {
      id: "dk2",
      seq: 2,
      authorId: "daniel-kim",
      kind: "text",
      text: "",
      timestamp: "Yesterday",
      deleted: true,
    },
  ],
  "sofia-rossi": [
    {
      id: "sr1",
      seq: 1,
      authorId: "sofia-rossi",
      authorName: "Sofia Rossi",
      avatarUrl: sofiaAvatar,
      kind: "text",
      text: "Let's grab coffee before the project kickoff.",
      timestamp: "Sep 20",
    },
  ],
  "elena-rostova": [
    {
      id: "er1",
      seq: 1,
      authorId: "elena-rostova",
      authorName: "Elena Rostova",
      kind: "text",
      text: "Thanks for checking in, talk soon!",
      timestamp: "Sep 18",
    },
  ],
  "new-hire-project": [],
}

export const deviceSessions: DeviceSession[] = [
  { id: "s1", deviceLabel: "MacBook Pro · Chrome", userAgent: "macOS 15", lastSeenAt: "now", current: true },
  { id: "s2", deviceLabel: "iPhone 16 · Vector app", userAgent: "iOS 18", lastSeenAt: "12 minutes ago" },
  { id: "s3", deviceLabel: "Windows PC · Edge", userAgent: "Windows 11", lastSeenAt: "3 days ago" },
]
