/**
 * Отказы API — в одном месте, а не у каждого вызывающего.
 *
 * Формат ответа один на весь API: `application/problem+json` (RFC 9457), тело
 * собирает `_problem_body()` в `apps/messenger/messenger/api/main.py`. Разбор,
 * разложенный по вызывающим, однажды вернёт «что-то пошло не так» там, где в
 * теле был `code`, — то есть там, где пользователю можно было сказать точнее.
 */

/** Тело отказа так, как оно приходит по проводу. Снимок `_problem_body()`. */
export interface ProblemBody {
  readonly type?: string;
  readonly title?: string;
  readonly status?: number;
  readonly detail?: string;
  /**
   * `code` в схеме контракта **не объявлен** — и всё же приходит.
   *
   * Схема `Problem` объявляет обязательными `[type, title, status]` и описывает
   * `detail` и `trace_id`; свойства `code` среди них нет. Стандарт (Часть 4)
   * при этом прямо на него опирается: «`code` схема не требует, но по нему
   * клиент различает виды отказа: без него остаётся только код состояния, а
   * `403` выдают три разные причины». Сервер кладёт его в тело рядом с `type`.
   *
   * Отсюда способ чтения: **сырое тело, а не сгенерированная модель
   * `Problem`** — у модели поля `code` нет вовсе, и обращение к нему не
   * компилируется. Разбор по модели вернул бы `undefined` там, где сервер
   * ответил.
   */
  readonly code?: string;
  /**
   * Дублирует заголовок `X-Trace-Id` намеренно (там же, Часть 4): в поддержку
   * приходят со снимком экрана, а не с заголовками ответа.
   */
  readonly trace_id?: string;
}

/** Заголовок, дублирующий `trace_id` тела. */
export const TRACE_ID_HEADER = "X-Trace-Id";

/**
 * Отказ, о котором сервер сказал определённо: `4xx`, кроме `401`.
 *
 * Несёт `code` — ради него тело и разбирается вручную: `403` выдают три разные
 * причины, и различить их по коду состояния нельзя.
 */
export class ApiProblem extends Error {
  readonly status: number;
  readonly type: string | undefined;
  readonly code: string | undefined;
  readonly traceId: string | undefined;

  constructor(params: {
    readonly status: number;
    readonly type?: string;
    readonly title?: string;
    readonly code?: string;
    readonly traceId?: string;
  }) {
    super(params.title ?? `HTTP ${params.status}`);
    this.name = "ApiProblem";
    this.status = params.status;
    this.type = params.type;
    this.code = params.code;
    this.traceId = params.traceId;
  }
}

/**
 * «Повтори позже» — не отказ пользователю и не кончившаяся сессия.
 *
 * Сервер разделяет их намеренно: `_login_failure()` (`api/main.py`) отдаёт
 * `503` при недоступном Keycloak и `401`, когда вход действительно нужен,
 * с объяснением «Слить их в один ответ значило бы отправлять человека на
 * повторный вход в момент, когда вход всё равно не работает».
 */
export class ServiceUnavailableError extends Error {
  readonly traceId: string | undefined;

  constructor(message: string, traceId?: string) {
    super(message);
    this.name = "ServiceUnavailableError";
    this.traceId = traceId;
  }
}

/** Сессии не было вовсе: cookie у первого посетителя просто нет. */
export class UnauthenticatedError extends Error {
  constructor(message = "Вход не выполнен") {
    super(message);
    this.name = "UnauthenticatedError";
  }
}

/** Сессия была и кончилась: `401` на обмене уже после `ready`. */
export class SessionExpiredError extends Error {
  constructor(message = "Сессия истекла") {
    super(message);
    this.name = "SessionExpiredError";
  }
}

/** Тело отказа, если оно вообще разобралось. Не-JSON — не падение разбора. */
async function readProblemBody(response: Response): Promise<ProblemBody | undefined> {
  try {
    const body: unknown = await response.json();
    if (typeof body !== "object" || body === null) {
      return undefined;
    }
    return body as ProblemBody;
  } catch {
    // Перед нами мог оказаться не Problem, а страница от шлюза: заголовок
    // `X-Trace-Id` в этом случае остаётся единственным следом.
    return undefined;
  }
}

function traceIdOf(response: Response, body: ProblemBody | undefined): string | undefined {
  return body?.trace_id ?? response.headers.get(TRACE_ID_HEADER) ?? undefined;
}

/** Разбирает не-2xx в `ApiProblem`. Вызывающий решает, `401` это или `503`. */
export async function problemFromResponse(response: Response): Promise<ApiProblem> {
  const body = await readProblemBody(response);
  return new ApiProblem({
    status: response.status,
    type: body?.type,
    title: body?.title,
    code: body?.code,
    traceId: traceIdOf(response, body),
  });
}

/** Трасса отказа, доступная там, где тело не разбирается (сеть, `503`). */
export function traceIdFromResponse(response: Response): string | undefined {
  return response.headers.get(TRACE_ID_HEADER) ?? undefined;
}
