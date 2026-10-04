"""HTTP-контракт вложений: коды, формы, видимость. Сервис подменяется."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from messenger.api import main
from messenger.api.main import app
from messenger.domain.attachment import Attachment, AttachmentState, RejectionCode
from messenger.domain.errors import Reason
from messenger.domain.ids import AttachmentId, UserId
from messenger.domain.user import User
from messenger.services import attachments as service
from messenger.services import identity

ACTOR = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
NOW = datetime(2026, 9, 19, tzinfo=UTC)
ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


class Store:
    class settings:  # noqa: N801 - имитация атрибута настоящего хранилища
        bucket = "b"


class Runtime:
    keys = None
    oidc_settings = None
    limiter = object()

    def __init__(self, store=None):
        self.object_store = store

    @asynccontextmanager
    async def connection(self, mode=None):
        yield None


def _attachment(state=AttachmentState.PROCESSING, reason=None) -> Attachment:
    return Attachment(
        attachment_id=AttachmentId(ID), uploader_id=ACTOR, message_id=None, state=state,
        bucket="b", object_key="k", content_type="image/png", size_bytes=5, created_at=NOW,
        rejection_reason=reason,
    )


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def runtime():
    original = app.state.runtime
    fake = Runtime(store=Store())
    app.state.runtime = fake
    yield fake
    app.state.runtime = original


def authenticated(monkeypatch):
    async def _current(*args, **kwargs):
        return identity.AuthResult(
            user=User(user_id=ACTOR, external_id="kc", display_name="А", email="a@e.org",
                      email_verified=True, created_at=NOW, updated_at=NOW)
        )

    monkeypatch.setattr(main, "_current", _current)


def init_returns(monkeypatch, result):
    async def _init(conn, **kwargs):
        return result

    monkeypatch.setattr(service, "init_upload", _init)


BODY = {"content_type": "image/png", "size_bytes": 5, "file_name": "a.png"}


def test_без_токена_не_принимает(client, monkeypatch, отказ):
    async def _current(*args, **kwargs):
        return identity.AuthResult()

    monkeypatch.setattr(main, "_current", _current)
    отказ(client.post("/attachments", json=BODY), status=401, code="unauthenticated")


def test_без_хранилища_вложения_недоступны_а_не_падают(client, runtime, monkeypatch, отказ):
    runtime.object_store = None
    authenticated(monkeypatch)
    отказ(client.post("/attachments", json=BODY), status=503, code="attachments_unavailable")


def test_инициация_отдаёт_ссылку_и_заголовки(client, monkeypatch):
    authenticated(monkeypatch)
    init_returns(monkeypatch, service.InitResult(
        attachment=_attachment(AttachmentState.PENDING),
        upload_url="https://s3.finops.local/x", upload_headers={"Content-Type": "image/png"},
    ))
    r = client.post("/attachments", json=BODY)
    assert r.status_code == 201
    assert set(r.json()) == {"attachment_id", "upload_url", "expires_at", "upload_headers"}
    assert r.json()["upload_headers"] == {"Content-Type": "image/png"}


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [
        (Reason.UNSUPPORTED_MEDIA_TYPE, 415, "unsupported_media_type"),
        (Reason.PAYLOAD_TOO_LARGE, 413, "payload_too_large"),
        (Reason.INVALID_VOICE, 400, "invalid_voice"),
    ],
)
def test_отказ_по_типу_и_размеру(client, monkeypatch, отказ, reason, status, code):
    authenticated(monkeypatch)
    init_returns(monkeypatch, service.InitResult(rejection=reason))
    отказ(client.post("/attachments", json=BODY), status=status, code=code)


def test_длительность_голосового_доходит_до_сервиса(client, monkeypatch):
    authenticated(monkeypatch)
    seen: dict = {}

    async def _init(conn, **kwargs):
        seen.update(kwargs)
        return service.InitResult(
            attachment=_attachment(AttachmentState.PENDING),
            upload_url="https://app.finops.local/storage/x", upload_headers={},
        )

    monkeypatch.setattr(service, "init_upload", _init)
    r = client.post("/attachments", json={
        "content_type": "audio/webm", "size_bytes": 40000, "duration_ms": 10000})
    assert r.status_code == 201
    assert seen["duration_ms"] == 10000


def test_лимит_инициаций_несёт_retry_after(client, monkeypatch):
    authenticated(monkeypatch)
    init_returns(monkeypatch, service.InitResult(rejection=Reason.RATE_LIMITED,
                                                 retry_after_seconds=9))
    r = client.post("/attachments", json=BODY)
    assert r.status_code == 429
    assert r.headers["retry-after"] == "9"


def test_размер_ноль_отвергается_транспортом(client, monkeypatch):
    authenticated(monkeypatch)
    assert client.post("/attachments", json={**BODY, "size_bytes": 0}).status_code == 422


def test_complete_принимает_и_отдаёт_состояние(client, monkeypatch):
    authenticated(monkeypatch)

    async def _complete(conn, **kwargs):
        return service.StatusResult(attachment=_attachment())

    monkeypatch.setattr(service, "complete", _complete)
    r = client.post(f"/attachments/{ID}/complete")
    assert r.status_code == 202
    assert r.json() == {"attachment_id": str(ID), "state": "processing"}


def test_отклонённое_называет_причину(client, monkeypatch):
    authenticated(monkeypatch)

    async def _status(conn, **kwargs):
        return service.StatusResult(
            attachment=_attachment(AttachmentState.REJECTED, RejectionCode.MALWARE)
        )

    monkeypatch.setattr(service, "status", _status)
    assert client.get(f"/attachments/{ID}").json() == {
        "attachment_id": str(ID), "state": "rejected", "rejection_code": "malware",
    }


@pytest.mark.parametrize("state", [AttachmentState.ERASED, AttachmentState.ORPHANED])
def test_стёртое_для_клиента_не_существует(client, monkeypatch, отказ, state):
    authenticated(monkeypatch)

    async def _status(conn, **kwargs):
        return service.StatusResult(attachment=_attachment(state))

    monkeypatch.setattr(service, "status", _status)
    отказ(client.get(f"/attachments/{ID}"), status=404, code="resource_not_found")


def test_чужое_неотличимо_от_несуществующего(client, monkeypatch, отказ):
    authenticated(monkeypatch)

    async def _status(conn, **kwargs):
        return service.StatusResult(rejection=Reason.ATTACHMENT_NOT_FOUND)

    monkeypatch.setattr(service, "status", _status)
    отказ(client.get(f"/attachments/{ID}"), status=404, code="resource_not_found")
