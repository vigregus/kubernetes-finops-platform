# 04-messenger

GitOps-срез мессенджера в локальном окружении: хранилища, идентичность,
realtime, почта и настоящий API. Веб, outbox-relay и потребители уже описаны
тем же чартом, но пока выключены до соответствующих гейтов G2/G3.

| Приложение | Что создаёт | Оператор |
| --- | --- | --- |
| `messenger-postgres` | `Cluster messenger-db` + `Database` + `Pooler` PgBouncer | CNPG |
| `messenger-redis` | `Redis messenger-redis`, БД 0/1/2 под три роли | ot-container-kit |
| `messenger-kafka-topics` | четыре `KafkaTopic` и отдельные KafkaUser с ACL | Strimzi |
| `messenger-secrets` | учётные данные MinIO и Centrifugo | джоб-хук |
| `messenger-minio` | `Tenant messenger-objects`, два бакета | MinIO |
| `messenger-keycloak` | `Cluster keycloak-db` + `Keycloak messenger-idp` | CNPG, Keycloak |
| `messenger-centrifugo` | Centrifugo на Redis | чарт (оператора нет) |
| `messenger-mailpit` | сток писем для подтверждения адреса | чарт |
| `messenger-services` | API, веб и потребители — один шаблон | чарт `charts/messenger` |
| `messenger-routes` | `HTTPRoute` на `rt`, `s3`, `mail` | — |

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

**Внешние имена.** `idp` — вход в систему, `rt` — realtime, `s3` — вложения,
`mail` — сток писем. Все на `edge-gateway`, `app` зарезервировано под само
приложение. У Centrifugo наружу выставлен только клиентский порт 8000: на 9000
живут HTTP API, админка и метрики.

**Неизменяемые образы.** Чарт отказывается собраться с подвижным тегом:
`:v1.4.0` сегодня и через неделю может быть разными байтами, и тогда «откатились
на предыдущую версию» ничего не гарантирует. Исключение снимается одним флагом
`image.allowMutableTag`. Локальная сборка использует тег, выведенный из
содержимого образа (`local-…`), поэтому другое содержимое получает другое имя;
stage и prod должны ссылаться на настоящий digest реестра.

**Текущая граница.** `api` — настоящий сервис: G1 закрыт, а G2 уже содержит
домен сообщения и атомарную запись message + fact/content outbox. Сам HTTP
маршрут отправки, relay и Kafka-потребители ещё не готовы, поэтому их нагрузки
в values остаются выключенными. Kafka принимает только SCRAM-подключения;
relay и каждый будущий потребитель имеют отдельную учётную запись и минимальные
ACL. Namespace `messenger` намеренно не включён в ambient: идентичность в mesh
относится к отдельному гейту безопасности.
