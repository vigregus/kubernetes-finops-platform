#!/usr/bin/env python3
"""Матрица окружения, скрейпов и образов чарта мессенджера (срез 9).

Проверка смотрит в **обе** стороны и на **три** вещи:

  * значение признака `services.<н>.backendEnv` и `services.<н>.metrics`
    в values — потому что проверка одного лишь рендера не отличает
    выполненный признак от случайно совпавшего отсутствия;
  * вид отрендеренного ресурса и состав переменных у каждой из шести
    нагрузок — потому что значение в values ещё не значит, что шаблон
    его читает;
  * репозиторий, в котором оказался образ каждой нагрузки — потому что
    потерянный ключ `image.repository` не роняет рендер молча: helper
    `messenger.image` падает на общий `image.repository`, и статика
    поехала бы за файлами в репозиторий бэкенда. Это не гипотеза:
    именно так и вёл себя прежний `scripts/promote-image.sh`, стиравший
    блок `image` целиком вместо одной строки `digest` (срез 9, B9в).

Шаблон общий на все нагрузки, и признак `backendEnv` (умолчание — ложь)
существует ровно затем, чтобы правка ради статики не отдала учётные
данные бэкенда статике и не отрезала их работающим нагрузкам. Обе
половины обязательны: «у web нет секретов» без второй половины
зеленеет и когда секретов нет ни у кого.

Матрица видов скрейпа, а не «метрики есть/нет»: у `api` есть служба и
нет порта метрик, поэтому снимается `VMServiceScrape`; у четырёх
фоновых наоборот — `VMPodScrape`; у `web` ни того, ни другого.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "charts/messenger"
VALUES = ROOT / "gitops/04-messenger/messenger-services/values.yaml"

# Пять нагрузок с backend-набором: ровно та же группа, что продвигает
# scripts/promote-image.sh без имён. Список закрытый и совпадает с ним
# намеренно — расширение одной стороны без другой заметно сразу.
BACKEND = ["api", "outbox-relay", "consumer-realtime", "consumer-unread", "presence-sweeper"]
# Полный список нагрузок: шестая — статика, ради которой признак и заведён.
ALL = BACKEND + ["web"]

# Синтетический digest — только для рендера. В values он не попадает:
# чарт отказывается собираться без digest при `allowMutableTag: false`,
# а настоящий приходит срезом 11.
RENDER_DIGEST = "sha256:" + "0" * 64

# Что чем проверяется. Семьи заданы префиксами, а не перечислением:
# «есть база» — это семья, и добавление в неё ещё одного адреса не
# должно требовать правки чека.
REQUIRED: dict[str, list[str]] = {
    "api": ["SERVICE_", "ENVIRONMENT", "OTEL_", "DATABASE_", "REDIS_URL",
            "KAFKA_BOOTSTRAP", "OIDC_", "CENTRIFUGO_", "KEYCLOAK_"],
    "outbox-relay": ["SERVICE_", "ENVIRONMENT", "OTEL_", "DATABASE_", "KAFKA_"],
    "consumer-realtime": ["SERVICE_", "ENVIRONMENT", "OTEL_", "DATABASE_", "KAFKA_"],
    "consumer-unread": ["SERVICE_", "ENVIRONMENT", "OTEL_", "DATABASE_", "KAFKA_"],
    "presence-sweeper": ["SERVICE_", "ENVIRONMENT", "OTEL_", "DATABASE_", "REDIS_URL"],
    # У статики ровно то, что перечислено: имя службы, окружение, версия
    # и адрес коллектора. Ни одного секрета и ни одного адреса зависимости.
    "web": ["SERVICE_", "ENVIRONMENT", "OTEL_"],
}
FORBIDDEN: dict[str, list[str]] = {
    # Фоновым запрещена только учётная запись Kafka там, где её нет:
    # уборщик в шину не ходит, и выданная ему запись была бы правами
    # без применения.
    "presence-sweeper": ["KAFKA_USERNAME", "KAFKA_PASSWORD"],
    # Статике запрещён весь backend-набор целиком. METRICS_PORT здесь
    # потому же, почему и у метрик: порт, который некому слушать, — это
    # обещание метрик, которых нет.
    "web": ["DATABASE_", "KAFKA_", "OIDC_", "CENTRIFUGO_", "KEYCLOAK_",
            "REDIS_", "WEB_ORIGIN", "AUTH_COOKIE_PATH", "METRICS_PORT"],
}

# Виды скрейпа по нагрузкам. `web` — ни того, ни другого: снимать нечего.
SCRAPE: dict[str, set[str]] = {
    "api": {"VMServiceScrape"},
    "outbox-relay": {"VMPodScrape"},
    "consumer-realtime": {"VMPodScrape"},
    "consumer-unread": {"VMPodScrape"},
    "presence-sweeper": {"VMPodScrape"},
    "web": set(),
}

# Репозиторий образа по нагрузкам — второй образ, второй репозиторий.
# Ожидание записано числом, а не выведено из values: ключ `repository`
# у нагрузки живёт в том же файле, что и проверяется, поэтому ожидание,
# прочитанное оттуда же, пропало бы вместе с ним и ничего не поймало.
# Так это и выглядело, пока старый promote-image.sh стирал блок целиком:
# рендер оставался собираемым, чек — зелёным, а статика уходила в
# `messenger-api`.
IMAGE_REPO: dict[str, str] = {
    "api": "ghcr.io/vigregus/messenger-api",
    "outbox-relay": "ghcr.io/vigregus/messenger-api",
    "consumer-realtime": "ghcr.io/vigregus/messenger-api",
    "consumer-unread": "ghcr.io/vigregus/messenger-api",
    "presence-sweeper": "ghcr.io/vigregus/messenger-api",
    "web": "ghcr.io/vigregus/messenger-web",
}


def render(extra: list[str]) -> list[dict]:
    cmd = ["helm", "template", "messenger", str(CHART), "-f", str(VALUES), *extra]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("  ✗ helm template не собрался:")
        for line in result.stderr.strip().splitlines():
            print(f"      {line}")
        sys.exit(1)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def env_names(docs: list[dict], name: str) -> list[str]:
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == name:
            container = doc["spec"]["template"]["spec"]["containers"][0]
            return [entry["name"] for entry in container.get("env", [])]
    return []


def image_repo(docs: list[dict], name: str) -> str:
    """Репозиторий образа нагрузки: часть строки до `@`, то есть до digest."""
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == name:
            container = doc["spec"]["template"]["spec"]["containers"][0]
            return container["image"].split("@", 1)[0]
    return ""


def kinds(docs: list[dict]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for doc in docs:
        grouped.setdefault(doc["metadata"]["name"], set()).add(doc["kind"])
    return grouped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print("· окружение и скрейпы чарта")

    values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    services = values.get("services", {})
    violations: list[str] = []

    # --- половина первая: значения признаков в values -----------------------
    #
    # Без неё забытый ключ `metrics: false` у web неотличим от честно
    # снятых метрик: оба дают отсутствие ресурса, и «отсутствие совпало
    # случайно» выглядит как выполненный признак.
    for name in ALL:
        svc = services.get(name) or {}
        expected_backend = name in BACKEND
        if "backendEnv" not in svc:
            violations.append(
                f"values.services.{name}: признак `backendEnv` не задан значением — "
                f"умолчание у него ложь, и «у web снято» оказалось бы следствием "
                f"забытого ключа, а не утверждением о нагрузке"
            )
        elif bool(svc["backendEnv"]) is not expected_backend:
            violations.append(
                f"values.services.{name}.backendEnv = {svc['backendEnv']!r}, "
                f"ожидалось {expected_backend!r}"
            )
        if "metrics" not in svc:
            violations.append(
                f"values.services.{name}: признак `metrics` не задан значением — "
                f"умолчание у него истина, и статика получила бы VMServiceScrape, "
                f"а заметил бы это тот, кто пошёл в хранилище за рядом"
            )
        elif bool(svc["metrics"]) is not expected_backend:
            violations.append(
                f"values.services.{name}.metrics = {svc['metrics']!r}, "
                f"ожидалось {expected_backend!r}"
            )

    # --- половина вторая: рендер -------------------------------------------
    #
    # В feature-PR `services.web.enabled: false`, поэтому чарт не рендерит
    # статику вовсе. Второй прогон включает её синтетическим оверлеем —
    # иначе «у web нет секретов» проверялось бы на отсутствующем web,
    # то есть зеленело бы всегда.
    active_docs = render([])
    overlay_docs = render([
        "--set", "services.web.enabled=true",
        "--set", f"services.web.image.digest={RENDER_DIGEST}",
    ])
    active = kinds(active_docs)
    overlay = kinds(overlay_docs)

    for name in ALL:
        docs = overlay_docs if name == "web" else active_docs
        grouped = overlay if name == "web" else active
        env = env_names(docs, name)
        if not env:
            violations.append(f"у нагрузки `{name}` не отрендерился Deployment — проверять нечего")
            continue

        for family in REQUIRED[name]:
            if not any(var.startswith(family) for var in env):
                violations.append(
                    f"`{name}` потерял семейство `{family}` — правка общего шаблона "
                    f"отрезала доступ работающей нагрузке (её переменные: {', '.join(env)})"
                )
        for family in FORBIDDEN.get(name, []):
            hit = [var for var in env if var == family or var.startswith(family)]
            if hit:
                violations.append(
                    f"`{name}` получил `{', '.join(hit)}` — нагрузке выдан "
                    f"набор, которого у неё быть не должно"
                )

        actual = {kind for kind in grouped.get(name, set()) if kind.endswith("Scrape")}
        if actual != SCRAPE[name]:
            expected = ", ".join(sorted(SCRAPE[name])) or "ни одного"
            got = ", ".join(sorted(actual)) or "ни одного"
            violations.append(
                f"`{name}`: вид скрейпа — {got}, ожидалось {expected}"
            )

        repo = image_repo(docs, name)
        if repo != IMAGE_REPO[name]:
            violations.append(
                f"`{name}`: образ адресован `{repo or 'ничем'}`, ожидался "
                f"`{IMAGE_REPO[name]}` — потерянный ключ `image.repository` "
                f"не роняет рендер, а молча уводит нагрузку в чужой репозиторий"
            )

    if violations:
        for violation in violations:
            print(f"  ✗ {violation}")
        return 1

    print(f"  ✓ шесть нагрузок: признаки заданы значением (web — ложь), "
          f"у web {len(env_names(overlay_docs, 'web'))} переменных и ни одной "
          f"бэкендовой, скрейпы по матрице, образы по репозиториям "
          f"(web — {IMAGE_REPO['web'].rsplit('/', 1)[-1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
