#!/usr/bin/env python3
"""DEP-001: один образ веба проходит окружения без пересборки.

Требование говорит буквально: «Given коммит C прошёл конвейер / And stage
использует образ с digest D / When версия продвигается в прод / Then прод
использует тот же digest D / And новая сборка образа не выполняется».

Для статики это утверждение ломается **не выкаткой, а сборкой**. Пока имя
источника идентичности вшито в бандл, два окружения с разными IdP требуют
двух сборок, и «собрано один раз и продвинуто как есть» перестаёт быть
правдой — при этом ни один из существующих детекторов этого не видит:
образ собирается, чарт рендерится, пробы отвечают. Поэтому проверка здесь
одна, но смотрит она с двух сторон.

  * половина «артефакт» — в `dist/` нет ни строки источника идентичности,
    который разворачивает чарт; файла `runtime-config.js` в образе нет
    вовсе; и читатель конфигурации в бандле **есть** — иначе «источника
    в бандле нет» было бы правдой по пустоте: нечего читать;
  * половина «чарт» — два окружения рендерятся **одним** digest'ом, и
    Deployment у них побайтово одинаков, а различается ровно ConfigMap.
    Одинаковый Deployment и есть утверждение «в образе нет ничего, что
    зависит от окружения»: среда задаётся рядом с образом, а не в нём.
    Рядом — форма тома, и она тоже несущая: каталогом и **без `subPath`**.
    `subPath` фиксирует файл на момент старта пода (kubelet не перерисовывает
    такой том у работающего пода), поэтому смена источника идентичности
    остановилась бы на границе ConfigMap: в ConfigMap новый IdP, в поде
    прежний. Каталог при этом свой, а не корень статики, — том в корне накрыл
    бы index.html.

    Первое и второе связаны, и это не совпадение: аннотация
    `checksum/<что-нибудь>` над значением из окружения заставила бы Deployment
    различаться между стендами — то есть вернула бы окружение в манифест ровно
    тем приёмом, которым его оттуда убирали. Проверка это ловит: первая
    редакция тома несла такую аннотацию, и Deployment краснел.

  * источники идентичности объявлены один раз и совпадают. Браузер уходит по
    `services.web.runtimeConfig.values.oidcIssuer`, а сервер сверяет `iss` по
    `dependencies.keycloak.issuer` — это два независимых поля в одном файле,
    и комментарий рядом обещает, что они не разойдутся, но обещанием это и
    оставалось: проверка читала только первое. Разошедшись, они дают вход,
    который **проходит**, и первый защищённый вызов, который отвергается:
    браузер вошёл на один IdP, токен выписан им, а API ждёт другой. Отказ при
    этом приходит от API и выглядит как «токен не тот», а не как «стенд собран
    из двух разных источников»; расхождение в values рядом с ним никто не
    читает.

  * стык страницы, nginx и чарта — адрес файла конфигурации объявлен один раз
    и выведен в обе стороны: `nginx.conf` его отдаёт, чарт монтирует по тому
    же пути, страница его подключает **обычным** скриптом. Вид тега здесь
    несущий, а место в файле — нет, и это измерено: сборщик переносит модуль
    приложения в `<head>`, поэтому в собранной странице модуль стоит **выше**
    конфигурации, и вход при этом работает. Держит порядок то, что обычный
    скрипт исполняется при разборе, а модуль отложен до его конца; дописанное
    `type="module"` или `defer` сделало бы отложенными обе части, и точка
    входа выполнилась бы первой — прочитав конфигурацию, которой ещё нет.
    Проверка поэтому смотрит и на собранное дерево: на исходнике нет ни
    переноса в `<head>`, ни самой этой разницы.

Проверка говорит «выводится из», а не «равно», там, где равенство было бы
неверной формулировкой: строка запроса в бандле минифицирована и обрезана,
поэтому ищется **host** источника, а не значение целиком.

Чего эта проверка **не** доказывает и не пытается: что реестр не пересобрал
образ. Это факт конвейера и реестра (digest W и время его старта), а не
свойство дерева; здесь доказывается то, из-за чего пересборка была бы
**необходима**, — зависимость артефакта от окружения. Нет зависимости —
нет и причины собирать второй раз.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "charts/messenger"
VALUES = ROOT / "gitops/04-messenger/messenger-services/values.yaml"
DIST = ROOT / "apps/web/dist"

# Страница, которая подключает конфигурацию, и конфигурация nginx, задающая
# корень статики. Обе нужны, чтобы проверять **вывод**, а не совпадение: адрес
# в `index.html` — это путь тома без корня статики, и написанный рядом он
# однажды разошёлся бы с ним. Корень поэтому читается, а не подразумевается.
INDEX_HTML = ROOT / "apps/web/index.html"
NGINX_CONF = ROOT / "apps/web/nginx.conf"

# Имя, под которым бандл ищет конфигурацию. Оно названо здесь, а не
# выведено из одной из сторон: проверка обязана ловить расхождение
# **между** половинами, а вывод значения из проверяемого файла дал бы
# согласие с чем угодно — переименовали в TypeScript, и чарт «совпал».
GLOBAL_KEY = "__MESSENGER_RUNTIME_CONFIG__"

# Базовое имя объявленного пути. Оно же — ключ в ConfigMap и имя файла внутри
# смонтированного каталога. Названо здесь, а не выведено: проверка обязана
# ловить расхождение между сторонами, а вывод значения из проверяемого файла
# дал бы согласие с чем угодно.
RUNTIME_FILE = "runtime-config.js"

# Синтетические адреса двух окружений. Значения — только для рендера и в
# values не попадают: проверяется не конкретный стенд, а то, что разница
# окружений не выходит за пределы ConfigMap.
ENV_A = "https://idp.stage.example/realms/messenger"
ENV_B = "https://idp.prod.example/realms/messenger"

# Синтетический digest: чарт отказывается собираться без digest при
# `allowMutableTag: false`. Здесь он — тот самый «W», который в обоих
# окружениях обязан быть одним и тем же.
RENDER_DIGEST = "sha256:" + "1" * 64


def render(issuer: str) -> str:
    cmd = [
        "helm", "template", "messenger", str(CHART), "-f", str(VALUES),
        "--set", "services.web.enabled=true",
        "--set", f"services.web.image.digest={RENDER_DIGEST}",
        # Окружение задаётся **двумя** ключами, а не одним. Браузер уходит по
        # `runtimeConfig.values.oidcIssuer`, сервер сверяет `iss` по
        # `dependencies.keycloak.issuer` — и переопределив только первый,
        # рендер получил бы стенд, собранный наполовину: у `web` источник
        # подменён, у API остался от values. Совпадение этих двух записей
        # стережёт отдельная проверка в `main`; здесь важно, чтобы рендер
        # спрашивал оба, иначе модель «двух стендов с разными IdP» держалась бы
        # на одном ключе из двух.
        "--set", f"services.web.runtimeConfig.values.oidcIssuer={issuer}",
        "--set", f"dependencies.keycloak.issuer={issuer}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("  ✗ helm template не собрался:")
        for line in result.stderr.strip().splitlines():
            print(f"      {line}")
        sys.exit(1)
    return result.stdout


def doc_of(manifest: str, kind: str, name: str) -> dict | None:
    for doc in yaml.safe_load_all(manifest):
        if doc and doc.get("kind") == kind and doc["metadata"]["name"] == name:
            return doc
    return None


def issuer_origin(issuer: str) -> str:
    parsed = urlparse(issuer)
    return f"{parsed.scheme}://{parsed.netloc}"


def shown(path: Path) -> str:
    """Путь для сообщения: внутри репозитория — относительный, иначе как есть.

    `--dist` принимает и путь снаружи (`/tmp/...` — обычное дело), а
    `relative_to` на таком бросает `ValueError`. Проверка падала бы
    трассировкой вместо того, чтобы назвать, что именно не сошлось: красное по
    неверной причине читается как «проверка сломана», и настоящий отказ в нём
    теряется.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def served_runtime_url() -> tuple[str | None, str | None, list[str]]:
    """Корень статики и адрес файла конфигурации — как их объявил nginx.

    Оба читаются из `nginx.conf`, а не записываются здесь рядом: адрес в
    странице и путь тома — это одно значение с точностью до корня статики.
    Выписанное дважды, оно однажды разойдётся — при зелёной сборке, зелёном
    рендере и отвечающих пробах, — а замечено это будет по человеку, который
    вошёл не на тот стенд.
    """
    if not NGINX_CONF.is_file():
        return None, None, [
            f"нет `{NGINX_CONF.relative_to(ROOT)}` — адрес файла конфигурации не с чем сверить"
        ]

    text = NGINX_CONF.read_text(encoding="utf-8")
    roots = re.findall(r"(?m)^[ \t]*root[ \t]+(\S+?)/?[ \t]*;", text)
    if len(roots) != 1:
        return None, None, [
            f"директив `root` в nginx — {len(roots)}, ожидалась одна: без неё "
            f"неизвестно, чему равен путь файла на диске, и проверка молчала бы "
            f"на расхождении с чартом"
        ]
    root = roots[0].rstrip("/")

    served = re.findall(r"(?m)^[ \t]*location[ \t]+=[ \t]+(/[^ \t{]*)[ \t]*\{", text)
    for_runtime = [path for path in served if PurePosixPath(path).name == RUNTIME_FILE]
    if len(for_runtime) != 1:
        return root, None, [
            f"путей, отдающих `{RUNTIME_FILE}`, в nginx — {len(for_runtime)}, "
            f"ожидался один: под SPA-fallback файл конфигурации ушёл бы как HTML "
            f"вместо скрипта, и вход падал бы на «конфигурация не загружена» — при "
            f"живых пробах, отвечающей странице и ненайденной причине"
        ]
    return root, for_runtime[0], []


def loader_violations(html_file: Path, url: str) -> list[str]:
    """Страница подключает конфигурацию обычным скриптом — и только так.

    Несущий здесь **вид** тега, а не место в файле, и это измерено, а не
    выведено. Сборщик переносит модуль приложения в `<head>`, поэтому в
    собранной странице он стоит **выше** конфигурации (проверено на
    `dist/index.html`: строка модуля 18, строка конфигурации 40) — и вход при
    этом работает. Работает потому, что обычный скрипт исполняется сразу при
    разборе, а модуль отложен до конца разбора. Стоит дописать этому тегу
    `type="module"`, `defer` или `async` — обе части станут отложенными, пойдут
    в порядке документа, и точка входа выполнится первой, прочитав
    конфигурацию, которой ещё нет.

    Отсюда — и то, почему проверка стоит на **собранном** дереве: на исходнике
    нет ни переноса в `<head>`, ни самой этой разницы, и утверждение «порядок
    несущий» держалось бы там на файле, которого в образ не едет.
    """
    rel = shown(html_file)
    if not html_file.is_file():
        return [f"нет `{rel}` — страницы, подключающей конфигурацию, не найти"]

    html = html_file.read_text(encoding="utf-8")
    tags = [(match.start(), match.group(0)) for match in re.finditer(r"<script\b[^>]*>", html)]

    # Точка входа: без неё «конфигурация загружена раньше приложения» верно по
    # пустоте — приложения, которое её читает, в дереве нет.
    if not any("module" in tag for _, tag in tags):
        return [
            f"в `{rel}` нет точки входа (`<script type=\"module\">`) — тогда "
            f"«конфигурация загружена раньше приложения» верно по пустоте"
        ]

    loaders = []
    for position, tag in tags:
        src = re.search(r'src="([^"]+)"', tag)
        if src and PurePosixPath(src.group(1)).name == RUNTIME_FILE:
            loaders.append((position, tag, src.group(1)))
    if len(loaders) != 1:
        return [
            f"в `{rel}` скриптов, подключающих `{RUNTIME_FILE}`, — {len(loaders)}, "
            f"ожидался один: ноль — конфигурация не загрузится вовсе, два — "
            f"неясно, какое из значений победит"
        ]

    _, tag, src = loaders[0]
    if src != url:
        return [
            f"страница подключает `{src}`, а nginx отдаёт `{url}` — адрес объявлен "
            f"дважды и разошёлся: снаружи читается один, отдаётся другой"
        ]

    deferred = []
    if re.search(r'type\s*=\s*["\']?module', tag):
        deferred.append('type="module"')
    if re.search(r"\bdefer\b", tag):
        deferred.append("defer")
    if re.search(r"\basync\b", tag):
        deferred.append("async")
    if deferred:
        return [
            f"скрипт конфигурации помечен `{'`, `'.join(deferred)}` — он станет "
            f"отложенным и пойдёт в порядке документа, а модуль приложения сборщик "
            f"ставит выше: точка входа выполнится первой и прочитает конфигурацию, "
            f"которой ещё нет"
        ]
    return []


def check_seam(mount_path: str) -> tuple[list[str], str | None]:
    """Стык страницы, nginx и чарта: адрес файла конфигурации — одно значение."""
    root, url, violations = served_runtime_url()
    if url is None or root is None:
        return violations, None

    on_disk = f"{root}{url}"
    if on_disk != mount_path:
        violations.append(
            f"страница подключает `{url}` — на диске это `{on_disk}`, а чарт монтирует "
            f"`{mount_path}`: файл лёг бы не туда, откуда его читает страница, и вход "
            f"падал бы при зелёном рендере и отвечающих пробах"
        )
    violations += loader_violations(INDEX_HTML, url)
    return violations, url


def check_artifact(dist: Path, deployed_issuer: str, runtime_url: str | None) -> list[str]:
    """Половина «артефакт»: в образе нет ничего, что зависит от окружения."""
    violations: list[str] = []
    if not dist.is_dir():
        return [
            f"нет каталога {shown(dist)} — собирать нечего; "
            f"половина «артефакт» без него зеленела бы по пустоте"
        ]

    # Страница — здесь, а не только в исходнике, и это не дублирование.
    # Сборщик переносит модуль приложения в `<head>`; на исходнике этого не
    # видно, а именно там и обнаруживается, что порядок строк ничего не держит.
    if runtime_url is not None:
        violations += loader_violations(dist / "index.html", runtime_url)

    host = issuer_origin(deployed_issuer)
    baked = [
        path.relative_to(dist).as_posix()
        for path in dist.rglob("*")
        if path.is_file() and host in path.read_text(encoding="utf-8", errors="replace")
    ]
    if baked:
        violations.append(
            f"источник идентичности `{host}` вшит в артефакт: {', '.join(baked)} — "
            f"одно и то же собранное дерево не может обслужить окружения с разными "
            f"IdP, и DEP-001 закрыт не будет"
        )

    # Поиск по **всему** дереву, а не по одному пути. Первая редакция
    # спрашивала только `dist/runtime-config.js` — и файл, положенный туда,
    # откуда его действительно читает страница, `dist/runtime/runtime-config.js`,
    # не ловила. Собранный модуль уезжает под объявленным префиксом, поэтому
    # наиболее вероятная неверная упаковка оказывалась ровно в непроверенном
    # месте: проверка печатала «файла нет», глядя на соседний каталог.
    # Дороже это не стоит, а смонтированный том встал бы **поверх** такого
    # файла — то есть подмена из ConfigMap не отменила бы вшитое значение,
    # а спрятала бы его, и расхождение нашлось бы по человеку на другом стенде.
    baked_runtime = sorted(
        path.relative_to(dist).as_posix() for path in dist.rglob(RUNTIME_FILE)
    )
    if baked_runtime:
        violations.append(
            f"в артефакте есть `{RUNTIME_FILE}`: {', '.join(baked_runtime)} — файл "
            f"обязан приходить из ConfigMap окружения, а не из образа: вшитый, он "
            f"становится значением по умолчанию, и подстановка тома его не отменяет — "
            f"она его прячет"
        )

    has_reader = any(
        GLOBAL_KEY in path.read_text(encoding="utf-8", errors="replace")
        for path in dist.rglob("*.js")
        if path.is_file()
    )
    if not has_reader:
        violations.append(
            f"в артефакте нет чтения `{GLOBAL_KEY}` — тогда «источника в бандле нет» "
            f"верно по пустоте: читать конфигурацию нечему, и окружение на неё "
            f"не влияет просто потому, что она не используется"
        )
    return violations


def check_chart(mount_path: str) -> list[str]:
    """Половина «чарт»: разница окружений не выходит за пределы ConfigMap."""
    violations: list[str] = []
    a, b = render(ENV_A), render(ENV_B)

    deploys = {}
    for label, manifest in (("A", a), ("B", b)):
        doc = doc_of(manifest, "Deployment", "web")
        if doc is None:
            violations.append(f"в окружении {label} не отрендерился Deployment `web` — проверять нечего")
            continue
        deploys[label] = doc
    if len(deploys) != 2:
        return violations

    # Главное утверждение целиком: под окружение A и под окружение B —
    # один и тот же объект. Не «образ совпал», а «в поде нет ни одного
    # поля, зависящего от окружения»: совпадение одного образа оставило бы
    # место переменной, которую кто-то однажды добавит рядом.
    if deploys["A"] != deploys["B"]:
        violations.append(
            "Deployment `web` различается между окружениями — в него попало "
            "что-то, зависящее от среды; тогда окружение задаётся образом, "
            "и второй стенд требует второй сборки"
        )

    image_a = deploys["A"]["spec"]["template"]["spec"]["containers"][0]["image"]
    image_b = deploys["B"]["spec"]["template"]["spec"]["containers"][0]["image"]
    if image_a != image_b:
        violations.append(f"образы окружений разошлись: {image_a!r} против {image_b!r}")
    if RENDER_DIGEST not in image_a:
        violations.append(
            f"образ `{image_a}` не сослался на digest {RENDER_DIGEST} — продвижение "
            f"обязано адресовать манифест, а не тег"
        )

    # Объявленный путь — это путь **файла**; каталог тома и ключ ConfigMap
    # выводятся из него. Проверяется именно вывод, а не совпадение: две
    # независимо записанные строки однажды разойдутся, и под получит пустой
    # файл с другим именем — при зелёном рендере и отвечающих пробах.
    mounted_dir = str(PurePosixPath(mount_path).parent)
    if PurePosixPath(mount_path).name != RUNTIME_FILE:
        violations.append(
            f"объявленный путь `{mount_path}` оканчивается не на `{RUNTIME_FILE}` — "
            f"имя файла в ConfigMap выводится из него, и стороны разошлись бы"
        )

    # Том монтируется **каталогом** и **без `subPath`**.
    #
    # Обе половины про одно: `subPath` фиксирует файл на момент старта пода —
    # kubelet не перерисовывает такой том у работающего пода, — и смена
    # источника идентичности остановилась бы на границе ConfigMap. Каталог при
    # этом свой, а не корень статики: том в корне накрыл бы index.html, и nginx
    # отдавал бы 404 на страницу при живых пробах, потому что /healthz отдаёт
    # сам nginx, а не файл.
    mounts = deploys["A"]["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    runtime_mounts = [m for m in mounts if m["name"] == "runtime-config"]
    if len(runtime_mounts) != 1:
        violations.append(
            f"у `web` {len(runtime_mounts)} монтирований runtime-config, ожидалось одно"
        )
    else:
        mount = runtime_mounts[0]
        if "subPath" in mount:
            violations.append(
                f"том смонтирован с `subPath: {mount['subPath']}` — такой том не "
                f"обновляется у работающего пода, и смена источника идентичности "
                f"остановилась бы на границе ConfigMap"
            )
        if mount.get("mountPath") != mounted_dir:
            violations.append(
                f"том смонтирован в `{mount.get('mountPath')}`, а объявленный путь "
                f"`{mount_path}` требует каталога `{mounted_dir}` — файл лёг бы не "
                f"туда, откуда его читает страница"
            )
        if not mount.get("readOnly"):
            violations.append("том runtime-config смонтирован на запись — файл конфигурации не пишут")

    configs = {}
    for label, manifest in (("A", a), ("B", b)):
        doc = doc_of(manifest, "ConfigMap", "web-runtime")
        if doc is None:
            violations.append(f"в окружении {label} нет ConfigMap `web-runtime` — окружению неоткуда взять конфигурацию")
            continue
        keys = sorted((doc.get("data") or {}).keys())
        if keys != [RUNTIME_FILE]:
            violations.append(
                f"в ConfigMap окружения {label} ключи {keys}, ожидался ровно "
                f"`{RUNTIME_FILE}` — имя выводится из объявленного пути"
            )
            continue
        configs[label] = doc["data"][RUNTIME_FILE]
    if len(configs) != 2:
        return violations

    if configs["A"] == configs["B"]:
        violations.append(
            "ConfigMap окружений совпал — тогда окружения неразличимы, и проверка "
            "«разница не выходит за пределы ConfigMap» проходит на одинаковых файлах"
        )

    for label, issuer, other in (("A", ENV_A, ENV_B), ("B", ENV_B, ENV_A)):
        if issuer not in configs[label]:
            violations.append(
                f"ConfigMap окружения {label} не несёт свой источник `{issuer}` — "
                f"окружение разворачивается не с тем IdP, с которым объявлено"
            )
        if other in configs[label]:
            violations.append(
                f"ConfigMap окружения {label} несёт чужой источник `{other}`"
            )
        if GLOBAL_KEY not in configs[label]:
            violations.append(
                f"ConfigMap окружения {label} не задаёт `{GLOBAL_KEY}` — бандл читает "
                f"именно это имя, и конфигурация осталась бы незамеченной"
            )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=DIST,
                        help="собранная статика (по умолчанию apps/web/dist)")
    parser.add_argument("--skip-artifact", action="store_true",
                        help="без половины «артефакт»: чарт и стык, но не собранный dist")
    args = parser.parse_args()
    print("· runtime-config и DEP-001")

    values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    runtime = (values.get("services", {}).get("web", {}) or {}).get("runtimeConfig", {}) or {}
    issuer = (runtime.get("values") or {}).get("oidcIssuer")
    if not re.fullmatch(r"https?://[^\s]+", str(issuer or "")):
        print(f"  ✗ values.services.web.runtimeConfig.values.oidcIssuer = {issuer!r} — "
              f"окружение обязано объявить источник идентичности явно")
        return 1

    # Второй источник — тот, по которому API сверяет `iss` выданного токена
    # (`OIDC_ISSUER`, `charts/messenger/templates/workloads.yaml`). Поля
    # независимые, и это ровно тот случай, когда согласие между ними держалось
    # комментарием: комментарий обещал, что они не разойдутся, а расхождение
    # до этой проверки не видел никто. Цена расхождения — не отказ, а успех:
    # браузер уходит на один IdP, тот выписывает токен, и API отвергает первый
    # же защищённый вызов. Наружу это выглядит как «токен не тот», и искать
    # будут в токене, а не в двух строках values, разошедшихся молча.
    backend_issuer = (
        (values.get("dependencies") or {}).get("keycloak") or {}
    ).get("issuer")
    if not re.fullmatch(r"https?://[^\s]+", str(backend_issuer or "")):
        print(f"  ✗ values.dependencies.keycloak.issuer = {backend_issuer!r} — "
              f"источник идентичности API не объявлен: сверять источник браузера "
              f"не с чем")
        return 1
    if issuer != backend_issuer:
        print(f"  ✗ источники разошлись: браузер уходит на `{issuer}`, а API "
              f"сверяет `iss` по `{backend_issuer}`. Вход пройдёт — токен выпишет "
              f"первый, — и первый же защищённый вызов будет отвергнут вторым. "
              f"Это две записи одного значения, и разойтись они должны не молча, "
              f"а здесь")
        return 1

    mount_path = runtime.get("mountPath")
    if not re.fullmatch(r"/[^\s]+/[^\s/]+", str(mount_path or "")):
        print(f"  ✗ values.services.web.runtimeConfig.mountPath = {mount_path!r} — "
              f"не объявлено, куда класть файл конфигурации; каталог тома и ключ "
              f"ConfigMap выводятся из него")
        return 1

    violations, runtime_url = check_seam(mount_path)
    violations += check_chart(mount_path)
    if not args.skip_artifact:
        # Третье — про собранную страницу — живёт внутри половины «артефакт» и
        # пропускается вместе с ней. Молчаливым пропуском это не становится:
        # `runtime_url` пуст ровно тогда, когда стык уже покраснел выше.
        violations += check_artifact(args.dist, issuer, runtime_url)

    # Тот же дефект виден и в исходной странице, и в собранной: адрес
    # сверяется с nginx в обеих. Две одинаковые строки читаются как два разных
    # отказа, поэтому повторы снимаются. Разные остаются: расхождение,
    # случившееся только в одной из страниц, даёт разные тексты — в них
    # назван разный `src`.
    violations = list(dict.fromkeys(violations))

    if violations:
        for violation in violations:
            print(f"  ✗ {violation}")
        return 1

    # Итог собирается из того, что действительно прогонялось: строка,
    # перечисляющая непроверенное, читается как отчёт и врёт молча — а это
    # ровно тот отказ, против которого заведена вся проверка.
    checked = [
        f"web и API объявляют один источник идентичности `{issuer}`",
        "один digest на оба окружения, Deployment web побайтово один, "
        "различается только ConfigMap",
        f"адрес `{runtime_url}` выведен из nginx, совпал с путём тома чарта, "
        f"и подключается он обычным скриптом, а не отложенным",
    ]
    if not args.skip_artifact:
        checked.append(
            f"в артефакте нет `{issuer_origin(issuer)}` и нет файла "
            f"`{RUNTIME_FILE}` нигде в дереве, а чтение `{GLOBAL_KEY}` есть"
        )
    print(f"  ✓ {'; '.join(checked)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
