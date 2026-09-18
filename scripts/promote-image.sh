#!/usr/bin/env bash
# Продвижение образа: подставляет digest из реестра в values и выключает
# разрешение подвижного тега.
#
# Отдельный шаг, а не часть сборки, и это намеренно: сборка говорит «такие
# байты существуют», продвижение — «эти байты идут в окружение». Смешение
# означало бы, что любая успешная сборка выкатывается сама.
#
# Digest один на все нагрузки: API, отправитель outbox и потребители
# собраны из одного коммита, и разные образы у них означали бы, что
# «выкатили версию» больше ничего не значит.
#
#   scripts/promote-image.sh sha256:...            все включённые нагрузки
#   scripts/promote-image.sh sha256:... api        только названные
set -euo pipefail

DIGEST="${1:-}"
shift || true
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALUES="$ROOT/gitops/04-messenger/messenger-services/values.yaml"

case "$DIGEST" in
    sha256:*) ;;
    *) echo "укажите digest вида sha256:… (берётся из итога сборки в CI)" >&2; exit 1 ;;
esac

python3 - "$VALUES" "$DIGEST" "$@" <<'PY'
import io, re, sys

# Правка текстом, а не разбором YAML: values на три четверти состоит
# из комментариев, объясняющих каждое значение, а любой сериализатор
# выбрасывает их молча.
path, digest, *requested = sys.argv[1], sys.argv[2], *sys.argv[3:]
s = io.open(path, encoding="utf-8").read()

section_match = re.search(r"^services:\n(?:(?:  \S.*|    .*|      .*|\s*)\n)*", s, re.M)
if not section_match:
    sys.exit("не найдена секция services")
section = section_match.group(0)

# Имя нагрузки — единственное, что стоит на двух пробелах.
HEAD = r"^  ([a-z0-9-]+):\n"


def block_of(text: str, name: str) -> tuple[str, str]:
    """Заголовок нагрузки и её тело в текущем тексте секции."""
    head = re.search(rf"^  {re.escape(name)}:\n", text, re.M)
    if not head:
        sys.exit(f"не найден блок сервиса {name}")
    nxt = re.search(HEAD, text[head.end():], re.M)
    body = text[head.end(): head.end() + nxt.start()] if nxt else text[head.end():]
    return head.group(0), body


# Список нагрузок снимается один раз, до правок: границы блоков сдвигаются
# после каждой подстановки, и повторный разбор по устаревшим смещениям
# однажды уже дал молчаливый пропуск - скрипт отчитался за три нагрузки,
# а изменил одну.
targets, skipped = [], []
for head in re.finditer(HEAD, section, re.M):
    name = head.group(1)
    _, body = block_of(section, name)
    if requested:
        if name in requested:
            targets.append(name)
        continue
    if re.search(r"^    enabled: false$", body, re.M):
        # Выключенной нагрузке образ не нужен: чарт её не разворачивает.
        skipped.append(name)
        continue
    targets.append(name)

unknown = sorted(set(requested) - set(targets))
if unknown:
    sys.exit(f"нет таких нагрузок: {', '.join(unknown)}")

promoted = []
for name in targets:
    head, body = block_of(section, name)

    keys = re.findall(r"^    image:.*$", body, re.M)
    if len(keys) != 1:
        # Два ключа `image` в одном отображении - это не опечатка в глазах
        # YAML: побеждает последний, и digest, подставленный в первый,
        # молча не доедет до кластера.
        sys.exit(f"{name}: ключей `image` {len(keys)}, ожидался один — почините values")

    new_body, count = re.subn(
        r"^    image:(?: \{\}\n|\n(?:      .*\n)*)",
        f"    image:\n      digest: {digest}\n",
        body,
        count=1,
        flags=re.M,
    )
    if count != 1:
        sys.exit(f"{name}: не удалось подставить digest — форма блока `image` изменилась")

    updated = section.replace(head + body, head + new_body, 1)
    if updated == section:
        sys.exit(f"{name}: подстановка не записалась — блок не найден в секции")
    section = updated
    promoted.append(name)

if not promoted:
    # Раньше скрипт в этом случае печатал успех и всё равно запрещал
    # подвижный тег - то есть оставлял чарт, который не собирается вовсе.
    sys.exit("ни одной нагрузки не продвинуто — проверьте имена сервисов")

s = s.replace(section_match.group(0), section, 1)

# Digest есть — послабление больше не нужно. Оставленное включённым,
# оно однажды пропустит выкатку по тегу в прод.
s = s.replace("  allowMutableTag: true", "  allowMutableTag: false")
io.open(path, "w", encoding="utf-8").write(s)

print(f"· digest {digest[:23]}… у нагрузок: {', '.join(promoted)}")
if skipped:
    print(f"· пропущены выключенные: {', '.join(skipped)}")
print("· подвижный тег запрещён")
PY
