import http from 'k6/http';
import encoding from 'k6/encoding';
import crypto from 'k6/crypto';

// Тот же production-совместимый code+PKCE поток, что проверяет
// tests/integration/login_check.py и уже используют provision/
// prepare-conversations (Python-версия того же алгоритма, в
// gitops/04-messenger/load-testing/manifests/workflow-template.yaml) —
// не самодельный JWT (docs/messenger/15-load-testing-platform.md, раздел 8).
export const KEYCLOAK = 'http://messenger-idp-service.keycloak.svc.cluster.local:8080';
export const REALM = 'messenger';
export const CLIENT_ID = 'messenger-web';
export const API = 'http://api.messenger.svc.cluster.local';
export const ORIGIN = 'https://app.finops.local';
export const REDIRECT = `${ORIGIN}/callback`;

function pkce() {
  const verifierBytes = crypto.randomBytes(48);
  const verifier = encoding.b64encode(verifierBytes, 'rawurl');
  const digestBinary = crypto.sha256(verifier, 'binary');
  const challenge = encoding.b64encode(digestBinary, 'rawurl');
  return { verifier, challenge };
}

// `URL`/`URLSearchParams` — не везде доступны в k6 JS-рантайме,
// поэтому разбор строки запроса и извлечение `action="..."` — ручные
// регулярки, а не встроенный парсер, как в Python-версии этого потока.
function queryParam(qs, name) {
  const re = new RegExp(`[?&]${name}=([^&]*)`);
  const m = qs.match(re);
  return m ? decodeURIComponent(m[1]) : null;
}

function pathAndQuery(url) {
  const idx = url.indexOf('/', url.indexOf('://') + 3);
  return idx === -1 ? '' : url.slice(idx);
}

/**
 * Логинит одного synthetic-пользователя и возвращает access_token.
 * Бросает исключение на любом отклонении от happy path — вызывающий
 * решает, ловить его или дать k6 засчитать это как ошибку итерации.
 */
export function login(email, password) {
  const state = `${__VU}-${__ITER}-${Date.now()}`;
  const { verifier, challenge } = pkce();

  const authUrl =
    `${KEYCLOAK}/realms/${REALM}/protocol/openid-connect/auth?` +
    `client_id=${CLIENT_ID}&response_type=code&scope=openid&` +
    `redirect_uri=${encodeURIComponent(REDIRECT)}&state=${state}&` +
    `code_challenge=${challenge}&code_challenge_method=S256`;

  const page = http.get(authUrl);
  if (page.status !== 200) {
    throw new Error(`страница входа вернула ${page.status} для ${email}`);
  }
  const formMatch = page.body.match(/action="([^"]+)"/);
  if (!formMatch) {
    throw new Error(`форма входа не найдена для ${email}`);
  }
  // Keycloak строит абсолютную ссылку на свой внешний hostname
  // (idp.finops.local), которого изнутри кластера нет — переписываем
  // источник на внутренний, как и login_check.py.
  const action = `${KEYCLOAK}${pathAndQuery(formMatch[1].replace(/&amp;/g, '&'))}`;

  const submitted = http.post(
    action,
    { username: email, password: password },
    { redirects: 0 },
  );
  const location = submitted.headers['Location'] || submitted.headers['location'];
  if (!location) {
    throw new Error(`форма входа не перенаправила для ${email} (status ${submitted.status})`);
  }
  const code = queryParam(location, 'code');
  if (!code) {
    throw new Error(`в перенаправлении нет code для ${email}`);
  }

  const exchanged = http.post(
    `${API}/auth/callback`,
    JSON.stringify({ code, code_verifier: verifier, redirect_uri: REDIRECT }),
    { headers: { 'Content-Type': 'application/json', Origin: ORIGIN } },
  );
  if (exchanged.status !== 200) {
    throw new Error(`обмен кода отказал для ${email}: ${exchanged.status} ${exchanged.body}`);
  }
  return JSON.parse(exchanged.body).access_token;
}

export function authHeaders(token) {
  return { Authorization: `Bearer ${token}`, Origin: ORIGIN, 'Content-Type': 'application/json' };
}
