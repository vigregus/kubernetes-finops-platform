"""Миниатюры: разбор недоверенного изображения (адаптер на Pillow)."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from messenger.adapters.imaging import PillowThumbnailer
from messenger.domain.attachment import MAX_SOURCE_PIXELS, UnreadableImage

make = PillowThumbnailer().make


def _encode(image: Image.Image, fmt: str, **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_большая_картинка_ужимается_до_предела_с_сохранением_пропорций():
    thumb = make(_encode(Image.new("RGB", (2000, 1000), "red"), "PNG"))
    assert (thumb.width, thumb.height) == (480, 240)
    assert (thumb.source_width, thumb.source_height) == (2000, 1000)
    assert thumb.content_type == "image/webp"
    decoded = _open(thumb.data)
    assert decoded.format == "WEBP"
    assert decoded.size == (480, 240)


def test_малая_картинка_не_увеличивается():
    thumb = make(_encode(Image.new("RGB", (100, 50), "blue"), "PNG"))
    assert (thumb.width, thumb.height) == (100, 50)


def test_поворот_по_exif_применяется_и_размеры_называют_показываемое():
    # Файл лежит 600x400, а камера велела показывать его повёрнутым на 90°.
    image = Image.new("RGB", (600, 400), "green")
    exif = Image.Exif()
    exif[0x0112] = 6
    thumb = make(_encode(image, "JPEG", exif=exif))
    assert (thumb.source_width, thumb.source_height) == (400, 600)
    assert (thumb.width, thumb.height) == (320, 480)


def test_метаданные_не_переживают_ни_местоположение_ни_камера():
    image = Image.new("RGB", (800, 600), "white")
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMaker"
    exif[0x0112] = 1
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    thumb = make(_encode(image, "JPEG", exif=exif))
    decoded = _open(thumb.data)
    assert "exif" not in decoded.info
    assert b"SecretCameraMaker" not in thumb.data


def test_прозрачность_сохраняется():
    thumb = make(_encode(Image.new("RGBA", (600, 600), (255, 0, 0, 0)), "PNG"))
    assert _open(thumb.data).mode in ("RGBA", "LA")


def test_анимированный_gif_даёт_миниатюру_первого_кадра():
    frames = [Image.new("P", (900, 600), i) for i in (0, 1, 2)]
    data = _encode(frames[0], "GIF", save_all=True, append_images=frames[1:], loop=0)
    thumb = make(data)
    assert (thumb.width, thumb.height) == (480, 320)
    assert getattr(_open(thumb.data), "n_frames", 1) == 1


def test_палитровая_и_cmyk_картинки_приводятся_к_показываемому_виду():
    assert make(_encode(Image.new("P", (600, 600), 3), "PNG")).width == 480
    assert make(_encode(Image.new("CMYK", (600, 600)), "JPEG")).width == 480


def test_битый_файл_не_разбирается():
    with pytest.raises(UnreadableImage):
        make(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    with pytest.raises(UnreadableImage):
        make(b"")


def test_бомба_сжатия_отвергается_до_распаковки():
    side = int(MAX_SOURCE_PIXELS**0.5) + 100
    bomb = _encode(Image.new("1", (side, side)), "PNG")
    # Файл крошечный, а в памяти он занял бы десятки мегабайт на пиксель.
    assert len(bomb) < 1_000_000
    with pytest.raises(UnreadableImage):
        make(bomb)


def test_большой_jpeg_с_поворотом_даёт_верные_размеры_без_полной_распаковки():
    # 4000x3000 в осях файла, показывать повёрнутым: 3000x4000 → 360x480.
    exif = Image.Exif()
    exif[0x0112] = 6
    thumb = make(_encode(Image.new("RGB", (4000, 3000), "gray"), "JPEG", exif=exif))
    assert (thumb.source_width, thumb.source_height) == (3000, 4000)
    assert (thumb.width, thumb.height) == (360, 480)
