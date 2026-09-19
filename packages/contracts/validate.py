#!/usr/bin/env python3
"""Проверка контрактов (CTR-001…003).

Две разные вещи:

1. Схемы синтаксически корректны и внутренне связны — сломанная ссылка
   `$ref` не должна доезжать до генерации клиента.
2. Изменения обратно совместимы. Это главное: удалить поле или сузить тип
   можно только осознанно, а не случайно, потому что по ту сторону контракта
   уже работает чужой код.

Осознанный разрыв объявляется в compat-allowlist.yaml — поимённо, с датой и
причиной. Проверка при этом не ослабляется: разрешённой становится ровно
названная записью пара (вид, имя), всё остальное остаётся ошибкой. Список
закрыт и не расширяется, как GRANDFATHERED в проверке миграций.

Разрешение одноразовое, и держится это не обещанием убрать запись, а отказом
собираться, пока она лежит: запись, не совпавшая ни с одним разрывом, — это
ошибка. Пока разрыв есть, запись совпадает с ним; как только он попал в базу
сравнения, совпадать перестаёт, и следующий запуск требует её удалить.

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
ALLOWLIST = ROOT / "compat-allowlist.yaml"


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


ALLOWED_KINDS = ("field", "field_type", "required", "route")


def load_allowlist(errors):
    """Осознанные разрывы. Отсутствие файла — не ошибка: тогда разрешено ничего."""
    if not ALLOWLIST.exists():
        return []
    try:
        doc = yaml.safe_load(ALLOWLIST.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - причина важнее типа
        errors.append(f"compat-allowlist.yaml: не разобран — {exc}")
        return []
    entries = doc.get("allowed") or []
    if not isinstance(entries, list):
        errors.append("compat-allowlist.yaml: «allowed» обязан быть списком")
        return []

    valid = []
    for i, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or not {"path", "kind", "name"} <= set(entry):
            errors.append(f"compat-allowlist.yaml: запись {i}: нет path/kind/name")
            continue
        if entry["kind"] not in ALLOWED_KINDS:
            errors.append(
                f"compat-allowlist.yaml: запись {i}: неизвестный вид {entry['kind']!r}, "
                f"бывает {', '.join(ALLOWED_KINDS)}"
            )
            continue
        valid.append(entry)
    return valid


def check_compat(base_rev, errors, notes):
    # Находки собираются тройками (вид, имя, сообщение) и только потом делятся
    # на разрешённые и нет. Сопоставление — по виду и имени, а не по тексту
    # сообщения: текст можно переформулировать, а разрешение обязано остаться
    # привязанным к самому элементу контракта.
    allowed = {(e["path"], e["kind"], e["name"]): e for e in load_allowlist(errors)}
    matched = set()

    for path in [OPENAPI, *sorted(ROOT.glob("kafka/*.json")), *sorted(ROOT.glob("websocket/*.json"))]:
        old = load_from_git(base_rev, path)
        if old is None:
            notes.append(f"{path.name}: новый файл, сравнивать не с чем")
            continue
        new = load(path)
        findings = []

        old_props, new_props = {}, {}
        for name, schema in schemas_of(old).items():
            flatten_schema(schema, name, old_props)
        for name, schema in schemas_of(new).items():
            flatten_schema(schema, name, new_props)

        for key, old_type in old_props.items():
            if key not in new_props:
                findings.append(("field", key, f"поле {key} удалено"))
            elif new_props[key] != old_type:
                findings.append((
                    "field_type", key,
                    f"у поля {key} изменился тип {old_type!r} → {new_props[key]!r}",
                ))

        # Новое обязательное поле ломает старого отправителя так же надёжно,
        # как удалённое — старого получателя.
        for name, schema in schemas_of(new).items():
            old_schema = schemas_of(old).get(name)
            if not isinstance(old_schema, dict):
                continue
            added = set(schema.get("required") or []) - set(old_schema.get("required") or [])
            for field in sorted(added):
                findings.append((
                    "required", f"{name}.{field}",
                    f"поле {name}.{field} стало обязательным",
                ))

        if "openapi" in new:
            removed = set(old.get("paths", {})) - set(new.get("paths", {}))
            for route in sorted(removed):
                findings.append(("route", route, f"маршрут {route} удалён"))

        for kind, name, message in findings:
            key = (path.name, kind, name)
            entry = allowed.get(key)
            if entry is None:
                errors.append(f"{path.name}: {message}")
                continue
            matched.add(key)
            notes.append(
                f"{path.name}: {message} — разрешено осознанно "
                f"({entry.get('since', '?')}): {entry.get('reason', 'причина не указана')}"
            )

    # Запись, которой не нашлось разрыва, — это ошибка, а не замечание.
    # Сопоставление идёт по (вид, имя), поэтому забытая запись однажды
    # разрешит чужой разрыв: удалят другой маршрут с тем же именем, и
    # проверка промолчит. Одноразовость держится не обещанием её убрать,
    # а отказом собираться, пока она лежит. Пока запись нужна, она совпадает
    # с находкой; когда разрыв попал в базу сравнения, совпадать перестаёт —
    # и следующий же запуск требует её удалить.
    for key, entry in allowed.items():
        if key not in matched:
            errors.append(
                f"compat-allowlist.yaml: {entry['kind']} {entry['name']} "
                f"({entry['path']}) не совпал ни с одним разрывом — удалить. "
                f"Пока он лежит, он разрешит будущее удаление с тем же именем."
            )


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
