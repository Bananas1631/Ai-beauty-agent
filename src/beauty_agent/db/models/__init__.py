"""Все модели в одном месте.

Импорт пакета регистрирует таблицы в Base.metadata — по ним Alembic строит миграции.
"""

from beauty_agent.db.base import Base
from beauty_agent.db.models.admin import Draft, SalonAdmin
from beauty_agent.db.models.booking import Booking
from beauty_agent.db.models.catalog import Master, MasterService, Service
from beauty_agent.db.models.client import Client, ClientIdentity
from beauty_agent.db.models.consent import Consent
from beauty_agent.db.models.conversation import Conversation, Message, ToolCall
from beauty_agent.db.models.knowledge import KnowledgeChunk, KnowledgeItem
from beauty_agent.db.models.outbox import OutboundMessage
from beauty_agent.db.models.salon import ChannelAccount, Salon

__all__ = [
    "Base",
    "Booking",
    "ChannelAccount",
    "Client",
    "ClientIdentity",
    "Consent",
    "Conversation",
    "Draft",
    "KnowledgeChunk",
    "KnowledgeItem",
    "Master",
    "MasterService",
    "Message",
    "OutboundMessage",
    "Salon",
    "SalonAdmin",
    "Service",
    "ToolCall",
]
