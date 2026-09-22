#!/usr/bin/env bash
# Генерация TypeScript-клиента из packages/contracts/openapi.yaml.
#
# Единственный механизм генерации: и npm-скрипт, и стадия `codegen` в
# apps/web/Dockerfile зовут ровно этот файл с ровно этой конфигурацией.
#
# Корень репозитория берётся у git, а не отсчётом `..`. Контракт лежит в
# packages/contracts/ — то есть ВЫШЕ apps/web, откуда скрипт запускается:
# `$PWD/..` указал бы на repo/apps, где контракта нет. Одна формула для
# npm, Make и CI.
#
#   bash scripts/generate-web-client.sh
#
# Внутри Docker не вызывается никогда: у Node-стадии образа нет демона.

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
LOCK="$ROOT/apps/web/base-image.lock"
CONFIG="$ROOT/apps/web/openapi-generator.yaml"
CONTRACT="$ROOT/packages/contracts/openapi.yaml"
OUT="$ROOT/apps/web/src/api/generated"

GENERATOR_DIGEST="$(grep '^GENERATOR_DIGEST=' "$LOCK" | cut -d= -f2-)"

if [ -z "$GENERATOR_DIGEST" ]; then
  echo "ОСТАНОВ: GENERATOR_DIGEST не найден в $LOCK" >&2
  exit 1
fi

if [ ! -f "$CONTRACT" ]; then
  echo "ОСТАНОВ: контракта нет по пути $CONTRACT" >&2
  exit 1
fi

# Каталог чистится перед генерацией: иначе клиент, оставшийся от прежнего
# контракта, дожил бы до сборки — и «сгенерировано из контракта» стало бы
# утверждением о намерении.
rm -rf "$OUT"
mkdir -p "$OUT"

# Значения по умолчанию у digest'а нет: без него не собирается ничего и
# никуда. `--user` — чтобы файлы принадлежали вызывающему, а не root.
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$ROOT:/work" \
  "openapitools/openapi-generator-cli@${GENERATOR_DIGEST}" generate \
  -i /work/packages/contracts/openapi.yaml \
  -c /work/apps/web/openapi-generator.yaml \
  -o /work/apps/web/src/api/generated \
  --global-property apiDocs=false,modelDocs=false,apiTests=false,modelTests=false

echo "готово: клиент сгенерирован в apps/web/src/api/generated"
