"""Диалоги, сообщения и журнал вызовов инструментов ToolCall.

По журналу видно, почему агент ответил именно так: какой инструмент с какими аргументами
вызван, что вернул и сколько ждал. Из этих же таблиц восстанавливается истёкшая сессия Redis.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert

from beauty_agent.agent.registry import ToolOutcome
from beauty_agent.agent.state import MAX_FACTS, MAX_STORED_TURNS, DialogState, Turn
from beauty_agent.db.models import Client, Conversation, Message, ToolCall
from beauty_agent.db.repositories.base import SalonRepository
from beauty_agent.domain.enums import Channel, ConversationStatus, MessageRole


@dataclass(frozen=True, slots=True)
class Cursor:
    """Позиция в выдаче: время и id последней строки предыдущей страницы."""

    at: datetime
    id: uuid.UUID


class ConversationsRepository(SalonRepository[Conversation]):
    model = Conversation

    async def open_for(self, client_id: uuid.UUID, channel: Channel) -> Conversation:
        """Незакрытый диалог клиента в канале или новый."""
        statement = (
            self._select()
            .filter_by(client_id=client_id, channel=channel)
            .where(Conversation.status != ConversationStatus.CLOSED)
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
        conversation = await self.session.scalar(statement)
        if conversation is None:
            conversation = self.add(Conversation(client_id=client_id, channel=channel))
            await self.session.flush()
        return conversation

    async def add_message(
        self,
        conversation_id: uuid.UUID,
        role: MessageRole,
        content: str,
        *,
        external_id: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_rub: Decimal | None = None,
    ) -> uuid.UUID | None:
        """Сохраняет сообщение. Повтор вебхука с тем же external_id — None, дубль не создаётся.

        Время — clock_timestamp(), а не now(): реплика клиента и ответ агента пишутся в одной
        транзакции, и с одинаковым временем их порядок в истории был бы случайным.
        """
        statement = (
            insert(Message)
            .values(
                salon_id=self.salon_id,
                conversation_id=conversation_id,
                role=role,
                content=content,
                external_id=external_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_rub=cost_rub,
                created_at=func.clock_timestamp(),
                updated_at=func.clock_timestamp(),
            )
            .on_conflict_do_nothing(index_elements=["conversation_id", "external_id"])
            .returning(Message.id)
        )
        message_id = await self.session.scalar(statement)
        if message_id is not None:
            await self.session.execute(
                update(Conversation)
                .where(Conversation.salon_id == self.salon_id, Conversation.id == conversation_id)
                .values(last_message_at=func.clock_timestamp())
            )
        return message_id

    async def count_messages(self, conversation_id: uuid.UUID, role: MessageRole) -> int:
        statement = select(func.count()).where(
            Message.salon_id == self.salon_id, Message.conversation_id == conversation_id, Message.role == role
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def set_route(self, conversation_id: uuid.UUID, route: dict[str, Any]) -> None:
        await self.session.execute(
            update(Conversation)
            .where(Conversation.salon_id == self.salon_id, Conversation.id == conversation_id)
            .values(reply_route=route)
        )

    # ------------------------------------------------------------- инбокс: keyset-пагинация

    async def page(self, *, limit: int, after: "Cursor | None" = None) -> list[tuple[Conversation, str | None]]:
        """Диалоги салона, новые сверху; следующая страница — строго «раньше» последней строки.

        Условие (время, id) < (время_курсора, id_курсора) идёт по индексу и не замедляется
        на длинной истории, в отличие от OFFSET, который перебирает пропущенные строки.
        """
        statement = (
            select(Conversation, Client.name)
            .join(Client, Client.id == Conversation.client_id)
            .where(Conversation.salon_id == self.salon_id)
            .order_by(Conversation.last_message_at.desc(), Conversation.id.desc())
            .limit(limit)
        )
        if after is not None:
            statement = statement.where(
                tuple_(Conversation.last_message_at, Conversation.id) < tuple_(after.at, after.id)
            )
        return [(row[0], row[1]) for row in await self.session.execute(statement)]

    async def messages_page(
        self, conversation_id: uuid.UUID, *, limit: int, before: "Cursor | None" = None
    ) -> list[Message]:
        """Сообщения диалога, новые сверху; курсор — последнее сообщение предыдущей страницы."""
        statement = (
            select(Message)
            .where(Message.salon_id == self.salon_id, Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        if before is not None:
            statement = statement.where(tuple_(Message.created_at, Message.id) < tuple_(before.at, before.id))
        return list((await self.session.scalars(statement)).all())

    async def last_messages(self, conversation_id: uuid.UUID, limit: int = MAX_STORED_TURNS) -> list[Message]:
        statement = (
            select(Message)
            .where(Message.salon_id == self.salon_id, Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        rows: Sequence[Message] = (await self.session.scalars(statement)).all()
        return list(reversed(rows))

    async def add_tool_calls(self, conversation_id: uuid.UUID, outcomes: Iterable[ToolOutcome]) -> None:
        for outcome in outcomes:
            self.session.add(
                ToolCall(
                    salon_id=self.salon_id,
                    conversation_id=conversation_id,
                    name=outcome.name,
                    arguments=outcome.arguments,
                    result=outcome.result,
                    error=outcome.error,
                    duration_ms=outcome.duration_ms,
                )
            )

    async def last_tool_results(self, conversation_id: uuid.UUID, limit: int = MAX_FACTS) -> list[str]:
        statement = (
            select(ToolCall)
            .where(ToolCall.salon_id == self.salon_id, ToolCall.conversation_id == conversation_id)
            .order_by(ToolCall.created_at.desc(), ToolCall.id.desc())
            .limit(limit)
        )
        calls = list(reversed((await self.session.scalars(statement)).all()))
        return [
            ToolOutcome("restored", call.name, call.arguments, call.result, call.error, call.duration_ms).content()
            for call in calls
        ]

    async def set_summary(self, conversation_id: uuid.UUID, summary: str | None) -> None:
        await self.session.execute(
            update(Conversation)
            .where(Conversation.salon_id == self.salon_id, Conversation.id == conversation_id)
            .values(summary=summary)
        )

    async def set_status(self, conversation_id: uuid.UUID, status: ConversationStatus) -> None:
        await self.session.execute(
            update(Conversation)
            .where(Conversation.salon_id == self.salon_id, Conversation.id == conversation_id)
            .values(status=status)
        )

    async def restore_state(self, conversation_id: uuid.UUID) -> DialogState:
        """Состояние диалога из PostgreSQL, когда сессия в Redis истекла."""
        conversation = await self.get(conversation_id)
        messages = await self.last_messages(conversation_id)
        turns = [
            Turn("user" if message.role is MessageRole.CLIENT else "assistant", message.content) for message in messages
        ]
        return DialogState(
            summary=conversation.summary if conversation else None,
            turns=turns,
            facts=await self.last_tool_results(conversation_id),
        )
