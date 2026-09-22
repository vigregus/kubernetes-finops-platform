#!/usr/bin/env bash
# Продвижение образа: подставляет digest из реестра в values и выключает
# разрешение подвижного тега.
#
# Отдельный шаг, а не часть сборки, и это намеренно: сборка говорит «такие
# байты существуют», продвижение — «эти байты идут в окружение». Смешение
# означало бы, что любая успешная сборка выкатывается сама.
#
# Digest один на группу: API, отправитель outbox и потребители собраны
# из одного коммита, и разные образы у них означали бы, что «выкатили
# версию» больше ничего не значит. Статика — вторая группа: у неё свой
# репозиторий, свой конвейер и свой digest.
#
#   scripts/promote-image.sh sha256:...            пять backend-нагрузок
#   scripts/promote-image.sh sha256:... web        только веб
#   scripts/promote-image.sh sha256:... api        только названные
#
# «Без имён» означает именно backend, а не «все услуги»: так этот вызов
# понимался и раньше, и менять его смысл в тот день, когда включится
# web, значило бы отправить веб-диджест в пять работающих нагрузок (B26).
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


def image_block(body: str) -> str:
    """Внутренние строки блока `image` нагрузки — без строки-заголовка.

    Одна регулярка на разбор и на правку: если бы отбор и подстановка
    понимали форму блока по-разному, скрипт отбирал бы одну нагрузку,
    а правил другую.
    """
    match = re.search(r"^    image:(?: \{\})?\n((?:      [^\n]*\n)*)", body, re.M)
    return match.group(1) if match else ""


# Список нагрузок снимается один раз, до правок: границы блоков сдвигаются
# после каждой подстановки, и повторный разбор по устаревшим смещениям
# однажды уже дал молчаливый пропуск - скрипт отчитался за три нагрузки,
# а изменил одну.
targets, skipped, own_image = [], [], []
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
    if re.search(r"^      repository:", image_block(body), re.M):
        # Свой репозиторий — свой конвейер и свой digest. «Без имён»
        # продвигает группу backend, а не все включённые нагрузки: до
        # появления web это было одно и то же множество, а с включённым
        # web прежнее понимание отправило бы веб-диджест в пять
        # python-нагрузок. Существующий вызов бэкенда при этом не
        # меняется — он и раньше продвигал ровно эти пять (B26).
        own_image.append(name)
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

    # Блок `image` правится построчно, а не заменяется целиком. Прежняя
    # редакция подставляла `    image:\n      digest: …` вместо всего
    # блока разом, и в нём не оставалось ничего, кроме digest: у статики
    # так пропадал `repository`, а helper `messenger.image` падал на
    # общий `image.repository` — то есть образ молча уезжал в репозиторий
    # API. Чарт при этом собирался, и заметил бы это тот, кто пошёл за
    # образом в реестр, а не тот, кто читал вывод скрипта.
    block = re.search(r"^    image:(?: \{\})?\n((?:      [^\n]*\n)*)", body, re.M)
    if not block:
        sys.exit(f"{name}: не найден блок `image` — форма values изменилась")

    # Строки блока сохраняются как есть, вместе с комментариями: они
    # объясняют, почему у нагрузки свой репозиторий, и потерять их —
    # та же потеря, что и потеря ключа.
    lines = image_block(body).splitlines()
    kept = [line for line in lines if not re.match(r"^      digest:", line)]
    digest_line = f"      digest: {digest}"
    at = next((i for i, line in enumerate(lines) if re.match(r"^      digest:", line)), None)
    if at is None:
        # Ключа ещё не было — он встаёт последним, порядок соседей не трогается.
        kept.append(digest_line)
    else:
        # Ключ был: он остаётся на своём месте, а не переезжает в конец.
        before = sum(1 for line in lines[:at] if not re.match(r"^      digest:", line))
        kept.insert(before, digest_line)
    new_body = body[: block.start()] + "    image:\n" + "".join(f"{line}\n" for line in kept) + body[block.end():]

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
if own_image:
    print(f"· пропущены со своим репозиторием: {', '.join(own_image)} "
          f"(их продвигает своя команда: `… {digest[:23]}… <имя>`)")
print("· подвижный тег запрещён")
PY
