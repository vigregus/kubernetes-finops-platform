#!/usr/bin/env python3
"""Проверка дашбордов: метрика панели существует, манифест применяется.

Два изменения в этом репозитории проходят молча, потому что их не смотрит
никто: ни CI, ни `local-test`. Оба выглядят законченной работой.

Первое — имя метрики внутри `expr`. Опечатка `http_requests_totall` не
ломает ни схему, ни разбор YAML: дашборд применяется, панель рисуется,
и на ней просто нет данных. Пустая панель неотличима от «нагрузки нет»,
а заметить это можно только глазами, причём в тот момент, когда панель
понадобилась. То же касается метрики, которую никто не снимает: панель на
`centrifugo_node_num_clients` была бы вечно пустой ровно потому, что
scrape-объекта на Centrifugo нет, — и «соединений нет» снаружи выглядит
точно так же, как «метрика не снимается».

Второе — манифест, лежащий в каталоге, который никто не применяет.
Application со `directory.recurse` читает каталог целиком, но новый каталог
не появляется в `source.path` сам. Файл существует в репозитории, проходит
все ревью и не доезжает до кластера. Инвариант B отвечает на вопрос
«применяется ли этот файл хоть чем-нибудь», а не «лежит ли он на месте».

Устройство проверки:

    A. Метрика панели существует. У дашбордов `messenger-*` (наши, набранные
       руками) из `expr` снимаются селекторы, строковые литералы, `$переменные`
       и имена функций — остаются имена метрик. Каждое обязано либо
       встретиться в `apps/**`, либо начинаться на префикс из таблицы
       EXTERNAL_METRICS, у которого есть названный манифест, и этот манифест
       покрыт инвариантом B. Таблица — allowlist, и она видна в диффе:
       внешняя метрика не «просто проходит», а попадает в неё строкой.

    B. Манифест применяется. Каждый файл под `gitops/**/manifests/**/*.yaml`
       обязан попадать в `source.path`/`sources[].path` какого-нибудь
       Application с учётом `directory.recurse` и `include`/`exclude`.

Имена функций снимаются структурно (идентификатор перед скобкой), а не
таблицей: таблица потребовала бы правки при каждом новом `rate`, а забытая
строка в ней молча пропускает имя метрики — то есть таблица протекла бы
ровно тем же классом, от которого защищает. Таблицей остаётся только то,
что структурой не снимается: ключевые слова без скобок.

Проверяются только `messenger-*`: остальные дашборды каталога —
`k8s-views-*`, `node-exporter-full`, `kube-prometheus`-сборки — описывают
метрики чужих экспортёров, которых в `apps/**` нет и не будет, и проверка
на них была бы либо всегда красной, либо выродилась бы в allowlist на сотни
строк. Наш `expr` — тот, за который мы отвечаем.
"""
from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

import yaml

from grafana_correlation.dashboard_yaml import load

ROOT = Path(__file__).resolve().parent.parent

# Метрики, которые приходят не из `apps/**`, а от чужого экспортёра, чей
# scrape-объект лежит в этом же репозитории. Ключ — имя метрики целиком,
# значение — манифест, который её снимает. Манифест обязан существовать и
# быть покрытым инвариантом B: иначе строка в таблице разрешает панель на
# метрике, которой в кластере никто не снимает, — то есть ровно тот дефект,
# от которого таблица и защищает.
#
# Именно имя целиком, а не префикс семейства: префикс разрешил бы опечатку
# в любом другом имени того же семейства, а это тот же класс, что и опечатка
# в метрике приложения. Новая метрика того же экспортёра — новая строка,
# и она видна в диффе.
EXTERNAL_METRICS: dict[str, str] = {
    # Отдаёт Centrifugo на внутреннем порту 9000, объявлена не в `apps/**`,
    # а в самом чарте. Снимается скрейпом из этого же репозитория.
    "centrifugo_node_num_clients": (
        "gitops/04-messenger/messenger-centrifugo/manifests/service-scrape.yaml"
    ),
}

# Ключевые слова PromQL, которые не сопровождаются скобкой и потому не
# снимаются структурой. Список закрытый и намеренно короткий: сюда попадает
# только то, что может стоять в `expr` отдельным словом.
KEYWORDS = {
    "and",
    "or",
    "unless",
    "bool",
    "offset",
    "inf",
    "nan",
    "start",
    "end",
}

# Суффиксы гистограмм: у `messenger_x_bucket` имя метрики объявлено как
# `messenger_x`, и в `apps/**` встречается именно оно.
HISTOGRAM_SUFFIXES = ("_bucket", "_count", "_sum")

SELECTOR = re.compile(r"\{[^{}]*\}")
STRING = re.compile(r'"(?:[^"\\]|\\.)*"|`[^`]*`')
VARIABLE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")
GROUP = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)")
FUNCTION = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*(?=\s*\()")
DURATION = re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h|d|w|y)\b")
NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
IDENTIFIER = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")


def metric_names(expr: str) -> set[str]:
    """Имена метрик, упомянутые в выражении.

    Снятие идёт по частям, и порядок важен: строковые литералы раньше
    селекторов (в литерале бывает `}`), селекторы раньше групп (в селекторе
    встречается слово `by`), группы раньше имён функций. Последнее не
    косметика: сними имена первыми — от `sum by (route) (…)` осталась бы
    `(route)`, и `route` попал бы в имена метрик как нарушение.
    """
    text = STRING.sub(" ", expr)
    text = SELECTOR.sub(" ", text)
    text = GROUP.sub(" ", text)
    text = FUNCTION.sub(" ", text)
    text = VARIABLE.sub(" ", text)
    text = DURATION.sub(" ", text)
    text = NUMBER.sub(" ", text)
    return {n for n in IDENTIFIER.findall(text) if n not in KEYWORDS}


def base_name(name: str) -> str:
    """Имя метрики без суффикса гистограммы.

    Серии `_bucket`, `_count` и `_sum` порождает клиентская библиотека, а
    в `apps/**` объявлено основание — поэтому суффикс снимается. Кроме
    одного случая: `messenger_x_total_count` основанием иметь `_total` не
    может — у счётчика серии `_count` не бывает, — и там суффикс остаётся,
    иначе опечатка в `messenger_x_total` проходила бы молча: основание
    в `apps/**` есть, имя не опознаётся как чужое, а панель пустая.
    """
    for suffix in HISTOGRAM_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            stem = name[: -len(suffix)]
            if stem.endswith("_total"):
                continue
            return stem
    return name


def panels(document: dict) -> list[dict]:
    """Панели дашборда, включая вложенные в строки."""
    found: list[dict] = []

    def walk(items: list) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            found.append(item)
            walk(item.get("panels") or [])

    walk(document.get("panels") or [])
    return found


def expressions(document: dict) -> list[tuple[object, str]]:
    """Пары «номер панели, выражение» — номер нужен в отказе.

    Без номера отказ называет имя метрики и файл на 193 строки, и искать,
    какая из десяти панелей её упомянула, приходится глазами.
    """
    return [
        (panel.get("id"), target["expr"])
        for panel in panels(document)
        for target in (panel.get("targets") or [])
        if isinstance(target, dict) and isinstance(target.get("expr"), str)
    ]


def empty_targets(document: dict) -> list[str]:
    """Панели, у которых цель есть, а выражения в ней нет.

    Такой запрос проверка пропустила бы молча: имя метрики в нём не ищется,
    потому что искать нечего. А панель при этом пустая — то же последствие,
    что и у опечатки, только незаметнее.
    """
    return [
        f"панель {panel.get('id')} ({panel.get('title')}): цель без `expr`"
        for panel in panels(document)
        for target in (panel.get("targets") or [])
        if not (isinstance(target, dict) and isinstance(target.get("expr"), str))
    ]


def covers(source: dict, manifest: Path) -> bool:
    """Покрывает ли один `source` Application этот файл."""
    path = source.get("path")
    if not isinstance(path, str):
        return False
    # Путь в Application относителен корня репозитория, а манифесты найдены
    # абсолютными — сравнение идёт на одном основании.
    base = ROOT / path.rstrip("/")
    try:
        relative = manifest.relative_to(base)
    except ValueError:
        return False
    directory = source.get("directory") or {}
    if len(relative.parts) > 1 and not directory.get("recurse"):
        return False
    name = relative.as_posix()
    exclude = directory.get("exclude") or []
    if any(fnmatch.fnmatch(name, pattern) for pattern in exclude):
        return False
    include = directory.get("include") or []
    if include and not any(fnmatch.fnmatch(name, pattern) for pattern in include):
        return False
    return True


def application_sources() -> tuple[list[dict], list[str]]:
    """Все источники Application'ов и отказы по самим Application'ам."""
    sources: list[dict] = []
    violations: list[str] = []
    files = sorted(ROOT.glob("gitops/**/application.yaml"))
    for path in files:
        try:
            document = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            violations.append(f"{path.relative_to(ROOT)}: не разбирается как YAML — {exc}")
            continue
        if not isinstance(document, dict):
            continue
        spec = document.get("spec") or {}
        items = []
        if isinstance(spec.get("source"), dict):
            items.append(spec["source"])
        items.extend(s for s in (spec.get("sources") or []) if isinstance(s, dict))
        if not items:
            violations.append(
                f"{path.relative_to(ROOT)}: ни `source`, ни `sources` — "
                f"каталог, который этот Application применяет, назвать нечем, "
                f"и инвариант B его не видит"
            )
            continue
        sources.extend(items)
    return sources, violations


def main() -> int:
    violations: list[str] = []
    sources, app_violations = application_sources()
    violations.extend(app_violations)

    manifests = sorted(ROOT.glob("gitops/**/manifests/**/*.yaml"))
    uncovered = 0
    for manifest in manifests:
        if not any(covers(source, manifest) for source in sources):
            uncovered += 1
            violations.append(
                f"{manifest.relative_to(ROOT)}: не покрыт ни одним `source.path` — "
                f"файл есть в репозитории, но его никто не применяет"
            )

    external_seen: dict[str, str] = {}
    dashboards = sorted(ROOT.glob("gitops/**/dashboards/manifests/messenger-*.yaml"))
    # Объявленной метрика считается только в коде приложения: имя, живущее
    # лишь в тесте, — это имя, которого в проде не отдаёт никто.
    app_text = "\n".join(
        path.read_text(errors="replace")
        for path in sorted(ROOT.glob("apps/**/*.py"))
        if "tests" not in path.parts
    )
    checked_expressions = 0
    checked_names = 0
    unique_names: set[str] = set()
    for dashboard in dashboards:
        document, _, _, _, _ = load(str(dashboard))
        where = dashboard.relative_to(ROOT)
        for empty in empty_targets(document):
            violations.append(f"{where}: {empty}")
        for panel_id, expr in expressions(document):
            checked_expressions += 1
            for name in sorted(metric_names(expr)):
                checked_names += 1
                unique_names.add(name)
                base = base_name(name)
                known = base if base in EXTERNAL_METRICS else None
                if known is not None:
                    manifest = ROOT / EXTERNAL_METRICS[known]
                    external_seen[known] = EXTERNAL_METRICS[known]
                    if not manifest.exists():
                        violations.append(
                            f"{where} (панель {panel_id}): `{base}` разрешена строкой "
                            f"EXTERNAL_METRICS, но манифеста {EXTERNAL_METRICS[known]} нет"
                        )
                    elif not any(covers(source, manifest) for source in sources):
                        violations.append(
                            f"{where} (панель {panel_id}): `{base}` разрешена строкой "
                            f"EXTERNAL_METRICS, но {EXTERNAL_METRICS[known]} "
                            f"не покрыт ни одним `source.path`"
                        )
                elif base not in app_text:
                    violations.append(
                        f"{where} (панель {panel_id}): `{base}` не объявлена в `apps/**` — "
                        f"либо опечатка в имени, либо метрику никто не снимает"
                    )

    print(f"  проверено манифестов: {len(manifests)}, применённых: {len(manifests) - uncovered}")
    print(f"  проверено дашбордов: {len(dashboards)}, выражений: {checked_expressions}")
    external = "не названо" if not external_seen else ", ".join(sorted(external_seen))
    print(
        f"  имён метрик: {len(unique_names)} уникальных в {checked_names} упоминаниях, "
        f"внешних по таблице: {external}"
    )
    if violations:
        print(f"  нарушений: {len(violations)}")
        for violation in violations:
            print(f"  ✗ {violation}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
