"""Фото для тестов: генерируются на лету, в репозитории нет ни одного настоящего снимка."""

import io

from PIL import Image

from beauty_agent.adapters.llm.vision import PhotoDescription

GPS_IFD = 0x8825
ORIENTATION = 0x0112


def jpeg_with_metadata(width: int = 2000, height: int = 1000, *, orientation: int | None = None) -> bytes:
    """JPEG с EXIF (координаты съёмки, модель телефона) и комментарием — как с телефона клиента."""
    image = Image.new("RGB", (width, height), (220, 120, 160))
    exif = Image.Exif()
    exif[0x0110] = "Phone X"  # модель камеры
    exif.get_ifd(GPS_IFD).update({1: "N", 2: (55.0, 45.0, 0.0), 3: "E", 4: (37.0, 37.0, 0.0)})
    if orientation is not None:
        exif[ORIENTATION] = orientation
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif.tobytes(), comment=b"secret note")
    return out.getvalue()


def png_bytes(width: int = 300, height: int = 200, mode: str = "RGBA") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (width, height), (10, 200, 30, 128) if mode == "RGBA" else 0).save(out, format="PNG")
    return out.getvalue()


def gif_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("P", (10, 10)).save(out, format="GIF")
    return out.getvalue()


def french_manicure(**overrides: object) -> PhotoDescription:
    data: dict[str, object] = {
        "is_beauty_reference": True,
        "category": "nails",
        "service": "маникюр с покрытием гель-лак",
        "technique": "френч",
        "length": "short",
        "design_complexity": "simple",
        "summary": "френч с дизайном на двух ногтях",
        "confidence": 0.9,
    }
    data.update(overrides)
    return PhotoDescription.model_validate(data)
