# Образ веб-клиента. Три стадии: клиент из контракта, статика, её отдача.
#
# Контекст сборки — КОРЕНЬ репозитория, а не apps/web: контракт лежит в
# packages/contracts/openapi.yaml, то есть выше приложения. Формулировка одна
# на весь план, и она не «..»: `..` зависит от текущего каталога и читается
# по-разному из apps/web, из корня и из CI.
#
#   docker build -f apps/web/Dockerfile .
#
# Ни у одного основания нет значения по умолчанию: сборка без --build-arg
# падает, а не подставляет «что-нибудь похожее» (DEP-001). Значения читает
# scripts/build-web.sh из apps/web/base-image.lock — один источник для
# человека и для конвейера, поэтому локальная сборка и CI собирают одно и то
# же, а не «то же самое на вид».

ARG GENERATOR_DIGEST
ARG NODE_DIGEST
ARG NGINX_DIGEST


# --- стадия 1: клиент из контракта -----------------------------------------
#
# Тот же digest генератора и тот же apps/web/openapi-generator.yaml, что и у
# хостовой генерации. Один бинарник — не одна конфигурация: расхождение
# параметров дало бы двух разных клиентов из одного контракта, и заметить это
# было бы нечем (B24).
FROM --platform=$BUILDPLATFORM openapitools/openapi-generator-cli@${GENERATOR_DIGEST} AS codegen

WORKDIR /work

COPY apps/web/openapi-generator.yaml /work/apps/web/openapi-generator.yaml
COPY packages/contracts/openapi.yaml /work/packages/contracts/openapi.yaml

# ENTRYPOINT образа на `RUN` не действует: `RUN` исполняется shell-формой, а
# не `docker run`, поэтому без явного вызова скрипта здесь искали бы команду
# `generate` в PATH и не нашли бы её.
RUN /usr/local/bin/docker-entrypoint.sh generate \
      -i /work/packages/contracts/openapi.yaml \
      -c /work/apps/web/openapi-generator.yaml \
      -o /work/apps/web/src/api/generated \
      --global-property apiDocs=false,modelDocs=false,apiTests=false,modelTests=false


# --- стадия 2: сборка статики ----------------------------------------------
#
# Тоже на платформе сборки: результат — статика, одинаковая для amd64 и
# arm64. Без этого Node-сборка исполнялась бы через QEMU на каждой целевой
# архитектуре, и две архитектуры стоили бы как две медленных сборки вместо
# одной быстрой плюс одной дешёвой (B19).
FROM --platform=$BUILDPLATFORM node@${NODE_DIGEST} AS build

WORKDIR /app

# Зависимости отдельным слоем: код меняется каждую сборку, зависимости —
# редко. `npm ci` — по закоммиченному lock, а не `install`: сборка обязана
# воспроизводиться из того, что лежит в git, а не из того, что сейчас в
# реестре пакетов.
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

COPY apps/web/ ./

# Клиент кладётся ПОСЛЕ дерева: иначе он был бы затёрт копией (в контексте
# его нет — Dockerfile.dockerignore исключает generated/, — но порядок должен
# быть верен сам по себе, а не совпадением двух обстоятельств).
COPY --from=codegen /work/apps/web/src/api/generated ./src/api/generated

# Именно build:app, а не build. `npm run build` зовёт api:generate, а тот —
# docker: внутри Node-стадии демона нет, и вызов был бы не медленным, а
# невозможным. Проверяется статически, scripts/check-web-dockerfile.py.
RUN npm run build:app


# --- стадия 3: статика на nginx --------------------------------------------
FROM nginx@${NGINX_DIGEST}

# Тот же идентификатор, что в securityContext чарта: расхождение даёт
# «permission denied» на записи, которое выглядит как ошибка тома.
COPY apps/web/nginx.conf /etc/nginx/nginx.conf

COPY --from=build /app/dist /usr/share/nginx/html
RUN chown -R 10001:10001 /usr/share/nginx/html

# Корень только для чтения. nginx по умолчанию пишет pid и временные файлы
# рядом с собой — все эти пути уведены в /tmp, который чарт даёт томом.
USER 10001

EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]
