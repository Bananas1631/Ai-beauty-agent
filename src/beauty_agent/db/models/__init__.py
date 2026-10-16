"""Все модели в одном месте.

Импорт пакета регистрирует таблицы в Base.metadata — по ним Alembic строит миграции.
"""

from beauty_agent.db.base import Base
from beauty_agent.db.models.admin import Draft, SalonAdmin
from beauty_agent.db.models.api_key import ApiKey
from beauty_agent.db.models.booking import Booking, Reminder
from beauty_agent.db.models.catalog import Master, MasterService, Service
from beauty_agent.db.models.client import Client, ClientIdentity
from beauty_agent.db.models.consent import Consent
from beauty_agent.db.models.conversation import Conversation, Message, ToolCall
from beauty_agent.db.models.event_outbox import EventOutbox
from beauty_agent.db.models.knowledge import KnowledgeChunk, KnowledgeItem
from beauty_agent.db.models.outbox import OutboundMessage
from beauty_agent.db.models.salon import ChannelAccount, Salon

__all__ = [
    "ApiKey",
    "Base",
    "Booking",
    "ChannelAccount",
    "Client",
    "ClientIdentity",
    "Consent",
    "Conversation",
    "Draft",
    "EventOutbox",
    "KnowledgeChunk",
    "KnowledgeItem",
    "Master",
    "MasterService",
    "Message",
    "OutboundMessage",
    "Reminder",
    "Salon",
    "SalonAdmin",
    "Service",
    "ToolCall",
]
