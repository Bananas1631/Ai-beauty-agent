"""WebSocket веб-чата и встраиваемый виджет /widget.js для сайтов салонов.

- Неизвестный салон или чужой Origin — соединение закрывается до accept (защита от CSWSH:
  чужой сайт не сможет говорить с агентом от имени посетителя).
- Сессия — случайный токен в localStorage браузера; по нему узнаём клиента между визитами.
- Лимиты частоты на сессию и IP; сообщения одной сессии обрабатываются по одному.
- Ответ отправляется только текстом: страница вставляет его через textContent, а не innerHTML.
"""

import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.agent.guardrails import MAX_INPUT_CHARS
from beauty_agent.api.deps import SLUG_PATTERN
from beauty_agent.conversation.service import ConversationService, IncomingMessage
from beauty_agent.core.config import Settings
from beauty_agent.core.rate_limit import IP_LIMIT, SESSION_LIMIT, RateLimiter
from beauty_agent.core.redis import LockBusyError
from beauty_agent.db.models import Salon
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.domain.enums import Channel

logger = structlog.get_logger(__name__)
router = APIRouter()

_SESSION = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SLUG = re.compile(SLUG_PATTERN)


WIDGET = Path(__file__).parent / "static" / "widget.js"


@router.get("/widget.js", include_in_schema=False)
async def widget_script() -> FileResponse:
    """Скрипт виджета для чужих сайтов: кэшируется браузером на час, обновление — с новым деплоем."""
    return FileResponse(
        WIDGET,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def origin_allowed(origin: str | None, salon: Salon, settings: Settings) -> bool:
    if not origin:
        return False
    allowed = {_origin(settings.app.public_url), *salon.config.allowed_origins}
    return origin.rstrip("/").lower() in allowed


async def _load_salon(factory: async_sessionmaker[AsyncSession], slug: str) -> Salon | None:
    if len(slug) > 64 or not _SLUG.fullmatch(slug):
        return None
    async with factory() as session:
        return await SalonsRepository(session).get_by_slug(slug)


def _text_of(data: Any) -> str | None:
    if not isinstance(data, dict) or data.get("type") != "message":
        return None
    text = data.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    return text if 0 < len(text) <= MAX_INPUT_CHARS else None


@router.websocket("/ws/s/{slug}")
async def widget(websocket: WebSocket, slug: str) -> None:
    app = websocket.app
    settings: Settings = app.state.settings
    salon = await _load_salon(app.state.session_factory, slug)
    if salon is None or not origin_allowed(websocket.headers.get("origin"), salon, settings):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()

    session_id = websocket.query_params.get("session") or ""
    if not _SESSION.fullmatch(session_id):
        session_id = secrets.token_urlsafe(24)
        await websocket.send_json({"type": "session", "id": session_id})
    ip = websocket.client.host if websocket.client else "unknown"
    limiter = RateLimiter(app.state.redis)
    service: ConversationService = app.state.conversations

    while True:
        try:
            data = await websocket.receive_json()
        except WebSocketDisconnect:
            return
        except ValueError:  # не JSON
            await websocket.send_json(
                {"type": "error", "code": "bad_request", "text": "Не удалось прочитать сообщение"}
            )
            continue
        text = _text_of(data)
        if text is None:
            await websocket.send_json(
                {"type": "error", "code": "bad_request", "text": f"Сообщение — от 1 до {MAX_INPUT_CHARS} символов"}
            )
            continue
        if not await limiter.hit(SESSION_LIMIT, session_id) or not await limiter.hit(IP_LIMIT, ip):
            await websocket.send_json(
                {"type": "error", "code": "rate_limited", "text": "Слишком много сообщений — подождите минуту"}
            )
            continue

        await websocket.send_json({"type": "typing"})
        try:
            reply = await service.handle(IncomingMessage(salon, Channel.WEB, session_id, text))
        except LockBusyError:
            await websocket.send_json({"type": "error", "code": "busy", "text": "Ещё отвечаю на прошлое сообщение"})
            continue
        except Exception:
            logger.exception("widget_message_failed", salon_id=str(salon.id))
            await websocket.send_json(
                {"type": "error", "code": "internal", "text": "Не получилось ответить. Попробуйте ещё раз чуть позже."}
            )
            continue
        if reply.text:
            await websocket.send_json({"type": "message", "text": reply.text, "handed_off": reply.handed_off})
