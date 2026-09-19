#!/usr/bin/env python3
"""Проверка контрактов (CTR-001…003).

Две разные вещи:

1. Схемы синтаксически корректны и внутренне связны — сломанная ссылка
   `$ref` не должна доезжать до генерации клиента.
2. Изменения обратно совместимы. Это главное: удалить поле или сузить тип
   можно только осознанно, а не случайно, потому что по ту сторону контракта
   уже работает чужой код.

**Что именно видит вторая проверка.** Поля и их типы — внутри
`components.schemas`, обязательность — у схем, названных в обеих ревизиях,
маршруты — по списку путей. Отдельно — то, чего в схемах не видно:

* **параметры запроса** — удаление, `required: true` и сужение схемы
  самого параметра (`COMPARED_KEYS`). Разрыв здесь **тише всех прочих**:
  параметр не поле схемы, `flatten_schema` его не видит, и удаление
  `before_conversation_id` не тронуло бы ни одной строки, которую проверка
  умеет сравнивать;
* **ссылка на схему ответа** — по ключу «метод, путь, статус, тип
  содержимого». Подмена `$ref` на другую существующую компоненту иначе
  не видна вовсе: обе компоненты на месте, поля в них не менялись, а
  клиент получает другой тип. Содержимое сравнивается отдельно, по именам
  схем, и второй раз называть его здесь нечем.

Статус и тип содержимого в ключе ответа — не подробность: у операции
ответов несколько, и ключ «метод + путь» разрешал бы положить второй
inline-ответ рядом с уже разрешённым старому.

Чего вторая проверка не видит и после этого: **сужение семантики** уже
существующего параметра (он на месте, схема его не изменилась); тип
элемента, заданный ссылкой, — `flatten_schema` ссылки не разворачивает,
так что `items: { $ref: … }` сравнивается как «массив» и остаётся тем же
массивом при подмене элемента; и ключи схемы параметра, не названные
в `COMPARED_KEYS`, — `pattern`, `multipleOf`, `const`. Первые два случая
записываются прозой, а не allowlist: запись требует находки, а находки
здесь нет.

Там же причина, по которой ответы обязаны быть `$ref` на компоненту, а не
`inline`: у inline-схемы нет имени, и поля её ответа для сравнения
не существуют. Пять ответов, объявленных inline до появления этого
правила, перечислены в `INLINE_RESPONSE_SCHEMAS` — как `GRANDFATHERED`
в проверке миграций: список закрыт, новый inline-ответ роняет проверку,
а исчезнувший из контракта требует удалить себя из списка. Записи названы
целиком — метод, путь, статус, тип содержимого: разрешение, выданное
операции, разрешало бы и второй inline-ответ в ней. Поля этих пяти
проверка по-прежнему не сравнивает — это известная дыра, а не гарантия.

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


def lookup(doc, ref: str):
    """Узел по локальной ссылке. `None` — ссылки нет, она внешняя или битая."""
    if not ref.startswith("#/"):
        return None  # внешние не проверяем: не наш файл
    node = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def resolve(doc, ref: str):
    """Только локальные ссылки: контракт обязан быть самодостаточным файлом."""
    if not ref.startswith("#/"):
        return True  # внешние не проверяем
    return lookup(doc, ref) is not None


# Ответы, объявленные inline до того, как проверка научилась их требовать.
# Список закрыт: новый inline-ответ — ошибка структуры, потому что его поля
# невидимы для сравнения совместимости, то есть разрыв в них пройдёт молча.
# Записи этих пяти, наоборот, остаются невидимыми — это долг, а не гарантия.
#
# Названы целиком — метод, путь, статус, тип содержимого. Пока запись была
# парой «метод + путь», она разрешала операции, а не ответу: у `GET /sessions`
# уже есть inline-ответ `200`, и второй, `503`, добавлялся рядом с ним, не
# сдвинув множество найденного ни на элемент. Разрешение, выданное операции,
# распространялось на всё, что в ней появится.
INLINE_RESPONSE_SCHEMAS = (
    "GET /conversations/{conversation_id}/messages 200 application/json",
    "GET /sessions 200 application/json",
    "POST /attachments 201 application/json",
    "POST /conversations/{conversation_id}/receipts 200 application/json",
    "POST /realtime/token 200 application/json",
)


def response_schemas(doc):
    """Схемы ответов: ключ «метод путь статус тип содержимого» → `$ref`|None.

    `None` означает схему, объявленную на месте. Ключ включает статус и тип
    содержимого, потому что ответов у операции несколько и различать их
    обязательно: и для закрытого списка выше, и для сравнения ссылок.

    Ответ, объявленный ссылкой на `components.responses`, разыменовывается
    по той же причине, что и параметр: у узла-ссылки нет ни `content`, ни
    схемы, поэтому без этого он не участвовал бы в сравнении вовсе — а так
    объявлены все двадцать два ответа-ошибки контракта.

    Сравнивается схема **тела**, а не имя компоненты: `Unauthorized` и
    `NotFound` отличаются статусом и описанием, но тело у обоих `Problem`,
    и сгенерированный клиент от подмены одной на другую не меняется.
    Разрывом считается смена схемы, до которой компонента доводит.
    """
    found = {}
    for route, methods in (doc.get("paths") or {}).items():
        for method, op in methods.items():
            if method in ("parameters", "summary", "description"):
                continue
            if not isinstance(op, dict):
                continue
            for status, response in (op.get("responses") or {}).items():
                if not isinstance(response, dict):
                    continue
                if "$ref" in response:
                    response = lookup(doc, response["$ref"])
                    if not isinstance(response, dict):
                        continue
                for media, body in (response.get("content") or {}).items():
                    schema = body.get("schema") if isinstance(body, dict) else None
                    if not isinstance(schema, dict):
                        continue
                    found[f"{method.upper()} {route} {status} {media}"] = schema.get("$ref")
    return found


def inline_responses(doc):
    """Ответы, у которых схема объявлена на месте, а не ссылкой."""
    return {key for key, ref in response_schemas(doc).items() if ref is None}


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

    found = inline_responses(doc)
    for name in sorted(found - set(INLINE_RESPONSE_SCHEMAS)):
        errors.append(
            f"{name}: схема ответа объявлена inline — вынести в components.schemas. "
            "Поля inline-ответа не сравниваются, и разрыв в них пройдёт молча."
        )
    for name in sorted(set(INLINE_RESPONSE_SCHEMAS) - found):
        errors.append(
            f"INLINE_RESPONSE_SCHEMAS: {name} больше не объявлен inline — убрать "
            "из списка. Пока запись лежит, она разрешит новый inline-ответ "
            "с тем же именем."
        )


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


ALLOWED_KINDS = ("field", "field_type", "required", "route", "parameter", "response")


def operation_parameters(doc, path_item, op):
    """Параметры операции: свои и унаследованные от уровня пути.

    Имя параметра в OpenAPI — пара (имя, место), а не имя: `limit`
    в строке запроса и `limit` в пути это разные параметры, и параметр
    операции перекрывает одноимённый параметр пути. Ключ поэтому пара.

    Параметры объявляются и ссылкой, и на месте, поэтому ссылка здесь
    разыменовывается: без этого `$ref: '#/components/parameters/Limit'`
    не участвовал бы в сравнении вовсе — то есть вынести параметр
    в компоненту значило бы спрятать его от проверки.
    """
    merged = {}
    for source in (path_item, op):
        for param in (source.get("parameters") or []):
            if not isinstance(param, dict):
                continue
            if "$ref" in param:
                param = lookup(doc, param["$ref"])
                if not isinstance(param, dict):
                    continue
            name, place = param.get("name"), param.get("in")
            if isinstance(name, str) and isinstance(place, str):
                merged[(place, name)] = param
    return merged


# Ключи схемы параметра, которые сравниваются. Перечислены поимённо,
# а не «всё, что найдётся»: неперечисленный ключ остаётся невидимым, и это
# названный пробел, а не гарантия. Границы разложены на две группы по тому,
# в какую сторону движение сужает множество допустимых значений.
_BOUNDS_UP = ("minimum", "exclusiveMinimum", "minLength", "minItems")
_BOUNDS_DOWN = ("maximum", "exclusiveMaximum", "maxLength", "maxItems")
COMPARED_KEYS = ("type", "format", "enum", *_BOUNDS_UP, *_BOUNDS_DOWN)


def _param_schema(doc, param):
    """Схема параметра, разыменованная, если объявлена ссылкой."""
    schema = param.get("schema")
    if not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        resolved = lookup(doc, schema["$ref"])
        return resolved if isinstance(resolved, dict) else {}
    return schema


def _types(schema):
    """Множество объявленных типов. `None` — тип не объявлен, судить не о чем."""
    value = schema.get("type")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {t for t in value if isinstance(t, str)}
    return None


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parameter_schema_findings(old_doc, new_doc, old_param, new_param):
    """Сужения схемы одного параметра: список сообщений.

    Параметр, оставшийся на месте, всё ещё может сломать вызывающего:
    сужение типа, смена формата, поднятая нижняя граница, опущенная верхняя
    или укороченное перечисление отвергают запрос, который вчера был
    законным. Пропущенное `required: true` — тот же класс, и оно ловится
    рядом, в `parameter_findings`.

    Расширение разрывом не считается: `string` → `[string, 'null']`, снятие
    перечисления, ослабленная граница оставляют вчерашний запрос законным.
    Из этого же следует, что формат сравнивается **любой** разницей,
    включая появление и исчезновение: сгенерированный клиент отображает
    `date-time` в свой тип, и ответ на вопрос «что за значение» меняется
    вместе с форматом, даже когда по проводу едет та же строка.

    Появление `type` там, где его не было, — тот же класс, что появление
    перечисления, границы или формата, и ловится наравне с ними: «тип
    не объявлен» — не «нечего сравнивать», а самое широкое из возможных
    состояний. Схема `{}` принимает любое значение, схема `{type: string}`
    — только строку, и запрос, вчера законный, сегодня получает `400`.
    Отсутствие типа находкой не сопровождается: снятие ограничения
    оставляет вчерашний запрос законным.
    """
    was = _param_schema(old_doc, old_param)
    became = _param_schema(new_doc, new_param)
    findings = []

    old_types, new_types = _types(was), _types(became)
    if new_types is not None and old_types is None:
        findings.append(f"появилось ограничение типа {sorted(new_types)}")
    elif old_types and new_types and old_types - new_types:
        findings.append(f"тип сужен {sorted(old_types)} → {sorted(new_types)}")

    old_format, new_format = was.get("format"), became.get("format")
    if old_format != new_format:
        findings.append(f"формат {old_format or 'не объявлен'} → {new_format or 'не объявлен'}")

    old_enum, new_enum = was.get("enum"), became.get("enum")
    if isinstance(old_enum, list) and isinstance(new_enum, list):
        dropped = [v for v in old_enum if v not in new_enum]
        if dropped:
            findings.append(f"из перечисления убраны {dropped}")
    elif isinstance(new_enum, list) and old_enum is None:
        findings.append("появилось перечисление, которого не было")

    for key in _BOUNDS_UP:
        old_bound, new_bound = was.get(key), became.get(key)
        if _number(new_bound) and (not _number(old_bound) or new_bound > old_bound):
            findings.append(f"граница {key} поднята {old_bound!r} → {new_bound!r}")
    for key in _BOUNDS_DOWN:
        old_bound, new_bound = was.get(key), became.get(key)
        if _number(new_bound) and (not _number(old_bound) or new_bound < old_bound):
            findings.append(f"граница {key} опущена {old_bound!r} → {new_bound!r}")

    return findings


def parameter_findings(old, new):
    """Разрывы в параметрах запроса: тройки (вид, имя, сообщение).

    Отдельной функцией, а не строкой в `check_compat`, потому что проверку
    самой проверки иначе не написать: `check_compat` читает контракт
    из git и складывает ошибки в общий список, а сравнивать надо два
    словаря. Проверка, которую нельзя проверить, — это тот же зелёный CTR,
    который ничего не доказывает, только на уровень выше; поэтому она и
    закреплена `test_validate.py`.

    Сравниваются операции, присутствующие в обеих ревизиях: удаление
    операции целиком — разрыв вида `route`, и второй раз называть его
    здесь нечем.

    Параметр — не только имя: он приходит со схемой, и сужение её ломает
    ровно так же, как удаление. Поэтому сравнение параметра идёт в два
    шага — сам параметр и его схема (`parameter_schema_findings`), — и оба
    дают находки одного вида: разрешение выдаётся элементу контракта,
    а не тексту сообщения о нём.
    """
    findings = []
    for route, old_methods in (old.get("paths") or {}).items():
        new_item = (new.get("paths") or {}).get(route)
        if not isinstance(new_item, dict):
            continue
        for method, old_op in old_methods.items():
            if method in ("parameters", "summary", "description"):
                continue
            new_op = new_item.get(method)
            if not isinstance(old_op, dict) or not isinstance(new_op, dict):
                continue
            was = operation_parameters(old, old_methods, old_op)
            became = operation_parameters(new, new_item, new_op)
            for key, param in sorted(was.items()):
                place, name = key
                label = f"{method.upper()} {route} {place}:{name}"
                if key not in became:
                    findings.append(("parameter", label, f"параметр {label} удалён"))
                    continue
                if (
                    became[key].get("required") is True
                    and param.get("required") is not True
                ):
                    findings.append(
                        ("parameter", label, f"параметр {label} стал обязательным")
                    )
                for detail in parameter_schema_findings(old, new, param, became[key]):
                    findings.append(
                        ("parameter", label, f"параметр {label}: {detail}")
                    )
    return findings


def response_findings(old, new):
    """Разрывы в схемах ответов: тройки (вид, имя, сообщение).

    Сравнивается **ссылка** на компоненту, а не её содержимое: содержимое
    сравнивается отдельно, по именам схем, и второй раз называть его здесь
    нечем. Подмена `$ref` на другую существующую компоненту — это и есть
    разрыв, который иначе не виден: обе компоненты на месте, поля в них
    не менялись, а клиент получает другой тип.

    `None` в старой ревизии означает inline-схему, и такой ответ
    пропускается: сравнивать его не с чем, и в этом была причина запретить
    inline. Отсюда же следует, что вынос inline-ответа в компоненту
    разрывом не считается — направление, в котором проверка велит идти,
    не может быть ею же и наказано. Обратное движение, `$ref` → inline,
    разрывом считается: сравнивать ответ перестаёт быть возможно.
    """
    findings = []
    was, became = response_schemas(old), response_schemas(new)
    for key, old_ref in sorted(was.items()):
        if key not in became:
            findings.append(("response", key, f"ответ {key} удалён"))
        elif old_ref is None:
            continue
        elif became[key] != old_ref:
            findings.append((
                "response", key,
                f"у ответа {key} схема сменилась "
                f"{old_ref} → {became[key] or 'inline'}",
            ))
    return findings


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

            # Параметры запроса. Удалённый параметр, параметр, ставший
            # обязательным, и суженная схема параметра ломают вызывающего
            # так же, как удалённое поле схему получателя, — но невидимы
            # там, где ищутся поля.
            findings.extend(parameter_findings(old, new))

            # Ссылки на схемы ответов. Смена `$ref` на другую компоненту
            # не меняет ни одного поля, которое умеет сравнивать `flatten_schema`:
            # обе компоненты остаются в `components.schemas` и сравниваются
            # сами с собой.
            findings.extend(response_findings(old, new))

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
