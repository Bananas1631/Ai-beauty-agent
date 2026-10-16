"""Онбординг пилотного салона одной командой: салон, настройки, код привязки, канал, FAQ и smoke-проверка."""

import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.channel_helpers import TOKEN, FakeBotApi

from beauty_agent.core.config import Settings
from beauty_agent.core.redis import key
from beauty_agent.db.models import ChannelAccount, ClientIdentity, KnowledgeItem, Salon
from beauty_agent.domain.enums import Channel, KnowledgeSource, ReplyMode
from beauty_agent.runtime import Runtime, create_runtime
from scripts.onboard_salon import OnboardingConfig, onboard


@pytest.fixture
def bot_api() -> FakeBotApi:
    return FakeBotApi({"getMe": {"id": 7001, "username": "lavanda_bot", "is_bot": True}})


@pytest.fixture
async def runtime(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession], bot_api: FakeBotApi
) -> AsyncIterator[Runtime]:
    public = settings.model_copy(
        update={"app": settings.app.model_copy(update={"public_url": "https://agent.example"})}
    )
    built = create_runtime(public, http=bot_api.http)
    # данные — в транзакции теста, как у остальных интеграционных тестов
    built.session_factory = session_factory
    built.conversations.session_factory = session_factory
    yield built
    await built.close()


def config(slug: str) -> OnboardingConfig:
    return OnboardingConfig.model_validate(
        {
            "slug": slug,
            "crm": "fake",
            "privacy": {
                "operator_name": "ИП Иванова Анна Андреевна",
                "inn": "500100732259",
                "address": "Москва",
                "email": "privacy@lavanda.example",
            },
            "channels": {"telegram": {"token_env": "TEST_ONBOARDING_TG", "business": True}},
            "faq": [
                {"question": "Есть ли парковка?", "answer": "Да, во дворе."},
                {"question": "Можно картой?", "answer": "Да, и по СБП."},
            ],
        }
    )


async def test_onboard_salon(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
    redis: Redis,
    runtime: Runtime,
    bot_api: FakeBotApi,
) -> None:
    monkeypatch.setenv("TEST_ONBOARDING_TG", TOKEN)
    slug = f"pilot-{uuid.uuid4().hex[:8]}"

    report = await onboard(config(slug), runtime.settings, runtime)

    salon = await session.scalar(select(Salon).where(Salon.slug == slug))
    assert salon is not None
    assert salon.config.reply_mode is ReplyMode.SHADOW  # пилот начинается в теневом режиме
    assert salon.config.privacy is not None
    assert await redis.exists(key("admin", "bind", report.bind_code or "")) == 1
    account = await session.scalar(select(ChannelAccount).where(ChannelAccount.salon_id == salon.id))
    assert account is not None
    assert (account.channel, account.external_id) == (Channel.TELEGRAM_BUSINESS, "7001")
    assert TOKEN.encode() not in (account.token_encrypted or b"")  # токен в БД только зашифрованным
    [webhook] = bot_api.sent("setWebhook")
    assert webhook["url"] == f"https://agent.example/api/v1/webhooks/telegram/{account.id}"
    assert webhook["secret_token"] == runtime.security.webhook_secret("telegram", account.id)
    faq = await session.scalar(
        select(func.count())
        .select_from(KnowledgeItem)
        .where(KnowledgeItem.salon_id == salon.id, KnowledgeItem.source == KnowledgeSource.FAQ)
    )
    assert (faq, report.faq_indexed) == (2, 2)
    assert report.smoke[Channel.TELEGRAM_BUSINESS.value].startswith("черновик:")
    # тестовый клиент smoke-проверки удалён
    assert await session.scalar(select(ClientIdentity).where(ClientIdentity.salon_id == salon.id)) is None


async def test_onboarding_again_updates_instead_of_duplicating(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, runtime: Runtime
) -> None:
    monkeypatch.setenv("TEST_ONBOARDING_TG", TOKEN)
    slug = f"pilot-{uuid.uuid4().hex[:8]}"

    first = await onboard(config(slug), runtime.settings, runtime)
    second = await onboard(config(slug), runtime.settings, runtime)

    assert first.salon_id == second.salon_id
    assert first.channels == second.channels  # тот же бот — тот же канал
    assert first.bind_code != second.bind_code  # каждый запуск выдаёт новый одноразовый код
    faq = await session.scalar(
        select(func.count()).select_from(KnowledgeItem).where(KnowledgeItem.salon_id == first.salon_id)
    )
    assert faq is not None
    assert faq >= 2


def test_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="extra"):
        OnboardingConfig.model_validate({"slug": "x", "token": "в файле токенов не бывает"})
