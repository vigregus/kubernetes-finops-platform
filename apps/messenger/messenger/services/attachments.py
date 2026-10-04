"""Конвейер вложений: инициация, завершение, состояние, обработка.

Состояния и их смысл — в миграции `0013_attachment_pipeline.sql`; правила
(что разрешено, что показывает сигнатура) — в `domain/attachment.py`. Здесь
только порядок действий и граница с хранилищем.

Отказ хранилища не роняет соседние маршруты: у API без настроенного
хранилища вложения отвечают `503 attachments_unavailable`, остальное живёт.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

import asyncpg
import httpx

from messenger.adapters.object_store import ObjectStore
from messenger.adapters.ratelimit import OnFailure, RateLimiter
from messenger.domain import attachment as domain
from messenger.domain.attachment import (
    Attachment,
    AttachmentState,
    RejectionCode,
    Scanner,
    ScanVerdict,
)
from messenger.domain.errors import Reason
from messenger.domain.ids import AttachmentId, MessageId, UserId
from messenger.domain.message import MessageKind
from messenger.repositories import attachments as repo
from messenger.telemetry import metrics

log = logging.getLogger(__name__)

# Сколько раз обработка пробует хранилище, прежде чем сдаться `failed`.
MAX_ATTEMPTS = 5
# Через сколько снова пробовать, если сканер не ответил. Короткая пауза, а
# не отказ: недоступный сканер — не вина файла.
SCANNER_RETRY_DELAY = timedelta(seconds=30)
STORAGE_RETRY_DELAY = timedelta(seconds=15)
LEASE = timedelta(seconds=120)
INIT_LIMIT = 30
INIT_WINDOW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class InitResult:
    attachment: Attachment | None = None
    upload_url: str | None = None
    upload_headers: dict[str, str] | None = None
    expires_in_seconds: int = domain.UPLOAD_URL_TTL_SECONDS
    rejection: Reason | None = None
    # Предел, который нарушен (`413`), — чтобы клиент мог назвать его человеку.
    limit_bytes: int | None = None
    retry_after_seconds: int | None = None

    @property
    def ok(self) -> bool:
        return self.rejection is None and self.attachment is not None


@dataclass(frozen=True, slots=True)
class StatusResult:
    attachment: Attachment | None = None
    rejection: Reason | None = None

    @property
    def ok(self) -> bool:
        return self.rejection is None and self.attachment is not None


async def init_upload(
    conn: asyncpg.Connection,
    *,
    store: ObjectStore,
    limiter: RateLimiter,
    user_id: UserId,
    content_type: str,
    size_bytes: int,
    file_name: str | None,
    duration_ms: int | None = None,
) -> InitResult:
    """Выдаёт ссылку на загрузку — или отказывает **до** её выдачи (`ATT-005`).

    Проверка идёт раньше и записи, и подписи: отказ не должен оставлять ни
    строки `pending`, ни ссылки, которой можно воспользоваться.
    """
    try:
        allowed = domain.validate_init(content_type, size_bytes)
    except domain.NotAllowed:
        return InitResult(rejection=Reason.UNSUPPORTED_MEDIA_TYPE)
    except domain.TooLarge as exc:
        return InitResult(rejection=Reason.PAYLOAD_TOO_LARGE, limit_bytes=exc.limit)

    # Длительность — только у голосового и обязательна у него (`ATT-004`).
    # Размер здесь заявленный, и сверка с ним — ранний отказ до ссылки; ту же
    # проверку по фактическому размеру повторяет `complete`.
    if allowed.kind is MessageKind.VOICE:
        try:
            domain.validate_voice(duration_ms=duration_ms, size_bytes=size_bytes)
        except domain.InvalidVoice:
            return InitResult(rejection=Reason.INVALID_VOICE)
    elif duration_ms is not None:
        return InitResult(rejection=Reason.INVALID_VOICE)

    # Считаются только годные запросы: негодный не стоит ни строки, ни
    # подписи, и лимит на него лишь наказывал бы за опечатку. Отказ в
    # закрытую сторону: ссылка стоит места в хранилище, и недоступный
    # счётчик не повод раздавать их без счёта (`ATT-006`).
    decision = await limiter.take(
        f"attachment-init:{user_id}",
        limit=INIT_LIMIT,
        window_seconds=INIT_WINDOW_SECONDS,
        on_failure=OnFailure.DENY,
    )
    if not decision.allowed:
        return InitResult(
            rejection=Reason.RATE_LIMITED, retry_after_seconds=decision.retry_after_seconds
        )

    normalized = domain.normalize_content_type(content_type)
    attachment_id = AttachmentId(uuid.uuid4())
    # Ключ строит сервер из идентификаторов, имя от клиента в него не попадает.
    object_key = f"u/{user_id}/{attachment_id}"

    await repo.insert_pending(
        conn,
        attachment_id=attachment_id,
        uploader_id=user_id,
        bucket=store.settings.bucket,
        object_key=object_key,
        content_type=normalized,
        size_bytes=size_bytes,
        file_name=domain.clean_file_name(file_name),
        duration_ms=duration_ms,
    )
    presigned = store.presign_put(object_key, normalized, domain.UPLOAD_URL_TTL_SECONDS)
    attachment = await repo.fetch(conn, attachment_id=attachment_id)
    assert attachment is not None  # noqa: S101 - только что вставлена в этой же транзакции
    return InitResult(
        attachment=attachment, upload_url=presigned.url, upload_headers=presigned.headers
    )


async def complete(
    conn: asyncpg.Connection,
    *,
    store: ObjectStore,
    user_id: UserId,
    attachment_id: AttachmentId,
) -> StatusResult:
    """Клиент сообщает «загрузил»: сверяем объект и ставим в обработку.

    Повтор безопасен: уже принятое вложение возвращает своё состояние, а не
    ошибку — клиент, не получивший ответа, имеет право позвать снова.
    """
    async with conn.transaction():
        current = await repo.fetch(conn, attachment_id=attachment_id, for_update=True)
        if current is None or current.uploader_id != user_id:
            return StatusResult(rejection=Reason.ATTACHMENT_NOT_FOUND)
        if current.state is not AttachmentState.PENDING:
            return StatusResult(attachment=current)

        actual = await store.head(current.object_key)
        if actual is None:
            return StatusResult(rejection=Reason.ATTACHMENT_UPLOAD_MISSING)

        allowed = domain.ALLOWED[current.content_type]
        limit = allowed.max_bytes
        bitrate: int | None = None
        invalid_voice = False
        if allowed.kind is MessageKind.VOICE and actual <= limit:
            # Заявленная длительность против фактического размера: сюда
            # доходит «малая запись», под которую загрузили большой файл.
            try:
                bitrate = domain.validate_voice(
                    duration_ms=current.duration_ms, size_bytes=actual
                )
            except domain.InvalidVoice:
                invalid_voice = True
        if actual > limit or invalid_voice:
            # Заявил малое, загрузил большое: белый список размеров не
            # обойти обещанием. Объект удаляется, вложение отклонено.
            await store.delete(current.object_key)
            await conn.execute(
                """
                UPDATE attachments
                   SET state = 'rejected', rejection_reason = $3,
                       size_bytes = $2, processed_at = now()
                 WHERE attachment_id = $1
                """,
                attachment_id,
                actual,
                (RejectionCode.INVALID_AUDIO if invalid_voice else RejectionCode.TOO_LARGE).value,
            )
        else:
            await repo.mark_processing(
                conn, attachment_id=attachment_id, size_bytes=actual, bitrate_kbps=bitrate
            )
        updated = await repo.fetch(conn, attachment_id=attachment_id)
    return StatusResult(attachment=updated)


async def status(
    conn: asyncpg.Connection, *, user_id: UserId, attachment_id: AttachmentId
) -> StatusResult:
    current = await repo.fetch(conn, attachment_id=attachment_id)
    if current is None or current.uploader_id != user_id:
        return StatusResult(rejection=Reason.ATTACHMENT_NOT_FOUND)
    return StatusResult(attachment=current)


@dataclass(frozen=True, slots=True)
class AttachmentView:
    attachment: Attachment
    download_url: str


async def views_for_messages(
    conn: asyncpg.Connection,
    *,
    store: ObjectStore | None,
    message_ids: Sequence[MessageId],
) -> dict[MessageId, list[AttachmentView]]:
    """Вложения сообщений со свежими ссылками на скачивание.

    Ссылка не хранится: она живёт минуты (`DOWNLOAD_URL_TTL_SECONDS`) и
    выдаётся при каждом чтении, то есть только тому, кто прошёл проверку
    доступа к беседе, — вызывающий вызывает это после неё.
    """
    if store is None or not message_ids:
        return {}
    grouped = await repo.list_for_messages(conn, message_ids=message_ids)
    return {
        message_id: [
            AttachmentView(
                attachment=item,
                download_url=store.presign_get(
                    item.object_key,
                    domain.DOWNLOAD_URL_TTL_SECONDS,
                    file_name=item.file_name,
                    content_type=item.content_type,
                ),
            )
            for item in items
        ]
        for message_id, items in grouped.items()
    }


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    ready: int = 0
    rejected: int = 0
    failed: int = 0
    deferred: int = 0


async def process_batch(
    conn: asyncpg.Connection,
    *,
    store: ObjectStore,
    scanner: Scanner,
    owner: str,
    limit: int = 10,
) -> ProcessOutcome:
    """Одна пачка обработки: сигнатура, сканер, итог. Для воркера и тестов."""
    async with conn.transaction():
        claimed = await repo.claim_processing(conn, owner=owner, lease=LEASE, limit=limit)

    ready = rejected = failed = deferred = 0
    for item in claimed:
        result = await _process_one(conn, store=store, scanner=scanner, item=item)
        metrics.attachment_processed(result)
        if result == "ready":
            ready += 1
        elif result == "rejected":
            rejected += 1
        elif result == "failed":
            failed += 1
        else:
            deferred += 1
    return ProcessOutcome(ready=ready, rejected=rejected, failed=failed, deferred=deferred)


async def _reject(
    conn: asyncpg.Connection,
    store: ObjectStore,
    item: Attachment,
    code: RejectionCode,
    detected: str | None = None,
) -> str:
    # Объект удаляется сразу: отклонённый файл не должен лежать в хранилище
    # в ожидании уборщика — он может быть опасным.
    try:
        await store.delete(item.object_key)
    except (httpx.HTTPError, OSError):
        log.warning(
            "не удалось удалить отклонённый объект",
            extra={"event": "attachment_process", "result": "failed",
                   "error_code": "delete_failed", "attachment_id": str(item.attachment_id)},
        )
    await repo.finish(
        conn,
        attachment_id=item.attachment_id,
        state=AttachmentState.REJECTED,
        detected_content_type=detected,
        rejection_reason=code,
    )
    log.info(
        "вложение отклонено",
        extra={"event": "attachment_process", "result": "rejected",
               "error_code": code.value, "attachment_id": str(item.attachment_id)},
    )
    return "rejected"


async def _process_one(
    conn: asyncpg.Connection, *, store: ObjectStore, scanner: Scanner, item: Attachment
) -> str:
    try:
        head = await store.read_range(item.object_key, domain.SNIFF_BYTES)
    except (httpx.HTTPError, OSError) as exc:
        return await _storage_failure(conn, item, exc)

    if not domain.sniff(item.content_type, head):
        return await _reject(conn, store, item, RejectionCode.TYPE_MISMATCH)

    try:
        content = await store.read_all(item.object_key)
    except (httpx.HTTPError, OSError) as exc:
        return await _storage_failure(conn, item, exc)

    verdict = await scanner.scan(content)
    if verdict is ScanVerdict.INFECTED:
        return await _reject(conn, store, item, RejectionCode.MALWARE, item.content_type)
    if verdict is ScanVerdict.UNAVAILABLE:
        # Отказ в закрытую сторону (`ATT-008`): не `ready` и не `rejected`.
        # Вложение остаётся в обработке и недоступно, пока сканер не вернётся.
        await repo.release_with_delay(
            conn, attachment_id=item.attachment_id, delay=SCANNER_RETRY_DELAY
        )
        log.warning(
            "сканер недоступен, вложение остаётся в обработке",
            extra={"event": "attachment_process", "result": "failed",
                   "error_code": "scanner_unavailable",
                   "attachment_id": str(item.attachment_id), "dependency": "scanner"},
        )
        return "deferred"

    await repo.finish(
        conn,
        attachment_id=item.attachment_id,
        state=AttachmentState.READY,
        detected_content_type=item.content_type,
    )
    return "ready"


async def _storage_failure(conn: asyncpg.Connection, item: Attachment, exc: Exception) -> str:
    if item.attempts >= MAX_ATTEMPTS:
        await repo.finish(
            conn,
            attachment_id=item.attachment_id,
            state=AttachmentState.FAILED,
            rejection_reason=RejectionCode.STORAGE_ERROR,
        )
        log.error(
            "обработка вложения не удалась",
            extra={"event": "attachment_process", "result": "failed",
                   "error_code": type(exc).__name__, "dependency": "object_store",
                   "attachment_id": str(item.attachment_id)},
        )
        return "failed"
    await repo.release_with_delay(
        conn, attachment_id=item.attachment_id, delay=STORAGE_RETRY_DELAY
    )
    return "deferred"


@dataclass(frozen=True, slots=True)
class CleanupOutcome:
    erased: int = 0
    failed: int = 0


async def cleanup_orphans(
    conn: asyncpg.Connection,
    *,
    store: ObjectStore,
    older_than: timedelta = timedelta(days=1),
    limit: int = 100,
) -> CleanupOutcome:
    """Уборка неприкреплённых старше суток: и запись, и объект (`ATT-001`)."""
    erased = failed = 0
    async with conn.transaction():
        orphans = await repo.claim_orphans(conn, older_than=older_than, limit=limit)
        for item in orphans:
            try:
                await store.delete(item.object_key)
            except (httpx.HTTPError, OSError):
                # Не помечаем стёртым то, что не стёрто: уборщик вернётся.
                failed += 1
                continue
            await repo.mark_erased(conn, attachment_id=item.attachment_id)
            erased += 1
    return CleanupOutcome(erased=erased, failed=failed)
