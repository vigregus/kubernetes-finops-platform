#!/usr/bin/env bash
# Применяет миграции к локальной базе, запуская исполнитель внутри кластера.
#
# Внутри, а не с ноутбука: на драйвере Docker база не доступна с хоста, и
# любой psql снаружи потребовал бы port-forward. Кроме того, так путь ровно
# тот же, каким пойдёт джоб миграции при выкатке - через PgBouncer, от роли
# приложения, а не от суперпользователя.
set -euo pipefail

NS="${NAMESPACE:-messenger}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
POD="migrations-$(date +%s)"
CM="migrations-run"

cleanup() {
    kubectl -n "$NS" delete pod "$POD" --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n "$NS" delete configmap "$CM" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT

args=(--from-file="migrate.sh=$ROOT/db/migrate.sh")
for f in "$ROOT"/db/migrations/*.sql; do
    args+=(--from-file="$f")
done

kubectl -n "$NS" create configmap "$CM" "${args[@]}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl -n "$NS" run "$POD" --restart=Never \
    --image=ghcr.io/cloudnative-pg/postgresql:17.2 \
    --overrides="$(cat <<JSON
{
  "spec": {
    "containers": [{
      "name": "migrate",
      "image": "ghcr.io/cloudnative-pg/postgresql:17.2",
      "command": ["sh","-c","mkdir -p /w/migrations && cp /src/*.sql /w/migrations/ && cp /src/migrate.sh /w/ && chmod +x /w/migrate.sh && MIGRATIONS_DIR=/w/migrations /w/migrate.sh"],
      "env": [
        {"name":"PGPASSWORD","valueFrom":{"secretKeyRef":{"name":"messenger-db-app","key":"password"}}},
        {"name":"DATABASE_URL","value":"postgresql://messenger@messenger-db-pool:5432/messenger?sslmode=disable"}
      ],
      "volumeMounts": [{"name":"src","mountPath":"/src"},{"name":"w","mountPath":"/w"}]
    }],
    "volumes": [
      {"name":"src","configMap":{"name":"$CM"}},
      {"name":"w","emptyDir":{}}
    ]
  }
}
JSON
)" >/dev/null

for _ in $(seq 1 60); do
    phase="$(kubectl -n "$NS" get pod "$POD" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    case "$phase" in
        Succeeded|Failed) break ;;
    esac
    sleep 3
done

kubectl -n "$NS" logs "$POD" 2>&1 | sed 's/^/  /'

# Код возврата пода, а не kubectl logs: журнал печатается одинаково и при
# успехе, и при падении, и тихий провал миграции - худшее, что здесь может
# случиться.
[ "${phase:-}" = "Succeeded" ] || {
    echo "миграции не применились" >&2
    exit 1
}
