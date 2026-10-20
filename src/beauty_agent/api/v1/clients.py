"""История диалогов клиента для интеграций: GET /api/v1/clients/{client_id}/history.

client_id — ID клиента в системе партнёра (тот же, что в POST /api/v1/chat). Keyset-пагинация
по (время, id): курсор из next_cursor возвращает более старые сообщения. Ключ салона A не видит
клиентов салона B: поиск клиента и сообщений идёт в пределах салона из ключа.
Доступ — ключ API салона или токен OAuth2 со scope history:read.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from beauty_agent.api.deps import SessionDep
from beauty_agent.api.pagination import decode_cursor, encode_cursor
from beauty_agent.api.partner import HistoryReader
from beauty_agent.api.v1.schemas.chat import HistoryMessage, HistoryPage
from beauty_agent.db.repositories.clients import ClientsRepository
from beauty_agent.db.repositories.conversations import ConversationsRepository
from beauty_agent.domain.enums import Channel

router = APIRouter(prefix="/clients", tags=["chat"])


@router.get("/{client_id}/history", response_model=HistoryPage)
async def client_history(
    client_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")],
    salon: HistoryReader,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> HistoryPage:
    client = await ClientsRepository(session, salon.id).by_identity(Channel.API, client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Клиент не найден")
    rows = await ConversationsRepository(session, salon.id).client_history(
        client.id, limit=limit, before=decode_cursor(cursor)
    )
    items = [
        HistoryMessage(
            id=row.id,
            conversation_id=row.conversation_id,
            role=row.role,
            content=row.content,
            created_at=row.created_at,
        )
        for row in rows
    ]
    last = items[-1] if len(items) == limit else None
    return HistoryPage(items=items, next_cursor=encode_cursor(last.created_at, last.id) if last else None)
