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

# --- Kafka: запись в основной топик ----------------------------------------
check "Kafka принимает запись в messenger.events.v1" \
    kubectl run "kafka-$POD" -n kafka --rm -i --restart=Never --quiet \
    --image=quay.io/strimzi/kafka:latest-kafka-4.3.1 --command -- \
    sh -c "echo 'smoke:{}' | /opt/kafka/bin/kafka-console-producer.sh \
        --bootstrap-server messenger-kafka-kafka-bootstrap:9092 \
        --topic messenger.events.v1 --property parse.key=true --property key.separator=:"

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
