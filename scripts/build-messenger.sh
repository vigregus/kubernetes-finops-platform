#!/usr/bin/env bash
# Сборка образа мессенджера с закреплением обоих digest.
#
# Два разных закрепления, и оба обязательны:
#
#   базовый образ  →  base-image.lock   чтобы пересборка из того же коммита
#                                        давала те же байты
#   свой образ     →  values.yaml       чтобы выкатка ссылалась на digest,
#                                        а не на подвижный тег (DEP-001)
#
# Собирается прямо в демон minikube, а не на хосте с последующей загрузкой:
# `minikube image load` считает образ с тем же тегом уже имеющимся и молча
# пропускает его даже с --overwrite, оставляя работать старый код без единой
# ошибки в цепочке.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/apps/messenger"
LOCK="$APP/base-image.lock"
BASE_IMAGE="${BASE_IMAGE:-python:3.12-slim}"
IMAGE="${IMAGE:-ghcr.io/vigregus/messenger/api}"
TAG="${TAG:-local}"

eval "$(minikube docker-env)"

# --- закрепление базового образа ------------------------------------------

if [ -f "$LOCK" ]; then
    BASE_DIGEST="$(cat "$LOCK")"
    echo "· базовый образ закреплён: ${BASE_DIGEST:0:23}…"
else
    echo "· базовый образ не закреплён, разрешаю $BASE_IMAGE"
    docker pull --quiet "$BASE_IMAGE" >/dev/null
    BASE_DIGEST="$(docker inspect --format '{{index .RepoDigests 0}}' "$BASE_IMAGE" \
        | sed 's/.*@//')"
    # Пустое значение здесь означало бы сборку от подвижного тега — то есть
    # ровно то, чего файл и должен не допустить.
    case "$BASE_DIGEST" in
        sha256:*) ;;
        *) echo "не удалось разрешить digest базового образа" >&2; exit 1 ;;
    esac
    echo "$BASE_DIGEST" > "$LOCK"
    echo "· записан в $(basename "$LOCK"): ${BASE_DIGEST:0:23}…"
fi

# --- сборка ----------------------------------------------------------------

echo "· сборка $IMAGE:$TAG"
docker build \
    --build-arg "BASE_DIGEST=$BASE_DIGEST" \
    -t "$IMAGE:$TAG" \
    -f "$APP/Dockerfile" \
    "$APP"

# Локальный образ не попадает в реестр, поэтому RepoDigests у него пуст.
# Идентификатор содержимого даёт Id — он так же неизменен и так же меняется
# при любом изменении слоёв.
DIGEST="$(docker inspect --format '{{.Id}}' "$IMAGE:$TAG")"
echo "· собран: ${DIGEST:0:23}…"

# --- закрепление в values --------------------------------------------------

VALUES="$ROOT/gitops/04-messenger/messenger-services/values.yaml"
python3 - "$VALUES" "$DIGEST" <<'PY'
import io, re, sys
path, digest = sys.argv[1], sys.argv[2]
s = io.open(path, encoding="utf-8").read()

# Заменяется только digest сервиса api; остальные значения не трогаются,
# чтобы скрипт нельзя было спутать с редактором конфигурации.
if "digest:" in s:
    s = re.sub(r'(\n      digest: ).*', r'\g<1>' + digest, s, count=1)
else:
    s = s.replace(
        "    image:\n      repository: registry.k8s.io/e2e-test-images/agnhost\n      tag: \"2.47\"",
        "    image:\n      repository: ghcr.io/vigregus/messenger/api\n      digest: " + digest,
    )
io.open(path, "w", encoding="utf-8").write(s)
print(f"· values.yaml: digest сервиса api обновлён")
PY

echo
echo "Дальше: закоммитить values.yaml и base-image.lock, дождаться синхронизации."
echo "Тег остаётся подвижным только пока image.allowMutableTag=true —"
echo "после первой сборки его следует выключить."
