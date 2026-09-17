"""Точки входа: cookie, источник запроса и коды ответа.

Ни базы, ни Keycloak: сервис входа подменяется, потому что проверяется
не он, а обработчик — куда попадает токен обновления, что уходит в тело
и чем отличается «клиент не прав» от «мы не смогли».
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from messenger.api import main
from messenger.api.main import app
from messenger.services import backchannel as backchannel_service
from messenger.services import login as login_service

# Значения латиницей не для красоты: cookie и заголовки кодируются
# в latin-1, и кириллица в них роняет ответ. Настоящие токены Keycloak —
# base64url, то есть ASCII.
ТЕЛО = {
    "code": "authorization-code",
    "code_verifier": "pkce-verifier",
    "redirect_uri": "https://app.finops.local/callback",
}


class FakeRuntime:
    """Владелец соединений, который никуда не ходит."""

    login = None
    keys = None
    oidc_settings = None
    backchannel_audience = "messenger-web"
    centrifugo = None

    @asynccontextmanager
    async def connection(self):
        yield None


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def runtime():
    original = app.state.runtime
    app.state.runtime = FakeRuntime()
    yield
    app.state.runtime = original


@pytest.fixture
def успех(monkeypatch):
    async def _fake(conn, **kwargs):
        return login_service.LoginResult(
            access_token="access-token",
            refresh_token="refresh-token",
            expires_in=300,
            refresh_expires_in=604800,
            device_id=None,
        )

    monkeypatch.setattr(login_service, "login_with_code", _fake)
    monkeypatch.setattr(login_service, "refresh_access", _fake)


def _отказ(monkeypatch, *, upstream: bool):
    async def _fake(conn, **kwargs):
        return login_service.LoginResult(
            error_class="invalid_grant", upstream_failed=upstream
        )

    monkeypatch.setattr(login_service, "login_with_code", _fake)
    monkeypatch.setattr(login_service, "refresh_access", _fake)


def test_токен_обновления_уходит_в_cookie_а_не_в_тело(client, успех):
    """Главное свойство схемы хранения (ADR 0005).

    Токен обновления в теле означал бы, что скрипт на странице может его
    прочитать и унести наружу, — разница между «навредил, пока был
    на странице» и «получил доступ навсегда».
    """
    r = client.post("/auth/callback", json=ТЕЛО)
    assert r.status_code == 200
    assert r.json()["access_token"] == "access-token"
    # Токен обновления в теле означал бы, что скрипт на странице его прочитает.
    assert "refresh-token" not in r.text

    установка = r.headers["set-cookie"]
    assert main.REFRESH_COOKIE in установка
    assert "HttpOnly" in установка
    assert "SameSite=strict" in установка.replace("SameSite=Strict", "SameSite=strict")
    # Путь узкий: на обычные запросы API браузер cookie не приложит.
    assert f"Path={main.REFRESH_COOKIE_PATH}" in установка


def test_чужой_источник_отклонён(client, успех):
    """Cookie браузер прикладывает сам, поэтому источник проверяется."""
    r = client.post("/auth/callback", json=ТЕЛО, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_свой_источник_разрешён(client, успех):
    r = client.post("/auth/callback", json=ТЕЛО, headers={"Origin": main.WEB_ORIGIN})
    assert r.status_code == 200


def test_обновление_без_cookie_не_ходит_в_keycloak(client, monkeypatch):
    """Отсутствие cookie — это отказ сразу, а не попытка обмена.

    Иначе точка обмена отвечает на каждый запрос обращением к Keycloak,
    то есть превращается в усилитель для того, кто её нагружает.
    """
    async def _не_должно_вызваться(conn, **kwargs):
        raise AssertionError("обмен начался без cookie")

    monkeypatch.setattr(login_service, "refresh_access", _не_должно_вызваться)
    assert client.post("/auth/refresh").status_code == 401


def test_неудачный_обмен_снимает_cookie(client, monkeypatch):
    """Нерабочая cookie обрекала бы вкладку на отказ при каждой перезагрузке."""
    _отказ(monkeypatch, upstream=False)
    # Путь здесь корневой, а не настроенный: в тесте перед приложением нет
    # шлюза, который снимает префикс `/api/v1`, и по настоящему пути клиент
    # cookie просто не приложил бы.
    client.cookies.set(main.REFRESH_COOKIE, "stale-token", path="/")
    r = client.post("/auth/refresh")
    assert r.status_code == 401
    assert "Max-Age=0" in r.headers["set-cookie"] or "expires=" in r.headers["set-cookie"].lower()


def test_недоступный_keycloak_это_503_а_не_401(client, monkeypatch):
    """Клиент ни в чём не виноват, и просить его войти заново бессмысленно."""
    _отказ(monkeypatch, upstream=True)
    assert client.post("/auth/callback", json=ТЕЛО).status_code == 503


def test_тело_запроса_проверяется_по_контракту(client, успех):
    """Пустой verifier — это вход без PKCE, то есть без защиты кода."""
    r = client.post("/auth/callback", json={**ТЕЛО, "code_verifier": ""})
    assert r.status_code == 422


def test_backchannel_logout_без_токена_отклонён(client, monkeypatch):
    async def _не_должно_вызваться(*args, **kwargs):
        raise AssertionError("сервис вызван без logout_token")

    monkeypatch.setattr(backchannel_service, "handle_logout", _не_должно_вызваться)
    response = client.post("/internal/oidc/backchannel-logout", data={})
    assert response.status_code == 400
    assert response.json() == {"code": "invalid_logout_token"}


def test_backchannel_logout_с_невалидной_кодировкой_не_роняет_api(client):
    response = client.post(
        "/internal/oidc/backchannel-logout",
        content=b"logout_token=\xff",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400
    assert response.json() == {"code": "invalid_logout_token"}


def test_backchannel_logout_передаёт_токен_сервису(client, monkeypatch):
    async def _accepted(conn, **kwargs):
        assert kwargs["token"] == "signed-logout-token"
        assert kwargs["audience"] == "messenger-web"
        return backchannel_service.BackchannelResult(accepted=True, revoked=2)

    monkeypatch.setattr(backchannel_service, "handle_logout", _accepted)
    response = client.post(
        "/internal/oidc/backchannel-logout",
        data={"logout_token": "signed-logout-token"},
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": True, "revoked": 2}


def test_backchannel_logout_с_неверной_подписью_отклонён(client, monkeypatch):
    async def _rejected(conn, **kwargs):
        return backchannel_service.BackchannelResult(
            rejection=main.TokenRejection.BAD_SIGNATURE
        )

    monkeypatch.setattr(backchannel_service, "handle_logout", _rejected)
    response = client.post(
        "/internal/oidc/backchannel-logout",
        data={"logout_token": "forged"},
    )
    assert response.status_code == 400
    assert response.json() == {"code": "invalid_logout_token"}
