# LOCAL-CAPACITY-001 — Kubernetes-native load testing platform для Messenger

> Статус: план, сохранён для поэтапной реализации. Прогресс отмечается по
> разделам ниже (`[ ]` не начато, `[~]` в работе, `[x]` сделано) по мере
> закрытия веток. Ссылка на исходную дискуссию и обоснование очерёдности —
> в истории PR, открывающего каждый раздел.

## Цель

Построить воспроизводимую систему нагрузочного тестирования Messenger на базе:

```
Argo Workflows
        +
k6 Operator
        +
Playwright
        +
VictoriaMetrics / Grafana
        +
Tempo / VictoriaLogs
```

Система должна не только создавать нагрузку, но и:

```
запускать полный lifecycle теста
→ подготавливать пользователей и данные
→ выполнять browser smoke
→ создавать распределённую k6-нагрузку
→ проверять фактическую доставку сообщений
→ выполнять recovery/reconciliation
→ сохранять результаты
→ сравнивать разные прогоны
→ гарантированно очищать test data
```

`LOCAL-CAPACITY-001` — локальный capacity benchmark текущего Minikube/ноутбука.
Его результаты не являются production capacity claim и не заменяют будущий
`PERF-009`.

## 1. Архитектурное решение — `[x]`

### Argo Workflows — orchestration layer

Argo Workflows отвечает за lifecycle:

```
preflight
    ↓
provision identities
    ↓
prepare conversations
    ↓
Playwright smoke
    ↓
start browser canary
    ↓
k6 TestRun
    ↓
reconciliation
    ↓
collect results
    ↓
recovery verification
    ↓
cleanup
```

Argo не генерирует нагрузку самостоятельно.

### k6 Operator — load execution layer

k6 Operator отвечает за:

- TestRun CRD
- distributed k6 runners
- parallelism
- runner lifecycle
- load-generator resources

### Playwright — real browser acceptance

Playwright используется для проверки реального browser-flow:

```
DOM → frontend → API → DB/outbox/Kafka → realtime → DOM
```

Playwright не используется для создания основной нагрузки.

### Existing observability stack

Результаты анализируются существующим стеком:

- VictoriaMetrics → performance history
- Grafana → run comparison
- Tempo → latency/RCA
- VictoriaLogs → failures/rejections

Не добавлять отдельную InfluxDB только ради k6.

## 2. Deployment model — `[x]`

Argo Workflows и k6 Operator должны ставиться декларативно через существующий
GitOps.

Важно: существующий в проекте Argo CD — это не Argo Workflows. Argo Workflows
устанавливается отдельным controller/UI, но его manifests/Helm release должны
управляться существующим Argo CD.

Предлагаемая структура:

```
gitops/
├── 02-infra/
│   ├── argo-workflows/
│   └── k6-operator/
│
├── 04-messenger/
│   └── load-testing/
│       ├── workflows/
│       ├── rbac/
│       └── dashboards/
│
tests/
├── load/
│   └── messenger/
│       ├── lib/
│       ├── identity.js
│       ├── connections.js
│       ├── messages.js
│       ├── stress.js
│       ├── recovery.js
│       └── mixed.js
│
└── e2e/
    └── ...
```

Не создавать отдельный `test-platform/` root, если существующий
`gitops/02-infra` и `tests/` уже выражают эти границы.

## 3. Namespace и RBAC — `[x]`

Выделить отдельный namespace, например:

```
load-testing
```

Argo workflow service account должен получать только необходимые права.

Он должен иметь возможность:

- create/get/watch/delete k6 TestRun
- create Jobs/Pods для fixture/reconciliation
- читать status созданных test resources
- читать необходимые ConfigMap/Secret

Не выдавать ему cluster-admin.

Provisioning пользователей в Keycloak должен происходить через специально
предназначенный test credential/Secret, а не через секрет, зашитый в
repository.

## 4. Главный Argo WorkflowTemplate — `[x]`

Создать reusable:

```
WorkflowTemplate:
messenger-local-capacity
```

С параметрами минимум:

- run_id
- git_sha
- profile
- users
- online_users
- target_rate
- duration
- k6_parallelism
- trace_sampling_mode
- cleanup

Пример вызова:

```
profile=messages
users=5000
online_users=2000
target_rate=400
duration=10m
k6_parallelism=2
```

Один и тот же template должен использоваться для разных нагрузочных профилей,
а не копироваться в несколько почти одинаковых Workflow YAML.

## 5. Run identity — `[x]`

Каждый прогон получает уникальный:

```
run_id
```

Например:

```
local-capacity-20260928-001
```

Этот идентификатор должен проходить через всю систему:

- Argo Workflow
- k6 tags
- synthetic usernames
- logs
- summary
- Grafana
- artifacts

Также записывать:

- git_sha
- build/version
- profile
- target_rate
- population
- parallelism
- host/minikube resources
- sampling configuration

Без этих данных сравнение двух результатов бессмысленно.

## 6. Workflow DAG — `[~]` (основная ветка + identity-ветка реализованы и живо проверены; playwright-smoke/browser-canary/recovery-check/summarize-run — нет)

Основной workflow реализовать примерно так:

```
                preflight
                    │
                    ▼
                provision
                    │
                    ▼
              prepare-auth
                    │
                    ▼
          prepare-conversations
                    │
                    ▼
             playwright-smoke
                    │
             ┌──────┴──────┐
             │             │
             ▼             ▼
      browser-canary     k6 TestRun
             │             │
             └──────┬──────┘
                    ▼
              reconciliation
                    │
                    ▼
             recovery-check
                    │
                    ▼
              summarize-run

finally / exit handler
                    │
                    ▼
                 cleanup
```

Cleanup должен быть Argo `onExit`/exit-handler логикой и выполняться также
после failed workflow.

## 7. Preflight — `[~]` (реализована доступность зависимостей и k6-оператора; baseline ресурсов — нет)

До нагрузки проверить:

- Messenger API reachable
- Keycloak reachable
- Centrifugo reachable
- PostgreSQL healthy
- Kafka healthy
- VictoriaMetrics reachable
- k6 Operator ready
- Argo execution environment ready
- Playwright browser available

Также записать baseline ресурсов:

- node CPU
- node RAM
- pod resources
- pod restarts
- DB connections
- Kafka lag
- outbox backlog

Если стенд уже находится в деградированном состоянии — benchmark не
запускать.

## 8. Provisioning — `[x]`

Для `messages`, `connections`, `stress`, `recovery`, `mixed`:

```
Keycloak Admin API
→ synthetic users
→ emailVerified=true
```

Это test fixture, а не обход проверяемого поведения.

После provisioning пользователь должен получить настоящую application
session через существующий production-compatible auth flow.

Не генерировать самодельные JWT.

Переиспользовать существующую логику/подходы из:

- `scripts/web-e2e-fixture.sh`
- `tests/integration/login_check.py`

Synthetic identities должны иметь ownership:

```
local-capacity-<run_id>-000001@...
```

## 9. Identity profile — `[~]` (поток подтверждения адреса реализован и живо проверен, 3/3 без ошибок; Keycloak CPU/RAM, DB pressure, Mailpit ingestion — нет, это метрики раздела 22)

Отдельный профиль:

```
profile=identity
```

не должен заранее выставлять `emailVerified=true`.

Он проверяет настоящий поток:

```
create user
→ login
→ /me email_verified=false
→ verification email
→ Mailpit API
→ extract Keycloak action-token
→ verify
→ new auth state
→ /me email_verified=true
→ start_conversation capability
```

Переиспользовать семантику уже существующего:

- `tests/integration/verification_check.py`

Identity throughput измерять отдельно:

- registrations/s
- verification/s
- login latency
- verification latency
- errors
- Keycloak CPU/RAM
- Keycloak DB pressure
- Mailpit ingestion

Не смешивать это с message throughput.

## 10. k6 profiles — `[~]` (messages и identity реализованы и живо проверены; connections/stress/recovery/mixed — нет)

Один framework, но отдельные сценарии:

| Profile | Назначение |
|---|---|
| identity | registration/login/email verification |
| connections | concurrent authenticated WS |
| messages | чистый message throughput |
| stress | выход за sustainable capacity |
| recovery | разбор backlog после перегруза |
| mixed | production-like смесь |

Общую auth/correlation/retry/reconciliation логику вынести в
`tests/load/messenger/lib/`.

Не копировать её между шестью JS-файлами.

## 11. Open-model message load — `[~]` (constant-arrival-rate для messages-профиля реализован и живо проверен — target_rate достигался точно; отдельный разбор dropped_iterations > 0, SUT vs generator — нет)

Для throughput использовать arrival-rate executor, а не closed model.

Нужно задавать:

```
10 msg/s
25
50
100
200
400
800
...
```

а не:

```
100 VU → send → wait → send
```

Причина: closed model уменьшает фактическую интенсивность, когда SUT
начинает тормозить, и скрывает onset saturation.

Отдельно фиксировать:

- target iterations/s
- actual iterations/s
- dropped_iterations

При `dropped_iterations > 0` необходимо определить, достигнут ли предел SUT
или самого load generator.

## 12. Distributed k6 через Operator — `[~]` (TestRun CRD + k6 Operator + parallelism-параметр реализованы и живо проверены; requests/limits на k6 runner не заданы, мониторинг рядом с SUT — нет, раздел 20/22)

Argo не должен самостоятельно создавать N копий k6 Job.

Он создаёт:

```
k6.io TestRun
```

а распределением занимается k6 Operator.

Параметр:

```
parallelism
```

должен передаваться из Workflow.

Начинать можно с:

```
parallelism=1
```

Затем:

```
2
4
```

если один runner становится bottleneck.

Ресурсы k6 runners должны быть явно заданы:

- requests
- limits

и мониториться наряду с SUT.

## 13. Message correlation — обязательно — `[~]` (агрегатная корреляция count-vs-count реализована и живо проверена, включая доказанный fatal-гейт при потере; per-message client_message_id/message_id/seq/duplicate — нет, нужен артефакт-репозиторий, раздел 24)

HTTP `201` не считается доказательством доставки.

Для каждого message хранить:

- client_message_id
- message_id
- conversation_id
- seq
- sender_id
- receiver_id
- send_started_at
- http_confirmed_at
- ws_received_at

Проверяемый путь:

```
A
→ POST message
→ message_id=M
→ seq=S

Kafka/outbox/realtime

B
← WebSocket
← message_id=M
← seq=S
```

Результат каждой ступени:

- offered
- accepted
- persisted
- ws_received
- missing
- duplicates
- unknown
- timeouts
- retries

## 14. REST reconciliation — `[~]` (аггрегатная сверка accepted-vs-persisted реализована и живо проверена — и штатный проход (accepted=persisted=101), и намеренный fatal-сценарий (persisted < accepted валит Workflow); message_id/seq/duplicate — нет, см. раздел 13)

После основной нагрузки отдельный Argo step должен сверить accepted
messages с REST history.

Не доверять только k6 HTTP metrics.

Проверять:

- accepted message существует
- message_id совпадает
- seq совпадает
- client_message_id не породил duplicate
- нет неожиданной потери durable message

Например:

```
accepted = 100000
REST     = 100000
WS       = 99700
```

это realtime degradation.

Но:

```
accepted = 100000
REST     = 99999
```

это correctness failure и весь Workflow должен завершиться `Failed`.

## 15. Playwright smoke перед нагрузкой — `[x]` (реализовано и живо проверено: полный прогон `local-capacity-messages-wmw9p` прошёл playwright-smoke → k6-load → reconciliation → cleanup целиком; при сбое smoke k6-load не стартует, `when`-зависимость в DAG)

До benchmark два browser context должны пройти production UI:

```
A login
B login

A
→ New conversation
→ выбирает B
→ создаёт conversation
→ вводит сообщение через MessageComposer

B
→ получает сообщение без reload
→ отвечает

A
→ получает ответ без reload
```

Если smoke не проходит — k6 benchmark не стартует.

Нет смысла измерять performance системы, функционально уже находящейся в
broken state.

## 16. Playwright browser canary во время нагрузки — `[ ]`

При `messages`, `stress`, `recovery`, `mixed` должен работать небольшой
browser canary.

Не тысячи Chromium.

Достаточно:

```
1–5 пар
```

Каждая пара периодически:

```
A sends unique canary
→ B sees exact text in DOM
→ B replies
→ A sees exact reply
```

Измерять:

- browser_send_to_visible_seconds
- canary_failures_total
- browser_reconnects_total

Canary должен работать параллельно k6 workload.

Argo Workflow должен дождаться результатов обоих.

## 17. Receipts в mixed profile — `[ ]`

`mixed` обязан создавать не только `POST messages`.

Часть клиентов должна генерировать:

- delivered_seq
- read_seq

потому что receipt path нагружает:

- DB
- unread projection
- realtime publication

и имеет другой resource profile.

## 18. Stress test — `[~]` (эскалация нагрузки — пять ступеней 25/50/100/150/200% от target_rate, каждая свой k6-scenario с автоматическим тегом `scenario` — реализована; k6 сам решает, куда стартовать следующую ступень через `startTime`. Автоматическая классификация R_healthy/R_knee/R_collapse не реализована — раздел прямо требует не сворачивать три режима в одно число, здесь только данные для того, чтобы это увидеть на графике)

Нужно экспериментально найти:

```
R_healthy
R_knee
R_collapse
```

**R_healthy** — максимальная устойчивая интенсивность, при которой:

- нет missing
- нет duplicates
- queues bounded
- latency stable
- error rate acceptable
- browser canary healthy

**R_knee** — после этой точки небольшой рост offered load вызывает
непропорциональный рост:

- latency
- outbox age
- Kafka lag
- DB waits
- timeouts

**R_collapse** — дополнительная нагрузка уже почти не увеличивает useful
throughput, но резко увеличивает:

- queues
- timeouts
- 5xx
- reconnects
- resource pressure

Не выдавать единственное число вроде `max = 843 msg/s` без этих трёх
состояний.

## 19. Recovery profile — `[ ]`

После накопления controlled backlog:

```
high load
→ lower load
```

Проверить:

- outbox → 0
- Kafka lag → 0
- latency → baseline
- errors → baseline
- Playwright → healthy
- no manual pod restart
- missing = 0

Записывать:

- recovery_seconds
- backlog_drain_rate

И проверять:

```
drain_rate > incoming_rate
```

пока backlog существует.

## 20. VictoriaMetrics как performance history — `[~]` (k6 теперь пишет свои метрики в VictoriaMetrics через `-o experimental-prometheus-rw`, размеченные `run_id`/`git_sha`/`profile` — живо проверено; метрики самого приложения, например `messenger_message_send_operations_total`, этой разметки не несут и не могут быть сгруппированы по run_id тем же способом — только k6-side метрики)

k6 metrics отправлять в существующий metrics backend таким образом, чтобы
результаты можно было группировать по:

- run_id
- git_sha
- profile

Не добавлять высококардинальные:

- message_id
- user_id
- conversation_id

в metric labels.

Они принадлежат logs/traces/test artifacts.

## 21. Summary metrics — `[~]` (8 из 12: load_run_accepted/persisted/missing_messages, load_run_http_p95/p99_seconds, load_run_delivery_p95_seconds, load_run_max_outbox_age_seconds, load_run_max_kafka_lag — reconciliation пушит их в VictoriaMetrics с run_id/git_sha/profile через `/api/v1/import/prometheus`, живо проверен сам механизм импорта. received/duplicate_messages, recovery_seconds, browser_canary_failures — нет, нужны разделы 13 (WS-телеметрия), 19, 16 соответственно)

Помимо raw k6 series сохранить компактный набор run-level результатов:

- load_run_http_p95_seconds
- load_run_http_p99_seconds
- load_run_delivery_p95_seconds
- load_run_accepted_messages
- load_run_persisted_messages
- load_run_received_messages
- load_run_missing_messages
- load_run_duplicate_messages
- load_run_max_outbox_age_seconds
- load_run_max_kafka_lag
- load_run_recovery_seconds
- load_run_browser_canary_failures

С labels:

- run_id
- git_sha
- profile

## 22. Grafana dashboards — `[~]` (03·Load Run и 04·Load Compare — оба реализованы и живо проверены на реальных данных; k6/Playwright canary-панели и сравнение по run_id — нет, см. текстовые панели самих дашбордов)

Создать минимум два dashboard.

### Messenger / Load Run

Показывает один run:

- offered vs accepted vs delivered
- HTTP p50/p95/p99
- WS delivery latency
- outbox pending/age
- Kafka lag
- DB latency
- CPU/RAM
- restarts
- k6 dropped iterations
- Playwright canary

### Messenger / Load Compare

Выбор:

- baseline run
- current run

и сравнение:

| Metric | Baseline | Current | Δ |
|---|---|---|---|
| HTTP p95 | | | |
| HTTP p99 | | | |
| WS delivery p95 | | | |
| R_healthy | | | |
| max outbox age | | | |
| recovery | | | |
| missing | | | |

## 23. Traces и logs во время benchmark — `[ ]`

Capacity run не должен постоянно выполняться с текущим debugging 100% trace
sampling.

Сделать два режима:

```
observability-debug
capacity
```

**Observability debug** — 100% trace, moderate load. Используется, чтобы
доказать correlation и completeness spans.

**Capacity** — используется production-like sampling. Иначе benchmark
измеряет стоимость максимальной telemetry instrumentation вместе с
Messenger.

Отдельный короткий тест:

```
normal sampling vs 100%
```

может измерить tracing overhead.

## 24. Argo artifacts — `[ ]`

Каждый workflow должен сохранять summary artifact, например:

```
summary.json
```

Пример:

```json
{
  "run_id": "local-capacity-20260928-001",
  "git_sha": "...",
  "profile": "messages",
  "users": 5000,
  "online_users": 2000,
  "target_rate": 400,
  "accepted": 120000,
  "persisted": 120000,
  "ws_received": 120000,
  "missing": 0,
  "duplicates": 0,
  "http_p95_ms": 92,
  "http_p99_ms": 171,
  "delivery_p95_ms": 138,
  "max_kafka_lag": 31,
  "max_outbox_age_ms": 211,
  "browser_canary_failures": 0
}
```

Не полагаться только на Argo stdout.

## 25. Workflow status semantics — `[~]` (functional preflight, k6 execution completion и корректностный гейт accepted-but-not-durable реализованы и живо проверены — persisted < accepted валит Workflow; duplicates==0, browser canary, recovery, saturation — нет)

Workflow считается `Succeeded` только если:

- functional preflight passed
- k6 execution completed
- durability invariant passed
- missing == 0
- duplicates == 0
- REST reconciliation passed
- browser canary passed
- recovery passed where applicable
- load generator was not saturated

Latency/performance thresholds могут быть отдельными configurable gates.

Но correctness gates:

- missing > 0
- duplicate logical message
- accepted-but-not-durable

всегда fatal.

## 26. Cleanup — `[~]` (Keycloak и Postgres — users/conversations/messages/sessions/devices/realtime_connections и т.д. — удаляются по run_id и живо проверены прямым SELECT (0 строк после прогона); TestRun удаляется trap'ом на EXIT в k6-load. Mailpit test messages (письма identity-load) и Playwright resources — не покрыты)

Cleanup выполняется независимо от результата Workflow.

Удалять только ресурсы текущего `run_id`.

Cleanup должен охватывать:

- Keycloak synthetic users
- sessions
- devices
- realtime connections
- test conversations/messages
- Mailpit test messages
- k6 TestRun
- temporary Jobs/ConfigMaps
- Playwright resources

Не делать:

```sql
DELETE ... WHERE email LIKE 'load-%'
```

без run-specific ownership.

## 27. Argo UI как execution history — `[x]` (свойство самого Argo Workflows, ничего отдельно не строилось; список прогонов и статус каждого шага уже проверялись этой сессией через `kubectl get wf`/`kubectl get wf -o json .status.nodes` — тот же API, что показывает UI)

После реализации пользователь должен иметь возможность открыть Argo
Workflows UI и увидеть:

```
messenger-local-capacity-xxx   Succeeded
messenger-local-capacity-yyy   Failed
messenger-local-capacity-zzz   Succeeded
```

Внутри конкретного Workflow:

```
preflight              ✓
provision              ✓
playwright-smoke       ✓
k6-load                ✓
reconciliation         ✓
recovery               ✓
summary                ✓
cleanup                ✓
```

Argo — источник истории execution/lifecycle.

Grafana — источник истории performance measurements.

Не пытаться превращать Argo UI в performance dashboard.

## 28. Сравнение прогонов — `[ ]`

Ключевой use case:

```
commit A
→ LOCAL-CAPACITY
→ baseline

commit B
→ LOCAL-CAPACITY
→ compare
```

В Grafana должно быть возможно увидеть:

```
p95 +18%
p99 +31%
R_healthy -9%
DB commit latency +4%
Kafka lag +240%
browser latency +12%
```

И затем перейти:

```
Grafana anomaly
→ corresponding time window
→ Tempo trace
→ VictoriaLogs
```

## 29. CI/GitOps integration — `[x]` (`scripts/load-testing-run.sh` + `make load-test` — submit WorkflowTemplate(parameters) одной командой, живо проверено; сам триггер из CI по расписанию/on-merge — раздел явно оставляет его на будущее, "иметь возможность", а не "уже подключено")

Не запускать полный stress suite на каждый PR.

Предлагаемая модель:

```
PR
→ unit/integration
→ Playwright functional

merge main
→ lightweight performance baseline

manual / scheduled
→ LOCAL-CAPACITY full

release candidate
→ stress + recovery + mixed
```

Argo workflow должен запускаться вручную и иметь возможность будущего
trigger из CI одной командой/API call.

CI не должен содержать саму orchestration logic.

Правильная модель:

```
CI:
submit WorkflowTemplate(parameters)
```

а не:

```
CI:
200 строк kubectl/bash
```

## 30. Definition of Done

`LOCAL-CAPACITY-001` считается завершённым, когда через Argo Workflows можно
одним запуском получить воспроизводимый benchmark, в котором k6 Operator
создаёт нагрузку, Playwright доказывает реальный пользовательский путь, REST
reconciliation подтверждает durability, VictoriaMetrics/Grafana сохраняют и
сравнивают результаты, Tempo/VictoriaLogs позволяют выполнить RCA, а cleanup
гарантированно удаляет synthetic state.

На выходе должны быть экспериментально получены:

```
max tested users
max healthy WS connections

R_healthy
R_knee
R_collapse

first saturated component
failure mode under overload
recovery time

missing messages
duplicates

browser behaviour under load
generator saturation status
```

с обязательной привязкой к:

- git SHA
- run_id
- machine resources
- Minikube configuration
- sampling configuration

## Что специально НЕ делать в этой таске

Не оптимизировать найденный bottleneck внутри `LOCAL-CAPACITY-001`.

Например, если окажется:

```
Postgres pool saturates first
```

результат этой задачи:

```
доказано нагрузкой и telemetry, что первым ограничением
является DB pool при X msg/s.
```

А изменение pool size/architecture оформляется отдельной задачей и затем
проверяется:

```
baseline run
vs
optimized run
```

Именно тогда вся эта система начинает давать настоящую инженерную ценность:
изменение инфраструктуры можно доказать цифрами до/после, а не ощущением,
что «стало быстрее».
