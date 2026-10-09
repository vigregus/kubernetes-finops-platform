# Runbook: звонки 1:1

Для того, кого разбудил алерт или кому сообщили «не звонит». Устройство — [часть 17](17-calls.md), решения — [ADR 0007](../ADR/0007-calls-after-web-push.md),
устойчивость к блокировкам — [часть 18](18-resilient-access-and-guest-calls.md). Метрики и алерты — дашборд `messenger-calls` и правила
`gitops/02-infra/observability-objects/victoria-stack/rules/messenger-calls.yaml`.

## Быстрый выключатель

Звонки выключаются флагом `CALLS_ENABLED` (в `gitops/04-messenger/messenger-services/values.yaml`, у API и у `call-sweeper`). При `false` кнопки
звонка у клиента скрыты, маршруты отвечают `404`. Выключать при: потоке нежелательных звонков (пока нет блокировки, `CALL-017`), перегрузке TURN,
утечке данных звонка. Чат при этом работает.

## Алерты

| Алерт | Что значит | Первые действия |
| --- | --- | --- |
| `CallsEstablishFailureHigh` | больше 30% принятых звонков не состоялись за 15 мин (≥5 звонков) | 1) доля `relay` растёт? — смотри `CallsRelayShareHigh` и состояние TURN; 2) в `messenger_call_ended_total{reason}` растёт `failed` или `unavailable`? 3) Centrifugo жив? (`503 realtime_unavailable` при отправке сигнала) 4) массовая смена сети или сбой у оператора? |
| `CallsRelayShareHigh` | через релей идёт заметно больше допущения (15%) | это не отказ, а рост стоимости и нагрузки на TURN: проверить `TurnAllocationsNearQuota`, полосу, потери; возможна закрытая сеть (UDP закрыт у большой группы) |
| `CallSetupSlowP95` | p95 установки больше 10 с | сигнализация (задержка API, Centrifugo), сбор ICE по закрытому UDP (ждёт таймаута), перегрузка TURN |
| `CallSweeperStopped` | `call-sweeper` не работает | итоги просроченных звонков запаздывают, звонки застревают (`ringing`, `accepted`); перезапустить Deployment; при застрявших линиях звонящий при новом звонке закрывает их сам (`_expire_live`) |
| `TurnAllocationsNearQuota` | у coturn больше 80 из 100 выделений | поднять квоту (`--total-quota`) или добавить узел; проверить, не злоупотребляет ли кто-то выдачей доступа |

## Типовые обращения

**«Кнопки звонка нет или она выключена».** Сначала [часть 17](17-calls.md): страница по HTTP или с недоверенным сертификатом (нет `getUserMedia` и
`RTCPeerConnection`), нет подключённого realtime (`rt`), WebRTC выключен политикой (Lockdown на iPhone), у собеседника нет ни сокета, ни
push-подписки. Кнопка недоступна с причиной, а не молча.

**«Звонок идёт, звука нет».** Состояние `connected` ничего не доказывает: смотреть растёт ли `packetsReceived` (`getStats()`), путь соединения
(`host`/`srflx`/`relay`). Релей по TCP/TLS на потерях держит хуже UDP. Проверка — браузерная приёмка `g4-calls` и `tests/integration/turn_check.py`.

**«Звонок на спящем телефоне не пришёл».** Звонок доходит только до открытой страницы, пока не сделан `CALL-018` (Web Push о звонке). После —
см. ADR 0010 (iPhone: только приложение с экрана «Домой»; push не считается надёжным).

**«Застрял звонок, нельзя позвонить».** У человека один живой звонок. Просроченное закрывает следующий звонок или подметальщик; проверить
`call-sweeper`. Крайняя мера — завершить звонок в базе (`calls.state='ended'`, `end_reason='failed'`) и сообщить в инцидент.

## TURN (coturn, `messenger-turn`)

Манифест `gitops/04-messenger/messenger-turn/manifests/turn.yaml`: UDP 3478 и диапазон релея 30500–30519, учётные данные HMAC (`--use-auth-secret`,
секрет в `messenger-secrets`), частные диапазоны закрыты (`--denied-peer-ip`), потолки `--bps-capacity`, `--user-quota`, `--total-quota`.

- **Узел недоступен.** Клиент делает ICE restart в пределах 15 с; релейные звонки переустанавливаются, прямые не затронуты (`CALL-016`).
  Если второго TURN нет (`RES-006`), релейные звонки в этот период не соединяются.
- **TURN/TLS на 443 (`RES-005`).** Отдельная служба `messenger-turn-tls` (443 → 5349 в поде), имя `turns.finops.local`, сертификат `messenger-turn-tls`
  (cert-manager, локальный CA). Под без секрета с сертификатом не запустится. Признак отказа: звонки в закрытых сетях не соединяются, доля `turn_transport="tls"`
  в `messenger_call_media_path_total` падает. Проверка: `tests/integration/turn_check.py` (рукопожатие, релей по TLS); браузерная `RES-A4` (`E2E_TURNS_PORT`).
- **Сервис без внешнего адреса.** На стенде `messenger-turn` в `Progressing` (LoadBalancer без адреса): релей из внешних сетей недоступен,
  локальные звонки идут по `host`-кандидатам.
- **Ротация секрета.** Секрет TURN меняется в `messenger-secrets`; выданные доступы живут 5–10 минут, активные звонки обновляют их
  (`setConfiguration`), после ротации новые выдачи подписываются новым секретом.
- **Диагностика с клиента.** `GET /calls/{id}/ice-servers` (участник живого звонка); `turn_check.py` проверяет `Allocate` и релей на живом coturn.

## Чего runbook не покрывает (пока)

Блокировка пользователей и лимит звонков (`CALL-017`), Web Push о звонках (`CALL-018…020`), несколько TURN и TLS на 443 (`RES-005`/`RES-006`),
панель «Network capabilities» (`RES-012`), звонки по ссылке (`LINK-*`) — по мере реализации дополняют этот файл.

## Путь и причины отказов (RES-009)

- `messenger_call_media_path_total{path,turn_transport,network_country,network_class}`: `path` — `host | srflx | prflx | relay`, `turn_transport` — `udp | tcp | tls` у релея
  (`none` без релея; `relayProtocol` есть в `getStats()` Chromium, в других браузерах транспорт неизвестен). Доля TLS-релея — `…{path="relay",turn_transport="tls"}` к `…{path="relay"}`.
- `messenger_call_failure_total{reason,network_country,network_class}`: причины — `ice_connect_timeout` (связь так и не поднялась), `ice_disconnected` (оборвалась после
  разговора), `media_denied`, `unknown`; остальные значения таксономии (часть 18, §8) заполнят диагностика и запасные транспорты.
- Страна и класс сети приходят заголовками шлюза `X-Client-Country`, `X-Client-Network-Class`. Без GeoIP на шлюзе они `unknown`, и разреза «в мобильных сетях РФ»
  нет — это настройка входа, не ошибка приложения. IP не сохраняется и в метки не попадает.
