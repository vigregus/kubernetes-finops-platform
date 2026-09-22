#!/usr/bin/env bash
# Сборка образа веб-клиента.
#
# Контекст — корень репозитория, а не apps/web: контракт лежит в
# packages/contracts/, то есть выше приложения. Одна формулировка для
# человека, Make и CI, и она не «..».
#
# Значения оснований читаются из apps/web/base-image.lock, а не назначаются
# здесь: локальная сборка и конвейер обязаны собирать одно и то же, а второй
# источник значений разошёлся бы с первым молча. Ни одного значения по
# умолчанию нет и у Dockerfile: голый `docker build` падает — это и есть
# доказательство, что закрепление обязательно, а не пожелание (DEP-001).
#
#   bash scripts/build-web.sh                 # ghcr.io/vigregus/messenger-web:check
#   IMAGE=… TAG=… bash scripts/build-web.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$ROOT/apps/web/base-image.lock"
DOCKERFILE="$ROOT/apps/web/Dockerfile"
IMAGE="${IMAGE:-ghcr.io/vigregus/messenger-web}"
TAG="${TAG:-check}"

read_lock() {
    local name="$1" value
    value="$(grep "^${name}=" "$LOCK" | cut -d= -f2-)"
    if [ -z "$value" ]; then
        echo "ОСТАНОВ: ${name} не найден в $LOCK" >&2
        exit 1
    fi
    printf '%s' "$value"
}

GENERATOR_DIGEST="$(read_lock GENERATOR_DIGEST)"
NODE_DIGEST="$(read_lock NODE_DIGEST)"
NGINX_DIGEST="$(read_lock NGINX_DIGEST)"

echo "· основания: node ${NODE_DIGEST:7:12}…, nginx ${NGINX_DIGEST:7:12}…, генератор ${GENERATOR_DIGEST:7:12}…"
echo "· контекст: $ROOT"

docker build \
    -f "$DOCKERFILE" \
    --build-arg "GENERATOR_DIGEST=$GENERATOR_DIGEST" \
    --build-arg "NODE_DIGEST=$NODE_DIGEST" \
    --build-arg "NGINX_DIGEST=$NGINX_DIGEST" \
    -t "$IMAGE:$TAG" \
    "$ROOT"

echo "· собран: $IMAGE:$TAG"
