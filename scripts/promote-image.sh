#!/usr/bin/env bash
# Продвижение образа: подставляет digest из реестра в values и выключает
# разрешение подвижного тега.
#
# Отдельный шаг, а не часть сборки, и это намеренно: сборка говорит «такие
# байты существуют», продвижение — «эти байты идут в окружение». Смешение
# означало бы, что любая успешная сборка выкатывается сама.
#
#   scripts/promote-image.sh sha256:... [сервис]
set -euo pipefail

DIGEST="${1:-}"
SERVICE="${2:-api}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALUES="$ROOT/gitops/04-messenger/messenger-services/values.yaml"

case "$DIGEST" in
    sha256:*) ;;
    *) echo "укажите digest вида sha256:… (берётся из итога сборки в CI)" >&2; exit 1 ;;
esac

python3 - "$VALUES" "$SERVICE" "$DIGEST" <<'PY'
import io, re, sys

path, service, digest = sys.argv[1], sys.argv[2], sys.argv[3]
s = io.open(path, encoding="utf-8").read()

block = re.search(rf"(  {re.escape(service)}:\n(?:.*\n)*?)(    resources:)", s)
if not block:
    sys.exit(f"не найден блок сервиса {service}")

head, tail = block.group(1), block.group(2)
new_head = re.sub(
    r"(    image:\n)(?:      .*\n)+",
    rf"\g<1>      digest: {digest}\n",
    head,
)
s = s.replace(head + tail, new_head + tail)

# Digest есть — послабление больше не нужно. Оставленное включённым,
# оно однажды пропустит выкатку по тегу в прод.
s = s.replace("  allowMutableTag: true", "  allowMutableTag: false")
io.open(path, "w", encoding="utf-8").write(s)
print(f"· {service}: digest {digest[:23]}…, подвижный тег запрещён")
PY
