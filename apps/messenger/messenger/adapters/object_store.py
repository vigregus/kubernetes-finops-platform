"""Объектное хранилище (MinIO, в облаке S3). Единственное место, знающее про него.

Подпись AWS SigV4 написана здесь же, на стандартной библиотеке: ради двух
операций (предподписанные ссылки и три запроса сервера) тянуть `boto3` с
его десятками мегабайт и синхронным ядром в асинхронный процесс — цена,
которая не соответствует задаче.

Два адреса, и это не небрежность. Браузер ходит на публичный
(`https://s3.finops.local`) — от него считается подпись ссылки, потому что
в подпись входит заголовок `Host`. Сервер ходит на внутренний
(`http://minio.messenger.svc`) — к нему и ссылок никто не прикладывает.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

log = logging.getLogger(__name__)

_ALGORITHM = "AWS4-HMAC-SHA256"
_UNSIGNED = "UNSIGNED-PAYLOAD"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class ObjectStoreSettings:
    internal_endpoint: str
    public_endpoint: str
    access_key: str
    secret_key: str
    bucket: str = "messenger-attachments"
    region: str = "us-east-1"
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> ObjectStoreSettings | None:
        """`None`, когда хранилище не настроено: вложения тогда выключены.

        Отсутствие настройки не падение процесса: API обязан подниматься на
        стенде без объектного хранилища, а вложения в нём честно отвечают
        «недоступно», а не роняют соседние маршруты.
        """
        internal = os.getenv("S3_ENDPOINT_INTERNAL", "")
        access = os.getenv("S3_ACCESS_KEY", "")
        secret = os.getenv("S3_SECRET_KEY", "")
        if not (internal and access and secret):
            return None
        return cls(
            internal_endpoint=internal.rstrip("/"),
            public_endpoint=os.getenv("S3_ENDPOINT_PUBLIC", internal).rstrip("/"),
            access_key=access,
            secret_key=secret,
            bucket=os.getenv("S3_BUCKET", "messenger-attachments"),
        )


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, date: str, region: str) -> bytes:
    key = _sign(("AWS4" + secret).encode(), date)
    key = _sign(key, region)
    key = _sign(key, "s3")
    return _sign(key, "aws4_request")


def _encode(value: str) -> str:
    # Все символы, кроме безопасных по RFC 3986, — в процентную форму; `/`
    # в пути остаётся, в значениях запроса кодируется (`safe=""`).
    return quote(value, safe="-_.~")


def _canonical_path(bucket: str, key: str) -> str:
    return "/" + "/".join(quote(part, safe="-_.~") for part in [bucket, *key.split("/")])


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    url: str
    headers: dict[str, str]


class ObjectStore:
    def __init__(self, settings: ObjectStoreSettings) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(timeout=settings.timeout_seconds)

    async def close(self) -> None:
        await self._http.aclose()

    # --- предподписанные ссылки: считаются локально, в сеть не ходят ----------

    def _presign(
        self,
        method: str,
        key: str,
        expires_seconds: int,
        signed_headers: dict[str, str],
        extra_query: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> str:
        s = self.settings
        moment = now or _now()
        amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
        date = moment.strftime("%Y%m%d")
        public = urlsplit(s.public_endpoint)
        host = public.netloc

        headers = {"host": host, **{k.lower(): v.strip() for k, v in signed_headers.items()}}
        signed_names = ";".join(sorted(headers))
        scope = f"{date}/{s.region}/s3/aws4_request"

        query = {
            "X-Amz-Algorithm": _ALGORITHM,
            "X-Amz-Credential": f"{s.access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires_seconds),
            "X-Amz-SignedHeaders": signed_names,
            **(extra_query or {}),
        }
        canonical_query = "&".join(
            f"{_encode(k)}={_encode(v)}" for k, v in sorted(query.items())
        )
        path = _canonical_path(s.bucket, key)
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        canonical_request = "\n".join(
            [method, path, canonical_query, canonical_headers, signed_names, _UNSIGNED]
        )
        string_to_sign = "\n".join(
            [_ALGORITHM, amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
        )
        signature = hmac.new(
            _signing_key(s.secret_key, date, s.region), string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        return f"{s.public_endpoint}{path}?{canonical_query}&X-Amz-Signature={signature}"

    def presign_put(
        self, key: str, content_type: str, expires_seconds: int, now: datetime | None = None
    ) -> PresignedUpload:
        """Ссылка на `PUT`. Тип входит в подпись: загрузить под другим нельзя."""
        url = self._presign(
            "PUT", key, expires_seconds, {"content-type": content_type}, now=now
        )
        return PresignedUpload(url=url, headers={"Content-Type": content_type})

    def presign_get(
        self,
        key: str,
        expires_seconds: int,
        *,
        file_name: str | None = None,
        content_type: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Ссылка на скачивание. Имя и тип ответа задаются параметрами запроса."""
        extra: dict[str, str] = {}
        if file_name:
            safe = file_name.replace('"', "").replace("\r", "").replace("\n", "")
            # RFC 6266: ASCII-запас для старых клиентов и `filename*` для
            # настоящего имени. Голое UTF-8 в `filename=` браузеры читают
            # по-разному, а имя с кириллицей — обычный случай, не крайний.
            fallback = safe.encode("ascii", "replace").decode().replace("?", "_")
            extra["response-content-disposition"] = (
                f"inline; filename=\"{fallback}\"; filename*=UTF-8''{quote(safe, safe='')}"
            )
        if content_type:
            extra["response-content-type"] = content_type
        return self._presign("GET", key, expires_seconds, {}, extra, now=now)

    # --- запросы сервера: заголовочная подпись, внутренний адрес ---------------

    def _headers(
        self,
        method: str,
        key: str,
        extra: dict[str, str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, str]:
        s = self.settings
        moment = now or _now()
        amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
        date = moment.strftime("%Y%m%d")
        host = urlsplit(s.internal_endpoint).netloc
        headers = {
            "host": host,
            "x-amz-content-sha256": _EMPTY_SHA256,
            "x-amz-date": amz_date,
            **{k.lower(): v for k, v in (extra or {}).items()},
        }
        signed_names = ";".join(sorted(headers))
        scope = f"{date}/{s.region}/s3/aws4_request"
        canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers))
        canonical_request = "\n".join(
            [
                method,
                _canonical_path(s.bucket, key),
                "",
                canonical_headers,
                signed_names,
                _EMPTY_SHA256,
            ]
        )
        string_to_sign = "\n".join(
            [_ALGORITHM, amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
        )
        signature = hmac.new(
            _signing_key(s.secret_key, date, s.region), string_to_sign.encode(), hashlib.sha256
        ).hexdigest()
        headers["authorization"] = (
            f"{_ALGORITHM} Credential={s.access_key}/{scope}, "
            f"SignedHeaders={signed_names}, Signature={signature}"
        )
        return headers

    def _url(self, key: str) -> str:
        return f"{self.settings.internal_endpoint}{_canonical_path(self.settings.bucket, key)}"

    async def head(self, key: str) -> int | None:
        """Размер объекта или `None`, если его нет. Любой другой отказ — исключение."""
        response = await self._http.head(self._url(key), headers=self._headers("HEAD", key))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return int(response.headers["content-length"])

    async def read_range(self, key: str, length: int) -> bytes:
        """Первые `length` байт — для сверки сигнатуры, без чтения всего файла."""
        response = await self._http.get(
            self._url(key),
            headers=self._headers("GET", key, {"range": f"bytes=0-{length - 1}"}),
        )
        response.raise_for_status()
        return response.content

    async def read_all(self, key: str) -> bytes:
        response = await self._http.get(self._url(key), headers=self._headers("GET", key))
        response.raise_for_status()
        return response.content

    async def delete(self, key: str) -> None:
        response = await self._http.delete(self._url(key), headers=self._headers("DELETE", key))
        if response.status_code not in (200, 204, 404):
            response.raise_for_status()
