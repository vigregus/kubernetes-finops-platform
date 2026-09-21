/**
 * Идентификатор устройства — единственное, что клиент хранит между загрузками.
 *
 * Хранилище выбрано не по удобству, а по коду сервера: тело читается **только**
 * в `/auth/callback` (`_device_from(body.device_id, request)`, `api/main.py`),
 * все остальные защищённые пути — `_device_from(None, request)`, то есть
 * исключительно из заголовка `X-Device-Id`. Тела у `/auth/refresh` нет и в
 * контракте, поэтому в теле идентификатор ехать не может — только заголовком,
 * а взять его до первого запроса неоткуда, кроме хранилища.
 *
 * `ADR 0005` запрещает `localStorage` для **удостоверений** — дословно:
 * «`localStorage` для удостоверений не используется никогда». Идентификатор
 * устройства удостоверением не является: это метка, выданная сервером, и
 * именно она делает `ensure_device` upsert'ом. Токен в это хранилище не
 * попадает никогда — access token живёт в замыкании вкладки (`api/client.ts`),
 * `code_verifier` и `state` — в `sessionStorage` (`session.ts`).
 *
 * Отсюда инвариант, который проверяется чтением, а не намерением: в
 * `localStorage` ровно **один** ключ.
 */

/** Единственный ключ. Имя — как у заголовка и поля тела, чтобы не заводить третье. */
export const DEVICE_ID_KEY = "device_id";

/** Что нужно от хранилища. `removeItem` не нужен: идентификатор не снимается. */
export interface DeviceIdStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** `undefined` и пустая строка — не идентификатор: сервер завёл бы новое устройство. */
export function loadDeviceId(storage: DeviceIdStore): string | null {
  const value = storage.getItem(DEVICE_ID_KEY);
  return value === null || value === "" ? null : value;
}

export function saveDeviceId(storage: DeviceIdStore, deviceId: string): void {
  storage.setItem(DEVICE_ID_KEY, deviceId);
}

/**
 * Заводит идентификатор **до** первого запроса, если его ещё нет.
 *
 * Порядок здесь — предмет проверки, а не деталь. Если брать идентификатор из
 * ответа, получается петля: первый же `refresh` обязан нести `X-Device-Id`, а
 * взять его неоткуда. Сервер на вызов без заголовка не ошибается — он чеканит
 * новое устройство (`_device_for` при `device_id=None` выдаёт `DeviceId(uuid4())`),
 * то есть **каждая перезагрузка вкладки добавляла бы строку в `devices`**.
 *
 * Генератор — параметр, а не `crypto.randomUUID()` внутри: тесту нужно
 * предъявить, что сохранён именно он, а не совпадение.
 */
export function ensureDeviceId(
  storage: DeviceIdStore,
  generate: () => string = () => crypto.randomUUID(),
): string {
  const existing = loadDeviceId(storage);
  if (existing !== null) {
    return existing;
  }
  const created = generate();
  saveDeviceId(storage, created);
  return created;
}
