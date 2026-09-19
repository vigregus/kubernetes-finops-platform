#!/usr/bin/env python3
"""Проверка самой проверки контрактов.

`CTR-001` блокирует слияние, но его собственное поведение до этого файла
ничем не закреплялось: удали из `validate.py` сравнение параметров запроса —
и сборка осталась бы зелёной ровно так же, как была зелёной до того, как
сравнивать их научились. Разрыв в параметре прошёл бы молча и в этот раз,
и в следующий. Проверка, которую нельзя проверить, — это тот же зелёный
CTR, который ничего не доказывает, только на уровень выше.

Поэтому здесь не контракт, а маленькие словари: тесту нужно не то, что
сейчас написано в `openapi.yaml`, а то, что находка вообще возникает.
Проверки, которые обязаны находиться, и проверки, которые находиться
не обязаны, перечислены рядом — второй список не менее важен: проверка,
находящая всё, запретила бы любое изменение контракта.

Запускается обычным `python3`, без pytest: у проверки контрактов нет
зависимостей, кроме `pyyaml`, и заводить ей вторую систему тестов ради
нескольких функций значило бы платить установкой пакетов на каждой правке.
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate  # noqa: E402 - путь к модулю задан строкой выше


def _path_item(*, parameters=(), responses=None):
    """Минимальная операция: тесту нужны параметры и ответ, не больше."""
    return {
        "get": {
            "operationId": "op",
            "parameters": list(parameters),
            "responses": responses or {"200": {"description": "ок"}},
        }
    }


def _payload(*names):
    return [
        {"name": name, "in": "query", "schema": {"type": "string"}}
        for name in names
    ]


def _doc(*, parameters=(), component_parameters=None, response=None):
    doc = {"openapi": "3.1.0", "paths": {"/things": _path_item(
        parameters=parameters,
        responses={"200": {"description": "ок", "content": {
            "application/json": {"schema": response or {"$ref": "#/components/schemas/Page"}},
        }}},
    )}}
    if component_parameters is not None:
        doc["components"] = {"parameters": component_parameters}
    return doc


# --- то, что обязано находиться -------------------------------------------

def test_удаление_параметра_запроса_видно():
    old = _doc(parameters=_payload("before_activity_at", "before_conversation_id"))
    new = _doc(parameters=_payload("before_activity_at"))
    findings = validate.parameter_findings(old, new)
    assert findings == [(
        "parameter",
        "GET /things query:before_conversation_id",
        "параметр GET /things query:before_conversation_id удалён",
    )], findings


def test_параметр_ставший_обязательным_виден():
    # Обязательный параметр — новое требование к вызывающему: запрос,
    # который вчера был полным, сегодня получает `400`.
    old = _doc(parameters=[{"name": "before_activity_at", "in": "query"}])
    new = _doc(parameters=[
        {"name": "before_activity_at", "in": "query", "required": True},
    ])
    findings = validate.parameter_findings(old, new)
    assert [kind for kind, _, _ in findings] == ["parameter"], findings
    assert "стал обязательным" in findings[0][2], findings


def test_удаление_параметра_объявленного_ссылкой_видно():
    # Параметр в компоненте — тот же параметр: спрятать его от сравнения
    # вынесением в `components.parameters` нельзя, иначе разрыв в нём
    # становился бы тем незаметнее, чем аккуратнее написан контракт.
    ref = {"$ref": "#/components/parameters/Cursor"}
    cursor = {"name": "before_conversation_id", "in": "query"}
    old = _doc(parameters=[ref], component_parameters={"Cursor": cursor})
    new = _doc(parameters=[], component_parameters={"Cursor": cursor})
    findings = validate.parameter_findings(old, new)
    assert [kind for kind, _, _ in findings] == ["parameter"], findings


def test_inline_ответ_виден_а_ссылка_нет():
    # Поля inline-ответа для сравнения не существуют, и это не мнение:
    # их негде взять — у схемы нет имени. Поэтому новый inline-ответ
    # объявлен ошибкой структуры, а ответы, объявленные ссылкой, ею
    # не считаются — иначе правило запрещало бы правильное.
    inline = validate.inline_responses(_doc(response={"type": "object"}))
    assert inline == {"GET /things"}, inline
    assert validate.inline_responses(_doc()) == set()


# --- то, что находиться не обязано ----------------------------------------

def test_добавление_параметра_не_разрыв():
    # Новый необязательный параметр старый вызывающий не замечает.
    old = _doc(parameters=_payload("limit"))
    new = _doc(parameters=_payload("limit", "before_activity_at"))
    assert validate.parameter_findings(old, new) == []


def test_снятие_обязательности_не_разрыв():
    # Ослабление требования ломать нечего: вчерашний запрос остаётся
    # законным и сегодня.
    old = _doc(parameters=[
        {"name": "before_activity_at", "in": "query", "required": True},
    ])
    new = _doc(parameters=[{"name": "before_activity_at", "in": "query"}])
    assert validate.parameter_findings(old, new) == []


def test_разные_места_одного_имени_не_путаются():
    # `before_conversation_id` в пути и в строке запроса — разные параметры;
    # удаление одного не должно выглядеть как удаление другого.
    old = _doc(parameters=[
        {"name": "before_conversation_id", "in": "path"},
        {"name": "before_conversation_id", "in": "query"},
    ])
    new = _doc(parameters=[{"name": "before_conversation_id", "in": "query"}])
    findings = validate.parameter_findings(old, new)
    assert [name for _, name, _ in findings] == [
        "GET /things path:before_conversation_id"
    ], findings


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = []
    for test in tests:
        try:
            test()
        except AssertionError:
            failed.append(test.__name__)
            print(f"  ✗ {test.__name__}")
            traceback.print_exc(limit=1)
        else:
            print(f"  · {test.__name__}")

    if failed:
        print(f"\nпроверка контрактов сломана: {len(failed)} из {len(tests)}")
        return 1
    print(f"\nпроверка контрактов проверена: {len(tests)} проверок")
    return 0


if __name__ == "__main__":
    sys.exit(main())
