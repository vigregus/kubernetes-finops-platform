"""Проверки доменных правил. Без базы, без сети, без поднятого приложения.

Ровно то, ради чего слой `domain` не импортирует ничего из проекта.
"""
from __future__ import annotations

import uuid

import pytest

from messenger.domain.errors import Problem, Reason, Visibility, to_problem
from messenger.domain.ids import UserId, direct_key

A = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
B = UserId(uuid.UUID("22222222-2222-2222-2222-222222222222"))


# --- ключ беседы один-на-один ----------------------------------------------

def test_ключ_не_зависит_от_порядка_участников():
    """Гонка встречного создания решается здесь, а не блокировкой.

    Если порядок влияет, два одновременных запроса дают два разных ключа
    и две беседы — уникальный индекс их не остановит.
    """
    assert direct_key(A, B) == direct_key(B, A)


def test_ключ_различает_разные_пары():
    C = UserId(uuid.UUID("33333333-3333-3333-3333-333333333333"))
    assert direct_key(A, B) != direct_key(A, C)


def test_беседа_с_самим_собой_невозможна():
    with pytest.raises(ValueError):
        direct_key(A, A)


# --- отказ неотличим от отсутствия -----------------------------------------

def test_чужая_беседа_выглядит_как_отсутствующая():
    """Инвариант И-2.

    403 подтверждает существование беседы. Перебором идентификаторов так
    выясняется, кто с кем переписывается, без единого прочитанного слова.
    """
    assert to_problem(Reason.NOT_A_MEMBER).status == 404
    assert to_problem(Reason.CONVERSATION_NOT_FOUND).status == 404


def test_ответы_неотличимы_полностью():
    # Не только код, но и тело: разный текст выдаёт то же самое.
    assert to_problem(Reason.NOT_A_MEMBER) == to_problem(Reason.CONVERSATION_NOT_FOUND)


def test_403_только_когда_субъект_и_так_знает():
    """Своя беседа, из которой вышел, — там 403 понятнее и ничего не выдаёт."""
    assert to_problem(Reason.NOT_A_MEMBER, Visibility.KNOWN).status == 403


def test_скрытность_по_умолчанию():
    """Умолчание в другую сторону стоит утечки графа общения."""
    assert to_problem(Reason.NOT_A_MEMBER) == to_problem(Reason.NOT_A_MEMBER, Visibility.HIDDEN)


# --- прочая таксономия ------------------------------------------------------

def test_идемпотентный_повтор_не_ошибка():
    assert to_problem(Reason.DUPLICATE_MESSAGE).status == 200


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        (Reason.RATE_LIMITED, 429),
        (Reason.PAYLOAD_TOO_LARGE, 413),
        (Reason.UNSUPPORTED_MEDIA_TYPE, 415),
        (Reason.ATTACHMENT_NOT_READY, 409),
        (Reason.UNAUTHENTICATED, 401),
        (Reason.UPSTREAM_UNAVAILABLE, 503),
    ],
)
def test_остальные_причины_отображаются(reason, status):
    assert to_problem(reason).status == status


def test_каждая_причина_имеет_отображение():
    """Новая причина без отображения не должна превращаться в 500 незаметно.

    Тест падает при добавлении причины в перечисление — это и есть
    напоминание дописать строку, а не обнаружить её в проде.
    """
    for reason in Reason:
        problem = to_problem(reason)
        assert isinstance(problem, Problem)
        if reason is not Reason.INTERNAL:
            assert problem.status != 500, f"у причины {reason.value} нет отображения"
