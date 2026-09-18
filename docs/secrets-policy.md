# Политика и стратегия управления секретами

> kubernetes-finops-platform · управление секретами и конфигурацией

В репозитории действует строгое правило: **настоящих учётных данных, токенов, паролей и закрытых ключей в Git нет и не может быть ни в каком виде**.

В GitOps-манифестах (`values.yaml`, Helm-шаблоны) фиксируются **только имена** Secret-объектов Kubernetes, но не их значения. Сами `Kubernetes Secrets` рассматриваются исключительно как конечные точки доставки (deployment targets), а не как источник истины.

---

## 1. Секреты, необходимые сервисам приложения

Сервисы мессенджера ([API](file:///Users/grigoriypershin/Documents/MY_PROJECTS/FinOPS/kubernetes-finops-platform/apps/messenger/messenger/services/runtime.py), [Outbox Relay](file:///Users/grigoriypershin/Documents/MY_PROJECTS/FinOPS/kubernetes-finops-platform/apps/messenger/messenger/workers/outbox_relay.py), фоновые консьюмеры) потребляют секреты через переменные окружения, пробрасываемые через `valueFrom.secretKeyRef` в Helm-шаблоне [workloads.yaml](file:///Users/grigoriypershin/Documents/MY_PROJECTS/FinOPS/kubernetes-finops-platform/charts/messenger/templates/workloads.yaml):

| Секрет / Назначение | Переменная в поде | Имя K8s Secret | Ключ в Secret | Потребители | Описание |
|---|---|---|---|---|---|
| **PostgreSQL** | `DATABASE_PASSWORD` | `messenger-db-app` | `password` | API, Outbox Relay, тесты | Пароль роли приложения `messenger` для подключения через PgBouncer |
| **Kafka SCRAM** | `KAFKA_PASSWORD` | `messenger-outbox`<br>*(у каждого сервиса свой)* | `password` | Outbox Relay, консьюмеры | SCRAM-SHA-512 пароль с минимальными правами на топики (least privilege) |
| **Centrifugo HTTP API** | `CENTRIFUGO_HTTP_API_KEY` | `messenger-centrifugo` | `CENTRIFUGO_HTTP_API_KEY` | API | Ключ для вызова HTTP API Centrifugo (отзыв сессий, отключение WebSocket) |
| **Centrifugo HMAC** | `CENTRIFUGO_CLIENT_TOKEN_HMAC_SECRET_KEY` | `messenger-centrifugo` | `CENTRIFUGO_CLIENT_TOKEN_HMAC_SECRET_KEY` | API | Секретный ключ для генерации HMAC-токенов подключения WebSocket-клиентов |
| **Keycloak Client** | `OIDC_CLIENT_SECRET` | `messenger-api-client` | `client-secret` | API | Секрет OIDC-клиента для вызова Keycloak Admin REST API (повтор верификации почты) |

> [!NOTE]
> **Объектное хранилище (MinIO / AWS S3):**
> * **В Minikube:** MinIO требует статические учетные данные (`messenger-minio-env`).
> * **В Stage и Prod:** статических секретов **не существует**. Поды сервисов получают доступ к AWS S3 через **EKS Pod Identity / IRSA** по IAM-роли пода.

---

## 2. Локальное окружение (Minikube / Local v1)

### А. Автоматическая генерация в кластере
При выполнении `make bootstrap` и `make local-up` секреты генерируются внутри кластера автоматически:
1. **Операторы со встроенной генерацией:**
   * CloudNativePG создаёт кластер базы данных и автоматически выпускает K8s Secret `messenger-db-app`.
   * Strimzi создаёт пользователей Kafka и генерирует Secret со SCRAM-учётками (`messenger-outbox`, `messenger-realtime` и т.д.).
   * Оператор Keycloak генерирует пароль администратора и секрет OIDC-клиента `messenger-api-client`.
2. **Идемпотентный bootstrap-джоб:**
   * Для компонентов без операторов генерации (MinIO, Centrifugo) в GitOps настроен хук ArgoCD — Job [`messenger-secrets-bootstrap`](file:///Users/grigoriypershin/Documents/MY_PROJECTS/FinOPS/kubernetes-finops-platform/gitops/04-messenger/messenger-secrets/manifests/bootstrap.yaml).
   * Он проверяет наличие секретов `messenger-minio-env` и `messenger-centrifugo`. Если их нет, генерирует случайные значения через `/dev/urandom` и создаёт Secret. При повторных запусках существующие секреты не перезаписываются.

### Б. Локальный запуск на хосте (разработка и отладка)
Для запуска и отладки сервисов на машине разработчика вне K8s подготовлен файл [`apps/messenger/.env.example`](file:///Users/grigoriypershin/Documents/MY_PROJECTS/FinOPS/kubernetes-finops-platform/apps/messenger/.env.example):

1. Скопируйте шаблон в `.env` (файл находится в `.gitignore`):
   ```bash
   cp apps/messenger/.env.example apps/messenger/.env
   ```
2. Пробросьте порты зависимостей из Minikube:
   ```bash
   kubectl -n messenger port-forward svc/messenger-db-pool 5432:5432 &
   kubectl -n messenger port-forward svc/messenger-redis 6379:6379 &
   kubectl -n kafka port-forward svc/messenger-kafka-kafka-bootstrap 9092:9092 &
   kubectl -n messenger port-forward svc/messenger-centrifugo 9000:9000 &
   kubectl -n keycloak port-forward svc/messenger-idp-service 8080:8080 &
   ```
3. Извлеките сгенерированные кластером пароли и вставьте в `.env`:
   ```bash
   # Пароль БД:
   kubectl -n messenger get secret messenger-db-app -o jsonpath='{.data.password}' | base64 -d

   # Пароль Kafka Outbox:
   kubectl -n messenger get secret messenger-outbox -o jsonpath='{.data.password}' | base64 -d

   # Секрет Keycloak API Client:
   kubectl -n messenger get secret messenger-api-client -o jsonpath='{.data.client-secret}' | base64 -d

   # Ключи Centrifugo:
   kubectl -n messenger get secret messenger-centrifugo -o jsonpath='{.data.CENTRIFUGO_HTTP_API_KEY}' | base64 -d
   kubectl -n messenger get secret messenger-centrifugo -o jsonpath='{.data.CENTRIFUGO_CLIENT_TOKEN_HMAC_SECRET_KEY}' | base64 -d
   ```

### В. Интеграционные тесты (`make integration`)
Разработчику **не требуется** настраивать `.env` для прогона интеграционных тестов: скрипт [scripts/integration-messenger.sh](file:///Users/grigoriypershin/Documents/MY_PROJECTS/FinOPS/kubernetes-finops-platform/scripts/integration-messenger.sh) запускает временный под внутри Minikube, который монтирует тесты и напрямую инжектирует пароли из кластерных Secret'ов через `valueFrom.secretKeyRef`.

---

## 3. Облачные окружения (Stage и Prod в AWS)

В облачных средах источником истины для секретов является **AWS Secrets Manager**, зашифрованный ключом **AWS KMS**.

### Минимальный набор секретов в AWS Secrets Manager

Для каждого окружения (`stage/` и `prod/`) создаётся 4 секрета в формате JSON:

#### 1. `{env}/messenger/database`
Создаётся при развертывании Terraform-модуля RDS PostgreSQL Multi-AZ:
```json
{
  "username": "messenger",
  "password": "<GENERATED_STRONG_PASSWORD>",
  "host": "rds-cluster-endpoint.eu-central-1.rds.amazonaws.com",
  "port": "5432",
  "database": "messenger"
}
```

#### 2. `{env}/messenger/kafka-credentials`
Учётные записи SCRAM-SHA-512 для брокера Amazon MSK:
```json
{
  "outbox_password": "<STRONG_OUTBOX_SCRAM_PASSWORD>",
  "realtime_password": "<STRONG_REALTIME_SCRAM_PASSWORD>",
  "unread_password": "<STRONG_UNREAD_SCRAM_PASSWORD>",
  "notifications_password": "<STRONG_NOTIFICATIONS_SCRAM_PASSWORD>"
}
```

#### 3. `{env}/messenger/centrifugo`
Ключи и пароли для узлов Centrifugo:
```json
{
  "http_api_key": "<RANDOM_HEX_32_CHARS>",
  "hmac_secret_key": "<RANDOM_HEX_64_CHARS>",
  "admin_password": "<STRONG_ADMIN_PASSWORD>",
  "admin_secret": "<RANDOM_HEX_32_CHARS>"
}
```

#### 4. `{env}/messenger/keycloak`
Секреты сервисных клиентов Keycloak:
```json
{
  "client_secret": "<KEYCLOAK_GENERATED_SECRET>"
}
```

---

## 4. Проброс секретов через External Secrets Operator (ESO)

Синхронизация из AWS Secrets Manager в Kubernetes Secrets осуществляется оператором **External Secrets Operator**.

```
  AWS Secrets Manager (KMS)
             │
             ▼
  External Secrets Operator (аутентификация через Pod Identity / IRSA)
             │  [SecretStore]
             ▼
     ExternalSecret (декларация в GitOps)
             │
             ▼
     Kubernetes Secret (namespace: messenger)
             │
             ▼
     Workload Pods (valueFrom.secretKeyRef)
```

### Шаг 1. Настройка `SecretStore`

Манифест объявляет подключение к AWS Secrets Manager. Аутентификация происходит без статических ключей через ServiceAccount с привязанной IAM-ролью:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: SecretStore
metadata:
  name: aws-secretsmanager
  namespace: messenger
spec:
  provider:
    aws:
      service: SecretsManager
      region: eu-central-1
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets-sa
```

### Шаг 2. Манифесты `ExternalSecret`

Каждый манифест мапит ключи из JSON в Secrets Manager в целевой Kubernetes Secret, имя которого зафиксировано в [workloads.yaml](file:///Users/grigoriypershin/Documents/MY_PROJECTS/FinOPS/kubernetes-finops-platform/charts/messenger/templates/workloads.yaml).

#### База данных (`messenger-db-app`)
```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: messenger-db-app-sync
  namespace: messenger
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secretsmanager
    kind: SecretStore
  target:
    name: messenger-db-app
    creationPolicy: Owner
  data:
    - secretKey: username
      remoteRef:
        key: prod/messenger/database
        property: username
    - secretKey: password
      remoteRef:
        key: prod/messenger/database
        property: password
```

#### Centrifugo (`messenger-centrifugo`)
```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: messenger-centrifugo-sync
  namespace: messenger
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secretsmanager
    kind: SecretStore
  target:
    name: messenger-centrifugo
    creationPolicy: Owner
  data:
    - secretKey: CENTRIFUGO_HTTP_API_KEY
      remoteRef:
        key: prod/messenger/centrifugo
        property: http_api_key
    - secretKey: CENTRIFUGO_CLIENT_TOKEN_HMAC_SECRET_KEY
      remoteRef:
        key: prod/messenger/centrifugo
        property: hmac_secret_key
    - secretKey: CENTRIFUGO_ADMIN_PASSWORD
      remoteRef:
        key: prod/messenger/centrifugo
        property: admin_password
    - secretKey: CENTRIFUGO_ADMIN_SECRET
      remoteRef:
        key: prod/messenger/centrifugo
        property: admin_secret
```

#### Kafka Outbox (`messenger-outbox`)
```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: messenger-outbox-sync
  namespace: messenger
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secretsmanager
    kind: SecretStore
  target:
    name: messenger-outbox
    creationPolicy: Owner
  data:
    - secretKey: password
      remoteRef:
        key: prod/messenger/kafka-credentials
        property: outbox_password
```

#### OIDC Client Keycloak (`messenger-api-client`)
```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: messenger-api-client-sync
  namespace: messenger
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secretsmanager
    kind: SecretStore
  target:
    name: messenger-api-client
    creationPolicy: Owner
  data:
    - secretKey: client-secret
      remoteRef:
        key: prod/messenger/keycloak
        property: client_secret
```

---

## 5. Ротация и аудит секретов

Подробная архитектурная модель ротации и аудита описана в [docs/messenger/12-configuration.md](file:///Users/grigoriypershin/Documents/MY_PROJECTS/FinOPS/kubernetes-finops-platform/docs/messenger/12-configuration.md#L221):

1. **Ротация TLS-сертификатов:** автоматическая на лету через `cert-manager`.
2. **Ротация паролей БД:** требует временного окна с поддержкой двух действующих паролей (dual-password) в RDS/Postgres.
3. **Ротация ключей подписи токенов (JWKS Keycloak):** сервис проверки подписи токенов (`ROT-001`) изначально спроектирован с поддержкой нескольких активных ключей в кэше JWKS, предотвращая разлогин пользователей во время плановой смены ключей.
4. **Аудит изменений:** смена секрета регистрируется событием `packages/contracts/events/operational-event.v1.json` и направляется в поток аудита. В журнал попадают имена измененных ключей, но **никогда не их значения**.
