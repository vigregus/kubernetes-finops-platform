#!/usr/bin/env bash
# Интеграционные проверки: код против настоящей базы.
#
# Запускаются внутри кластера тем же образом, что и сервис. Причины те же,
# по которым внутри выполняются миграции: на драйвере Docker база не видна
# с ноутбука, а проверка, идущая мимо PgBouncer, доказывала бы работу
# другого пути, чем тот, которым пойдёт рабочий запрос.
#
# Образ берётся из values, а не собирается заново: проверяется ровно то
# содержимое, которое поедет в кластер.
set -euo pipefail

NS="${NAMESPACE:-messenger}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALUES="$ROOT/gitops/04-messenger/messenger-services/values.yaml"
POD="integration-$(date +%s)"
CM="integration-run"
ADMIN_SECRET="keycloak-admin"

cleanup() {
    kubectl -n "$NS" delete pod "$POD" --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n "$NS" delete configmap "$CM" --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n "$NS" delete secret "$ADMIN_SECRET" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT

DEFAULT_IMAGE="$(python3 - "$VALUES" <<'PY'
import re, sys

# Образ берётся из верхнеуровневого блока image: тег общий для всех
# нагрузок, а не свой у каждой. Раньше читался блок api, и после того
# как тег стал общим, разбор ломался на пустом месте.
s = open(sys.argv[1], encoding="utf-8").read()
block = re.search(r"^image:\n(?:  .*\n)+", s, re.M)
if not block:
    sys.exit("не найден верхнеуровневый блок image в values")
repo = re.search(r"^  repository:\s*(\S+)", block.group(0), re.M)
tag = re.search(r'^  tag:\s*"?([^"\n]+)"?', block.group(0), re.M)
if not repo or not tag:
    sys.exit("в блоке image нет repository или tag")
print(f"{repo.group(1)}:{tag.group(1)}")
PY
)"
IMAGE="${INTEGRATION_IMAGE:-$DEFAULT_IMAGE}"
API_ENDPOINT="${INTEGRATION_API_URL:-http://api.messenger.svc.cluster.local}"
CENTRIFUGO_ENDPOINT="${INTEGRATION_CENTRIFUGO_URL:-ws://messenger-centrifugo.messenger.svc.cluster.local:8000/connection/websocket}"
echo "  образ: $IMAGE"

args=(--from-file="run.sh=$ROOT/tests/integration/run.sh")
for f in "$ROOT"/tests/integration/*.py; do
    args+=(--from-file="$f")
done

# create, а не `create --dry-run=client | apply`: apply дописывает в объект
# аннотацию `kubectl.kubernetes.io/last-applied-configuration` со всем
# содержимым ConfigMap, и её предел (262144 байта) меньше этого каталога —
# на момент правки он занимал 297225 байт, а до неё запас составлял 577.
# Предел выбирала именно аннотация, а не данные: тот же объект без неё
# сервер принимает. Потолок теперь другой и выше — 1 МиБ на данные
# ConfigMap, — но и он конечен, и упирается в него первым этот же каталог.
#
# apply здесь был бы нужен, лишь если бы кто-то читал last-applied: его не
# читает никто, а `delete` перед `create` даёт ту же повторяемость после
# прогона, убитого без trap.
kubectl -n "$NS" delete configmap "$CM" --ignore-not-found >/dev/null
kubectl -n "$NS" create configmap "$CM" "${args[@]}" >/dev/null

# Учётные данные администратора Keycloak живут в своём namespace, а секреты
# через границу namespace не видны. Поэтому на время проверки заводится
# временная копия, которую снимает тот же trap, что и под.
#
# Значения переносятся как есть, в base64, и уходят через stdin: в аргументах
# команды они были бы видны в списке процессов, а расшифрованные - ещё и
# в журнале оболочки.
admin_user="$(kubectl -n keycloak get secret messenger-idp-initial-admin \
    -o jsonpath='{.data.username}')"
admin_pass="$(kubectl -n keycloak get secret messenger-idp-initial-admin \
    -o jsonpath='{.data.password}')"

kubectl apply -f - >/dev/null <<SECRET
apiVersion: v1
kind: Secret
metadata:
  name: $ADMIN_SECRET
  namespace: $NS
type: Opaque
data:
  username: $admin_user
  password: $admin_pass
SECRET

# Переменные те же, что у сервиса: проверка обязана ходить от роли
# приложения, а не от суперпользователя. Права у них разные, и отказ
# по правам должен находиться здесь, а не в проде.
# Метка `component: test` снимает вывод этого пода со сбора журналов:
# он для человека в терминале, а не для хранилища.
# Три переменные Centrifugo HTTP API нужны проверке квитанций: она поднимает
# ветку API прямо здесь (`ASGITransport`), а `centrifugo_client_from_env`
# без этой тройки возвращает `None` — и тишина в канале перестала бы
# отличаться от отсутствия публикатора.
kubectl -n "$NS" run "$POD" --restart=Never \
    --labels="finops.internal/component=test" \
    --image="$IMAGE" \
    --overrides="$(cat <<JSON
{
  "spec": {
    "containers": [{
      "name": "integration",
      "image": "$IMAGE",
      "imagePullPolicy": "IfNotPresent",
      "command": ["sh","/checks/run.sh"],
      "env": [
        {"name":"DATABASE_HOST","value":"messenger-db-pool"},
        {"name":"DATABASE_NAME","value":"messenger"},
        {"name":"SERVICE_NAME","value":"integration"},
        {"name":"KEYCLOAK_URL","value":"http://messenger-idp-service.keycloak.svc.cluster.local:8080"},
        {"name":"KEYCLOAK_REALM","value":"messenger"},
        {"name":"OIDC_ISSUER","value":"https://idp.finops.local/realms/messenger"},
        {"name":"OIDC_JWKS_URL","value":"http://messenger-idp-service.keycloak.svc.cluster.local:8080/realms/messenger/protocol/openid-connect/certs"},
        {"name":"OIDC_AUDIENCE","value":"messenger-api"},
        {"name":"API_URL","value":"$API_ENDPOINT"},
        {"name":"WEB_ORIGIN","value":"https://app.finops.local"},
        {"name":"KAFKA_BOOTSTRAP","value":"messenger-kafka-kafka-bootstrap.kafka.svc.cluster.local:9092"},
        {"name":"KAFKA_USERNAME","value":"messenger-outbox"},
        {"name":"KAFKA_PASSWORD","valueFrom":{"secretKeyRef":{"name":"messenger-outbox","key":"password"}}},
        {"name":"CENTRIFUGO_CLIENT_URL","value":"$CENTRIFUGO_ENDPOINT"},
        {"name":"CENTRIFUGO_API_URL","value":"http://messenger-centrifugo.messenger.svc.cluster.local:9000/api"},
        {"name":"CENTRIFUGO_HTTP_API_KEY","valueFrom":{"secretKeyRef":{"name":"messenger-centrifugo","key":"CENTRIFUGO_HTTP_API_KEY"}}},
        {"name":"CENTRIFUGO_CLIENT_TOKEN_HMAC_SECRET_KEY","valueFrom":{"secretKeyRef":{"name":"messenger-centrifugo","key":"CENTRIFUGO_CLIENT_TOKEN_HMAC_SECRET_KEY"}}},
        {"name":"INTEGRATION_ONLY","value":"${INTEGRATION_ONLY:-}"},
        {"name":"KEEP_ACCOUNTS","value":"${KEEP_ACCOUNTS:-}"},
        {"name":"BACKCHANNEL_TEST_URL","value":"${BACKCHANNEL_TEST_URL:-}"},
        {"name":"INTEGRATION_OIDC_CLIENT_ID","value":"${INTEGRATION_OIDC_CLIENT_ID:-}"},
        {"name":"KEYCLOAK_ADMIN","valueFrom":{"secretKeyRef":{"name":"keycloak-admin","key":"username"}}},
        {"name":"KEYCLOAK_ADMIN_PASSWORD","valueFrom":{"secretKeyRef":{"name":"keycloak-admin","key":"password"}}},
        {"name":"DATABASE_USER","valueFrom":{"secretKeyRef":{"name":"messenger-db-app","key":"username"}}},
        {"name":"DATABASE_PASSWORD","valueFrom":{"secretKeyRef":{"name":"messenger-db-app","key":"password"}}}
      ],
      "volumeMounts": [{"name":"checks","mountPath":"/checks"}]
    }],
    "volumes": [{"name":"checks","configMap":{"name":"$CM"}}]
  }
}
JSON
)" >/dev/null

for _ in $(seq 1 40); do
    phase="$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    case "$phase" in
        Succeeded|Failed) break ;;
    esac
    sleep 3
done

kubectl -n "$NS" logs "$POD" 2>&1 | sed 's/^/  /'

# Код возврата пода, а не kubectl logs: журнал печатается одинаково
# и при успехе, и при падении.
[ "${phase:-}" = "Succeeded" ] || {
    echo "интеграционные проверки не прошли" >&2
    exit 1
}
