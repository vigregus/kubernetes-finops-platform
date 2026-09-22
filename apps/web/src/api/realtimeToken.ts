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
import type { Configuration } from "./generated";
import { SessionsApi } from "./generated";

/** Что вернёт вызывающий: свежий тикет на каждую попытку соединения. */
export type RealtimeTicketIssuer = () => Promise<string>;

export function createRealtimeTicketIssuer(configuration: Configuration): RealtimeTicketIssuer {
  const sessions = new SessionsApi(configuration);

  return async () => {
    const response = await sessions.issueRealtimeToken();
    return response.token;
  };
}
