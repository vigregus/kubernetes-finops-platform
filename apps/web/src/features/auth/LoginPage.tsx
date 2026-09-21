import { LoginScreen } from "./components/LoginScreen";
import { startLogin } from "./session";

interface LoginPageProps {
  sessionExpired?: boolean;
}

/**
 * Настоящий вход: `state` и `code_verifier` заводятся, откладываются в
 * `sessionStorage` и происходит уход на Keycloak.
 *
 * `beginLogin` асинхронен, потому что считает S256 через `crypto.subtle`, а он
 * асинхронен по природе. Отклонение здесь не перехватывается: `subtle` в
 * браузере есть (в отличие от jsdom, где его подменяют тесты), и его отсутствие
 * — дефект окружения, а не состояние интерфейса, о котором человеку есть что
 * сказать.
 */
function login(): void {
  void startLogin({
    store: window.sessionStorage,
    origin: window.location.origin,
  });
}

export function LoginPage({ sessionExpired }: LoginPageProps) {
  return <LoginScreen sessionExpired={sessionExpired} onLogin={login} />;
}
