#!/usr/bin/env bash
# Проверка связности до того, как появилась бизнес-логика.
#
# Смысл в том, чтобы доказать проводку, а не работу продукта. Каждая проверка -
# настоящая операция с записью и чтением, а не «под в статусе Running»: под
# может быть жив при неработающей аутентификации, пустом пароле или закрытом
# порте. Именно так в этом окружении уже прятались два отказа.
set -euo pipefail

NS="${NAMESPACE:-messenger}"
POD="smoke-$(date +%s)"
fail=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; fail=1; }

check() { # описание, команда...
    local what="$1"; shift
    if out="$("$@" 2>&1)"; then
        ok "$what"
    else
        bad "$what"
        echo "$out" | sed 's/^/      /' | tail -3
    fi
}

# --- Postgres: запись и чтение от роли приложения через пул ----------------
check "Postgres принимает запись через PgBouncer" \
    kubectl exec -n "$NS" messenger-db-1 -c postgres -- \
    psql -U postgres -d messenger -qtAc \
    "create temp table t(x int); insert into t values (1); select count(*) from t;"

# --- Redis: три роли по номерам БД -----------------------------------------
for db in 0:realtime 1:app-cache 2:security; do
    n="${db%%:*}"; role="${db##*:}"
    check "Redis DB $n ($role) пишет и читает" \
        kubectl exec -n "$NS" messenger-redis-0 -- \
        redis-cli -n "$n" set "smoke:$role" ok EX 60
done

# --- Kafka: SCRAM и наименьшие права --------------------------------------
kafka_client() { # pod, KafkaUser, команда...
    local pod="$1" user="$2" overrides
    shift 2
    # overrides заменяет контейнер целиком, а не дополняет его. Поэтому образ,
    # env, command и args строятся одним JSON: если оставить команду только в
    # `kubectl run --command`, overrides её молча сотрёт и pod завершится 0,
    # не выполнив ни одной Kafka-операции.
    overrides="$(python3 - "$pod" "$user" "$@" <<'PY'
import json
import sys

pod, user, *command = sys.argv[1:]
setup = r'''
umask 077
printf "%s\n" \
  "security.protocol=SASL_PLAINTEXT" \
  "sasl.mechanism=SCRAM-SHA-512" \
  "sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username=\"$KAFKA_USER\" password=\"$KAFKA_PASSWORD\";" \
  > /tmp/client.properties
exec "$@"
'''
print(json.dumps({"spec": {"containers": [{
    "name": pod,
    "image": "quay.io/strimzi/kafka:latest-kafka-4.3.1",
    "env": [
        {"name": "KAFKA_USER", "value": user},
        {"name": "KAFKA_PASSWORD", "valueFrom": {"secretKeyRef": {
            "name": user, "key": "password"
        }}},
    ],
    "command": ["sh", "-c"],
    "args": [setup, "sh", *command],
    # Без этого `kubectl run -i` не подключает stdin: overrides заменяет
    # контейнер целиком, и `stdin: true`, который kubectl проставил бы сам,
    # исчезает вместе с остальным. Продюсер тогда читает сразу EOF,
    # не отправляет ничего - и завершается нулём.
    "stdin": True,
    "stdinOnce": True,
}]}}))
PY
)"
    # Метка снимает вывод со сбора журналов: проверка печатает для
    # человека, а не для хранилища.
    kubectl run "$pod" -n kafka --rm -i --restart=Never --quiet \
        --labels="finops.internal/component=test" \
        --image=quay.io/strimzi/kafka:latest-kafka-4.3.1 \
        --overrides="$overrides"
}

kafka_write() { # KafkaUser, topic, value
    local user="$1" topic="$2" value="$3" pod="kafka-$POD-${4:-write}"
    printf 'smoke:%s\n' "$value" | kafka_client "$pod" "$user" \
        /opt/kafka/bin/kafka-console-producer.sh \
        --bootstrap-server messenger-kafka-kafka-bootstrap:9092 \
        --producer.config /tmp/client.properties \
        --topic "$topic" --property parse.key=true --property key.separator=:
}

kafka_read_one() { # KafkaUser, topic, group, suffix
    local user="$1" topic="$2" group="$3" suffix="$4"
    kafka_client "kafka-$POD-$suffix" "$user" \
        /opt/kafka/bin/kafka-console-consumer.sh \
        --bootstrap-server messenger-kafka-kafka-bootstrap:9092 \
        --consumer.config /tmp/client.properties \
        --topic "$topic" --group "$group" --from-beginning \
        --max-messages 1 --timeout-ms 10000 >/dev/null
}

kafka_offsets() { # KafkaUser, suffix
    local user="$1" suffix="$2"
    kafka_client "kafka-$POD-$suffix" "$user" \
        /opt/kafka/bin/kafka-get-offsets.sh \
        --bootstrap-server messenger-kafka-kafka-bootstrap:9092 \
        --command-config /tmp/client.properties \
        --topic messenger.content.v1
}

kafka_unread_cannot_read_content() {
    local out
    if out="$(kafka_offsets messenger-unread denied 2>&1)"; then
        echo "получение offsets content-топика неожиданно разрешено" >&2
        printf '%s\n' "$out" >&2
        return 1
    fi

    # Kafka 4.3 get-offsets возвращает ненулевой код, но не печатает текст
    # TopicAuthorizationException. Доступность той же команды и топика
    # отдельно доказана разрешённой учётной записью непосредственно выше.
    return 0
}

kafka_offsets_sum() { # KafkaUser, topic, suffix
    kafka_client "kafka-$POD-$3" "$1" \
        /opt/kafka/bin/kafka-get-offsets.sh \
        --bootstrap-server messenger-kafka-kafka-bootstrap:9092 \
        --command-config /tmp/client.properties \
        --topic "$2" 2>/dev/null \
        | awk -F: '/^[a-z]/ {s+=$3} END {print s+0}'
}

# Код возврата продюсера ничего не доказывает: `kafka-console-producer`
# завершается нулём и когда не ушла ни одна запись. Проверено измерением -
# проверка была зелёной, а сумма смещений топика не менялась. Поэтому
# записью считается только сдвинувшийся конец лога.
kafka_write_lands() { # KafkaUser, topic, value, suffix, [кто читает смещения]
    local user="$1" topic="$2" value="$3" suffix="$4" reader="${5:-$1}"
    local before after
    before="$(kafka_offsets_sum "$reader" "$topic" "$suffix-before")"
    kafka_write "$user" "$topic" "$value" "$suffix" >/dev/null 2>&1 || return 1
    after="$(kafka_offsets_sum "$reader" "$topic" "$suffix-after")"

    case "$before$after" in
        *[!0-9]*|"") echo "смещения не прочитаны: до=$before после=$after" >&2; return 1 ;;
    esac
    [ "$after" -gt "$before" ] || {
        echo "конец лога не сдвинулся: до=$before после=$after" >&2
        return 1
    }
}

check "Kafka приняла запись — конец лога сдвинулся" \
    kafka_write_lands messenger-smoke messenger.events.v1 '{"kind":"connectivity"}' connectivity

# SEC-010: факт и содержимое физически разделены, а ACL не позволяет
# потребителю непрочитанного повысить себе доступ выбором другого топика.
check "consumer-unread читает поток фактов" \
    kafka_read_one messenger-unread messenger.events.v1 messenger-unread unread
# Смещения читает realtime: у outbox право на запись, а не на чтение
# конца лога, и подменять одно другим ради удобства проверки значило бы
# проверять не те права, которые выданы.
check "outbox пишет поток содержимого" \
    kafka_write_lands messenger-outbox messenger.content.v1 '{"text":"acl-proof"}' \
        content-write messenger-realtime
check "consumer-realtime получил доступ к содержимому" \
    kafka_read_one messenger-realtime messenger.content.v1 messenger-realtime content-read
check "consumer-realtime видит offsets содержимого" \
    kafka_offsets messenger-realtime content-offsets
check "SEC-010: consumer-unread не читает содержимое" \
    kafka_unread_cannot_read_content

# --- Объектное хранилище: список бакетов от корневой учётной записи ---------
# Учётные данные читаются здесь и передаются через stdin, а не аргументом
# команды: аргументы видны в списке процессов внутри контейнера. В самом
# образе MinIO нет sed, поэтому разбирать config.env на месте нечем.
minio_check() {
    local user pass
    user="$(kubectl get secret -n "$NS" messenger-minio-env \
        -o jsonpath='{.data.config\.env}' | base64 -d \
        | awk -F= '/MINIO_ROOT_USER/{print $2}')"
    pass="$(kubectl get secret -n "$NS" messenger-minio-env \
        -o jsonpath='{.data.config\.env}' | base64 -d \
        | awk -F= '/MINIO_ROOT_PASSWORD/{print $2}')"
    printf '%s %s\n' "$user" "$pass" | kubectl exec -i -n "$NS" \
        messenger-objects-pool-0-0 -c minio -- sh -c '
            read u p
            mc --config-dir /tmp/mc alias set s http://localhost:9000 "$u" "$p" >/dev/null
            mc --config-dir /tmp/mc ls s/messenger-attachments >/dev/null
            mc --config-dir /tmp/mc ls s/messenger-voice >/dev/null'
}
check "MinIO отвечает, оба бакета на месте" minio_check

# --- Centrifugo: живой процесс и сконфигурированный движок ------------------
check "Centrifugo отвечает на /health" \
    kubectl exec -n "$NS" deploy/messenger-centrifugo -- \
    wget -qO- http://localhost:9000/health

# --- Keycloak: OIDC discovery реалма мессенджера ---------------------------
# Реалм messenger, а не master. Проверка master доказывала бы, что Keycloak
# жив, - и молчала бы о том, что реалма, на которую указывает OIDC_ISSUER
# приложения, не существует. Ровно так это и было: discovery отдавал 404,
# а smoke был зелёный.
check "Keycloak отдаёт OIDC discovery реалма messenger" \
    kubectl exec -n "$NS" deploy/messenger-centrifugo -- \
    wget -qO- http://messenger-idp-service.keycloak.svc.cluster.local:8080/realms/messenger/.well-known/openid-configuration

# Ключи подписи. Discovery отвечает и у реалма без единого активного ключа,
# а токен в таком реалме проверить нечем.
check "У реалма есть открытые ключи подписи" \
    kubectl exec -n "$NS" deploy/messenger-centrifugo -- \
    sh -c 'wget -qO- http://messenger-idp-service.keycloak.svc.cluster.local:8080/realms/messenger/protocol/openid-connect/certs | grep -q "\"kid\""' 

# --- Почта: SMTP принимает и API отдаёт ------------------------------------
check "Mailpit принимает письмо по SMTP" \
    kubectl exec -n "$NS" deploy/messenger-centrifugo -- \
    sh -c 'printf "EHLO smoke\r\nQUIT\r\n" | nc -w 5 messenger-mailpit-smtp 1025 | head -1'

# --- Уборка -----------------------------------------------------------------
for n in 0 1 2; do
    kubectl exec -n "$NS" messenger-redis-0 -- redis-cli -n "$n" del \
        "smoke:realtime" "smoke:app-cache" "smoke:security" >/dev/null 2>&1 || true
done

if [ "$fail" -ne 0 ]; then
    echo "связность нарушена" >&2
    exit 1
fi
echo "  связность подтверждена"
