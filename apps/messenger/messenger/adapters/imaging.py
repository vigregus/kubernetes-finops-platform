"""Миниатюры изображений на Pillow. Единственное место, знающее про разбор картинок.

Файл здесь **недоверенный**: он прошёл сверку сигнатуры и сканер, но разбирает
его сложный декодер, и именно декодеры изображений — классический источник
уязвимостей и «бомб сжатия». Поэтому порядок такой:

1. размер читается из заголовка, **до** распаковки пикселей, и больше
   `MAX_SOURCE_PIXELS` отвергается — файл в десять мегабайт не должен
   превращаться в гигабайты памяти воркера;
2. разбор идёт в воркере, а не в API, у которого свои задержки;
3. метаданные (EXIF: местоположение, камера, время) в миниатюру **не
   переходят**: WebP сохраняется без них, а поворот по EXIF применяется к
   пикселям — человек видит ту же сторону, что на оригинале.
"""
from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from messenger.domain.attachment import (
    MAX_SOURCE_PIXELS,
    THUMBNAIL_MAX_PX,
    Thumbnail,
    UnreadableImage,
    fit_within,
)

# Предупреждение Pillow о подозрительно большой картинке — не отказ, а наш
# собственный предел ниже; превращаем его в исключение, чтобы не пропустить.
warnings.simplefilter("error", Image.DecompressionBombWarning)

_WEBP_QUALITY = 80


class PillowThumbnailer:
    def make(self, content: bytes) -> Thumbnail:
        try:
            return self._make(content)
        except UnreadableImage:
            raise
        except (
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            ValueError,
            SyntaxError,  # так Pillow сообщает о битом PNG
            EOFError,
            MemoryError,
        ) as exc:
            raise UnreadableImage(type(exc).__name__) from exc

    def _make(self, content: bytes) -> Thumbnail:
        with Image.open(io.BytesIO(content)) as image:
            # Размер известен из заголовка: проверяем до `load()`.
            if image.width * image.height > MAX_SOURCE_PIXELS:
                raise UnreadableImage("слишком много пикселей")
            image.seek(0)  # анимация: берётся первый кадр

            # Размеры оригинала «как показывается» считаются **до** любых
            # ужатий при чтении: поворот по EXIF на 90° меняет стороны местами.
            source_width, source_height = image.size
            rotated = image.getexif().get(0x0112) in (5, 6, 7, 8)
            if rotated:
                source_width, source_height = source_height, source_width
            target = fit_within(source_width, source_height, THUMBNAIL_MAX_PX)

            if image.format == "JPEG":
                # JPEG умеет распаковываться сразу в 1/2, 1/4 или 1/8 размера:
                # снимок на 48 мегапикселей не занимает сотни мегабайт в
                # воркере с лимитом памяти, а на качество миниатюры в 480 px
                # это не влияет — декодер не опускается ниже запрошенного.
                # Запрошенный размер — в осях файла, а не повёрнутого вида.
                image.draft("RGB", target[::-1] if rotated else target)
            oriented = ImageOps.exif_transpose(image)
            has_alpha = oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info
            frame = oriented.convert("RGBA" if has_alpha else "RGB")
            frame.thumbnail(target, Image.Resampling.LANCZOS)

            out = io.BytesIO()
            frame.save(out, format="WEBP", quality=_WEBP_QUALITY, method=4)
            return Thumbnail(
                data=out.getvalue(),
                content_type="image/webp",
                width=frame.width,
                height=frame.height,
                source_width=source_width,
                source_height=source_height,
            )
