"""Приём вебхуков мессенджеров: проверка подписи, отсев повторов, очередь и ответ 200 за <100 мс.

Мессенджер не ждёт, пока агент думает: сообщение уходит в очередь, обработку делает воркер.
Подпись проверяется первой, до обращения к базе: чужой запрос не нагружает БД и не узнаёт,
существует ли канал. Тело ограничено 256 КБ — сообщения мессенджеров намного меньше.
"""

import hmac
import json
import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse

from beauty_agent.adapters.channels import telegram, vk
from beauty_agent.api.deps import SessionDep
from beauty_agent.conversation.inbound import InboundGate
from beauty_agent.core.security import Security
from beauty_agent.db.repositories.channels import ChannelAccountsRepository
from beauty_agent.domain.enums import Channel

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"], include_in_schema=False)

MAX_BODY_BYTES = 256 * 1024


async def read_json(request: Request) -> dict[str, Any]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Тело запроса — не JSON") from exc
    if not isinstance(data, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ожидался JSON-объект")
    return data


def _security(request: Request) -> Security:
    security: Security = request.app.state.security
    return security


def _inbound(request: Request) -> InboundGate:
    gate: InboundGate = request.app.state.inbound
    return gate


@router.post("/telegram/{account_id}")
async def telegram_webhook(
    account_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    secret: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
) -> Response:
    if not _security(request).verify_webhook("telegram", account_id, secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    payload = await read_json(request)
    found = await ChannelAccountsRepository(session).get_with_salon(account_id)
    # Канал отключён — всё равно 200: иначе Telegram будет повторять обновление часами
    if found is not None and found[0].channel in (Channel.TELEGRAM, Channel.TELEGRAM_BUSINESS):
        update = telegram.parse_update(payload)
        if update is not None:
            await _inbound(request).accept(account_id, update)
    return Response(status_code=status.HTTP_200_OK)


@router.post("/vk/{account_id}")
async def vk_webhook(account_id: uuid.UUID, request: Request, session: SessionDep) -> Response:
    payload = await read_json(request)
    security = _security(request)
    expected = vk.vk_secret(security.webhook_secret("vk", account_id))
    provided = str(payload.get("secret") or "")
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    found = await ChannelAccountsRepository(session).get_with_salon(account_id)
    if found is None or found[0].channel is not Channel.VK:
        return PlainTextResponse("ok")
    account, _ = found
    if payload.get("type") == "confirmation":
        credentials = security.decrypt(account.token_encrypted)
        return PlainTextResponse(str(credentials.get("confirmation", "")))
    update = vk.parse_event(payload)
    if update is not None:
        await _inbound(request).accept(account_id, update)
    return PlainTextResponse("ok")  # VK ждёт ровно «ok», иначе повторит событие
