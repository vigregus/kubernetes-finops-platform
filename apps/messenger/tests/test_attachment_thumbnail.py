"""Миниатюра в обработке вложения: порядок действий, отказы, уборка."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import httpx

from messenger.domain.attachment import (
    Attachment,
    AttachmentState,
    RejectionCode,
    ScanVerdict,
    Thumbnail,
    UnreadableImage,
)
from messenger.domain.ids import AttachmentId, UserId
from messenger.services import attachments as service

ACTOR = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
ID = AttachmentId(uuid.UUID("44444444-4444-4444-4444-444444444444"))
NOW = datetime(2026, 10, 4, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class Store:
    def __init__(self, put_error: Exception | None = None):
        self.put_error = put_error
        self.puts: list[tuple[str, bytes, str]] = []
        self.deleted: list[str] = []

    async def read_range(self, key, length):
        return PNG[:length]

    async def read_all(self, key):
        return PNG

    async def put(self, key, data, content_type):
        if self.put_error:
            raise self.put_error
        self.puts.append((key, data, content_type))

    async def delete(self, key):
        self.deleted.append(key)


class Scanner:
    async def scan(self, content):
        return ScanVerdict.CLEAN


class Thumbnailer:
    def __init__(self, error: Exception | None = None):
        self.error = error

    def make(self, content):
        if self.error:
            raise self.error
        return Thumbnail(data=b"webp", content_type="image/webp", width=480, height=240,
                         source_width=2000, source_height=1000)


def _item(content_type="image/png") -> Attachment:
    return Attachment(
        attachment_id=ID, uploader_id=ACTOR, message_id=None, state=AttachmentState.PROCESSING,
        bucket="b", object_key="u/x/y", content_type=content_type, size_bytes=len(PNG),
        created_at=NOW,
    )


def _run(monkeypatch, *, store, thumbnailer, item=None):
    finished: dict = {}

    async def finish(conn, **kwargs):
        finished.update(kwargs)

    async def failure(conn, it, exc):
        finished["storage_failure"] = type(exc).__name__
        return "deferred"

    monkeypatch.setattr(service.repo, "finish", finish)
    monkeypatch.setattr(service, "_storage_failure", failure)
    result = asyncio.run(service._process_one(
        None, store=store, scanner=Scanner(), thumbnailer=thumbnailer, item=item or _item()))
    return result, finished


def test_изображение_получает_миниатюру_и_размеры_оригинала(monkeypatch):
    store = Store()
    result, finished = _run(monkeypatch, store=store, thumbnailer=Thumbnailer())
    assert result == "ready"
    assert store.puts == [("u/x/y.thumb", b"webp", "image/webp")]
    assert finished["thumbnail_key"] == "u/x/y.thumb"
    assert (finished["width"], finished["height"]) == (2000, 1000)


def test_без_миниатюризатора_вложение_всё_равно_готово(monkeypatch):
    store = Store()
    result, finished = _run(monkeypatch, store=store, thumbnailer=None)
    assert result == "ready"
    assert store.puts == []
    assert finished["thumbnail_key"] is None


def test_не_изображение_миниатюры_не_получает(monkeypatch):
    store = Store()
    pdf = _item("application/pdf")
    monkeypatch.setattr(service.domain, "sniff", lambda declared, head: True)
    result, finished = _run(monkeypatch, store=store, thumbnailer=Thumbnailer(), item=pdf)
    assert result == "ready"
    assert store.puts == []


def test_неразбираемое_изображение_отклоняется_и_объект_удаляется(monkeypatch):
    store = Store()
    result, finished = _run(
        monkeypatch, store=store, thumbnailer=Thumbnailer(error=UnreadableImage("битый")))
    assert result == "rejected"
    assert finished["rejection_reason"] is RejectionCode.INVALID_IMAGE
    assert store.deleted == ["u/x/y"]
    assert store.puts == []


def test_отказ_хранилища_при_записи_миниатюры_не_делает_вложение_готовым(monkeypatch):
    store = Store(put_error=httpx.ConnectError("minio лежит"))
    result, finished = _run(monkeypatch, store=store, thumbnailer=Thumbnailer())
    assert result == "deferred"
    assert finished == {"storage_failure": "ConnectError"}


class Conn:
    def transaction(self):
        class _Tx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()


def test_уборка_стирает_и_миниатюру(monkeypatch):
    orphan = Attachment(
        attachment_id=ID, uploader_id=ACTOR, message_id=None, state=AttachmentState.READY,
        bucket="b", object_key="u/x/y", content_type="image/png", size_bytes=1, created_at=NOW,
        thumbnail_key="u/x/y.thumb",
    )
    erased: list = []

    async def claim_orphans(conn, **kwargs):
        return [orphan]

    async def mark_erased(conn, **kwargs):
        erased.append(kwargs["attachment_id"])

    monkeypatch.setattr(service.repo, "claim_orphans", claim_orphans)
    monkeypatch.setattr(service.repo, "mark_erased", mark_erased)
    store = Store()
    outcome = asyncio.run(service.cleanup_orphans(Conn(), store=store))
    assert store.deleted == ["u/x/y", "u/x/y.thumb"]
    assert outcome.erased == 1 and erased == [ID]
