#!/usr/bin/env python3
"""Проверка миграций на операторы, запрещённые ADR 0006.

Два класса нарушений, и они разные по природе.

Несовместимость: DROP, RENAME, сужение типа. Ломают старый код, который ещё
работает в окне между миграцией и завершением выкатки.

Блокировки: ADD CONSTRAINT без NOT VALID, CREATE INDEX без CONCURRENTLY.
Совместимы, но останавливают таблицу на время сканирования — то есть
останавливают переписку.

Второй класс коварнее: на пустой таблице он не проявляется никак, поэтому
проходит и стенд, и ревью, и обнаруживается в проде на объёме.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "db" / "migrations"

# Миграции, принятые до введения правила. Переписывать применённый файл
# нельзя — проверка контрольной суммы это запрещает, — поэтому они остаются
# как есть и перечислены поимённо. Список не расширяется: новая миграция,
# попавшая сюда, означает, что правило обошли.
GRANDFATHERED = {"0001_init", "0002_erasure", "0003_erasure_ledger"}

FORBIDDEN = [
    (r"\bDROP\s+COLUMN\b", "DROP COLUMN ломает старый код в окне выкатки; сужение — отдельной миграцией"),
    (r"\bDROP\s+TABLE\b", "DROP TABLE — то же самое, только целиком"),
    (r"\bRENAME\s+(COLUMN|TO)\b", "переименование — это DROP плюс ADD, замаскированные одним словом"),
    (r"\bALTER\s+COLUMN\s+\w+\s+TYPE\b", "смена типа переписывает таблицу; нужна новая колонка и переход"),
    (r"\bSET\s+NOT\s+NULL\b", "полное сканирование под блокировкой; нужна проверка NOT VALID"),
]


def check_constraints(sql: str) -> list[str]:
    """ADD CONSTRAINT ... CHECK обязан быть NOT VALID.

    Разбор идёт по оператору целиком, а не построчно: условие CHECK
    переносится на несколько строк, и NOT VALID оказывается далеко
    от слова CONSTRAINT.
    """
    problems = []
    for stmt in sql.split(";"):
        if re.search(r"\bADD\s+CONSTRAINT\b", stmt, re.I) and re.search(r"\bCHECK\b", stmt, re.I):
            if not re.search(r"\bNOT\s+VALID\b", stmt, re.I):
                name = re.search(r"ADD\s+CONSTRAINT\s+(\w+)", stmt, re.I)
                problems.append(
                    f"ADD CONSTRAINT {name.group(1) if name else '?'} ... CHECK без NOT VALID: "
                    "полное сканирование под исключительной блокировкой"
                )
    return problems


def check_indexes(sql: str) -> list[str]:
    problems = []
    for stmt in sql.split(";"):
        if re.search(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", stmt, re.I):
            # В CREATE TABLE индексы создаются вместе с пустой таблицей —
            # там CONCURRENTLY не нужен и не разрешён.
            if not re.search(r"\bCONCURRENTLY\b", stmt, re.I):
                name = re.search(r"INDEX\s+(?:CONCURRENTLY\s+)?(\w+)", stmt, re.I)
                problems.append(
                    f"CREATE INDEX {name.group(1) if name else '?'} без CONCURRENTLY: "
                    "блокирует запись в таблицу на время построения"
                )
    return problems


def strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"/\*.*?\*/", "", sql, flags=re.S)


def main() -> int:
    files = sorted(MIGRATIONS.glob("*.sql"))
    if not files:
        print(f"  миграций не найдено в {MIGRATIONS}", file=sys.stderr)
        return 1

    violations: list[str] = []
    skipped = 0

    for path in files:
        version = path.stem
        if version in GRANDFATHERED:
            skipped += 1
            continue

        sql = strip_comments(path.read_text(encoding="utf-8"))
        found = []
        for pattern, why in FORBIDDEN:
            if re.search(pattern, sql, re.I):
                found.append(why)
        found += check_constraints(sql)
        found += check_indexes(sql)
        violations += [f"{path.name}: {f}" for f in found]

    print(f"  проверено миграций: {len(files) - skipped}, принято до правила: {skipped}")
    if violations:
        print(f"  нарушений ADR 0006: {len(violations)}")
        for v in violations:
            print(f"  ✗ {v}")
        return 1
    print("  запрещённых операторов нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
