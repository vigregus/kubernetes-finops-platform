"""Правила вложений без хранилища: белый список, пределы, сигнатуры, имя."""
from __future__ import annotations

import pytest

from messenger.domain import attachment as domain
from messenger.domain.message import MessageKind, validate_attachments

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_тип_и_размер_проверяются_до_всего():
    assert domain.validate_init("image/png", 10).kind is MessageKind.IMAGE
    assert domain.validate_init("application/pdf", 1).kind is MessageKind.FILE


def test_регистр_и_параметры_типа_не_обходят_белый_список():
    assert domain.validate_init("Image/PNG; charset=x", 10).kind is MessageKind.IMAGE
    with pytest.raises(domain.NotAllowed):
        domain.validate_init("application/x-msdownload", 10)
    with pytest.raises(domain.NotAllowed):
        domain.validate_init("", 10)


def test_размер_выше_предела_называет_предел():
    with pytest.raises(domain.TooLarge) as caught:
        domain.validate_init("image/png", 10 * domain.MIB + 1)
    assert caught.value.limit == 10 * domain.MIB


def test_исполняемый_файл_под_видом_картинки_не_проходит_сигнатуру():
    assert domain.sniff("image/png", PNG)
    assert not domain.sniff("image/png", b"MZ\x90\x00")
    assert not domain.sniff("image/png", b"\x7fELF")
    assert not domain.sniff("image/jpeg", PNG)


def test_webp_отличается_от_других_riff():
    assert domain.sniff("image/webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    assert not domain.sniff("image/webp", b"RIFF\x00\x00\x00\x00WAVEfmt ")


def test_текст_без_нулевых_байт_и_в_utf8():
    assert domain.sniff("text/plain", "привет".encode())
    assert not domain.sniff("text/plain", b"abc\x00def")
    assert not domain.sniff("text/plain", b"\xff\xfe\xfa binary")


def test_обрезанный_на_границе_окна_символ_не_порча():
    assert domain.sniff("text/plain", "я".encode()[:1])


def test_имя_файла_без_путей_и_управляющих_символов():
    assert domain.clean_file_name("../../etc/pass\x00wd.png") == "passwd.png"
    assert domain.clean_file_name("C:\\\\dir\\\\a.txt") == "a.txt"
    assert domain.clean_file_name("   ") is None
    assert domain.clean_file_name(None) is None
    assert len(domain.clean_file_name("я" * 400)) == domain.MAX_FILE_NAME_LENGTH


@pytest.mark.parametrize(
    ("kind", "count", "ok"),
    [
        (MessageKind.IMAGE, 1, True),
        (MessageKind.FILE, 1, True),
        (MessageKind.IMAGE, 0, False),
        (MessageKind.IMAGE, 2, False),
        (MessageKind.TEXT, 0, True),
        (MessageKind.TEXT, 1, False),
    ],
)
def test_вид_и_число_вложений(kind, count, ok):
    if ok:
        validate_attachments(kind, count)
    else:
        with pytest.raises(ValueError):
            validate_attachments(kind, count)


# --- голосовые сообщения (ATT-004) ---------------------------------------------

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 16
OGG = b"OggS" + b"\x00" * 16
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16


def test_голосовые_типы_в_белом_списке_как_голосовое():
    for declared in ("audio/webm", "audio/ogg", "audio/mp4"):
        assert domain.validate_init(declared, 10).kind is MessageKind.VOICE


def test_параметр_кодека_не_меняет_голосовой_тип():
    # MediaRecorder так и называет тип: `audio/webm;codecs=opus`.
    assert domain.normalize_content_type("audio/webm;codecs=opus") == "audio/webm"
    assert domain.validate_init("audio/webm;codecs=opus", 10).kind is MessageKind.VOICE


def test_сигнатуры_аудиоконтейнеров():
    assert domain.sniff("audio/webm", WEBM)
    assert domain.sniff("audio/ogg", OGG)
    assert domain.sniff("audio/mp4", MP4)
    assert not domain.sniff("audio/webm", OGG)
    assert not domain.sniff("audio/ogg", b"MZ\x90\x00")
    assert not domain.sniff("audio/mp4", b"\x00\x00\x00\x18moovmp42")


def test_длительность_голосового_обязательна_и_ограничена():
    assert domain.validate_voice(duration_ms=5_000, size_bytes=20_000) > 0
    for bad in (None, 0, -1, domain.MIN_VOICE_MS - 1, domain.MAX_VOICE_MS + 1):
        with pytest.raises(domain.InvalidVoice):
            domain.validate_voice(duration_ms=bad, size_bytes=20_000)


def test_битрейт_считается_из_размера_и_длительности():
    # 40 000 байт за 10 с = 32 кбит/с.
    assert domain.validate_voice(duration_ms=10_000, size_bytes=40_000) == 32


def test_невозможный_битрейт_отвергается():
    # Десять мегабайт «за секунду» — не голос, а заявленная неправда.
    with pytest.raises(domain.InvalidVoice):
        domain.validate_voice(duration_ms=1_000, size_bytes=5 * domain.MIB)
    # Один байт за минуту — пустая запись.
    with pytest.raises(domain.InvalidVoice):
        domain.validate_voice(duration_ms=60_000, size_bytes=10)


def test_голосовое_сообщение_требует_ровно_одно_вложение():
    validate_attachments(MessageKind.VOICE, 1)
    with pytest.raises(ValueError):
        validate_attachments(MessageKind.VOICE, 0)
    with pytest.raises(ValueError):
        validate_attachments(MessageKind.VOICE, 2)
