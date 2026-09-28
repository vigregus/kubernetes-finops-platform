import http from 'k6/http';
import crypto from 'k6/crypto';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';

import { login, authHeaders, API } from './lib/auth.js';

// Профиль `messages` (docs/messenger/15-load-testing-platform.md, раздел
// 10) — чистый message throughput. Открытая модель нагрузки (раздел 11):
// arrival-rate executor держит темп предложения запросов независимо от
// того, тормозит ли SUT, а не closed-model VU-цикл, который сам снижает
// интенсивность при деградации и прячет момент насыщения.
//
// Correlation-поля раздела 13 (message_id/seq/ws_received) здесь ещё не
// собираются — это отдельный шаг (REST reconciliation, раздел 14),
// которому нужен свой доступ к данным вне k6. Этот профиль пока
// подтверждает только предложенную/принятую сторону: offered vs accepted.
//
// Живьём проверен командой `k6 run` внутри load-testing-namespace против
// пользователей и беседы, заведённых provision/prepare-conversations —
// см. gitops/04-messenger/load-testing/manifests/k6-messages-configmap.yaml
// и WorkflowTemplate-шаг k6-load.

const RUN_ID = __ENV.RUN_ID;
const USERS = parseInt(__ENV.USERS || '10', 10);
const RUN_PASSWORD = __ENV.RUN_PASSWORD;
const TARGET_RATE = parseInt(__ENV.TARGET_RATE || '10', 10);
const DURATION = __ENV.DURATION || '30s';

const accepted = new Counter('load_run_accepted_messages');
const offered = new Counter('load_run_offered_messages');
const sendDuration = new Trend('load_run_send_duration_ms', true);

export const options = {
  scenarios: {
    messages: {
      executor: 'constant-arrival-rate',
      rate: TARGET_RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(TARGET_RATE * 2, 2),
      maxVUs: Math.max(TARGET_RATE * 4, 4),
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

// Состояние на VU: логин и id беседы стоят одного похода на пользователя
// за весь прогон, а не на каждую итерацию — иначе профиль измерял бы
// стоимость логина, а не message throughput.
let cachedToken = null;
let cachedConversationId = null;

function senderLogin() {
  if (cachedToken) return cachedToken;
  const index = ((__VU - 1) % USERS) + 1;
  const email = `local-capacity-${RUN_ID}-${String(index).padStart(6, '0')}@finops.local`;
  cachedToken = login(email, RUN_PASSWORD);
  return cachedToken;
}

function senderConversationId(token) {
  if (cachedConversationId) return cachedConversationId;
  const r = http.get(`${API}/conversations`, { headers: authHeaders(token) });
  if (r.status !== 200) {
    throw new Error(`не удалось получить список бесед: ${r.status} ${r.body}`);
  }
  const items = JSON.parse(r.body).items || [];
  if (items.length === 0) {
    throw new Error('у пользователя нет ни одной беседы — prepare-conversations не отработал для него');
  }
  cachedConversationId = items[0].conversation_id;
  return cachedConversationId;
}

export default function () {
  const token = senderLogin();
  const conversationId = senderConversationId(token);

  offered.add(1);
  const start = Date.now();
  const res = http.post(
    `${API}/conversations/${conversationId}/messages`,
    JSON.stringify({
      client_message_id: uuidv4(),
      type: 'text',
      payload: { text: `load-testing ${RUN_ID} ${Date.now()}` },
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
