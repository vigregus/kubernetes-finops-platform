import http from 'k6/http';
import crypto from 'k6/crypto';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';

import { login, authHeaders, API } from './lib/auth.js';

// Профиль `mixed` (docs/messenger/15-load-testing-platform.md, раздел 17)
// — не только `POST messages`: часть клиентов генерирует receipts
// (delivered_seq/read_seq), потому что этот путь нагружает DB/unread
// projection/realtime publication по-другому, чем просто запись
// сообщения, и production-трафик — это всегда смесь, а не только отправка.
//
// Два независимых k6-сценария вместо одного с ветвлением внутри
// итерации: у k6 каждый сценарий выделяет свои VU от 1, поэтому "VU 1
// сценария messages" и "VU 1 сценария receipts" — разные synthetic-
// пользователи по построению. Пары (1,2)(3,4)... — те же, что уже завела
// prepare-conversations: нечётный номер пары шлёт сообщения, чётный —
// квитирует.

const RUN_ID = __ENV.RUN_ID;
const USERS = parseInt(__ENV.USERS || '10', 10);
const RUN_PASSWORD = __ENV.RUN_PASSWORD;
const TARGET_RATE = parseInt(__ENV.TARGET_RATE || '10', 10);
const DURATION = __ENV.DURATION || '30s';
// Receipts — не отдельный параметр Workflow: доля от того же target_rate,
// как и просит раздел 17 ("часть клиентов"), не отдельно настраиваемый
// поток.
const RECEIPT_RATE = Math.max(1, Math.round(TARGET_RATE * 0.5));
const PAIRS = Math.max(1, Math.floor(USERS / 2));

const accepted = new Counter('load_run_accepted_messages');
const offered = new Counter('load_run_offered_messages');
const sendDuration = new Trend('load_run_send_duration_ms', true);
const receiptsAccepted = new Counter('load_run_accepted_receipts');
const receiptsOffered = new Counter('load_run_offered_receipts');
const receiptDuration = new Trend('load_run_receipt_duration_ms', true);

export const options = {
  scenarios: {
    messages: {
      executor: 'constant-arrival-rate',
      rate: TARGET_RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(TARGET_RATE * 2, 2),
      maxVUs: Math.max(TARGET_RATE * 4, 4),
      exec: 'sendMessage',
    },
    receipts: {
      executor: 'constant-arrival-rate',
      rate: RECEIPT_RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(RECEIPT_RATE * 2, 2),
      maxVUs: Math.max(RECEIPT_RATE * 4, 4),
      exec: 'sendReceipt',
    },
  },
};

function uuidv4() {
  const bytes = new Uint8Array(crypto.randomBytes(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes)
    .map((x) => x.toString(16).padStart(2, '0'))
    .join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// Логин кэшируется на VU (один поход за прогон), а список бесед — нет: у
// receipts значение имеет именно текущий seq, и кэшировать его означало
// бы слать одну и ту же квитанцию всю длительность прогона, а не отражать
// движение реальной переписки во время нагрузки.
let cachedToken = null;

function loginForPair(offsetFromPairStart) {
  if (cachedToken) return cachedToken;
  const pair = ((__VU - 1) % PAIRS) + 1;
  const index = pair * 2 - 1 + offsetFromPairStart;
  const email = `local-capacity-${RUN_ID}-${String(index).padStart(6, '0')}@finops.local`;
  cachedToken = login(email, RUN_PASSWORD);
  return cachedToken;
}

const senderLoginOdd = () => loginForPair(0);
const senderLoginEven = () => loginForPair(1);

function firstConversation(token) {
  const r = http.get(`${API}/conversations`, { headers: authHeaders(token) });
  if (r.status !== 200) {
    throw new Error(`не удалось получить список бесед: ${r.status} ${r.body}`);
  }
  const items = JSON.parse(r.body).items || [];
  if (items.length === 0) {
    throw new Error('у пользователя нет ни одной беседы — prepare-conversations не отработал для него');
  }
  return items[0];
}

export function sendMessage() {
  const token = senderLoginOdd();
  const conversationId = firstConversation(token).conversation_id;

  offered.add(1);
  const start = Date.now();
  const res = http.post(
    `${API}/conversations/${conversationId}/messages`,
    JSON.stringify({
      client_message_id: uuidv4(),
      type: 'text',
      payload: { text: `load-testing-mixed ${RUN_ID} ${Date.now()}` },
      attachment_ids: [],
    }),
    { headers: authHeaders(token) },
  );
  sendDuration.add(Date.now() - start);

  const ok = check(res, {
    'сообщение принято (200/201)': (r) => r.status === 200 || r.status === 201,
  });
  if (ok) {
    accepted.add(1);
  }
}

export function sendReceipt() {
  const token = senderLoginEven();
  const conversation = firstConversation(token);
  const conversationId = conversation.conversation_id;
  const seq = conversation.last_message ? conversation.last_message.seq : 0;

  receiptsOffered.add(1);
  const start = Date.now();
  const res = http.post(
    `${API}/conversations/${conversationId}/receipts`,
    JSON.stringify({ delivered_seq: seq, read_seq: seq }),
    { headers: authHeaders(token) },
  );
  receiptDuration.add(Date.now() - start);

  const ok = check(res, {
    'квитанция принята (200)': (r) => r.status === 200,
  });
  if (ok) {
    receiptsAccepted.add(1);
  }
}
