import http from 'k6/http';
import crypto from 'k6/crypto';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

import { login, authHeaders, API } from './lib/auth.js';

// Профиль `recovery` (docs/messenger/15-load-testing-platform.md, раздел
// 19) — контролируемый backlog, затем возврат к базовой нагрузке:
//
//   stage_high (HIGH_RATE_FRACTION × TARGET_RATE, HIGH_DURATION)
//   → stage_low (TARGET_RATE, DURATION)
//
// Сам k6 не решает, восстановился ли backlog — это отдельный Argo-шаг
// `recovery-check` (workflow-template.yaml), который поллит
// messenger_outbox_pending/kafka_consumergroup_lag в VictoriaMetrics
// после того, как stage_low снизил темп. Здесь — только создание
// контролируемой нагрузки двумя ступенями, тем же механизмом
// `startTime`, что и в stress.js (раздел 18): без него оба сценария
// стартовали бы одновременно, и «высокая, потом низкая» превратилось бы
// в сумму рейтов с первой секунды.
//
// Логин — в setup(), не в первой итерации каждого VU: тот же живой
// дефект verification §18 (прогон stress-probe1/2), что уже исправлен в
// stress.js — constant-arrival-rate преаллоцирует VU каждой ступени
// отдельно, и логин на границе ступени означал бы залп в Keycloak, а не
// измерение backlog. Тот же принцип, что и там: registration (provision)
// / login (здесь, в setup(), с разбросом) / отправка (stage_*) — три
// разные фазы, не смешанные в одну.

const RUN_ID = __ENV.RUN_ID;
const USERS = parseInt(__ENV.USERS || '10', 10);
const RUN_PASSWORD = __ENV.RUN_PASSWORD;
const TARGET_RATE = parseInt(__ENV.TARGET_RATE || '10', 10);
const DURATION = __ENV.DURATION || '30s';
const HIGH_RATE_FRACTION = parseFloat(__ENV.HIGH_RATE_FRACTION || '3');
const HIGH_DURATION = __ENV.HIGH_DURATION || '1m';
const LOGIN_STAGGER_SECONDS = parseFloat(__ENV.LOGIN_STAGGER_SECONDS || '1');

const accepted = new Counter('load_run_accepted_messages');
const offered = new Counter('load_run_offered_messages');
const sendDuration = new Trend('load_run_send_duration_ms', true);

function parseDurationSeconds(value) {
  const match = /^(\d+)(ms|s|m|h)$/.exec(value);
  if (!match) {
    throw new Error(`длительность не распознана как k6-длительность: ${value}`);
  }
  const amount = parseInt(match[1], 10);
  const unit = { ms: 0.001, s: 1, m: 60, h: 3600 }[match[2]];
  return amount * unit;
}

const HIGH_DURATION_SECONDS = parseDurationSeconds(HIGH_DURATION);
const HIGH_RATE = Math.max(1, Math.round(TARGET_RATE * HIGH_RATE_FRACTION));

export const options = {
  scenarios: {
    stage_high: {
      executor: 'constant-arrival-rate',
      rate: HIGH_RATE,
      timeUnit: '1s',
      duration: HIGH_DURATION,
      startTime: '0s',
      preAllocatedVUs: Math.max(HIGH_RATE * 2, 2),
      maxVUs: Math.max(HIGH_RATE * 4, 4),
      exec: 'sendMessage',
    },
    stage_low: {
      executor: 'constant-arrival-rate',
      rate: TARGET_RATE,
      timeUnit: '1s',
      duration: DURATION,
      startTime: `${Math.round(HIGH_DURATION_SECONDS)}s`,
      preAllocatedVUs: Math.max(TARGET_RATE * 2, 2),
      maxVUs: Math.max(TARGET_RATE * 4, 4),
      exec: 'sendMessage',
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

/** Логин и разбор бесед — здесь, не в `sendMessage` (см. комментарий выше). */
export function setup() {
  const sessions = {};
  for (let i = 1; i <= USERS; i += 1) {
    const email = `local-capacity-${RUN_ID}-${String(i).padStart(6, '0')}@finops.local`;
    const token = login(email, RUN_PASSWORD);

    const r = http.get(`${API}/conversations`, { headers: authHeaders(token) });
    if (r.status !== 200) {
      throw new Error(`не удалось получить список бесед для ${email}: ${r.status} ${r.body}`);
    }
    const items = JSON.parse(r.body).items || [];
    if (items.length === 0) {
      throw new Error(`у пользователя ${email} нет ни одной беседы — prepare-conversations не отработал`);
    }

    sessions[i] = { token, conversationId: items[0].conversation_id };
    if (i < USERS) sleep(LOGIN_STAGGER_SECONDS);
  }
  return { sessions };
}

export function sendMessage(data) {
  const index = ((__VU - 1) % USERS) + 1;
  const session = data.sessions[index];

  offered.add(1);
  const start = Date.now();
  const res = http.post(
    `${API}/conversations/${session.conversationId}/messages`,
    JSON.stringify({
      client_message_id: uuidv4(),
      type: 'text',
      payload: { text: `load-testing-recovery ${RUN_ID} ${Date.now()}` },
      attachment_ids: [],
    }),
    { headers: authHeaders(session.token) },
  );
  sendDuration.add(Date.now() - start);

  const ok = check(res, {
    'сообщение принято (200/201)': (r) => r.status === 200 || r.status === 201,
  });
  if (ok) {
    accepted.add(1);
  }
}
