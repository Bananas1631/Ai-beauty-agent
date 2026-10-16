"""Общие фикстуры тестов мессенджеров: салон на фейковом CRM и подключённый бот Telegram."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.channel_helpers import SECURITY, TOKEN

from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.db.models import ChannelAccount, Salon
from beauty_agent.db.repositories.channels import ChannelAccountsRepository
from beauty_agent.domain.enums import Channel, CrmKind


@pytest.fixture
async def fake_salon(session: AsyncSession, salon: Salon) -> Salon:
    salon.settings = {"crm_kind": CrmKind.FAKE.value}
    await session.commit()
    await import_catalog(session, FakeCRM(), slug=salon.slug)
    await session.refresh(salon)
    return salon


@pytest.fixture
async def account(session: AsyncSession, fake_salon: Salon) -> ChannelAccount:
    connected = await ChannelAccountsRepository(session).connect(
        fake_salon.id, Channel.TELEGRAM, f"bot-{uuid.uuid4().hex[:6]}", {"token": TOKEN}, SECURITY
    )
    await session.commit()
    return connected
