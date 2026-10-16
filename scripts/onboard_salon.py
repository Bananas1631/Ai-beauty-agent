"""Подключение пилотного салона одной командой.

    poetry run python -m scripts.onboard_salon scripts/onboarding.example.toml

Шаги: салон и каталог из CRM → настройки (теневой режим, срок хранения, реквизиты для политики) →
код привязки админ-бота → каналы (Telegram / Telegram Business, VK, MAX) с вебхуками →
FAQ из анкеты в базу знаний → smoke-проверка: тестовое сообщение из каждого канала доходит
до агента и даёт черновик (тестовый клиент потом удаляется).

Токены каналов в файл не пишутся: в файле — имена переменных окружения, где они лежат.
Так токен не попадёт ни в репозиторий, ни в историю команд. Чек-лист для салона — docs/onboarding.md.
"""

import argparse
import asyncio
import os
import sys
import tomllib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from beauty_agent.adapters.channels.admin_bot import ADMIN_BOT_ACCOUNT, bind_link, issue_bind_code
from beauty_agent.adapters.channels.base import ChannelRejectedError, ChannelUnavailableError
from beauty_agent.adapters.channels.vk import vk_secret
from beauty_agent.adapters.crm.base import BookingProvider
from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.adapters.crm.yclients import YclientsProvider
from beauty_agent.adapters.llm.models import build_embedder
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.conversation.service import IncomingMessage, ReplyRoute
from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.logging import configure_logging
from beauty_agent.db.models import ChannelAccount, Client, ClientIdentity, Draft, Salon
from beauty_agent.db.repositories.channels import ChannelAccountsRepository
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.domain.enums import AdminRole, Channel, CrmKind, ReplyMode
from beauty_agent.domain.salon_settings import PrivacyInfo, SalonSettings
from beauty_agent.rag.ingest import FaqItem, ingest_faq
from beauty_agent.runtime import Runtime, create_runtime

SMOKE_QUESTION = "Здравствуйте! Сколько стоит маникюр?"


class TelegramChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_env: str
    business: bool = False  # бот подключён к аккаунту салона в режиме Telegram Business (нужен Premium)


class VkChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: int = Field(gt=0)
    token_env: str
    confirmation_env: str


class MaxChannel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_env: str


class Channels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telegram: TelegramChannel | None = None
    vk: VkChannel | None = None
    max: MaxChannel | None = None


class FaqEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=300)
    answer: str = Field(min_length=1, max_length=4000)


class OnboardingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    crm: Literal["yclients", "demo", "fake"] = "yclients"
    company_id: int | None = Field(None, gt=0)
    reply_mode: ReplyMode = ReplyMode.SHADOW
    retention_days: int = Field(365, ge=30, le=1825)
    privacy: PrivacyInfo | None = None
    channels: Channels = Field(default_factory=Channels)
    faq: list[FaqEntry] = Field(default_factory=list, max_length=200)


@dataclass
class OnboardingReport:
    salon_id: uuid.UUID | None = None
    bind_code: str | None = None
    bind_link: str | None = None
    channels: dict[str, uuid.UUID] = field(default_factory=dict)
    faq_indexed: int = 0
    smoke: dict[str, str] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        self.lines.append(line)


def secret_from_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Переменная окружения {name} не задана")
    return value


def webhook_url(settings: Settings, channel: str, account_id: uuid.UUID) -> str | None:
    """Вебхуку нужен публичный HTTPS; локально — long polling (сервис bot в docker-compose)."""
    base = settings.app.public_url.rstrip("/")
    return f"{base}/api/v1/webhooks/{channel}/{account_id}" if base.startswith("https://") else None


async def _catalog(config: OnboardingConfig, settings: Settings, runtime: Runtime, report: OnboardingReport) -> Salon:
    yclients = YclientsProvider(config.company_id, settings.yclients) if config.company_id else None
    if config.crm != "fake" and yclients is None:
        raise SystemExit("Для YCLIENTS и демо нужен company_id")
    provider: BookingProvider = yclients if yclients is not None and config.crm != "fake" else FakeCRM()
    try:
        async with runtime.session_factory() as session:
            imported = await import_catalog(
                session,
                provider,
                slug=config.slug,
                company_id=config.company_id if config.crm != "fake" else None,
                is_demo=config.crm == "demo",
                embedder=build_embedder(settings.llm),
            )
    finally:
        if yclients is not None:
            await yclients.aclose()
    report.lines.extend(imported.lines())
    async with runtime.session_factory() as session:
        salon = await SalonsRepository(session).get_by_slug(config.slug)
        if salon is None:
            raise SystemExit("Салон не создан — см. отчёт импорта")
        merged = {
            **(salon.settings or {}),
            "crm_kind": CrmKind(config.crm).value,
            "reply_mode": config.reply_mode.value,
            "retention_days": config.retention_days,
        }
        if config.privacy is not None:
            merged["privacy"] = config.privacy.model_dump()
        salon.settings = SalonSettings.model_validate(merged).model_dump(mode="json", exclude_defaults=True)
        await session.commit()
        await session.refresh(salon)
        return salon


async def _connect(
    runtime: Runtime, salon: Salon, channel: Channel, external_id: str, credentials: dict[str, str]
) -> uuid.UUID:
    async with runtime.session_factory() as session:
        account = await ChannelAccountsRepository(session).connect(
            salon.id, channel, external_id, credentials, runtime.security
        )
        await session.commit()
        return account.id


async def _channels(
    config: OnboardingConfig, settings: Settings, runtime: Runtime, salon: Salon, report: OnboardingReport
) -> None:
    security = runtime.security
    if config.channels.telegram is not None:
        token = secret_from_env(config.channels.telegram.token_env)
        me = await runtime.telegram.call(token, "getMe", {})
        channel = Channel.TELEGRAM_BUSINESS if config.channels.telegram.business else Channel.TELEGRAM
        account_id = await _connect(runtime, salon, channel, str(me["id"]), {"token": token})
        report.channels[channel.value] = account_id
        url = webhook_url(settings, "telegram", account_id)
        if url:
            await runtime.telegram.set_webhook(token, url, security.webhook_secret("telegram", account_id))
            report.say(f"Telegram @{me.get('username')}: вебхук {url}")
        else:
            report.say(f"Telegram @{me.get('username')}: локально — long polling, сервис bot в docker-compose")
    if config.channels.vk is not None:
        vk = config.channels.vk
        credentials = {"token": secret_from_env(vk.token_env), "confirmation": secret_from_env(vk.confirmation_env)}
        account_id = await _connect(runtime, salon, Channel.VK, str(vk.group_id), credentials)
        report.channels[Channel.VK.value] = account_id
        url = webhook_url(settings, "vk", account_id) or "(нужен публичный HTTPS)"
        report.say(
            f"VK: Управление → Callback API → адрес {url}, секретный ключ "
            f"{vk_secret(security.webhook_secret('vk', account_id))}, события «Входящее сообщение»"
        )
    if config.channels.max is not None:
        token = secret_from_env(config.channels.max.token_env)
        me = await runtime.max_bot.me(token)
        account_id = await _connect(runtime, salon, Channel.MAX, str(me.get("user_id")), {"token": token})
        report.channels[Channel.MAX.value] = account_id
        url = webhook_url(settings, "max", account_id)
        if url:
            await runtime.max_bot.subscribe(token, url, security.webhook_secret("max", account_id))
            report.say(f"MAX {me.get('name')}: вебхук {url}")
        else:
            report.say(f"MAX {me.get('name')}: вебхук не настроен — нужен публичный HTTPS на порту 443")


async def _admin_bot_webhook(settings: Settings, runtime: Runtime, report: OnboardingReport) -> None:
    """Вебхук админ-бота — один на сервис; повторная установка безопасна."""
    base = settings.app.public_url.rstrip("/")
    if settings.admin_bot.token is None or not base.startswith("https://"):
        return
    await runtime.telegram.set_webhook(
        settings.admin_bot.token.get_secret_value(),
        f"{base}/api/v1/webhooks/admin-bot",
        runtime.security.webhook_secret("admin_bot", ADMIN_BOT_ACCOUNT),
        allowed_updates=["message", "callback_query"],
    )
    report.say(f"Админ-бот: вебхук {base}/api/v1/webhooks/admin-bot")


async def _smoke(runtime: Runtime, salon: Salon, report: OnboardingReport) -> None:
    """Тестовое сообщение через сервис диалога каждого канала; тестовый клиент затем удаляется."""
    for channel_name, account_id in report.channels.items():
        channel = Channel(channel_name)
        user_id = f"smoke-{uuid.uuid4().hex[:12]}"
        try:
            reply = await runtime.conversations.handle(
                IncomingMessage(
                    salon=salon,
                    channel=channel,
                    external_user_id=user_id,
                    text=SMOKE_QUESTION,
                    external_message_id=f"smoke-{uuid.uuid4().hex}",
                    reply_to=ReplyRoute(account_id, user_id),
                )
            )
            if reply.draft_id is not None:
                async with runtime.session_factory() as session:
                    draft = await session.get(Draft, reply.draft_id)
                report.smoke[channel_name] = f"черновик: {draft.text[:120] if draft else '?'}"
            else:
                report.smoke[channel_name] = f"ответ: {(reply.text or '')[:120]}"
        except Exception as exc:  # smoke-проверка сообщает об ошибке, а не падает на первом канале
            report.smoke[channel_name] = f"ОШИБКА: {type(exc).__name__}"
        finally:
            await _forget_smoke_client(runtime, salon.id, channel, user_id)


async def _forget_smoke_client(runtime: Runtime, salon_id: uuid.UUID, channel: Channel, user_id: str) -> None:
    async with runtime.session_factory() as session:
        client_id = await session.scalar(
            select(ClientIdentity.client_id).where(
                ClientIdentity.salon_id == salon_id,
                ClientIdentity.channel == channel,
                ClientIdentity.external_user_id == user_id,
            )
        )
        if client_id is not None:
            # диалоги, сообщения, черновики и outbox удаляются каскадом
            await session.execute(delete(Client).where(Client.salon_id == salon_id, Client.id == client_id))
            await session.commit()


async def onboard(config: OnboardingConfig, settings: Settings, runtime: Runtime) -> OnboardingReport:
    report = OnboardingReport()
    salon = await _catalog(config, settings, runtime, report)
    report.salon_id = salon.id

    report.bind_code = await issue_bind_code(runtime.redis, salon.id, AdminRole.OWNER)
    report.bind_link = bind_link(settings.admin_bot.username, report.bind_code)
    await _admin_bot_webhook(settings, runtime, report)

    try:
        await _channels(config, settings, runtime, salon, report)
    except (ChannelRejectedError, ChannelUnavailableError) as exc:
        report.say(f"Канал не подключён: {exc.message}")

    if config.faq:
        async with runtime.session_factory() as session:
            faq = await ingest_faq(
                session,
                salon.id,
                runtime.conversations.embedder,
                [FaqItem(item.question, item.answer) for item in config.faq],
            )
            await session.commit()
        report.faq_indexed = faq.pages_indexed + faq.pages_unchanged
        report.lines.extend(f"FAQ: {error}" for error in faq.errors)

    async with runtime.session_factory() as session:
        # без активных каналов smoke-проверке нечего проверять
        active = await session.scalar(select(ChannelAccount.id).where(ChannelAccount.salon_id == salon.id).limit(1))
    if active is not None:
        await _smoke(runtime, salon, report)
    return report


def print_report(report: OnboardingReport, slug: str, settings: Settings) -> None:
    print("\n".join(report.lines))
    print(f"Страница салона: {settings.app.public_url.rstrip('/')}/s/{slug}")
    print(f"FAQ в базе знаний: {report.faq_indexed}")
    print(f"Код привязки админ-бота (7 дней, одноразовый): {report.bind_code}")
    if report.bind_link:
        print(f"Ссылка для владельца: {report.bind_link}")
    for channel, result in report.smoke.items():
        print(f"Smoke {channel}: {result}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Подключение пилотного салона")
    parser.add_argument("config", type=Path, help="TOML-файл анкеты (пример: scripts/onboarding.example.toml)")
    return parser.parse_args(argv)


async def run(config: OnboardingConfig, settings: Settings) -> OnboardingReport:
    runtime = create_runtime(settings)
    try:
        return await onboard(config, settings, runtime)
    finally:
        await runtime.close()


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    args = parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    config = OnboardingConfig.model_validate(tomllib.loads(args.config.read_text(encoding="utf-8")))
    settings = settings or get_settings()
    report = asyncio.run(run(config, settings))
    print_report(report, config.slug, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
