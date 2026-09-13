#!/usr/bin/env sh
# Исполнитель миграций. Нумерованные файлы, таблица применённых версий,
# консультативная блокировка.
#
# Блокировка - не перестраховка. Джоб миграции запускается вместе с выкаткой,
# а выкаток может быть две одновременно: откат поверх релиза, повтор упавшего
# джоба, ручной запуск во время автоматического. Два процесса, применяющих
# CREATE TABLE параллельно, оставят базу в состоянии, из которого выходят
# руками.
#
# Каждый файл применяется в одной транзакции вместе с записью его версии:
# «применён наполовину» - невозможное состояние.
set -eu

DIR="${MIGRATIONS_DIR:-$(dirname "$0")/migrations}"
# 4 019 — произвольное, но постоянное число: важно лишь, чтобы все
# исполнители брали одну и ту же блокировку.
LOCK_ID="${MIGRATION_LOCK_ID:-4019}"

: "${DATABASE_URL:?нужен DATABASE_URL}"

psql_q() {
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -qtA "$@"
}

psql_q -c "
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    checksum    text        NOT NULL
);" >/dev/null

applied="$(psql_q -c "SELECT version FROM schema_migrations ORDER BY version;")"

is_applied() {
    echo "$applied" | grep -qx "$1"
}

checksum_of() {
    # sha256sum есть в alpine, shasum - в macOS; на чужой машине скрипт
    # не должен молча посчитать пустую строку.
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        echo "нет sha256sum и shasum" >&2
        exit 1
    fi
}

pending=0
for file in "$DIR"/*.sql; do
    [ -e "$file" ] || { echo "миграций не найдено в $DIR" >&2; exit 1; }
    version="$(basename "$file" .sql)"
    sum="$(checksum_of "$file")"

    if is_applied "$version"; then
        # Уже применённый файл изменился - значит, у кого-то на машине
        # схема не та, что в git. Молча пропустить хуже, чем упасть.
        recorded="$(psql_q -c "SELECT checksum FROM schema_migrations WHERE version = '$version';")"
        if [ "$recorded" != "$sum" ]; then
            echo "миграция $version изменилась после применения" >&2
            echo "  в базе:  $recorded" >&2
            echo "  в файле: $sum" >&2
            exit 1
        fi
        echo "= $version уже применена"
        continue
    fi

    echo "+ $version применяется"
    # Блокировка берётся внутри той же сессии, что и миграция, и снимается
    # вместе с ней. pg_advisory_xact_lock ждёт, а не отказывает: параллельный
    # джоб должен дождаться и увидеть версию применённой, а не упасть.
    # --output=/dev/null: DDL ничего не печатает, а взятие блокировки
    # печатает строку результата, которая в журнале выкатки только мешает.
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q --single-transaction --output=/dev/null \
        -c "SELECT pg_advisory_xact_lock($LOCK_ID);" \
        -f "$file" \
        -c "INSERT INTO schema_migrations (version, checksum) VALUES ('$version', '$sum')
            ON CONFLICT (version) DO NOTHING;"
    pending=$((pending + 1))
done

echo "готово, применено новых: $pending"
