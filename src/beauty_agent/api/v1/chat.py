"""REST API чата для внешних интеграций: сайт салона, мобильное приложение, партнёрская CRM.

- POST /api/v1/chat сохраняет сообщение и сразу отвечает 202 с ID и ссылкой на статус: ответ LLM
  занимает секунды, держать всё это время HTTP-соединение нельзя. Ответ считает воркер Celery.
- GET /api/v1/chat/messages/{id} — статус (в очереди, обрабатывается, готово, ошибка) и ответ агента;
  статус хранится в строке сообщения в PostgreSQL, а не в result backend Celery.
- WebSocket /api/v1/chat/ws?client_id=… — готовые ответы без опроса (Redis Pub/Sub).
- Доступ — по ключу API салона в заголовке X-API-Key (в БД только хеш) или по токену OAuth2 со
  scope chat:write (api/partner.py); салон берётся из ключа или токена.
- Повторный POST с тем же Idempotency-Key не создаёт второе сообщение и не ставит вторую задачу.
"""

import asyncio
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Header, HTTPException, Query, Request, WebSocket, status
from fastapi.websockets import WebSocketDisconnect
from pydantic import ValidationError

from beauty_agent.api.deps import SessionDep
from beauty_agent.api.partner import ChatWriter
from beauty_agent.api.v1.schemas.chat import (
    ChatAccepted,
    ChatMessageIn,
    ChatMessageStatusOut,
    ChatReply,
    ClientId,
)
from beauty_agent.conversation.notifier import ChatEvents
from beauty_agent.conversation.service import ConversationService, IncomingMessage
from beauty_agent.core.rate_limit import API_KEY_LIMIT, RateLimiter
from beauty_agent.core.security import API_AUDIENCE, Security
from beauty_agent.db.models import Salon
from beauty_agent.db.repositories.api_keys import ApiKeysRepository
from beauty_agent.db.repositories.conversations import ConversationsRepository
from beauty_agent.domain.enums import Channel, MessageRole, MessageStatus
from beauty_agent.domain.errors import DomainError
from beauty_agent.runtime import Runtime

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        pattern=r"^[A-Za-z0-9_.:-]{8,100}$",
        description="Повтор запроса с тем же ключом не создаёт второе сообщение",
    ),
]


ApiSalon = ChatWriter  # ключ API или токен со scope chat:write


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=ChatAccepted)
async def post_message(
    body: ChatMessageIn, request: Request, salon: ApiSalon, idempotency_key: IdempotencyKey = None
) -> ChatAccepted:
    service: ConversationService = request.app.state.conversations
    message_id, created = await service.accept(
        IncomingMessage(
            salon,
            Channel.API,
            body.client_id,
            body.text,
            external_message_id=f"api:{idempotency_key}" if idempotency_key else None,
            client_name=body.client_name,
        )
    )
    if created:
        runtime: Runtime = request.app.state.runtime
        await runtime.queue.process_chat(salon.id, message_id)
    async with request.app.state.session_factory() as session:
        stored = await ConversationsRepository(session, salon.id).message(message_id)
    current = stored.status if stored is not None and stored.status is not None else MessageStatus.QUEUED
    return ChatAccepted(message_id=message_id, status=current, status_url=f"/api/v1/chat/messages/{message_id}")


@router.get("/messages/{message_id}", response_model=ChatMessageStatusOut)
async def get_message(message_id: uuid.UUID, salon: ApiSalon, session: SessionDep) -> ChatMessageStatusOut:
    conversations = ConversationsRepository(session, salon.id)
    message = await conversations.message(message_id)
    if message is None or message.role is not MessageRole.CLIENT:  # сообщение другого салона не найдётся
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сообщение не найдено")
    reply = await conversations.reply_of(message_id)
    return ChatMessageStatusOut(
        message_id=message.id,
        status=message.status or MessageStatus.DONE,
        reply=ChatReply(message_id=reply.id, text=reply.content, created_at=reply.created_at) if reply else None,
        error=message.error,
    )


@router.websocket("/ws")
async def chat_events(websocket: WebSocket, client_id: Annotated[str, Query(max_length=128)]) -> None:
    """Готовые ответы клиенту client_id. Ключ — в заголовке X-API-Key или токен chat:write в Authorization."""
    app = websocket.app
    try:
        valid_client = ChatMessageIn.model_validate({"client_id": client_id, "text": "-"}).client_id
    except ValidationError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    salon = await _websocket_salon(websocket)
    if salon is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    events = ChatEvents(app.state.redis)
    async with events.subscribe(salon.id, valid_client) as pubsub:

        async def forward() -> None:
            async for event in pubsub.listen():
                if event.get("type") == "message":
                    await websocket.send_text(str(event["data"]))

        async def until_closed() -> None:
            while True:
                await websocket.receive_text()  # входящие игнорируем: канал только для ответов

        tasks = [asyncio.create_task(forward()), asyncio.create_task(until_closed())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            error = task.exception()
            if error is not None and not isinstance(error, WebSocketDisconnect):
                logger.warning("chat_ws_failed", error=type(error).__name__)


async def _websocket_salon(websocket: WebSocket) -> Salon | None:
    """Салон по ключу API или токену chat:write; None — нет доступа или исчерпан лимит."""
    app = websocket.app
    api_key = websocket.headers.get("x-api-key") or ""
    authorization = websocket.headers.get("authorization") or ""
    async with app.state.session_factory() as session:
        if api_key:
            found = await ApiKeysRepository(session).authenticate(api_key)
            if found is None:
                return None
            subject, salon = str(found[0].id), found[1]
        elif authorization.lower().startswith("bearer "):
            security: Security = app.state.security
            try:
                claims = security.verify_access_token(authorization[7:], audience=API_AUDIENCE, scope="chat:write")
            except DomainError:
                return None
            found_salon = await session.get(Salon, claims.salon_id) if claims.salon_id else None
            if found_salon is None:
                return None
            subject, salon = f"oauth:{claims.subject}", found_salon
        else:
            return None
    if not await RateLimiter(app.state.redis).hit(API_KEY_LIMIT, subject):
        return None
    return salon


__all__ = ["ApiSalon", "ClientId", "router"]
