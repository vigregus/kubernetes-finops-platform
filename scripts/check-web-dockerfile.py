#!/usr/bin/env python3
"""Статический гейт Dockerfile веба (срез 8, DEP-001, CTR-004).

Проверяются шесть вещей, и каждая ловит свой отказ, который иначе не виден
нигде до момента, когда им пользуются:

1. в Node-стадии стоит `RUN npm run build:app`;
2. `npm run build` самостоятельной командой там **не** стоит;
3. рядом лежит `apps/web/Dockerfile.dockerignore`;
4. ни у одного основания нет значения по умолчанию, и каждый использованный
   `@${…DIGEST}` объявлен как `ARG`;
5. сборочные стадии идут на платформе сборки, а не целевой;
6. внутри Dockerfile не вызывается Docker.

Про (2) и (1) вместе. `npm run build` зовёт `api:generate`, а тот — `docker
run`: внутри Node-стадии демона нет, и вызов был бы не медленным, а
невозможным — то есть сборка образа падала бы. Проверка обязана различать
строки по границе слова, иначе `build:app` совпадёт с шаблоном и чек
позеленеет на пустом месте, а `build-storybook` — тем более.

Про (4). Значения по умолчанию у оснований превратили бы закрепление в
пожелание: сборка без `--build-arg` собралась бы из чего-нибудь похожего, и
`DEP-001` держался бы на дисциплине, а не на файле.

Про (6). Docker внутри Docker — не стилистика: у стадии нет демона, а
`--privileged` сборки в проекте нет ни в одном конвейере.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "apps/web/Dockerfile"
IGNORE = ROOT / "apps/web/Dockerfile.dockerignore"

# `npm run build`, за которым не идёт продолжение имени: `:app`, `-storybook`
# и любая другая буква означают другую команду.
BARE_BUILD_RE = re.compile(r"npm\s+run\s+build(?![\w:-])")

# Сборочные стадии — те, что производят артефакты и не зависят от целевой
# архитектуры. Имя стадии берётся из `AS <имя>`.
STAGE_RE = re.compile(r"^FROM\s+(?P<flags>(?:--\S+\s+)*)(?P<image>\S+)(?:\s+AS\s+(?P<name>\S+))?", re.M | re.I)
BUILD_STAGES = {"codegen", "build"}

ARG_RE = re.compile(r"^ARG\s+(?P<name>\w+)(?P<default>\s*=.*)?$", re.M)
USED_BASE_RE = re.compile(r"^FROM\s+.*@\$\{(?P<name>\w+)\}", re.M | re.I)


def code_lines(path: Path) -> list[tuple[int, str]]:
    """Строки Dockerfile без комментариев — с номерами.

    Комментарии не команды: `# RUN npm run build` ничего не делает, и чек,
    читающий файл целиком, краснел бы на объяснении того, почему команды нет.
    """
    lines = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append((number, line))
    return lines


def main() -> int:
    print("· Dockerfile веба")

    if not DOCKERFILE.exists():
        print(f"  ✗ {DOCKERFILE.relative_to(ROOT)} отсутствует — образа веба нет")
        return 1

    lines = code_lines(DOCKERFILE)
    text = "\n".join(line for _, line in lines)
    violations: list[str] = []

    # (1) сборка статики идёт командой без генерации.
    if not re.search(r"npm\s+run\s+build:app(?![:\w-])", text):
        violations.append(
            "нет `RUN npm run build:app` — статика в образе не собирается; "
            "`npm run build` для этого не годится: он зовёт api:generate, а тот — docker"
        )

    # (2) обратной проверки мало одной: она обязана различать границу слова.
    for number, line in lines:
        if BARE_BUILD_RE.search(line):
            violations.append(
                f"{number}: `npm run build` в стадии сборки — внутри образа нет демона, "
                f"api:generate выполнить нечем; в образе собирается build:app"
            )

    # (3) ignore-файл рядом с Dockerfile.
    if not IGNORE.exists():
        violations.append(
            "apps/web/Dockerfile.dockerignore отсутствует — контекст от корня увозит в демон "
            "весь репозиторий вместе с .git"
        )

    # (4) закрепление оснований, а не пожелание.
    declared: dict[str, bool] = {}
    for match in ARG_RE.finditer(text):
        declared[match.group("name")] = match.group("default") is not None
    for name, has_default in declared.items():
        if name.endswith("DIGEST") and has_default:
            violations.append(
                f"ARG {name} объявлен со значением по умолчанию — сборка без --build-arg "
                f"соберётся «из чего-нибудь похожего», и закрепление перестанет что-либо значить"
            )
    for match in USED_BASE_RE.finditer(text):
        name = match.group("name")
        if name not in declared:
            violations.append(
                f"основание `${{{name}}}` не объявлено через ARG — значение неоткуда взять, "
                f"кроме как из значения по умолчанию, которого быть не должно"
            )

    # (5) сборочные стадии — на платформе сборки.
    stages = {m.group("name"): m.group("flags") or "" for m in STAGE_RE.finditer(text) if m.group("name")}
    for name in sorted(BUILD_STAGES):
        flags = stages.get(name)
        if flags is None:
            violations.append(f"стадия `{name}` не найдена — сборка опирается на неё по контракту")
            continue
        if "$BUILDPLATFORM" not in flags:
            violations.append(
                f"стадия `{name}` объявлена без `--platform=$BUILDPLATFORM` — под buildx её работа "
                f"исполняется через QEMU, и две архитектуры стоят как две медленных сборки"
            )
    nginx_flags = stages.get("nginx", "")
    if "$TARGETPLATFORM" in nginx_flags or "$BUILDPLATFORM" in nginx_flags:
        violations.append(
            "стадия `nginx` объявлена с явной платформой — она обязана собираться под целевую "
            "архитектуру по умолчанию, иначе образ окажется не той архитектуры, что запросили"
        )

    # (6) Docker внутри Docker.
    for number, line in lines:
        if re.search(r"(?<![\w./-])docker\s+(run|build|pull|push)(?![\w-])", line):
            violations.append(
                f"{number}: вызов Docker внутри Dockerfile — у стадии нет демона, "
                f"и вызов был бы не медленным, а невозможным"
            )

    if violations:
        for violation in violations:
            print(f"  ✗ {violation}")
        return 1

    print(f"  ✓ Dockerfile: build:app, {len(declared)} ARG без умолчаний, "
          f"сборочные стадии на платформе сборки, ignore-файл на месте")
    return 0


if __name__ == "__main__":
    sys.exit(main())
