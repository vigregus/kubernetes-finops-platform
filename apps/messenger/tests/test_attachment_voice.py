"""Голосовые вложения на уровне сервиса: инициация и завершение (ATT-004)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from messenger.adapters.object_store import PresignedUpload
from messenger.domain.attachment import Attachment, AttachmentState, RejectionCode
from messenger.domain.errors import Reason
from messenger.domain.ids import AttachmentId, UserId
from messenger.services import attachments as service

ACTOR = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
ID = AttachmentId(uuid.UUID("44444444-4444-4444-4444-444444444444"))
NOW = datetime(2026, 10, 4, tzinfo=UTC)


class Decision:
    allowed = True
    retry_after_seconds = 0


class Limiter:
    async def take(self, *args, **kwargs):
        return Decision()


class Store:
    class settings:  # noqa: N801
        bucket = "b"

    def __init__(self, actual: int | None = 0):
        self.actual = actual
        self.deleted: list[str] = []

    def presign_put(self, key, content_type, ttl):
        return PresignedUpload(url="https://app/storage/x", headers={"Content-Type": content_type})

    async def head(self, key):
        return self.actual

    async def delete(self, key):
        self.deleted.append(key)


class Conn:
    """Только то, что трогает `complete`: транзакция и обновление строки."""

    def __init__(self):
        self.executed: list[tuple] = []

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"


def _attachment(state=AttachmentState.PENDING, *, content_type="audio/webm", duration_ms=10_000,
                size=40_000) -> Attachment:
    return Attachment(
        attachment_id=ID, uploader_id=ACTOR, message_id=None, state=state, bucket="b",
        object_key="k", content_type=content_type, size_bytes=size, created_at=NOW,
        duration_ms=duration_ms,
    )


def _init(monkeypatch, **overrides):
    inserted: dict = {}

    async def insert_pending(conn, **kwargs):
        inserted.update(kwargs)

    async def fetch(conn, **kwargs):
        return _attachment()

    monkeypatch.setattr(service.repo, "insert_pending", insert_pending)
    monkeypatch.setattr(service.repo, "fetch", fetch)
    kwargs = {"content_type": "audio/webm;codecs=opus", "size_bytes": 40_000,
              "file_name": None, "duration_ms": 10_000, **overrides}
    result = asyncio.run(service.init_upload(
        None, store=Store(), limiter=Limiter(), user_id=ACTOR, **kwargs))
    return result, inserted


def test_голосовое_с_длительностью_получает_ссылку_и_пишет_длительность(monkeypatch):
    result, inserted = _init(monkeypatch)
    assert result.ok
    assert inserted["duration_ms"] == 10_000
    assert inserted["content_type"] == "audio/webm"


def test_голосовое_без_длительности_отвергается_до_ссылки(monkeypatch):
    result, inserted = _init(monkeypatch, duration_ms=None)
    assert result.rejection is Reason.INVALID_VOICE
    assert inserted == {}


def test_длительность_у_не_голосового_отвергается(monkeypatch):
    result, inserted = _init(monkeypatch, content_type="image/png", duration_ms=5_000)
    assert result.rejection is Reason.INVALID_VOICE
    assert inserted == {}


def test_неправдоподобный_битрейт_отвергается_до_ссылки(monkeypatch):
    result, inserted = _init(monkeypatch, size_bytes=5 * 1024 * 1024, duration_ms=1_000)
    assert result.rejection is Reason.INVALID_VOICE
    assert inserted == {}


def _complete(monkeypatch, *, actual: int, duration_ms: int = 10_000):
    marked: dict = {}
    conn = Conn()
    current = _attachment(duration_ms=duration_ms)

    async def fetch(conn_, **kwargs):
        return current

    async def mark_processing(conn_, **kwargs):
        marked.update(kwargs)
        return True

    monkeypatch.setattr(service.repo, "fetch", fetch)
    monkeypatch.setattr(service.repo, "mark_processing", mark_processing)
    store = Store(actual=actual)
    result = asyncio.run(service.complete(conn, store=store, user_id=ACTOR, attachment_id=ID))
    return result, marked, conn, store


def test_завершение_записывает_битрейт_из_фактического_размера(monkeypatch):
    _, marked, conn, store = _complete(monkeypatch, actual=40_000)
    assert marked["bitrate_kbps"] == 32
    assert marked["size_bytes"] == 40_000
    assert conn.executed == []
    assert store.deleted == []


def test_заявил_малое_загрузил_большое_отклоняется_как_неправдоподобное(monkeypatch):
    # Заявлено 10 с, а в хранилище лёг файл на пять мегабайт: 4 мбит/с.
    _, marked, conn, store = _complete(monkeypatch, actual=5 * 1024 * 1024)
    assert marked == {}
    assert store.deleted == ["k"]
    (sql, args), = conn.executed
    assert "state = 'rejected'" in sql
    assert RejectionCode.INVALID_AUDIO.value in args
