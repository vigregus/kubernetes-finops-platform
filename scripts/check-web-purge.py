#!/usr/bin/env python3
"""Статический гейт production-пути веб-клиента (срез 6, CTR-004).

Граф импортов от `src/main.tsx` не должен достигать ни одного модуля фикстур и
ни одного элемента из закрытого списка B15.

Почему граф, а не поиск строки по файлам. `ChatPage.tsx` может больше не
упоминать `mock-data` и всё равно получить его транзитивно — через компонент,
который его импортирует. И наоборот: имя `MessageComposer` встречается в
комментарии, объясняющем, почему он **не** подключён, и текстовый поиск покраснел
бы на этом объяснении. Вопрос здесь не «упомянуто ли», а «доедет ли до браузера».

Резолвер импортов намеренный и маленький: в проекте нет алиасов (`vite.config.ts`
не объявляет `resolve.alias`), относительные пути идут с расширением и без,
индексные файлы — единственная форма каталога. Голые спецификаторы (`react`) не
резолвятся вовсе и в граф не попадают: они и не наши модули.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "apps/web/src"
ENTRY = SRC / "main.tsx"

# Спецификатор импорта: `from "..."` и побочный `import "..."`.
IMPORT_RE = re.compile(r"""(?:^|\n)\s*(?:import|export)[\s\S]*?from\s+["']([^"']+)["']""")
BARE_IMPORT_RE = re.compile(r"""(?:^|\n)\s*import\s+["']([^"']+)["']""")

# То, чего в графе быть не должно. Пути — от `src/`, по одному на строку списка,
# и список закрытый: он и есть определение «production-путь» в этом срезе.
FORBIDDEN = {
    "shared/lib/mock-data.ts": "фикстуры в production-пути",
    "features/messages/components/MessageComposer.tsx": "вне объёма G3-005 (B15)",
    "features/messages/components/MessageTimeline.tsx": "вне объёма G3-005 (B15)",
    # Строка добавлена этим гейтом, и это **изменение списка**, а не сохранение.
    # До неё `MessageBubble` был недостижим лишь транзитивно — через запрещённый
    # `MessageTimeline.tsx:6`, — а прямое подключение не ловил никто: измерено
    # подстановкой импорта в `ChatPage.tsx` (граф 75 модулей, чек зелёный).
    # Решение «весь штатный messaging UI остаётся запасом» должно быть закреплено
    # статически, а не следовать из одного лишь чужого импорта (B13, вариант A).
    "features/messages/components/MessageBubble.tsx": "вне объёма G3-005/G3-006 (B13)",
    "features/messages/components/ConnectionStateBanner.tsx": "вне объёма G3-005 (B15)",
    "features/messages/components/TypingIndicator.tsx": "вне объёма G3-005 (B15)",
    "features/messages/components/SyncIndicator.tsx": "вне объёма G3-005 (B15)",
    "features/conversations/components/BlockedNotice.tsx": "вне объёма G3-005 (B15)",
    "features/auth/components/EmailVerificationBanner.tsx": "вне объёма G3-005 (B15)",
    "features/auth/SettingsSessionsPage.tsx": "вне объёма G3-005 (B15)",
    "shared/ui/SearchField.tsx": "фильтр по первой странице пагинированного списка (B28)",
}

# Что граф обязан содержать. Без этой проверки сломанный резолвер даёт граф из
# одного `main.tsx`, и гейт зеленеет на пустоте — «ни одного нарушения» и «ни
# одного узла» выглядят одинаково.
REQUIRED = (
    "App.tsx",
    "api/client.ts",
    "features/auth/me-adapter.ts",
    "features/conversations/ChatPage.tsx",
    "features/conversations/adapter.ts",
    # Поверхность среза 5: лента и строка состояния. Обе — не оформление, а то,
    # по чему приёмка читает состояние (B8). В списке они потому, что «объявлен
    # рабочим, а не подключён» и «не смонтирован вовсе» выглядят одинаково, и
    # без REQUIRED снятый импорт прошёл бы молча.
    "features/messages/components/MessageList.tsx",
    "features/realtime/components/ConnectionStatusLine.tsx",
)


def fail(message: str) -> None:
    print(f"  ✗ {message}")
    raise SystemExit(1)


def resolve(specifier: str, importer: Path) -> Path | None:
    """Спецификатор → файл. `None` — не наш модуль или его нет."""
    if not specifier.startswith("."):
        return None

    base = (importer.parent / specifier).resolve()
    for candidate in (base, base.with_suffix(".ts"), base.with_suffix(".tsx"), base / "index.ts", base / "index.tsx"):
        if candidate.is_file():
            return candidate
    return None


def imports_of(path: Path) -> set[str]:
    source = path.read_text(encoding="utf-8")
    return set(IMPORT_RE.findall(source)) | set(BARE_IMPORT_RE.findall(source))


def graph(entry: Path) -> dict[Path, Path | None]:
    """Обход в ширину: файл → тот, через кого он попал в граф."""
    seen: dict[Path, Path | None] = {entry: None}
    queue = [entry]
    while queue:
        current = queue.pop()
        for specifier in sorted(imports_of(current)):
            target = resolve(specifier, current)
            if target is None or target in seen:
                continue
            seen[target] = current
            queue.append(target)
    return seen


def chain(seen: dict[Path, Path | None], target: Path) -> str:
    """Путь от `main.tsx` до нарушителя — чтобы отчёт называл, через кого он вошёл."""
    names: list[str] = []
    cursor: Path | None = target
    while cursor is not None:
        names.append(str(cursor.relative_to(SRC)))
        cursor = seen[cursor]
    return " → ".join(reversed(names))


def main() -> int:
    if not ENTRY.is_file():
        fail(f"нет точки входа {ENTRY}")

    seen = graph(ENTRY)
    reached = {str(path.relative_to(SRC)) for path in seen}

    print(f"  граф от main.tsx: {len(reached)} модулей")

    violations: list[str] = []

    for required in REQUIRED:
        if required not in reached:
            # Так выглядят **оба** случая сразу — и сломанный резолвер, и модуль,
            # который в графе оказался бы, только если бы его кто-то импортировал.
            # Различить их чек не может и не должен: зелёный в обоих случаях
            # ничего не значит, а назвать одну причину значило бы выдать догадку
            # за измерение.
            violations.append(
                f"граф не содержит {required}: так выглядит и сломанный резолвер, "
                f"и модуль, не подключённый к production-пути"
            )

    for path, reason in sorted(FORBIDDEN.items()):
        if path in reached:
            violations.append(f"production-путь достигает {path} ({reason}): {chain(seen, SRC / path)}")

    stories = sorted(name for name in reached if name.endswith(".stories.tsx"))
    if stories:
        violations.append(f"production-путь достигает stories: {', '.join(stories)}")

    # Печатаются **все** нарушения, а не первое. Остановка на первом прячет
    # остальные, и красный прогон перестаёт быть доказательством: на дереве до
    # правки композиции нарушений два — фикстуры в графе и пропавший
    # `me-adapter`, — и, увидев только одно, нельзя сказать, искали ли второе.
    if violations:
        for violation in violations:
            print(f"  ✗ {violation}")
        return 1

    print(f"  ✓ ни одного модуля из закрытого списка среди {len(reached)} достигнутых")
    return 0


if __name__ == "__main__":
    sys.exit(main())
