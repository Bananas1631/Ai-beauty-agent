"""Подготовка фото: без EXIF и комментариев, с поворотом, уменьшением и защитой от «бомб»."""

import io
from collections.abc import Callable

import httpx
import pytest
from PIL import Image
from tests.photo_helpers import GPS_IFD, gif_bytes, jpeg_with_metadata, png_bytes

from beauty_agent.media import images
from beauty_agent.media.images import MAX_PHOTO_BYTES, BadPhotoError, download, prepare_image
from beauty_agent.rag.safe_http import UnsafeUrlError


def opened(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def test_metadata_is_stripped_and_photo_is_resized() -> None:
    raw = jpeg_with_metadata(2000, 1000)
    assert opened(raw).getexif().get_ifd(GPS_IFD)  # исходник действительно с координатами

    result = opened(prepare_image(raw))

    assert result.format == "JPEG"
    assert result.size == (1024, 512)
    assert not result.getexif()  # ни координат, ни модели телефона
    assert "exif" not in result.info
    assert "comment" not in result.info
    assert "icc_profile" not in result.info


def test_orientation_is_applied_before_exif_is_dropped() -> None:
    result = opened(prepare_image(jpeg_with_metadata(400, 200, orientation=6)))  # снято «на боку»

    assert result.size == (200, 400)


def test_png_with_transparency_becomes_jpeg() -> None:
    result = opened(prepare_image(png_bytes()))

    assert (result.format, result.mode, result.size) == ("JPEG", "RGB", (300, 200))


@pytest.mark.parametrize(
    "raw",
    [gif_bytes(), b"not an image at all", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, b"x" * (MAX_PHOTO_BYTES + 1)],
    ids=["gif", "text", "broken-png", "too-big"],
)
def test_not_a_photo_is_rejected(raw: bytes) -> None:
    with pytest.raises(BadPhotoError):
        prepare_image(raw)


def test_pixel_bomb_is_rejected_before_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images, "MAX_PIXELS", 100)

    with pytest.raises(BadPhotoError, match="Слишком большое"):
        prepare_image(png_bytes(20, 20))


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_download_with_limits() -> None:
    photo = jpeg_with_metadata(100, 100)

    async with client(lambda _: httpx.Response(200, content=photo)) as ok:
        assert await download(ok, "https://sun9-1.userapi.com/photo.jpg") == photo
    async with client(lambda _: httpx.Response(200, content=b"x" * 2048)) as big:
        with pytest.raises(BadPhotoError, match="больше"):
            await download(big, "https://cdn.example/photo.jpg", max_bytes=1024)
    async with client(lambda _: httpx.Response(404)) as missing:
        with pytest.raises(BadPhotoError, match="404"):
            await download(missing, "https://cdn.example/photo.jpg")
    async with client(lambda _: httpx.Response(302, headers={"location": "http://10.0.0.1/"})) as redirect:
        with pytest.raises(BadPhotoError):
            await download(redirect, "https://cdn.example/photo.jpg")  # редиректы не выполняются


@pytest.mark.parametrize("url", ["http://cdn.example/photo.jpg", "https://127.0.0.1/photo.jpg", "file:///etc/passwd"])
async def test_download_refuses_unsafe_urls(url: str) -> None:
    async with client(lambda _: httpx.Response(200, content=b"never")) as http:
        with pytest.raises((BadPhotoError, UnsafeUrlError)):
            await download(http, url)
