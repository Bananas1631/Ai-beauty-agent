"""HTTP-клиент для обхода чужих сайтов без SSRF.

Адрес сайта салона приходит из CRM или от оператора — значит, его может подставить кто угодно.
Без защиты обходчик сходил бы на http://127.0.0.1:6379, http://169.254.169.254 (метаданные облака)
или во внутреннюю сеть и сохранил бы ответ в базу знаний, откуда его прочитает агент.

Защита:
- только http/https и порты 80/443;
- IP проверяется в момент соединения, а не до запроса: имя резолвится один раз, и подключение идёт
  к уже проверенному адресу — подмена DNS между проверкой и запросом (DNS rebinding) не проходит;
- разрешены только глобальные адреса: без частных, loopback, link-local, CGNAT, multicast,
  зарезервированных и IPv4, спрятанных в IPv6;
- редиректы — вручную и только внутри того же сайта, каждый адрес проверяется заново;
- ответ ограничен по размеру и типу, у всех операций таймауты.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from beauty_agent.domain.errors import DomainValidationError

USER_AGENT = "BeautyAgentBot/0.1 (+https://github.com/Bananas1631/Ai-beauty-agent)"
ALLOWED_PORTS = frozenset({80, 443})
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3


class UnsafeUrlError(DomainValidationError):
    default_message = "Адрес сайта недопустим"


def is_public_ip(raw: str) -> bool:
    address = ipaddress.ip_address(raw.split("%", 1)[0])  # fe80::1%eth0 — зона интерфейса не в счёт
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped or address.sixtofour or (address.teredo[1] if address.teredo else None)
        if mapped is not None:
            return is_public_ip(str(mapped))
    return address.is_global and not address.is_multicast


def check_url(url: str) -> httpx.URL:
    """Проверка схемы, хоста и порта до любого сетевого обращения."""
    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise UnsafeUrlError("Нужен адрес http:// или https://")
    if parsed.userinfo:
        raise UnsafeUrlError("Адрес с логином и паролем не допускается")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise UnsafeUrlError("Допустимы только порты 80 и 443")
    try:
        literal = ipaddress.ip_address(parsed.host)
    except ValueError:
        return parsed
    if not is_public_ip(str(literal)):
        raise UnsafeUrlError("Адрес во внутренней сети")
    return parsed


class SafeNetworkBackend(httpcore.AsyncNetworkBackend):
    """Резолвит имя сам и подключается к проверенному IP. TLS при этом проверяет исходное имя хоста."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 — сигнатуру задаёт интерфейс httpcore
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if port not in ALLOWED_PORTS:
            raise httpcore.ConnectError(f"порт {port} запрещён")
        try:
            infos = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout or 10
            )
        except (OSError, TimeoutError) as exc:
            raise httpcore.ConnectError(f"не удалось найти {host}") from exc
        addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
        if not addresses or not all(is_public_ip(address) for address in addresses):
            raise httpcore.ConnectError(f"{host} указывает во внутреннюю сеть")
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self.inner.connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise httpcore.ConnectError(str(last_error))

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 — сигнатуру задаёт интерфейс httpcore
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix-сокеты запрещены")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


def make_safe_client(*, timeout_s: float = 10.0) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(retries=0)
    # У httpx нет публичного параметра для сетевого бэкенда: подменяем пул соединений транспорта.
    # Версия httpx зафиксирована в poetry.lock; тест test_safe_client_uses_safe_backend проверит подмену.
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(),
        max_connections=5,
        network_backend=SafeNetworkBackend(),
    )
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(timeout_s, connect=5.0),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.1"},
    )


@dataclass(frozen=True, slots=True)
class Fetched:
    url: str  # адрес после редиректов
    status: int
    content_type: str
    text: str


def same_site(first: str, second: str) -> bool:
    """example.ru и www.example.ru — один сайт; поддомены и чужие домены — нет."""
    a = (urlsplit(first).hostname or "").removeprefix("www.")
    b = (urlsplit(second).hostname or "").removeprefix("www.")
    return bool(a) and a == b


async def fetch(
    client: httpx.AsyncClient, url: str, *, allowed_types: tuple[str, ...] = ("text/html",)
) -> Fetched | None:
    """GET с ручными редиректами внутри сайта и лимитом размера. None — страница не подходит."""
    current = str(check_url(url))
    for _ in range(MAX_REDIRECTS + 1):
        async with client.stream("GET", current) as response:
            if response.is_redirect:
                location = response.headers.get("location", "")
                target = urljoin(current, location)
                if not same_site(url, target):
                    return None
                current = str(check_url(target))
                continue
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if response.status_code != 200 or not content_type.startswith(allowed_types):
                return None
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                return None
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    return None
            encoding = response.encoding or "utf-8"
            return Fetched(current, response.status_code, content_type, body.decode(encoding, errors="replace"))
    return None
