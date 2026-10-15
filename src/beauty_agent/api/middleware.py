"""trace_id для каждого запроса: по нему в логах находятся все события одного обращения.

Чистый ASGI-middleware, без BaseHTTPMiddleware: тот ломает contextvars и стриминг ответов
и не работает с WebSocket, а веб-чат на WebSocket появится 30 октября.
"""

import re
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TRACE_HEADER = "X-Trace-ID"
# Заголовок от клиента или прокси принимается, только если он похож на ID:
# иначе через него можно подсунуть в логи переводы строк и поддельные записи.
_VALID_TRACE_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def pick_trace_id(incoming: str | None) -> str:
    """Свой ID из заголовка, если он безопасен; иначе новый."""
    if incoming and _VALID_TRACE_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


class TraceIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        raw = dict(scope["headers"]).get(TRACE_HEADER.lower().encode("latin-1"))
        trace_id = pick_trace_id(raw.decode("latin-1") if raw else None)
        # request.state.trace_id — для обработчика 500: он работает уже после выхода из middleware
        scope.setdefault("state", {})["trace_id"] = trace_id

        async def send_with_trace(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[TRACE_HEADER] = trace_id
            await send(message)

        tokens = structlog.contextvars.bind_contextvars(trace_id=trace_id)
        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            structlog.contextvars.reset_contextvars(**tokens)
