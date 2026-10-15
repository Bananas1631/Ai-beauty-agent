"""Заголовки безопасности для всех HTTP-ответов, строгая CSP — для HTML-страниц.

CSP разрешает скрипты, стили и соединения только со своего домена: даже если в страницу
попадёт чужой текст, браузер не выполнит внедрённый скрипт и не отправит данные на чужой сервер.
Swagger (/docs) грузит скрипты с CDN, поэтому CSP к нему не применяется — в проде его нет.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_NO_CSP_PREFIXES = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault("X-Frame-Options", "DENY")
                content_type = headers.get("content-type", "")
                if content_type.startswith("text/html") and not path.startswith(_NO_CSP_PREFIXES):
                    headers.setdefault("Content-Security-Policy", CSP)
            await send(message)

        await self.app(scope, receive, send_with_headers)
