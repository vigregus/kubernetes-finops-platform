"""Вложение без знания о Postgres, MinIO и HTTP.

Здесь живут три вещи, у которых нет естественного дома в сервисе: что
разрешено загружать, что показывает сигнатура файла и как называется
вердикт сканера. Сервис оркестрирует, а правила — отсюда, чтобы тест
проверял их без хранилища.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from messenger.domain.ids import AttachmentId, MessageId, UserId
from messenger.domain.message import MessageKind

MIB = 1024 * 1024


class AttachmentState(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    REJECTED = "rejected"
    FAILED = "failed"
    ATTACHED = "attached"
    ORPHANED = "orphaned"
    ERASED = "erased"


class RejectionCode(str, Enum):
    """Причина отказа, понятная человеку. Идёт наружу в `rejection_code`."""

    TYPE_MISMATCH = "type_mismatch"
    MALWARE = "malware"
    TOO_LARGE = "too_large"
    STORAGE_ERROR = "storage_error"
    # Голосовое с неправдоподобной длительностью или битрейтом (`ATT-004`).
    INVALID_AUDIO = "invalid_audio"


@dataclass(frozen=True, slots=True)
class AllowedType:
    kind: MessageKind
    max_bytes: int
    # Сигнатуры начала файла. Пустой кортеж у текста: у него сигнатуры нет,
    # и проверка идёт по отсутствию нулевых байт (`sniff`).
    signatures: tuple[bytes, ...]


# Белый список, а не чёрный: всё, чего здесь нет, не принимается. Размеры
# разные намеренно — картинка в чате больше десяти мегабайт это ошибка
# камеры, а документ на двадцать пять вполне нормален.
_JPEG = (b"\xff\xd8\xff",)
_PNG = (b"\x89PNG\r\n\x1a\n",)
_GIF = (b"GIF87a", b"GIF89a")
_ZIP = (b"PK\x03\x04",)
# Контейнеры, в которых браузерный `MediaRecorder` пишет голос: WebM (EBML)
# в Chrome и Firefox, Ogg в Firefox, MP4 в Safari. MP4 узнаётся по метке
# `ftyp` в байтах 4–8, а не в начале, — её проверяет `sniff`.
_WEBM = (b"\x1a\x45\xdf\xa3",)
_OGG = (b"OggS",)
_MP4 = (b"",)

ALLOWED: dict[str, AllowedType] = {
    "image/jpeg": AllowedType(MessageKind.IMAGE, 10 * MIB, _JPEG),
    "image/png": AllowedType(MessageKind.IMAGE, 10 * MIB, _PNG),
    "image/gif": AllowedType(MessageKind.IMAGE, 10 * MIB, _GIF),
    # WebP — RIFF-контейнер: первые четыре байта общие с WAV/AVI, и без
    # проверки метки формата в байтах 8–12 любой RIFF прошёл бы как картинка.
    # Её делает `sniff`, а здесь сигнатура — только префикс.
    "image/webp": AllowedType(MessageKind.IMAGE, 10 * MIB, (b"RIFF",)),
    "application/pdf": AllowedType(MessageKind.FILE, 25 * MIB, (b"%PDF-",)),
    "application/zip": AllowedType(MessageKind.FILE, 25 * MIB, _ZIP),
    # Офисные форматы — тот же ZIP внутри; отличить docx от zip по
    # сигнатуре нельзя, и пытаться значило бы разбирать архив.
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": AllowedType(
        MessageKind.FILE, 25 * MIB, _ZIP
    ),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": AllowedType(
        MessageKind.FILE, 25 * MIB, _ZIP
    ),
    "text/plain": AllowedType(MessageKind.FILE, 5 * MIB, ()),
    # Голос: пять минут при 128 кбит/с — меньше пяти мегабайт; с запасом на
    # более щедрый кодек предел восемь.
    "audio/webm": AllowedType(MessageKind.VOICE, 8 * MIB, _WEBM),
    "audio/ogg": AllowedType(MessageKind.VOICE, 8 * MIB, _OGG),
    "audio/mp4": AllowedType(MessageKind.VOICE, 8 * MIB, _MP4),
}

# Голосовое сообщение (`ATT-004`). Длительность заявляет клиент, а размер —
# факт хранилища; битрейт выводится из них и обязан быть правдоподобным.
MIN_VOICE_MS = 500
MAX_VOICE_MS = 5 * 60 * 1000
MIN_BITRATE_KBPS = 4
MAX_BITRATE_KBPS = 512

# Сколько байт начала файла читает обработка для сверки типа.
SNIFF_BYTES = 8192

# Сколько живёт ссылка на загрузку и на скачивание.
UPLOAD_URL_TTL_SECONDS = 600
DOWNLOAD_URL_TTL_SECONDS = 300

MAX_FILE_NAME_LENGTH = 255


class NotAllowed(Exception):
    """Тип не в белом списке."""


class TooLarge(Exception):
    """Размер выше предела для этого типа."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"размер выше предела {limit}")
        self.limit = limit


def validate_init(content_type: str, size_bytes: int) -> AllowedType:
    """Решает, выдавать ли ссылку. Вызывается **до** обращения к хранилищу.

    Заголовок типа нормализуется: `Image/PNG; charset=x` — тот же тип, и
    принимать его за другой значило бы дать обойти белый список регистром.
    """
    normalized = content_type.split(";", 1)[0].strip().lower()
    allowed = ALLOWED.get(normalized)
    if allowed is None:
        raise NotAllowed(normalized)
    if size_bytes > allowed.max_bytes:
        raise TooLarge(allowed.max_bytes)
    return allowed


class InvalidVoice(Exception):
    """Длительность отсутствует, вне пределов или не сходится с размером."""


def validate_voice(*, duration_ms: int | None, size_bytes: int) -> int:
    """Проверяет голосовое и возвращает битрейт, кбит/с.

    Длительность в WebM, который пишет `MediaRecorder`, в заголовке обычно
    не записана (поток без финализации), поэтому достоверно её из файла
    не прочитать. Вместо этого берётся заявленная клиентом и сверяется с
    **фактическим** размером: «десять мегабайт за секунду» и «байт за минуту»
    — не голос, и обещание длительности нельзя использовать, чтобы пронести
    большой файл или пустышку.
    """
    if duration_ms is None or not MIN_VOICE_MS <= duration_ms <= MAX_VOICE_MS:
        raise InvalidVoice("длительность вне пределов")
    bitrate = round(size_bytes * 8 / duration_ms)
    if not MIN_BITRATE_KBPS <= bitrate <= MAX_BITRATE_KBPS:
        raise InvalidVoice(f"битрейт {bitrate} кбит/с неправдоподобен")
    return bitrate


def normalize_content_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def sniff(declared: str, head: bytes) -> bool:
    """Совпадает ли начало файла с заявленным типом (`ATT-007`).

    Возвращает `True`, когда совпало. Исполняемый файл под видом картинки не
    совпадёт ни с одной сигнатурой — отдельного списка «опасных» форматов
    нет и быть не должно: он всегда неполон, а белый список полон по
    построению.
    """
    allowed = ALLOWED.get(normalize_content_type(declared))
    if allowed is None:
        return False
    if declared == "text/plain":
        # Текст: ни одного нулевого байта и разбирается как UTF-8. Обрезанный
        # посередине символ в конце окна — не порча, поэтому `ignore` на хвосте.
        if b"\x00" in head:
            return False
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as exc:
            return exc.start >= len(head) - 3
        return True
    if not any(head.startswith(signature) for signature in allowed.signatures):
        return False
    if declared == "image/webp":
        return head[8:12] == b"WEBP"
    if declared == "audio/mp4":
        return head[4:8] == b"ftyp"
    return True


class ScanVerdict(str, Enum):
    CLEAN = "clean"
    INFECTED = "infected"
    # Сканер не ответил. Это не «чисто» и не «заражено»: вложение остаётся
    # в обработке и недоступно (`ATT-008`, отказ в закрытую сторону).
    UNAVAILABLE = "unavailable"


class Scanner(Protocol):
    async def scan(self, content: bytes) -> ScanVerdict: ...


@dataclass(frozen=True, slots=True)
class Attachment:
    attachment_id: AttachmentId
    uploader_id: UserId
    message_id: MessageId | None
    state: AttachmentState
    bucket: str
    object_key: str
    content_type: str
    size_bytes: int
    created_at: datetime
    file_name: str | None = None
    detected_content_type: str | None = None
    rejection_reason: RejectionCode | None = None
    attempts: int = 0
    duration_ms: int | None = None
    bitrate_kbps: int | None = None

    @property
    def kind(self) -> MessageKind:
        return ALLOWED[self.content_type].kind


def clean_file_name(name: str | None) -> str | None:
    """Имя для показа: без путей и управляющих символов, не длиннее предела.

    Имя идёт только в запись и в `Content-Disposition` скачивания и в ключ
    объекта не попадает никогда, поэтому `../../etc/passwd` здесь — просто
    странное имя, а не путь. Но и показывать его как есть незачем.
    """
    if name is None:
        return None
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in base if ch.isprintable()).strip()
    return cleaned[:MAX_FILE_NAME_LENGTH] or None
