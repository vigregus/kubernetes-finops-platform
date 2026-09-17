#!/bin/sh
# Запускает все проверки по очереди и падает, если упала хотя бы одна.
#
# Лежит файлом, а не строкой в аргументе пода: строка проходит через
# оболочку хоста, и `$f` в ней раскрывается там, а не в контейнере.
# Первая редакция на этом и сломалась - под поднял uvicorn вместо проверок.
rc=0
for f in /checks/*_check.py; do
    name="$(basename "$f")"
    if [ -n "${INTEGRATION_ONLY:-}" ]; then
        case ",$INTEGRATION_ONLY," in
            *",$name,"*) ;;
            *) continue ;;
        esac
    fi
    echo "  -- $name"
    python "$f" || rc=1
done
exit $rc
