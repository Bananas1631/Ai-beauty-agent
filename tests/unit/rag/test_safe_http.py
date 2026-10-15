import socket
from typing import Any

import httpcore
import httpx
import pytest

from beauty_agent.rag import safe_http
from beauty_agent.rag.safe_http import (
    MAX_BODY_BYTES,
    SafeNetworkBackend,
    UnsafeUrlError,
    check_url,
    fetch,
    is_public_ip,
    make_safe_client,
    same_site,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "169.254.169.254",  # метаданные облака
        "100.64.0.1",  # CGNAT
        "0.0.0.0",  # noqa: S104 — это проверяемый адрес, а не привязка сервера
        "224.0.0.1",
        "::1",
        "fe80::1%eth0",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4 внутри IPv6
        "2002:7f00:1::",  # 6to4 с адресом 127.0.0.1
    ],
)
def test_internal_addresses_are_not_public(address: str) -> None:
    assert not is_public_ip(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "77.88.55.242", "2a02:6b8::2:242"])
def test_public_addresses(address: str) -> None:
    assert is_public_ip(address)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://salon.example/",
        "gopher://salon.example/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://salon.example:6379/",
        "https://user:pass@salon.example/",
        "http:///nohost",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        check_url(url)


def test_normal_url_passes() -> None:
    assert str(check_url("https://salon.example/prices")) == "https://salon.example/prices"


def test_same_site() -> None:
    assert same_site("https://salon.example/", "https://www.salon.example/prices")
    assert not same_site("https://salon.example/", "https://evil.example/")
    assert not same_site("https://salon.example/", "https://salon.example.evil.example/")


async def test_backend_rejects_hosts_resolving_inside(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(_self: Any, host: str, port: int, **_: Any) -> list[tuple[Any, ...]]:
        # DNS-имя снаружи, а адрес — внутри: так выглядит SSRF через DNS rebinding
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", resolve)

    with pytest.raises(httpcore.ConnectError, match="внутреннюю сеть"):
        await SafeNetworkBackend().connect_tcp("innocent.example", 443)


async def test_backend_rejects_other_ports_and_unix_sockets() -> None:
    backend = SafeNetworkBackend()
    with pytest.raises(httpcore.ConnectError, match="порт"):
        await backend.connect_tcp("salon.example", 6379)
    with pytest.raises(httpcore.ConnectError, match="unix"):
        await backend.connect_unix_socket("/var/run/docker.sock")


async def test_safe_client_uses_safe_backend() -> None:
    client = make_safe_client()
    try:
        transport = client._transport
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        assert isinstance(transport._pool._network_backend, SafeNetworkBackend)  # подмена пула не потерялась
        assert client.follow_redirects is False
        assert "BeautyAgentBot" in client.headers["User-Agent"]
    finally:
        await client.aclose()


def mock_client(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


HTML = {"content-type": "text/html; charset=utf-8"}


async def test_redirect_to_other_site_is_not_followed() -> None:
    client = mock_client(
        {"https://salon.example/": httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})}
    )

    assert await fetch(client, "https://salon.example/") is None


async def test_redirect_inside_site_is_followed() -> None:
    client = mock_client(
        {
            "https://salon.example/": httpx.Response(301, headers={"location": "https://www.salon.example/"}),
            "https://www.salon.example/": httpx.Response(200, text="<p>ok</p>", headers=HTML),
        }
    )

    fetched = await fetch(client, "https://salon.example/")

    assert fetched is not None
    assert fetched.url == "https://www.salon.example/"


async def test_size_and_type_limits() -> None:
    big = "x" * (MAX_BODY_BYTES + 1)
    client = mock_client(
        {
            "https://salon.example/big": httpx.Response(200, text=big, headers=HTML),
            "https://salon.example/file.pdf": httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            ),
        }
    )

    assert await fetch(client, "https://salon.example/big") is None
    assert await fetch(client, "https://salon.example/file.pdf") is None


def test_max_body_is_reasonable() -> None:
    assert safe_http.MAX_BODY_BYTES <= 5 * 1024 * 1024
