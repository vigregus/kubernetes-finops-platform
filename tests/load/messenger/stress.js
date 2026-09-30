import http from 'k6/http';
import crypto from 'k6/crypto';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

import { login, authHeaders, API } from './lib/auth.js';

// Профиль `stress` (docs/messenger/15-load-testing-platform.md, раздел
// 18) — не одно число вроде "max = 843 msg/s", а пять последовательных
// ступеней растущей интенсивности (25% / 50% / 100% / 150% / 200% от
// TARGET_RATE), каждая — свой k6-сценарий с собственным именем. k6 сам
// проставляет тег `scenario` на каждый сэмпл метрики — этого достаточно,
// чтобы в Grafana разложить latency/error rate/queue-метрики по ступеням
// и НАЙТИ на графике R_healthy/R_knee/R_collapse глазами (раздел 18 прямо
// требует не сворачивать три режима в одно число — автоматическая
// классификация точки перегиба сюда сознательно не входит: то, что здесь
// проверено живьём, — это сама эскалация нагрузки и тегировка стадий, не
// алгоритм её интерпретации).
//
// Логика отправки — тот же код, что в messages.js (login/conversation-
// кэш на VU, POST /messages, accepted/offered Counter) — сознательно не
// вынесена в общий модуль в рамках этой ветки: рефакторинг двух уже
// живых профилей ради разделяемого файла — отдельная, самостоятельная
// правка, а не часть добавления stress.

const RUN_ID = __ENV.RUN_ID;
const USERS = parseInt(__ENV.USERS || '10', 10);
const RUN_PASSWORD = __ENV.RUN_PASSWORD;
const TARGET_RATE = parseInt(__ENV.TARGET_RATE || '10', 10);
const STAGE_DURATION = __ENV.DURATION || '30s';
/**
 * Разброс между логинами в `setup()` — секунд на аккаунт.
 *
 * Живой дефект (верификация §18, прогон `stress-probe1`): `constant-
 * arrival-rate` преаллоцирует VU каждой ступени отдельно (свой пул на
 * `stage_150pct`, свой на `stage_200pct`, …), и раньше логин был внутри
 * `sendMessage` — на первой итерации каждого свежего VU. На `stage_200pct`
 * (preAllocatedVUs ~120) это означало залп из ~120 одновременных логинов
 * в Keycloak ровно на границе ступени — и именно там появилась лавина
 * "форма входа не перенаправила": не обязательно предел throughput
 * отправки сообщений, а разовый шторм авторизации от самой конструкции
 * теста. Раздел 18 просит найти R_knee у ОТПРАВКИ сообщений — значит
 * логин обязан быть отдельной, не таймируемой фазой.
 */
const LOGIN_STAGGER_SECONDS = parseFloat(__ENV.LOGIN_STAGGER_SECONDS || '1');

const accepted = new Counter('load_run_accepted_messages');
const offered = new Counter('load_run_offered_messages');
const sendDuration = new Trend('load_run_send_duration_ms', true);

const STAGES = [
  { name: 'stage_25pct', fraction: 0.25 },
  { name: 'stage_50pct', fraction: 0.5 },
  { name: 'stage_100pct', fraction: 1.0 },
  { name: 'stage_150pct', fraction: 1.5 },
  { name: 'stage_200pct', fraction: 2.0 },
];

// Без явного `startTime` k6 стартует все сценарии `options.scenarios`
// одновременно (в 0с) — пять "ступеней" превратились бы в один залп на
// сумму всех rate, что прямо противоположно эскалации, которую раздел 18
// требует. `startTime` — накопительная сумма длительностей предыдущих
// ступеней, а не жёстко заданные оффсеты, чтобы список STAGES оставался
// единственным местом, которое нужно менять.
function parseDurationSeconds(value) {
  const match = /^(\d+)(ms|s|m|h)$/.exec(value);
  if (!match) {
    throw new Error(`STAGE_DURATION не распознан как k6-длительность: ${value}`);
  }
  const amount = parseInt(match[1], 10);
  const unit = { ms: 0.001, s: 1, m: 60, h: 3600 }[match[2]];
  return amount * unit;
}

const STAGE_DURATION_SECONDS = parseDurationSeconds(STAGE_DURATION);

function scenarioFor(stage, index) {
  const rate = Math.max(1, Math.round(TARGET_RATE * stage.fraction));
  return {
    executor: 'constant-arrival-rate',
    rate,
    timeUnit: '1s',
    duration: STAGE_DURATION,
    startTime: `${Math.round(index * STAGE_DURATION_SECONDS)}s`,
    preAllocatedVUs: Math.max(rate * 2, 2),
    maxVUs: Math.max(rate * 4, 4),
    exec: 'sendMessage',
  };
}

export const options = {
  scenarios: Object.fromEntries(STAGES.map((s, i) => [s.name, scenarioFor(s, i)])),
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

/**
 * Логин и разбор бесед — здесь, а не в `sendMessage`.
 *
 * `setup()` в k6 выполняется один раз, до старта всех таймированных
 * сценариев, вне VU-пула — значит регистрация (уже отдельный шаг
 * provision) и логин (здесь) физически отделены от измерения отправки
 * сообщений под растущей нагрузкой: `stage_*` видят только POST
 * /messages на уже готовых токенах, не смешивая "Keycloak не успевает
 * логинить" с "API не успевает принимать сообщения" в одной цифре.
 *
 * Разброс `LOGIN_STAGGER_SECONDS` между аккаунтами — не для скорости (эта
 * фаза не таймируется и не идёт в метрики стадий), а чтобы сам процесс
 * логина не создавал Keycloak собственный залп: `USERS` последовательных
 * запросов с паузой, а не десятки параллельных VU одновременно.
 */
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
      payload: { text: `load-testing-stress ${RUN_ID} ${Date.now()}` },
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
