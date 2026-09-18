"""Общее для тестов точек входа: проверка формы ответа об ошибке.

Форма одна на весь API (RFC 9457), и проверять её пересказом в каждом
файле значило бы получить пять разных представлений о том, что сервер
обязан отдавать. Тогда расхождение схемы и ответа, ради устранения
которого эта форма и заведена, вернулось бы через тесты.
"""
from __future__ import annotations

from typing import Protocol

import pytest


class ПроверкаОтказа(Protocol):
    def __call__(self, response, *, status: int, code: str) -> dict[str, object]: ...


@pytest.fixture
def отказ() -> ПроверкаОтказа:
    """Ответ об ошибке соответствует схеме `Problem` из контракта."""

    def _проверить(response, *, status: int, code: str) -> dict[str, object]:
        assert response.status_code == status
        # Тип содержимого отдельный: по нему посредник отличает описание
        # отказа от полезного ответа, не разбирая тело.
        assert response.headers["content-type"].startswith("application/problem+json")

        body = response.json()
        # Обязательные поля схемы. `code` не обязателен по RFC, но по нему
        # клиент различает виды отказа — без него остаётся только код
        # состояния, а `403` выдают три разные причины.
        assert body["type"].startswith("https://")
        assert isinstance(body["title"], str) and body["title"]
        assert body["status"] == status
        assert body["code"] == code

        # Трасса в теле — та же, что в заголовке. Разойдись они, поиск
        # по журналу приводил бы к чужому запросу, и это выглядело бы
        # как найденный ответ.
        assert body["trace_id"] == response.headers["X-Trace-Id"]
        return body

    return _проверить
