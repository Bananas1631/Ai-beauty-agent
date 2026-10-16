"""API инбокса для будущей веб-админки: диалоги салона и сообщения диалога.

- Доступ только по JWT из админ-бота (/token): в инбоксе персональные данные клиентов.
  Токен проверяется полностью (подпись HS256, срок, издатель, аудитория), а администратор —
  ещё и по БД: отвязанный администратор теряет доступ сразу, не дожидаясь конца срока токена.
- Салон берётся из токена, а не из адреса: чужой салон не запросить, подставив его ID.
- Keyset-пагинация по (время, id): курсор — непрозрачная строка с последней строкой страницы.
  OFFSET на длинной истории перебирал бы все пропущенные строки.
"""

import base64
import json
import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from beauty_agent.api.deps import SessionDep
from beauty_agent.core.security import AdminClaims, InvalidTokenError, Security
from beauty_agent.db.repositories.admins import AdminsRepository
from beauty_agent.db.repositories.conversations import ConversationsRepository, Cursor
from beauty_agent.domain.enums import Channel, ConversationStatus, MessageRole

router = APIRouter(prefix="/admin", tags=["admin"])
_bearer = HTTPBearer(auto_error=False)

_UNAUTHORIZED = HTTPException(
    status.HTTP_401_UNAUTHORIZED, "Нужен токен из админ-бота", headers={"WWW-Authenticate": "Bearer"}
)


async def current_admin(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AdminClaims:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _UNAUTHORIZED
    security: Security = request.app.state.security
    try:
        claims = security.verify_admin_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise _UNAUTHORIZED from exc
    if not await AdminsRepository(session).is_admin(claims.salon_id, claims.user_id):
        raise _UNAUTHORIZED
    structlog.contextvars.bind_contextvars(salon_id=str(claims.salon_id))
    return claims


AdminDep = Annotated[AdminClaims, Depends(current_admin)]
Limit = Annotated[int, Query(ge=1, le=100)]
CursorParam = Annotated[str | None, Query(max_length=200)]


def encode_cursor(at: datetime, id_: uuid.UUID) -> str:
    raw = json.dumps({"t": at.isoformat(), "id": str(id_)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None) -> Cursor | None:
    if not value:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        at = datetime.fromisoformat(data["t"])
        if at.tzinfo is None:
            raise ValueError("время курсора без часового пояса")
        return Cursor(at, uuid.UUID(data["id"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный курсор") from exc


class ConversationOut(BaseModel):
    id: uuid.UUID
    channel: Channel
    status: ConversationStatus
    client_name: str | None
    last_message_at: datetime


class ConversationsPage(BaseModel):
    items: list[ConversationOut]
    next_cursor: str | None


class MessageOut(BaseModel):
    id: uuid.UUID
    role: MessageRole
    content: str
    created_at: datetime


class MessagesPage(BaseModel):
    items: list[MessageOut]
    next_cursor: str | None


@router.get("/conversations")
async def list_conversations(
    admin: AdminDep, session: SessionDep, limit: Limit = 30, cursor: CursorParam = None
) -> ConversationsPage:
    rows = await ConversationsRepository(session, admin.salon_id).page(limit=limit, after=decode_cursor(cursor))
    items = [
        ConversationOut(
            id=conversation.id,
            channel=conversation.channel,
            status=conversation.status,
            client_name=client_name,
            last_message_at=conversation.last_message_at,
        )
        for conversation, client_name in rows
    ]
    last = items[-1] if len(items) == limit else None
    return ConversationsPage(items=items, next_cursor=encode_cursor(last.last_message_at, last.id) if last else None)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: uuid.UUID, admin: AdminDep, session: SessionDep, limit: Limit = 50, cursor: CursorParam = None
) -> MessagesPage:
    conversations = ConversationsRepository(session, admin.salon_id)
    if await conversations.get(conversation_id) is None:  # диалог другого салона не найдётся
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Диалог не найден")
    rows = await conversations.messages_page(conversation_id, limit=limit, before=decode_cursor(cursor))
    items = [MessageOut(id=row.id, role=row.role, content=row.content, created_at=row.created_at) for row in rows]
    last = items[-1] if len(items) == limit else None
    return MessagesPage(items=items, next_cursor=encode_cursor(last.created_at, last.id) if last else None)
