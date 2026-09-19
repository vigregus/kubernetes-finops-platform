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


def _typed(name, schema):
    """Параметр с заданной схемой: сравнение схемы без схемы не проверить."""
    return {"name": name, "in": "query", "schema": schema}


def _inline(*statuses):
    """Операция с inline-схемой ответа в каждом из перечисленных статусов."""
    return {
        "get": {
            "operationId": "op",
            "responses": {
                status: {"description": "ок", "content": {
                    "application/json": {"schema": {"type": "object"}},
                }}
                for status in statuses
            },
        }
    }


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
    assert inline == {"GET /things 200 application/json"}, inline
    assert validate.inline_responses(_doc()) == set()


def test_сужение_схемы_параметра_видно():
    # Параметр, оставшийся на месте, всё ещё может сломать вызывающего:
    # запрос, который вчера был законным, сегодня получает `400`. Классы
    # перечислены по одному, потому что каждый проверяется своим кодом,
    # и молчаливое выпадение любого из них — это дыра в проверке.
    cases = [
        ({"type": "string"}, {"type": "integer"}),
        ({"type": "integer"}, {"type": "integer", "maximum": 10}),
        ({"type": "string", "format": "date-time"}, {"type": "string", "format": "date"}),
        ({"type": "string", "format": "date-time"}, {"type": "string"}),
        ({"type": "string"}, {"type": "string", "format": "uuid"}),
        ({"type": "integer", "minimum": 1}, {"type": "integer", "minimum": 5}),
        ({"type": "integer", "maximum": 100}, {"type": "integer", "maximum": 50}),
        ({"type": "string", "enum": ["a", "b"]}, {"type": "string", "enum": ["a"]}),
        ({"type": "array", "maxItems": 10}, {"type": "array", "maxItems": 3}),
        ({"type": "array", "minItems": 1}, {"type": "array", "minItems": 4}),
    ]
    for was, became in cases:
        findings = validate.parameter_findings(
            _doc(parameters=[_typed("p", was)]),
            _doc(parameters=[_typed("p", became)]),
        )
        assert [kind for kind, _, _ in findings] == ["parameter"], (was, became, findings)
        assert "p" in findings[0][2], findings


def test_сужение_схемы_параметра_объявленной_ссылкой_видно():
    # Схема параметра — та же ссылка, что и у поля: спрятать её в компоненту
    # нельзя, иначе разрыв становился бы тем незаметнее, чем аккуратнее
    # написан контракт.
    old = _doc(parameters=[_typed("p", {"$ref": "#/components/schemas/Bounded"})])
    new = _doc(parameters=[_typed("p", {"$ref": "#/components/schemas/Bounded"})])
    old["components"] = {"schemas": {"Bounded": {"type": "integer", "maximum": 100}}}
    new["components"] = {"schemas": {"Bounded": {"type": "integer", "maximum": 10}}}
    findings = validate.parameter_findings(old, new)
    assert [kind for kind, _, _ in findings] == ["parameter"], findings


def test_подмена_ссылки_ответа_видна():
    # Обе компоненты на месте, поля в них не менялись — клиент при этом
    # получает другой тип. Заметить это, кроме сравнения ссылок, нечем.
    old = _doc(response={"$ref": "#/components/schemas/Page"})
    new = _doc(response={"$ref": "#/components/schemas/Other"})
    findings = validate.response_findings(old, new)
    assert [kind for kind, _, _ in findings] == ["response"], findings
    assert findings[0][1] == "GET /things 200 application/json", findings
    assert "Page → #/components/schemas/Other" in findings[0][2], findings


def test_ответ_объявленный_компонентой_сравнивается():
    # У узла-ссылки нет ни `content`, ни схемы: без разыменования ответ
    # не участвовал бы в сравнении вовсе, и смена компоненты в `400`
    # прошла бы молча — ровно то, что было с параметром до этой же правки.
    def document(component):
        doc = _doc()
        doc["paths"]["/things"]["get"]["responses"] = {
            "400": {"$ref": "#/components/responses/Problem"},
        }
        doc["components"] = {
            "responses": {"Problem": {"description": "нет", "content": {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{component}"},
                },
            }}},
        }
        return doc

    assert list(validate.response_schemas(document("Problem"))) == [
        "GET /things 400 application/json"
    ]
    findings = validate.response_findings(document("Problem"), document("Other"))
    assert [kind for kind, _, _ in findings] == ["response"], findings


def test_ответ_ставший_inline_виден():
    # Движение в обратную сторону — потеря сравнимости: с этого момента
    # разрыв в полях ответа не увидит никто.
    old = _doc(response={"$ref": "#/components/schemas/Page"})
    findings = validate.response_findings(old, _doc(response={"type": "object"}))
    assert [kind for kind, _, _ in findings] == ["response"], findings


def test_второй_inline_ответ_в_разрешённой_операции_виден():
    # Разрешение выдаётся ответу, а не операции. Пока ключом была пара
    # «метод + путь», запись про `GET /sessions` разрешала и второй
    # inline-ответ, добавленный рядом: множество найденного не менялось.
    document = {"openapi": "3.1.0", "paths": {"/sessions": _inline("200")}}
    assert validate.inline_responses(document) <= set(validate.INLINE_RESPONSE_SCHEMAS)

    document["paths"]["/sessions"] = _inline("200", "503")
    new = validate.inline_responses(document) - set(validate.INLINE_RESPONSE_SCHEMAS)
    assert new == {"GET /sessions 503 application/json"}, new


def test_закрытый_список_называет_ответы_а_не_операции():
    # Список сверяется с самим контрактом: сокращённая до «метод + путь»
    # запись перестанет совпадать с найденным, и `check_structure` потребует
    # её удалить. Проверка сторожит ровно то, ради чего список закрыт, —
    # что закрыт он по ответам.
    contract = validate.load(validate.OPENAPI)
    assert validate.inline_responses(contract) == set(validate.INLINE_RESPONSE_SCHEMAS)


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


def test_расширение_схемы_параметра_не_разрыв():
    # Проверка, находящая любое изменение схемы, запретила бы менять
    # контракт вовсе. Расширение оставляет вчерашний запрос законным.
    cases = [
        ({"type": "string"}, {"type": ["string", "null"]}),
        ({"type": "string", "enum": ["a"]}, {"type": "string"}),
        ({"type": "integer", "minimum": 5}, {"type": "integer", "minimum": 1}),
        ({"type": "integer", "maximum": 50}, {"type": "integer", "maximum": 100}),
        ({"type": "integer"}, {"type": ["integer", "null"]}),
    ]
    for was, became in cases:
        findings = validate.parameter_findings(
            _doc(parameters=[_typed("p", was)]),
            _doc(parameters=[_typed("p", became)]),
        )
        assert findings == [], (was, became, findings)


def test_вынос_inline_ответа_в_компоненту_не_разрыв():
    # Сравнивать inline-ответ не с чем, и запрещать его вынос — значит
    # наказывать за движение в ту сторону, куда велит сама проверка.
    old = _doc(response={"type": "object"})
    new = _doc(response={"$ref": "#/components/schemas/Page"})
    assert validate.response_findings(old, new) == []


def test_подмена_компоненты_с_тем_же_телом_не_разрыв():
    # `Unauthorized` и `NotFound` отличаются статусом и описанием, тело
    # у обоих `Problem`. Сравнивается схема тела, поэтому подмена одной
    # на другую разрывом не считается — иначе проверка запрещала бы
    # изменение, которого сгенерированный клиент не замечает.
    def document(name):
        doc = _doc()
        doc["paths"]["/things"]["get"]["responses"] = {
            "401": {"$ref": f"#/components/responses/{name}"},
        }
        body = {"description": "нет", "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/Problem"},
            },
        }}
        doc["components"] = {
            "responses": {key: dict(body) for key in ("Unauthorized", "NotFound")},
        }
        return doc

    assert validate.response_findings(document("Unauthorized"), document("NotFound")) == []


def test_новый_статус_ответа_не_разрыв():
    # Обход идёт по старой ревизии: ответ, которого вчера не было, старого
    # вызывающего не ломает.
    old = _doc(response={"$ref": "#/components/schemas/Page"})
    new = _doc(response={"$ref": "#/components/schemas/Page"})
    new["paths"]["/things"]["get"]["responses"]["503"] = {
        "description": "нет", "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Page"}},
        },
    }
    assert validate.response_findings(old, new) == []


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
