# 04-messenger

Хранилища мессенджера в локальном окружении. Только данные — сервисов здесь
пока нет.

| Приложение | Что создаёт | Оператор |
| --- | --- | --- |
| `messenger-postgres` | `Cluster messenger-db`, 1 экземпляр, база `messenger` | CNPG |
| `messenger-redis` | `Redis messenger-redis`, БД 0/1/2 под три роли | ot-container-kit |
| `messenger-kafka-topics` | три `KafkaTopic` на `messenger-kafka` | Strimzi |
| `messenger-secrets` | учётные данные MinIO и Centrifugo | джоб-хук |
| `messenger-minio` | `Tenant messenger-objects`, два бакета | MinIO |
| `messenger-keycloak` | `Cluster keycloak-db` + `Keycloak messenger-idp` | CNPG, Keycloak |
| `messenger-centrifugo` | Centrifugo на Redis | чарт (оператора нет) |

Кроме Centrifugo, у которого оператора не существует, ни один `StatefulSet`
и ни один `Deployment` не написан руками: всё поднимают операторы по CR.
В этом и смысл — локально и в проде меняется CR, а не способ выкатки.

**Учётные данные.** В git их нет (`docs/secrets-policy.md`). Postgres и Keycloak
генерируют свои секреты сами; для MinIO и Centrifugo это делает джоб
`messenger-secrets-bootstrap` — идемпотентный, без прав `update` и `delete`,
поэтому пересинк не меняет пароли под работающими подами.

**Порядок.** Волна 4: операторы стоят на 0, namespace `messenger` создаётся
`infra-bootstrap` на −1, кластер Kafka — на 1. Хранилища мессенджера
потребляют платформенный слой, поэтому идут после него целиком.

**Границы.** Кластер Kafka платформенный и живёт в `02-infra`; топики
принадлежат продукту и живут здесь. Та же граница, что между оператором CNPG
и `Cluster` мессенджера. Топики лежат в namespace `kafka`, а не `messenger`,
потому что оператор Strimzi смотрит только собственный namespace.

**Чего здесь нет.** PgBouncer, приёмник почты, сами сервисы API и веб,
внешние маршруты `HTTPRoute` на edge-gateway. Namespace `messenger`
намеренно не включён в ambient: идентичность в mesh относится к гейту
безопасности, а не к первому подъёму.
