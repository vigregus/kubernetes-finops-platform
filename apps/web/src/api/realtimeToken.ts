/**
 * Тикет подключения к Centrifugo — отдельной функцией, а не значением.
 *
 * Функция, а не строка: тикет живёт 120 секунд (`token_ttl_seconds` в
 * `adapters/centrifugo.py`), и соединение после окна офлайна восстанавливается
 * **после** того, как прежний истёк. Значение, взятое один раз при загрузке
 * страницы, гарантированно просрочено ровно в том сценарии, ради которого этот
 * гейт существует. Поэтому вызывающий получает **способ добыть тикет**, а
 * когда его звать — решает SDK: `getData` вызывается на каждой попытке
 * соединения (`Options.getData` у закреплённой версии).
 *
 * Через сгенерированный клиент, а не вручную: `POST /realtime/token` — операция
 * контракта, и путь, метод и заголовки обязаны приходить из него. Заголовок
 * устройства при этом едет сам — его ставит общий `send` в `api/client.ts`,
 * и вызов мимо него завёл бы серверу новое устройство на каждый вход.
 */
import { withUnwrappedErrors } from "./client";
import type { Configuration } from "./generated";
import { SessionsApi } from "./generated";

/** Что вернёт вызывающий: свежий тикет на каждую попытку соединения. */
export type RealtimeTicketIssuer = () => Promise<string>;

export function createRealtimeTicketIssuer(configuration: Configuration): RealtimeTicketIssuer {
  // `withUnwrappedErrors` — не украшение, как и у остальных API в `main.tsx`:
  // без неё `SessionExpiredError`/`UnauthenticatedError`, которые бросает
  // `fetchApi` (`api/client.ts`), не долетают сюда — `BaseAPI.request()`
  // сгенерированного клиента ловит их в своём `try/catch` и заворачивает в
  // `FetchError`. Живой дефект: `getData` в `realtimeClient.ts` ловит именно
  // `SessionExpiredError`/`UnauthenticatedError`, чтобы обернуть их в
  // `Centrifuge.UnauthorizedError` и остановить реконнект (`connectionMachine.ts`,
  // `reconnectAllowed`) — без этой обёртки инстанс `SessionsApi` отдавал
  // `FetchError` вместо них, проверка `instanceof` никогда не срабатывала, и
  // потерянная сессия ретраила `POST /realtime/token` бесконечно, ровно тот
  // цикл, который PR с "фиксом" должен был остановить.
  const sessions = withUnwrappedErrors(new SessionsApi(configuration));

  return async () => {
    const response = await sessions.issueRealtimeToken();
    return response.token;
  };
}
