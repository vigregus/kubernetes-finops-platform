#!/usr/bin/env bash
# Сборка образа мессенджера.
#
# Два открытия, сделанные при первой попытке собрать, и оба меняют способ:
#
# 1. Внутри демона minikube сборка не резолвит имена: на драйвере Docker
#    у узла /etc/resolv.conf указывает на внутренний резолвер Docker Desktop,
#    недостижимый из контейнеров. Та же причина, по которой в bootstrap
#    правится CoreDNS. Поэтому сборка идёт на хосте, где DNS работает,
#    а в minikube образ загружается готовым.
#
# 2. Локальный образ нельзя адресовать по digest. `repo@sha256:…` — это
#    ссылка на манифест в реестре; у образа, никуда не отправленного,
#    RepoDigests пуст, и kubelet попытается его скачать. Значит DEP-001
#    в форме «везде digest» локально недостижим без своего реестра.
#
#    Решение без реестра: тег выводится из содержимого образа. Он остаётся
#    тегом формально, но перестаёт быть подвижным по сути — другое
#    содержимое даёт другой тег. Ссылка снова означает конкретные байты.
#
#    В stage и проде образ проходит через реестр, там digest настоящий,
#    и image.allowMutableTag остаётся false.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/apps/messenger"
LOCK="$APP/base-image.lock"
BASE_IMAGE="${BASE_IMAGE:-python:3.12-slim}"
IMAGE="${IMAGE:-ghcr.io/vigregus/messenger/api}"

# --- закрепление базового образа ------------------------------------------

if [ -f "$LOCK" ]; then
    BASE_DIGEST="$(cat "$LOCK")"
    echo "· базовый образ закреплён: ${BASE_DIGEST:0:23}…"
else
    echo "· базовый образ не закреплён, разрешаю $BASE_IMAGE"
    docker pull --quiet "$BASE_IMAGE" >/dev/null
    BASE_DIGEST="$(docker inspect --format '{{index .RepoDigests 0}}' "$BASE_IMAGE" | sed 's/.*@//')"
    case "$BASE_DIGEST" in
        sha256:*) ;;
        *) echo "не удалось разрешить digest базового образа" >&2; exit 1 ;;
    esac
    echo "$BASE_DIGEST" > "$LOCK"
    echo "· записан в $(basename "$LOCK"): ${BASE_DIGEST:0:23}…"
fi

# --- сборка на хосте -------------------------------------------------------

echo "· сборка на хосте"
docker build --quiet \
    --build-arg "BASE_DIGEST=$BASE_DIGEST" \
    -t "$IMAGE:building" \
    -f "$APP/Dockerfile" \
    "$APP" >/dev/null

ID="$(docker inspect --format '{{.Id}}' "$IMAGE:building")"
TAG="local-${ID#sha256:}"
TAG="${TAG:0:18}"
docker tag "$IMAGE:building" "$IMAGE:$TAG"
docker rmi "$IMAGE:building" >/dev/null 2>&1 || true
echo "· собран: $IMAGE:$TAG"

# --- загрузка в minikube ---------------------------------------------------

# Тег выведен из содержимого, поэтому совпадение тега означает совпадение
# байтов — известная ловушка `image load`, молча пропускающего образ
# с тем же тегом, здесь безвредна.
echo "· загрузка в minikube"
minikube image load "$IMAGE:$TAG"

# --- закрепление в values --------------------------------------------------

VALUES="$ROOT/gitops/04-messenger/messenger-services/values.yaml"
python3 - "$VALUES" "$IMAGE" "$TAG" <<'PY'
import io, re, sys

path, repo, tag = sys.argv[1], sys.argv[2], sys.argv[3]
s = io.open(path, encoding="utf-8").read()

block = re.search(r"(  api:\n(?:.*\n)*?)(    resources:)", s)
if not block:
    sys.exit("не найден блок сервиса api")

head, tail = block.group(1), block.group(2)
new_head = re.sub(
    r"    image:\n(?:      .*\n)+",
    f"    image:\n      repository: {repo}\n      tag: \"{tag}\"\n",
    head,
)
# Заглушка agnhost уходит вместе с первым настоящим образом.
new_head = re.sub(r"    command:\n(?:      - .*\n)+", "", new_head)
new_head = re.sub(r"    # Заглушка на время каркаса.*?\n(?:    # .*\n)*", "", new_head)

s = s.replace(head + tail, new_head + tail)
io.open(path, "w", encoding="utf-8").write(s)
print("· values.yaml: образ сервиса api обновлён")
PY

echo
echo "Дальше: закоммитить values.yaml и base-image.lock, дождаться синхронизации."
