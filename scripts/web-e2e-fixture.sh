#!/usr/bin/env bash
# Браузерная приёмка G3-005: единственный оркестратор.
#
# Инвариант 0 — один процесс, а не два. Пароль фикстуры живёт только в
# окружении процесса: он не записывается ни в репозиторий, ни в файл, ни в
# Kubernetes Secret. Двумя отдельными `make`-целями этот контракт неисполним
# — окружение первого процесса во второй не передаётся, а `finally`-уборка
# первого завершится раньше, чем начнётся тест. Поэтому `npx playwright test`
# запускается **дочерним процессом этого shell**, и уборка охватывает сам
# Playwright, а не только подготовку.
#
# Порядок фаз (он же контракт уборки):
#
#   preflight  статические инварианты, типы, доступность стенда, уборка остатков
#   generate   новые пароли на прогон
#   set        пароли в Keycloak через Admin API (там же заводятся учётные записи)
#   export     E2E_* в окружение этого процесса
#   setup      фикстуры, device_id, идемпотентная беседа      ← проект fixture
#   run        приёмка                                        ← проект acceptance
#   finally    logout в Keycloak → DELETE realtime_connections → DELETE sessions
#              → четыре нуля (2 в Postgres, 2 в Keycloak)
#
# Всё, что ходит внутрь кластера, ходит туда **не как браузер**: учётные данные
# администратора читаются из секрета через `kubectl`, счёты и удаление — через
# `kubectl exec` в под Postgres. Административные операции Keycloak идут по
# публичному имени — тому же, которым пойдёт браузер. Ни одного туннеля
# (`port-forward`) и ни одного `.svc.cluster.local`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB="$ROOT/apps/web"

NS="${NAMESPACE:-messenger}"
DB_POD="${DB_POD:-messenger-db-1}"
DB_CONTAINER="${DB_CONTAINER:-postgres}"
DB_NAME="${DB_NAME:-messenger}"
DB_USER="${DB_USER:-postgres}"

ADMIN_NS="${KEYCLOAK_NAMESPACE:-keycloak}"
ADMIN_SECRET="${KEYCLOAK_ADMIN_SECRET:-messenger-idp-initial-admin}"
REALM="${KEYCLOAK_REALM:-messenger}"

APP_ORIGIN="${E2E_BASE_URL:-https://app.finops.local}"
IDP_ORIGIN="${E2E_IDP_ORIGIN:-https://idp.finops.local}"

USER_A="${E2E_USER_A_EMAIL:-g3-web-e2e-a@finops.local}"
USER_B="${E2E_USER_B_EMAIL:-g3-web-e2e-b@finops.local}"

# Два фиксированных идентификатора устройства — неслучайные и разные. Случайный
# дал бы новую строку `devices` на каждый вход: `_device_from` при отсутствии
# заголовка чеканит uuid4. Разные — потому что идентификатор принадлежит
# устройству, а не человеку: общий на двоих означал бы, что вход второй стороны
# отбирает устройство у первой (`_device_for` заменяет занятый идентификатор).
DEVICE_A="${E2E_USER_A_DEVICE_ID:-1a7f0c8e-6d21-4b90-9c33-0d5f4a2b7e10}"
DEVICE_B="${E2E_USER_B_DEVICE_ID:-2b8e1d9f-7c32-4ca1-8d44-1e605b3c8f21}"

FIXTURE_ONLY="${E2E_FIXTURE_ONLY:-0}"

PASSWORD_A=""
PASSWORD_B=""
ADMIN_USER=""
ADMIN_PASSWORD=""
CLEANUP_ARMED=0
STATE_DIR=""
PLAYWRIGHT_CODE=0

step() { printf '\n\033[36m·\033[0m %s  \033[2m%s\033[0m\n' "$1" "$(date -u +%H:%M:%SZ)"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
die()  { bad "$1"; exit 1; }

# --- доступ к стенду и его хранилищам -------------------------------------

# Локальный удостоверяющий центр стенда: браузеру он не доверяет
# (`ignoreHTTPSErrors` в конфиге), и curl здесь ведёт себя так же. Имя при этом
# настоящее — подмены адреса нет, только доверие к сертификату.
probe() {
    local code
    code="$(curl -s -k -o /dev/null -w '%{http_code}' --max-time 10 "$1" 2>/dev/null || true)"
    printf '%s' "${code:-000}"
}

psql_q() {
    # `ON_ERROR_STOP` обязателен: без него psql продолжает после ошибки и
    # возвращает ноль — неудавшееся удаление выглядело бы как выполненное.
    kubectl -n "$NS" exec "$DB_POD" -c "$DB_CONTAINER" -- \
        psql -U "$DB_USER" -d "$DB_NAME" -qtA -v ON_ERROR_STOP=1 -c "$1"
}

# Закрытый список строится из двух конкретных адресов: отбора по шаблону имени
# здесь нет и быть не может — он задел бы чужие строки. `quote_literal` отдаёт
# готовые литералы, поэтому подстановка не зависит от того, что в адресе.
fixture_ids_sql() {
    psql_q "SELECT string_agg(quote_literal(user_id), ',')
              FROM users
             WHERE lower(email) IN ('$USER_A', '$USER_B')"
}

fixture_counts_sql() {
    local ids="$1"
    psql_q "SELECT (SELECT count(*) FROM realtime_connections WHERE user_id IN ($ids))
                 || ' '
                 || (SELECT count(*) FROM sessions WHERE user_id IN ($ids))"
}

all_counts_sql() {
    psql_q "SELECT (SELECT count(*) FROM realtime_connections)
                 || ' '
                 || (SELECT count(*) FROM sessions)"
}

browsers_ready() {
    local cache dir
    for cache in "${PLAYWRIGHT_BROWSERS_PATH:-}" "$HOME/Library/Caches/ms-playwright" "$HOME/.cache/ms-playwright"; do
        [ -n "$cache" ] || continue
        for dir in "$cache"/chromium*; do
            [ -d "$dir" ] && return 0
        done
    done
    return 1
}

# --- администратор Keycloak ------------------------------------------------
#
# Значения переносятся через окружение и stdin, никогда через аргументы
# команды: в списке процессов они были бы видны целиком, а расшифрованные —
# ещё и в журнале оболочки. Это не сетевой путь в кластер, а чтение секрета.
arm_admin() {
    local raw_user raw_password

    if ! raw_user="$(kubectl -n "$ADMIN_NS" get secret "$ADMIN_SECRET" \
        -o jsonpath='{.data.username}' 2>/dev/null)"; then
        die "секрет $ADMIN_NS/$ADMIN_SECRET недоступен: без администратора Keycloak фикстуры не завести"
    fi
    if ! raw_password="$(kubectl -n "$ADMIN_NS" get secret "$ADMIN_SECRET" \
        -o jsonpath='{.data.password}' 2>/dev/null)"; then
        die "в секрете $ADMIN_NS/$ADMIN_SECRET нет ключа password"
    fi

    ADMIN_USER="$(printf '%s' "$raw_user" | base64 -d)"
    ADMIN_PASSWORD="$(printf '%s' "$raw_password" | base64 -d)"

    [ -n "$ADMIN_USER" ] || die "имя администратора Keycloak пусто"
    CLEANUP_ARMED=1
    ok "учётные данные администратора прочитаны из секрета $ADMIN_NS/$ADMIN_SECRET"
}

kc() { # подкоманда; пароли — только через окружение
    IDP_ORIGIN="$IDP_ORIGIN" KEYCLOAK_REALM="$REALM" \
    KEYCLOAK_ADMIN="$ADMIN_USER" KEYCLOAK_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
    USER_A="$USER_A" USER_B="$USER_B" \
    PASSWORD_A="${PASSWORD_A:-}" PASSWORD_B="${PASSWORD_B:-}" \
    python3 - "$1" <<'PY'
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

command = sys.argv[1]
origin = os.environ["IDP_ORIGIN"].rstrip("/")
realm = os.environ["KEYCLOAK_REALM"]
users = {"A": os.environ["USER_A"], "B": os.environ["USER_B"]}
passwords = {"A": os.environ["PASSWORD_A"], "B": os.environ["PASSWORD_B"]}

# Локальный удостоверяющий центр стенда. Имя в запросе настоящее — подмены
# адреса нет, только доверие к сертификату.
tls = ssl.create_default_context()
tls.check_hostname = False
tls.verify_mode = ssl.CERT_NONE


def ok(what):
    print(f"  \033[32m✓\033[0m {what}")


def bad(what):
    print(f"  \033[31m✗\033[0m {what}")


def fail(what):
    bad(what)
    sys.exit(1)


def call(method, path, body=None, token=None):
    request = urllib.request.Request(
        origin + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            **({} if token is None else {"Authorization": f"Bearer {token}"}),
        },
    )
    try:
        with urllib.request.urlopen(request, context=tls, timeout=30) as response:
            payload = response.read()
            return response.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as error:
        payload = error.read()
        try:
            decoded = json.loads(payload) if payload else None
        except json.JSONDecodeError:
            decoded = payload.decode(errors="replace")
        return error.code, decoded


def admin_token():
    data = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": os.environ["KEYCLOAK_ADMIN"],
            "password": os.environ["KEYCLOAK_ADMIN_PASSWORD"],
        }
    ).encode()
    request = urllib.request.Request(
        f"{origin}/realms/master/protocol/openid-connect/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, context=tls, timeout=30) as response:
        return json.loads(response.read())["access_token"]


def find(token, email):
    query = urllib.parse.urlencode({"username": email, "exact": "true"})
    status, body = call("GET", f"/admin/realms/{realm}/users?{query}", token=token)
    if status != 200:
        fail(f"поиск {email} вернул {status}: {body}")
    return body[0] if body else None


def ensure(token, email, password):
    """Заводит учётную запись, если её нет, и ставит новый пароль.

    Пароль ставится на **каждый** прогон: в Keycloak остаётся credential, без
    которого следующий вход был бы невозможен, и он заменяется новым случайным.
    «На стенде пароля не остаётся» было бы неправдой, и так это и не
    формулируется.
    """
    user = find(token, email)
    created = user is None

    # Имя и фамилия — не украшение профиля, а **условие входа**, и это
    # измерено, а не предположено. Реалм объявляет их обязательными
    # (declarative user profile), и Keycloak 26 добавляет `VERIFY_PROFILE`
    # на входе, если они пусты, — при этом `requiredActions` учётной записи
    # остаётся пустым. Готовая на вид запись не доходит до `/callback`:
    # Keycloak уводит на `login-actions/required-action?execution=VERIFY_PROFILE`,
    # и приёмка падает таймаутом ожидания обмена кода, не называя причины.
    # Значение детерминировано и различает сторону: display_name в интерфейсе
    # читается («E2E A»), и адрес при этом остаётся единственным ключом.
    side = email.split("@", 1)[0].rsplit("-", 1)[-1].upper()
    profile = {"firstName": "E2E", "lastName": side}

    if created:
        status, body = call(
            "POST",
            f"/admin/realms/{realm}/users",
            {
                "username": email,
                "email": email,
                # Подтверждённым намеренно: реалм требует подтверждения адреса,
                # и без него Keycloak показал бы вместо кода страницу «проверьте
                # почту» — ровно как в tests/integration/login_check.py.
                "emailVerified": True,
                "enabled": True,
                "requiredActions": [],
                **profile,
                "credentials": [{"type": "password", "value": password, "temporary": False}],
            },
            token=token,
        )
        if status not in (201, 409):
            fail(f"создание {email} вернуло {status}: {body}")
        user = find(token, email)

    if user is None:
        fail(f"учётная запись {email} не найдена и не завелась")

    status, body = call(
        "PUT",
        f"/admin/realms/{realm}/users/{user['id']}/reset-password",
        {"type": "password", "value": password, "temporary": False},
        token=token,
    )
    if status not in (200, 204):
        fail(f"смена пароля {email} вернула {status}: {body}")

    # Представление берётся целиком из GET и правится точечно: `PUT` без
    # остальных полей затёр бы их, а неподтверждённый адрес вернул бы страницу
    # «проверьте почту» вместо кода.
    #
    # Имя и фамилия проверяются наравне с адресом: учётная запись, заведённая
    # прежней версией скрипта, чинится здесь же, а не заводится заново, — и
    # `requiredActions` тут ничего не сообщает, потому что Keycloak добавляет
    # `VERIFY_PROFILE` на входе, не отражая его в списке.
    needs_profile = not user.get("firstName") or not user.get("lastName")
    if needs_profile or not user.get("emailVerified") or user.get("requiredActions"):
        patched = dict(user)
        patched["emailVerified"] = True
        patched["requiredActions"] = []
        patched.update(profile)
        status, body = call("PUT", f"/admin/realms/{realm}/users/{user['id']}", patched, token=token)
        if status not in (200, 204):
            fail(f"подтверждение адреса и профиля {email} вернуло {status}: {body}")

    return user["id"], created


def live_sessions(token, user_id):
    status, body = call("GET", f"/admin/realms/{realm}/users/{user_id}/sessions", token=token)
    if status != 200:
        fail(f"чтение сессий вернуло {status}: {body}")
    return body


def drop_sessions(token, user_id):
    status, body = call("POST", f"/admin/realms/{realm}/users/{user_id}/logout", token=token)
    if status in (200, 204):
        return
    if status in (404, 405):
        # Запасной путь из той же области: на части сборок Keycloak 26 user
        # logout отвечает отказом. Инвариант тот же — ноль живых сессий, — и он
        # всё равно читается ниже, а не подразумевается.
        for session in live_sessions(token, user_id):
            revoked, detail = call(
                "DELETE", f"/admin/realms/{realm}/sessions/{session['id']}", token=token
            )
            if revoked not in (200, 204):
                fail(f"отзыв сессии {session['id']} вернул {revoked}: {detail}")
        return
    fail(f"logout пользователя вернул {status}: {body}")


token = admin_token()

if command == "prepare":
    for side in ("A", "B"):
        user_id, created = ensure(token, users[side], passwords[side])
        ok(f"{users[side]}: учётная запись {'заведена' if created else 'уже была'}, пароль заменён")

elif command == "logout":
    for side in ("A", "B"):
        user = find(token, users[side])
        if user is None:
            ok(f"{users[side]}: учётной записи нет — снимать нечего")
            continue
        before = len(live_sessions(token, user["id"]))
        drop_sessions(token, user["id"])
        after = len(live_sessions(token, user["id"]))
        if after:
            fail(f"{users[side]}: после logout осталось {after} сессий")
        ok(f"{users[side]}: сессий было {before}, осталось {after}")

elif command == "sessions":
    counts = {}
    for side in ("A", "B"):
        user = find(token, users[side])
        counts[side] = 0 if user is None else len(live_sessions(token, user["id"]))
        ok(f"Keycloak: живых сессий у {users[side]} — {counts[side]}")
    if any(counts.values()):
        fail(f"живые сессии Keycloak остались: {counts}")

else:
    fail(f"неизвестная команда: {command}")
PY
}

# --- уборка: порядок — часть контракта, а не деталь реализации --------------

db_wipe() {
    local ids
    if ! ids="$(fixture_ids_sql)"; then
        bad "не удалось прочитать фикстурные user_id из базы"
        return 1
    fi

    if [ -z "$ids" ]; then
        ok "Postgres: фикстур в базе ещё нет — чистить нечего"
        return 0
    fi

    # Одной транзакцией и в этом порядке: `realtime_connections` ссылается на
    # `sessions`, и обратный порядок нарушил бы ссылочную целостность. `users`
    # и `conversations` не трогаются никогда.
    if ! psql_q "BEGIN;
                 DELETE FROM realtime_connections WHERE user_id IN ($ids);
                 DELETE FROM sessions WHERE user_id IN ($ids);
                 COMMIT;"; then
        bad "удаление транзитных строк не выполнилось"
        return 1
    fi
    ok "Postgres: realtime_connections → sessions, одной транзакцией, по двум фикстурным user_id"
}

read_zeros() {
    local ids filtered all failed=0

    if ! ids="$(fixture_ids_sql)"; then
        bad "не удалось прочитать фикстурные user_id из базы"
        return 1
    fi

    if ! filtered="$(fixture_counts_sql "${ids:-NULL}")"; then
        bad "не удалось прочитать счёт по фикстурам"
        return 1
    fi
    if ! all="$(all_counts_sql)"; then
        bad "не удалось прочитать нефильтрованный счёт"
        return 1
    fi

    set -- $filtered
    local f_realtime="$1" f_sessions="$2"
    set -- $all
    local a_realtime="$1" a_sessions="$2"

    if [ "$f_realtime" = "0" ] && [ "$f_sessions" = "0" ]; then
        ok "Postgres по фикстурам: realtime_connections=$f_realtime, sessions=$f_sessions"
    else
        bad "Postgres по фикстурам: realtime_connections=$f_realtime, sessions=$f_sessions — не ноль"
        failed=1
    fi

    # Нефильтрованный счёт печатается рядом и в проверку не входит: это
    # состояние стенда, а не наш угол. Назвать только своё число значило бы
    # выдать счёт с фильтром за состояние стенда.
    ok "Postgres весь стенд (для честности, в проверку не входит): realtime_connections=$a_realtime, sessions=$a_sessions"

    kc sessions || failed=1

    return "$failed"
}

cleanup_all() {
    local failed=0

    # 1. Контексты закрывает тот, кто их открыл: подготовка закрывает свои,
    #    раннер — страничный. Здесь их нет вовсе — процесс Playwright либо ещё
    #    не запущен (preflight), либо уже завершён (`finally`). Остаток убитого
    #    прогона виден не здесь, а в счёте сессий ниже.
    ok "контексты Playwright: закрывает тот, кто открыл (подготовка и раннер)"

    # 2. Живые сессии Keycloak. Без этого шага вход следующего прогона
    #    подхватывает живую сессию, и `sessions` в Postgres растут снова —
    #    уборка была бы полумерой.
    kc logout || failed=1

    # 3. Postgres.
    db_wipe || failed=1

    # 4. Утверждение о нуле — чтение, а не заявление: четыре числа.
    read_zeros || failed=1

    return "$failed"
}

finish() {
    local code=$?
    trap - EXIT
    set +e

    # Провал уборки обязан менять код выхода, а не только журнал. Приёмка
    # обещает «четыре нуля», и прогон, оставивший за собой живые сессии или
    # строки в `sessions`, эту посылку нарушил: следующий вход подхватит
    # сессию Keycloak, а счёт разъедется. `warn` здесь выдавал бы провал
    # инварианта за успех, и достаточно было бы одного «зелёного» прогона,
    # чтобы грязный стенд стал нормой.
    local cleanup_failed=0

    if [ "$CLEANUP_ARMED" = "1" ]; then
        step "уборка (finally)"
        cleanup_all || cleanup_failed=1
    else
        step "уборка (finally)"
        # Уборки не было — значит нулей никто не предъявил. Это тот же
        # провал, а не замечание: войти в Keycloak не удалось, и чем чисто
        # ли остался стенд — неизвестно.
        warn "административный доступ не получен: убирать нечем"
        cleanup_failed=1
    fi

    [ -n "$STATE_DIR" ] && rm -rf "$STATE_DIR"

    if [ "$code" -ne 0 ]; then
        bad "прогон завершился с кодом $code"
        exit "$code"
    fi

    if [ "$cleanup_failed" -ne 0 ]; then
        bad "уборка завершилась неполно: стенд остаётся грязным — см. отметки выше"
        exit 1
    fi

    exit 0
}
trap finish EXIT

# --- preflight --------------------------------------------------------------

step "preflight: статические инварианты"

python3 - "$ROOT" "$WEB" <<'PY'
import json
import re
import sys
from pathlib import Path

root, web = Path(sys.argv[1]), Path(sys.argv[2])

required = [
    web / "playwright.config.ts",
    root / "tests/e2e/g3-005-web.spec.ts",
    root / "tests/e2e/fixture.setup.ts",
    root / "tests/e2e/README.md",
    root / "tests/e2e/tsconfig.json",
]
inspected = [web / "playwright.config.ts"] + sorted((root / "tests/e2e").glob("*.ts"))

FORBIDDEN = {
    ".svc.cluster.local": "проверка ушла внутрь кластера: приёмка идёт снаружи, публичными именами",
    "port-forward": "проверка ходит на стенд туннелем: браузер так не умеет",
    "localhost": "адрес стенда подменён публичным именем ненастоящим",
    "127.0.0.1": "адрес стенда подменён публичным именем ненастоящим",
}

violations = []
inspected_lines = 0


def code_lines(path):
    """Строки кода.

    Построчные комментарии отброшены намеренно: спека объясняет, **почему** в
    ней нет ни удаления, ни адресов кластера, и это объяснение не должно
    краснить собственную проверку. Правило названо, а не выведено: строкой
    запрещённый адрес не начинается, а `https://` внутри URL — начинается, и
    наивная резка по `//` пропустила бы ровно то, что проверка ищет.
    """
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith(("//", "*", "/*")):
            continue
        yield number, line


for path in required:
    if not path.exists():
        violations.append(f"{path.relative_to(root)}: файла нет")

code_by_file = {}
for path in inspected:
    lines = []
    for number, line in code_lines(path):
        inspected_lines += 1
        lines.append(line)
        for token, reason in FORBIDDEN.items():
            if token in line:
                violations.append(f"{path.relative_to(root)}:{number}: {token} — {reason}")
        # Регистр здесь не важен намеренно: `fetch(…, { method: "delete" })` — такое
        # же удаление, как SQL-оператор, и запрет у них один. Границы слова
        # оставляют в покое `deleted_at` и `previewDeleted`.
        if re.search(r"\bdelete\b", line, re.IGNORECASE):
            violations.append(
                f"{path.relative_to(root)}:{number}: оператор удаления — уборка живёт "
                f"в scripts/web-e2e-fixture.sh"
            )
    code_by_file[path] = lines

spec = root / "tests/e2e/g3-005-web.spec.ts"
# Проверяется **код**, а не текст файла. Слово, оставшееся в комментарии,
# зеленело бы ровно так же, как работающий локатор, — и дефект «беседа
# опознаётся по имени» прошёл бы молча, назвав себя в объяснении.
spec_code = "\n".join(code_by_file.get(spec, []))

# «Ни одного нарушения» неотличимо от «ни одного узла»: проверка, у которой
# нечего проверять, зеленеет так же, как выполненная.
if "test(" not in spec_code:
    violations.append("tests/e2e/g3-005-web.spec.ts: в спеке нет ни одного теста")
if "data-conversation-id" not in spec_code:
    violations.append(
        "tests/e2e/g3-005-web.spec.ts: беседа не опознаётся по атрибуту — "
        "по имени её может совпасть со стендовыми данными случайно"
    )

# Инвариант 0 проверяется по тексту Makefile и скрипта: нарушение здесь
# молчаливое. Цель `web-e2e`, вызывающая `web-e2e-fixture`, выглядит как «то же
# самое», а на деле это два процесса — окружение первого во второй не приедет,
# а его `finally` закончится раньше теста.
makefile = (root / "Makefile").read_text(encoding="utf-8")
orchestrator = (root / "scripts/web-e2e-fixture.sh").read_text(encoding="utf-8")

web_e2e = re.search(r"^web-e2e:(.*?)(?=^\S)", makefile, re.M | re.S)
if web_e2e is None:
    violations.append("Makefile: цели web-e2e нет — приёмка осталась без входа")
else:
    body = web_e2e.group(1)
    if "scripts/web-e2e-fixture.sh" not in body:
        violations.append("Makefile: web-e2e не запускает scripts/web-e2e-fixture.sh")
    # Вызов **цели**, а не подстрока: имя скрипта этой же цели (`scripts/web-e2e-fixture.sh`)
    # содержит те же слова, и проверка по подстроке краснела бы на верном Makefile.
    if re.search(r"(?<![/\w-])web-e2e-fixture(?!\.sh)", body):
        violations.append(
            "Makefile: web-e2e зовёт web-e2e-fixture — это два процесса: окружение "
            "первого во второй не передаётся, а его finally закончится раньше теста"
        )
    if re.search(r"^\t.*export E2E_", body, re.M):
        violations.append(
            "Makefile: E2E_* экспортируются прямо в цели — пароль обязан жить в оркестраторе"
        )

if "export E2E_USER_A_PASSWORD" not in orchestrator:
    violations.append(
        "scripts/web-e2e-fixture.sh: пароль не экспортируется в окружение — тест его не увидит"
    )
if 'playwright" "$@"' not in orchestrator:
    violations.append(
        "scripts/web-e2e-fixture.sh: playwright не запускается из этого же процесса — "
        "уборка перестанет охватывать тест"
    )
# Пароль уходит только окружением — значит в **строках вызова** его быть не
# должно. Проход по строкам, а не поиск по всему файлу: сам этот текст проверки
# содержит те же слова, и поиск по файлу нашёл бы собственную формулировку —
# детектор, краснеющий на себе, не различает ничего.
for line in orchestrator.splitlines():
    if "python3" not in line and not re.search(r"\bplaywright\b", line):
        continue
    if "$PASSWORD" in line or "--password" in line:
        violations.append(
            "scripts/web-e2e-fixture.sh: пароль стоит в строке вызова — он виден "
            "в списке процессов; пароль передаётся только окружением"
        )
        break


def installed(package):
    manifest = web / "node_modules" / package / "package.json"
    if not manifest.exists():
        return None
    return json.loads(manifest.read_text(encoding="utf-8"))["version"]


runner = installed("playwright")
test_runner = installed("@playwright/test")

if runner is None or test_runner is None:
    violations.append(
        "apps/web/node_modules: playwright или @playwright/test не установлен — "
        "спека написана на раннере, которого нет"
    )
elif runner != test_runner:
    violations.append(
        f"playwright {runner} ≠ @playwright/test {test_runner}: рантайм и раннер разъехались"
    )

declared = json.loads((web / "package.json").read_text(encoding="utf-8"))["devDependencies"].get(
    "@playwright/test"
)
if declared is None:
    violations.append("apps/web/package.json: @playwright/test отсутствует в devDependencies")
elif not re.fullmatch(r"\d+\.\d+\.\d+", declared):
    violations.append(
        f"apps/web/package.json: @playwright/test объявлен как {declared} — "
        f"версия должна быть точной, иначе рантайм и раннер разойдутся на следующем npm install"
    )
elif test_runner is not None and declared != test_runner:
    violations.append(
        f"apps/web/package.json объявляет @playwright/test {declared}, установлен {test_runner}"
    )

if inspected_lines == 0:
    violations.append("не просмотрено ни одной строки кода: так выглядит и пустой набор, и опечатка в пути")

if violations:
    for violation in violations:
        print(f"  \033[31m✗\033[0m {violation}")
    sys.exit(1)

print(
    f"  \033[32m✓\033[0m {len(inspected)} файлов кода, {inspected_lines} строк: "
    f"ни адресов кластера, ни туннелей, ни удаления"
)
print(
    f"  \033[32m✓\033[0m playwright {runner} == @playwright/test {test_runner} "
    f"(в манифесте объявлен {declared})"
)
PY

step "preflight: типы"

if ! "$WEB/node_modules/.bin/tsc" -p "$ROOT/tests/e2e/tsconfig.json"; then
    die "спека и подготовка не проходят проверку типов"
fi
ok "tests/e2e собирается: tsc -p tests/e2e/tsconfig.json без ошибок"

if ! browsers_ready; then
    die "браузер Playwright не установлен: npx playwright install --with-deps chromium (в apps/web)"
fi
ok "браузер Playwright на месте"

step "preflight: стенд отвечает по публичным именам"

app_code="$(probe "$APP_ORIGIN/")"
idp_code="$(probe "$IDP_ORIGIN/realms/$REALM/.well-known/openid-configuration")"

if [ "$app_code" = "200" ]; then
    ok "$APP_ORIGIN/ → $app_code"
else
    bad "$APP_ORIGIN/ → $app_code (нужен 200)"
fi
if [ "$idp_code" = "200" ]; then
    ok "$IDP_ORIGIN → $idp_code"
else
    bad "$IDP_ORIGIN → $idp_code (нужен 200)"
fi

if [ "$app_code" != "200" ] || [ "$idp_code" != "200" ]; then
    # Две причины называются рядом, потому что снаружи они выглядят одинаково:
    # `000` — это и «маршрута нет», и «имя некому обслужить».
    warn "нужен и проброс края на хост (docs/local-setup.md, шаг 7), и маршрут:"
    warn "  app.finops.local обслуживается только после коммита продвижения (срез 12)"
    die "стенд не отвечает по публичным именам — приёмка не начнётся"
fi

step "preflight: администратор Keycloak"
arm_admin

step "preflight: уборка остатков прошлого прогона"
cleanup_all || die "уборка остатков не выполнена: начинать приёмку на грязном стенде нельзя"

# --- generate / set / export ------------------------------------------------

step "generate: новые пароли на прогон"
PASSWORD_A="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
PASSWORD_B="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
ok "оба пароля длиной ${#PASSWORD_A} и ${#PASSWORD_B} знаков; в файл не пишутся"

step "set: учётные записи и пароли в Keycloak"
kc prepare

step "export: E2E_* в окружение этого процесса"
STATE_DIR="$(mktemp -d)"
export E2E_BASE_URL="$APP_ORIGIN"
export E2E_IDP_ORIGIN="$IDP_ORIGIN"
export E2E_STATE_FILE="$STATE_DIR/state.json"
export E2E_USER_A_EMAIL="$USER_A"
export E2E_USER_A_PASSWORD="$PASSWORD_A"
export E2E_USER_A_DEVICE_ID="$DEVICE_A"
export E2E_USER_B_EMAIL="$USER_B"
export E2E_USER_B_PASSWORD="$PASSWORD_B"
export E2E_USER_B_DEVICE_ID="$DEVICE_B"
ok "состояние прогона: $E2E_STATE_FILE (каталог снимается вместе с уборкой)"

# --- run --------------------------------------------------------------------

if [ "$FIXTURE_ONLY" = "1" ]; then
    step "setup: только подготовка фикстур (диагностическая цель)"
    set -- test --project=fixture
else
    step "приёмка: playwright test — дочерний процесс этого shell"
    set -- test
fi

# Запуск из каталога конфига: из корня репозитория `playwright test` не находит
# `playwright.config.ts` и собирает чужие `*.test.ts` — проверено прогоном.
# Бинарь раннера берётся из `node_modules` напрямую: `npx` может дотянуть
# пакет из сети, а приёмка обязана прогонять **установленный** раннер.
if ! ( cd "$WEB" && "$WEB/node_modules/.bin/playwright" "$@" ); then
    PLAYWRIGHT_CODE=1
    bad "прогон Playwright не прошёл"
fi

if [ "$PLAYWRIGHT_CODE" -ne 0 ]; then
    exit 1
fi

if [ "$FIXTURE_ONLY" = "1" ]; then
    ok "подготовка фикстур прошла; спека приёмки этой целью не запускается"
    # Цель диагностическая — значит она обязана показать то, ради чего её
    # позвали. Без этой печати состояние фикстур умирало бы вместе с каталогом,
    # и «посмотреть» означало бы «запустить и ничего не увидеть».
    if [ -f "$E2E_STATE_FILE" ]; then
        step "состояние фикстур"
        cat "$E2E_STATE_FILE"
    fi
else
    ok "приёмка прошла"
fi
