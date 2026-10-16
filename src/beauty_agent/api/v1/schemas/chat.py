"""Схемы REST API чата и истории клиента."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from beauty_agent.agent.guardrails import MAX_INPUT_CHARS
from beauty_agent.domain.enums import MessageRole, MessageStatus

# ID клиента в системе партнёра: латиница, цифры и «_.:-» — он же часть имени канала Pub/Sub
ClientId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]


class ChatMessageIn(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"client_id": "app-user-42", "text": "Сколько стоит маникюр с покрытием?", "client_name": "Анна"}
            ]
        },
    )

    client_id: ClientId = Field(description="ID клиента в вашей системе: по нему продолжается диалог")
    text: str = Field(min_length=1, max_length=MAX_INPUT_CHARS, description="Сообщение клиента")
    client_name: str | None = Field(None, max_length=200, description="Имя клиента, если известно")


class ChatAccepted(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "message_id": "0f9d4c3e-6a4b-4a57-9d5e-2b1f6c0e7a11",
                    "status": "queued",
                    "status_url": "/api/v1/chat/messages/0f9d4c3e-6a4b-4a57-9d5e-2b1f6c0e7a11",
                }
            ]
        }
    )

    message_id: uuid.UUID
    status: MessageStatus
    status_url: str


class ChatReply(BaseModel):
    message_id: uuid.UUID
    text: str
    created_at: datetime


class ChatMessageStatusOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "message_id": "0f9d4c3e-6a4b-4a57-9d5e-2b1f6c0e7a11",
                    "status": "done",
                    "reply": {
                        "message_id": "6c2e9a0f-1d3b-4f6a-8e7c-5b4a3d2c1e0f",
                        "text": "Маникюр с покрытием гель-лак — от 2 500 до 3 000 ₽. Подобрать время?",
                        "created_at": "2026-12-16T10:15:03Z",
                    },
                    "error": None,
                }
            ]
        }
    )

    message_id: uuid.UUID
    status: MessageStatus
    reply: ChatReply | None = None
    error: str | None = None


class HistoryMessage(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: MessageRole
    content: str
    created_at: datetime


class HistoryPage(BaseModel):
    items: list[HistoryMessage]
    next_cursor: str | None = Field(None, description="Передайте в cursor, чтобы получить более старые сообщения")
