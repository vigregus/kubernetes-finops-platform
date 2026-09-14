# Образ API. Собирается один раз, дальше везде ссылаются на его digest —
# это DEP-001, и чарт откажется собраться с подвижным тегом.
#
# Базовый образ тоже закрепляется по digest, и по той же причине:
# python:3.12-slim сегодня и через месяц — разные байты, и тогда
# «пересобрали из того же коммита» перестаёт означать «то же самое».
#
# Значение приходит аргументом без значения по умолчанию: сборка без него
# падает. Разрешает и записывает его scripts/build-messenger.sh в файл
# base-image.lock — так digest попадает в git из реестра, а не из головы.

ARG BASE_DIGEST
FROM python@${BASE_DIGEST} AS build

WORKDIR /build

# Зависимости отдельным слоем: код меняется каждую сборку, зависимости —
# редко, и без этого разделения каждая сборка тянет их заново.
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


ARG BASE_DIGEST
FROM python@${BASE_DIGEST}

# Тот же идентификатор, что в securityContext чарта. Расхождение даёт
# «permission denied» на томе, которое выглядит как ошибка тома.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin messenger

COPY --from=build /install /usr/local
COPY messenger/ /app/messenger/

WORKDIR /app
USER 10001

# Корень только для чтения — так объявлено в чарте. Python не должен пытаться
# писать .pyc рядом с исходниками: это не оптимизация, а отказ при старте.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

EXPOSE 8080

# Один воркер в контейнере. Масштабирование — репликами, а не процессами
# внутри пода: иначе число реплик в чарте перестаёт означать ёмкость,
# а метрики пода складывают несколько независимых процессов.
CMD ["uvicorn", "messenger.api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--no-access-log"]
