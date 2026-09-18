#!/usr/bin/env python3
"""Проверка правила зависимостей между слоями.

Инженерный стандарт говорит, что зависимости идут только вниз. Такое правило
держится либо проверкой, либо не держится вовсе: на ревью один пропущенный
импорт выглядит безобидно, а через полгода `domain` тянет `fastapi`, и
доменные правила больше не тестируются без поднятого приложения.

Разбор идёт по AST, а не поиском по тексту: `# import fastapi` в комментарии
и строка в docstring не должны считаться нарушением.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "apps" / "messenger" / "messenger"

# Что каждому слою разрешено импортировать из проекта.
ALLOWED: dict[str, set[str]] = {
    "api": {"services", "domain", "telemetry"},
    # Фоновые процессы - такой же вход в систему, как HTTP, и правила
    # у них те же: разобрать окружение, вызвать сервис, отдать результат.
    # Без этой строки каталог просто не проверялся бы: неизвестный слой
    # проверка пропускает молча.
    "workers": {"services", "adapters", "repositories", "domain", "telemetry"},
    "services": {"repositories", "adapters", "domain", "telemetry"},
    # Только Postgres. Всё остальное снаружи — в adapters: репозиторий
    # обязан уметь участвовать в транзакции вызывающего, адаптер не может.
    "repositories": {"domain", "telemetry"},
    "adapters": {"domain", "telemetry"},
    # Ничего из проекта. Даже telemetry: доменное правило не пишет журнал -
    # оно возвращает результат, а пишет тот, кто его вызвал.
    "domain": set(),
    "telemetry": set(),
}

# Внешние библиотеки, запрещённые конкретным слоям. Смысл тот же:
# слой, знающий про HTTP, нельзя вызвать из потребителя Kafka.
FORBIDDEN_EXTERNAL: dict[str, set[str]] = {
    "domain": {"fastapi", "starlette", "psycopg", "asyncpg", "aiokafka",
               "redis", "httpx", "requests", "boto3"},
    "services": {"fastapi", "starlette"},
    "repositories": {"fastapi", "starlette",
                     # Сетевые клиенты — признак того, что это адаптер,
                     # а не репозиторий.
                     "aiokafka", "redis", "boto3", "httpx", "requests"},
    "adapters": {"fastapi", "starlette", "psycopg", "asyncpg"},
}


def layer_of(path: Path) -> str | None:
    rel = path.relative_to(PACKAGE)
    return rel.parts[0] if len(rel.parts) > 1 else None


def imported_names(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # Относительный импорт внутри своего слоя — не межслойная связь.
            if node.level == 0 and node.module:
                found.append((node.module, node.lineno))
    return found


def main() -> int:
    if not PACKAGE.exists():
        print(f"нет пакета {PACKAGE}", file=sys.stderr)
        return 1

    violations: list[str] = []
    checked = 0

    for path in sorted(PACKAGE.rglob("*.py")):
        layer = layer_of(path)
        if layer is None or layer not in ALLOWED:
            continue
        checked += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(ROOT)

        for name, lineno in imported_names(tree):
            head = name.split(".")[0]

            if head == "messenger":
                parts = name.split(".")
                target = parts[1] if len(parts) > 1 else None
                if target and target != layer and target not in ALLOWED[layer]:
                    violations.append(
                        f"{rel}:{lineno}: слой {layer} импортирует {target}; "
                        f"разрешено: {', '.join(sorted(ALLOWED[layer])) or 'ничего'}"
                    )
            elif head in FORBIDDEN_EXTERNAL.get(layer, set()):
                violations.append(
                    f"{rel}:{lineno}: слой {layer} импортирует {head} — "
                    f"этот слой не должен знать о нём"
                )

    print(f"  проверено файлов: {checked}")
    if violations:
        print(f"  нарушений правила зависимостей: {len(violations)}")
        for v in violations:
            print(f"  ✗ {v}")
        return 1
    print("  зависимости идут только вниз")
    return 0


if __name__ == "__main__":
    sys.exit(main())
