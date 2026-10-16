"""Фото клиента: безопасная загрузка, проверка и подготовка для мультимодальной модели.

- Загрузка — с лимитом размера (тело читается потоком и обрывается на лимите), только https и
  только публичные адреса (rag/safe_http.py): ссылка из вебхука не заставит нас ходить во внутреннюю сеть.
- Формат проверяется по содержимому, а не по расширению: JPEG, PNG, WebP. Число пикселей ограничено
  до декодирования — «бомба» 50 000 × 50 000 пикселей не съест память воркера.
- Подготовка: поворот по EXIF, уменьшение до 1024 px по длинной стороне, перекодирование в JPEG
  без метаданных. EXIF (координаты съёмки, модель телефона), ICC и комментарии не уходят никуда.
- Фото живёт только в памяти задачи: не пишется ни на диск, ни в БД, ни в логи.
"""

import io

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from beauty_agent.domain.errors import DomainError
from beauty_agent.rag.safe_http import check_url

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_SIDE = 1024
JPEG_QUALITY = 85
ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})


class BadPhotoError(DomainError):
    """Фото не скачалось или это не фото: передаём администратору, повтор не поможет."""

    default_message = "Не удалось обработать фото"


async def download(client: httpx.AsyncClient, url: str, *, max_bytes: int = MAX_PHOTO_BYTES) -> bytes:
    """GET без редиректов (CDN мессенджеров отдают файл сразу) с лимитом размера."""
    target = check_url(url)
    if target.scheme != "https":
        raise BadPhotoError("Фото доступно только по https")
    async with client.stream("GET", target, follow_redirects=False) as response:
        if response.status_code != 200:
            raise BadPhotoError(f"Фото не скачалось: HTTP {response.status_code}")
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise BadPhotoError("Фото больше 10 МБ")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise BadPhotoError("Фото больше 10 МБ")
    return bytes(body)


def prepare_image(raw: bytes, *, max_side: int = MAX_SIDE) -> bytes:
    """Проверенное, повёрнутое, уменьшенное фото в JPEG без метаданных."""
    if len(raw) > MAX_PHOTO_BYTES:
        raise BadPhotoError("Фото больше 10 МБ")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ALLOWED_FORMATS:
                raise BadPhotoError(f"Формат {source.format} не поддерживается")
            if source.width * source.height > MAX_PIXELS:  # размеры — из заголовка, до декодирования
                raise BadPhotoError("Слишком большое изображение")
            oriented = ImageOps.exif_transpose(source)  # поворот применяется до того, как EXIF отбросим
            rgb = oriented.convert("RGB")
        rgb.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        clean = Image.new("RGB", rgb.size)
        clean.paste(rgb)  # новое изображение без info: ни EXIF, ни ICC, ни комментариев
        out = io.BytesIO()
        clean.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except BadPhotoError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise BadPhotoError(f"Это не фото или файл повреждён: {type(exc).__name__}") from exc
    return out.getvalue()
