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

cleanup() {
    kubectl -n "$NS" delete pod "$POD" --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n "$NS" delete configmap "$CM" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT

IMAGE="$(python3 - "$VALUES" <<'PY'
import re, sys
s = open(sys.argv[1], encoding="utf-8").read()
block = re.search(r"  api:\n(?:.*\n)*?    resources:", s).group(0)
repo = re.search(r"repository:\s*(\S+)", block).group(1)
tag = re.search(r'tag:\s*"?([^"\n]+)"?', block).group(1)
print(f"{repo}:{tag}")
PY
)"
echo "  образ: $IMAGE"

args=()
for f in "$ROOT"/tests/integration/*.py; do
    args+=(--from-file="$f")
done

kubectl -n "$NS" create configmap "$CM" "${args[@]}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# Переменные те же, что у сервиса: проверка обязана ходить от роли
# приложения, а не от суперпользователя. Права у них разные, и отказ
# по правам должен находиться здесь, а не в проде.
kubectl -n "$NS" run "$POD" --restart=Never \
    --image="$IMAGE" \
    --overrides="$(cat <<JSON
{
  "spec": {
    "containers": [{
      "name": "integration",
      "image": "$IMAGE",
      "imagePullPolicy": "IfNotPresent",
      "command": ["python", "/checks/users_check.py"],
      "env": [
        {"name":"DATABASE_HOST","value":"messenger-db-pool"},
        {"name":"DATABASE_NAME","value":"messenger"},
        {"name":"SERVICE_NAME","value":"integration"},
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
