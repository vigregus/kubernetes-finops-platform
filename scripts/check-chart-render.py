#!/usr/bin/env python3
"""Render-regression: правка общего шаблона не меняет поды пяти нагрузок.

`charts/messenger/templates/workloads.yaml` — один шаблон на все нагрузки
мессенджера, и это его достоинство ровно до того дня, когда правка ради
одной нагрузки меняет пять работающих. Срез 9 вводит в него признаки
`backendEnv` и `metrics` ради статики; признаки поставлены так, что для
пяти python-нагрузок рендер обязан остаться **побайтово прежним**.

Проверки «у web нет секретов» и «у пяти секреты остались» смотрят на
переменные и на этом расходятся: они не заметят ни пропавшего
`AUTH_COOKIE_PATH`, ни лишнего ресурса, ни изменённой пробы. Поэтому чарт
рендерится дважды — из текущего дерева и из дерева, где взят **только**
`workloads.yaml` базовой версии, при тех же values, — и манифесты пяти
нагрузок сравниваются целиком: Deployment, Service, ServiceAccount и
`VMServiceScrape`/`VMPodScrape` (`api` и `outbox-relay` дают разные виды
скрейпа, и оба входят в сравнение).

База — **неподвижный SHA**, а не `origin/main` (B25). В CI
`actions/checkout@v4` по умолчанию берёт `fetch-depth: 1`, и `origin/main`
может просто не найтись; хуже того, он движется, поэтому сравнение «с тем,
что сейчас в main» краснеет от чужого слияния и зеленеет от своего.
Отсутствие базы — **внятное падение**, а не пустое сравнение: молчаливый
зелёный здесь опаснее ошибки, потому что выглядит как доказанная
неизменность.
"""
from __future__ import annotations

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "charts/messenger"
TEMPLATE = "templates/workloads.yaml"
VALUES = ROOT / "gitops/04-messenger/messenger-services/values.yaml"

# Сравниваемые нагрузки: ровно те, что уже выкачены, — пять python.
COMPARED = ["api", "outbox-relay", "consumer-realtime", "consumer-unread", "presence-sweeper"]


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def base_template(base: str) -> str:
    """Шаблон из базы или внятное падение.

    Разбор ошибок разделён намеренно: «такого дерева/файла нет» и «такого
    коммита нет» — разные отказы и лечатся по-разному. Первое означает,
    что база есть, а шаблона в ней не было; второе — что история не
    выкачана целиком.
    """
    if git("cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        print(f"  ✗ база {base} не найдена — сравнению не с чем себя сравнивать.")
        print("      В CI это означает checkout с `fetch-depth: 1`: история нужна целиком,")
        print("      потому что база берётся из `github.event.pull_request.base.sha`,")
        print("      а не из плавающего `origin/main` (B25).")
        sys.exit(1)
    result = git("show", f"{base}:charts/messenger/{TEMPLATE}")
    if result.returncode != 0:
        print(f"  ✗ в базе {base} нет `charts/messenger/{TEMPLATE}` — сравнивать нечего.")
        sys.exit(1)
    return result.stdout


def render(chart: Path) -> list[dict]:
    cmd = ["helm", "template", "messenger", str(chart), "-f", str(VALUES)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ✗ helm template не собрался ({chart}):")
        for line in result.stderr.strip().splitlines():
            print(f"      {line}")
        sys.exit(1)
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def by_name(docs: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for doc in docs:
        grouped.setdefault(doc["metadata"]["name"], []).append(doc)
    return grouped


def canonical(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=True, allow_unicode=True, default_flow_style=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("BASE_SHA"),
                        help="SHA базовой версии (или переменная окружения BASE_SHA)")
    args = parser.parse_args()
    print("· render-regression чарта")

    if not args.base:
        print("  ✗ база не задана. Передайте `--base <SHA>` или `BASE_SHA`.")
        print("      Значения по умолчанию у базы нет намеренно: `origin/main` — подвижная")
        print("      ссылка, и сравнение с ним краснеет от чужого слияния и зеленеет от своего.")
        sys.exit(1)

    workdir = Path(tempfile.mkdtemp(prefix="chart-render-base-"))
    try:
        base_chart = workdir / "messenger"
        shutil.copytree(CHART, base_chart)
        (base_chart / TEMPLATE).write_text(base_template(args.base), encoding="utf-8")
        print(f"  база: {args.base} — в её дереве подменён только {TEMPLATE}")

        current = by_name(render(CHART))
        based = by_name(render(base_chart))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    violations: list[str] = []
    for name in COMPARED:
        now = current.get(name, [])
        before = based.get(name, [])
        if not now:
            violations.append(f"`{name}` вообще не отрендерился — сравнивать нечего")
            continue
        # Сравнение по видам ресурсов, а не по всему списку сразу: иначе
        # `diff` смешивает Deployment со скрейпом, и из него не видно,
        # что именно изменилось у нагрузки. Вид, появившийся или
        # пропавший, — тоже расхождение, и он называется отдельно.
        kinds_now = {doc["kind"]: doc for doc in now}
        kinds_before = {doc["kind"]: doc for doc in before}
        for kind in sorted(set(kinds_now) | set(kinds_before)):
            if kind not in kinds_now or kind not in kinds_before:
                side = "появился" if kind in kinds_now else "пропал"
                violations.append(f"`{name}`: {kind} {side} относительно базы")
                continue
            now_text = canonical(kinds_now[kind]).splitlines()
            before_text = canonical(kinds_before[kind]).splitlines()
            if now_text == before_text:
                continue
            violations.append(f"`{name}`: {kind} расходится с базой —")
            violations.extend(
                "  " + line
                for line in difflib.unified_diff(
                    before_text, now_text, fromfile=f"база/{name}/{kind}",
                    tofile=f"HEAD/{name}/{kind}", lineterm="", n=2,
                )
            )

    if violations:
        print("  ✗ правка общего шаблона изменила вывод для работающих нагрузок:")
        for violation in violations:
            print(f"      {violation}")
        return 1

    print(f"  ✓ пять нагрузок ({', '.join(COMPARED)}) совпали с базой целиком — "
          f"Deployment, Service, ServiceAccount и скрейпы")
    return 0


if __name__ == "__main__":
    sys.exit(main())
