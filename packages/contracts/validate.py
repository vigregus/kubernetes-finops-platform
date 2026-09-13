#!/usr/bin/env python3
"""Проверка контрактов (CTR-001…003).

Две разные вещи:

1. Схемы синтаксически корректны и внутренне связны — сломанная ссылка
   `$ref` не должна доезжать до генерации клиента.
2. Изменения обратно совместимы. Это главное: удалить поле или сузить тип
   можно только осознанно, а не случайно, потому что по ту сторону контракта
   уже работает чужой код.

Сравнение идёт с версией из указанной ревизии git. Без базы для сравнения
выполняется только первая проверка — и об этом сказано вслух, чтобы отсутствие
второй не выглядело как её успешное прохождение.
"""
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
OPENAPI = ROOT / "openapi.yaml"


def load(path: Path):
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return json.loads(text)


def load_from_git(rev: str, path: Path):
    rel = path.relative_to(ROOT.parent.parent)
    out = subprocess.run(
        ["git", "show", f"{rev}:{rel}"],
        capture_output=True, text=True, cwd=ROOT.parent.parent,
    )
    if out.returncode != 0:
        return None
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(out.stdout)
    return json.loads(out.stdout)


# --- 1. связность ----------------------------------------------------------

def collect_refs(node, acc):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                acc.append(v)
            else:
                collect_refs(v, acc)
    elif isinstance(node, list):
        for v in node:
            collect_refs(v, acc)
    return acc


def resolve(doc, ref: str):
    """Только локальные ссылки: контракт обязан быть самодостаточным файлом."""
    if not ref.startswith("#/"):
        return True  # внешние не проверяем
    node = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def check_structure(errors):
    for path in [OPENAPI, *sorted(ROOT.glob("kafka/*.json")), *sorted(ROOT.glob("websocket/*.json"))]:
        try:
            doc = load(path)
        except Exception as exc:  # noqa: BLE001 - причина важнее типа
            errors.append(f"{path.name}: не разобран — {exc}")
            continue
        for ref in collect_refs(doc, []):
            if not resolve(doc, ref):
                errors.append(f"{path.name}: висячая ссылка {ref}")

    doc = load(OPENAPI)
    # Ответ без описания — то, что генератор клиента молча превратит в any.
    for route, methods in doc.get("paths", {}).items():
        for method, op in methods.items():
            if method in ("parameters", "summary", "description"):
                continue
            if not op.get("operationId"):
                errors.append(f"{method.upper()} {route}: нет operationId")
            if not op.get("responses"):
                errors.append(f"{method.upper()} {route}: нет ответов")


# --- 2. обратная совместимость --------------------------------------------

def flatten_schema(node, prefix, acc):
    """Плоский список путей до свойств и их типов."""
    if not isinstance(node, dict):
        return acc
    for name, prop in (node.get("properties") or {}).items():
        key = f"{prefix}.{name}" if prefix else name
        acc[key] = prop.get("type") or prop.get("const") or prop.get("enum") or "?"
        flatten_schema(prop, key, acc)
    for name, sub in (node.get("$defs") or {}).items():
        flatten_schema(sub, f"$defs.{name}", acc)
    return acc


def schemas_of(doc):
    if "openapi" in doc:
        return doc.get("components", {}).get("schemas", {})
    return {"": doc}


def check_compat(base_rev, errors, notes):
    for path in [OPENAPI, *sorted(ROOT.glob("kafka/*.json")), *sorted(ROOT.glob("websocket/*.json"))]:
        old = load_from_git(base_rev, path)
        if old is None:
            notes.append(f"{path.name}: новый файл, сравнивать не с чем")
            continue
        new = load(path)

        old_props, new_props = {}, {}
        for name, schema in schemas_of(old).items():
            flatten_schema(schema, name, old_props)
        for name, schema in schemas_of(new).items():
            flatten_schema(schema, name, new_props)

        for key, old_type in old_props.items():
            if key not in new_props:
                errors.append(f"{path.name}: поле {key} удалено")
            elif new_props[key] != old_type:
                errors.append(
                    f"{path.name}: у поля {key} изменился тип "
                    f"{old_type!r} → {new_props[key]!r}"
                )

        # Новое обязательное поле ломает старого отправителя так же надёжно,
        # как удалённое — старого получателя.
        for name, schema in schemas_of(new).items():
            old_schema = schemas_of(old).get(name)
            if not isinstance(old_schema, dict):
                continue
            added = set(schema.get("required") or []) - set(old_schema.get("required") or [])
            for field in sorted(added):
                errors.append(f"{path.name}: поле {name}.{field} стало обязательным")

        if "openapi" in new:
            removed = set(old.get("paths", {})) - set(new.get("paths", {}))
            for route in sorted(removed):
                errors.append(f"{path.name}: маршрут {route} удалён")


def main():
    base_rev = sys.argv[1] if len(sys.argv) > 1 else None
    errors, notes = [], []

    check_structure(errors)

    if base_rev:
        check_compat(base_rev, errors, notes)
    else:
        notes.append(
            "база для сравнения не задана — проверка совместимости НЕ выполнялась"
        )

    for note in notes:
        print(f"  · {note}")
    if errors:
        print(f"\nнесовместимых изменений и ошибок: {len(errors)}")
        for e in errors:
            print(f"  ✗ {e}")
        return 1
    print("\nконтракты согласованы")
    return 0


if __name__ == "__main__":
    sys.exit(main())
