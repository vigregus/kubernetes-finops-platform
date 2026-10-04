"""Предподписанные ссылки: форма и инвариант подписи при префиксе пути."""
from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from messenger.adapters.object_store import ObjectStore, ObjectStoreSettings

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def store(prefix: str = "") -> ObjectStore:
    return ObjectStore(
        ObjectStoreSettings(
            internal_endpoint="http://minio:80",
            public_endpoint="https://app.finops.local",
            access_key="key",
            secret_key="secret",
            public_path_prefix=prefix,
        )
    )


def signature(url: str) -> str:
    return parse_qs(urlsplit(url).query)["X-Amz-Signature"][0]


def test_префикс_попадает_в_адрес_но_не_в_подпись():
    plain = store().presign_get("u/a/b", 300, now=NOW)
    prefixed = store("/storage").presign_get("u/a/b", 300, now=NOW)

    assert urlsplit(plain).path == "/messenger-attachments/u/a/b"
    assert urlsplit(prefixed).path == "/storage/messenger-attachments/u/a/b"
    # Шлюз снимает префикс, и MinIO считает подпись от пути без него.
    assert signature(plain) == signature(prefixed)


def test_подпись_зависит_от_хоста_и_типа():
    a = store().presign_put("k", "image/png", 600, now=NOW)
    b = store().presign_put("k", "text/plain", 600, now=NOW)
    assert signature(a.url) != signature(b.url)
    assert a.headers == {"Content-Type": "image/png"}


def test_имя_файла_с_кириллицей_в_rfc6266():
    url = store().presign_get("k", 300, file_name="кот.png", content_type="image/png", now=NOW)
    disposition = parse_qs(urlsplit(url).query)["response-content-disposition"][0]
    assert "filename*=UTF-8''" in disposition
    assert 'filename="' in disposition


def test_запись_сервера_подписывает_хэш_тела_и_тип():
    import hashlib

    data = b"thumbnail-bytes"
    headers = store()._headers(
        "PUT", "k", {"content-type": "image/webp"}, now=NOW,
        payload_sha256=hashlib.sha256(data).hexdigest(),
    )
    assert headers["x-amz-content-sha256"] == hashlib.sha256(data).hexdigest()
    assert "content-type" in headers["authorization"]
    # Другое тело — другая подпись: подмена содержимого по пути не пройдёт.
    other = store()._headers(
        "PUT", "k", {"content-type": "image/webp"}, now=NOW,
        payload_sha256=hashlib.sha256(b"other").hexdigest(),
    )
    assert headers["authorization"] != other["authorization"]
