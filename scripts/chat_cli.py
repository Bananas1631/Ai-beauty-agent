"""Чат с агентом в терминале на фейковом CRM — главный способ вручную проверить агента.

    poetry run python -m scripts.chat_cli --debug          # или: make chat
    poetry run python -m scripts.chat_cli --model gigachat-2-lite

Нужен ключ хотя бы одной модели в .env (LLM_CLOUDRU_API_KEY, LLM_GIGACHAT_AUTH_KEY…)
или свой OpenAI-совместимый сервер (LLM_BASE_URL, LLM_MODEL — например, Ollama).
Redis — из настроек (make up); если он не запущен, состояние диалога хранится в памяти.
--debug печатает вызовы инструментов, токены, стоимость и задержку.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from beauty_agent.adapters.crm.fake import FAKE_SALON, FakeCRM
from beauty_agent.adapters.llm.base import LLMClient
from beauty_agent.adapters.llm.models import build_clients
from beauty_agent.adapters.llm.offline import OfflineLLM
from beauty_agent.agent.loop import Agent, AgentResult
from beauty_agent.agent.registry import ToolContext
from beauty_agent.agent.state import DialogState
from beauty_agent.agent.tools import CardSalonInfo, ProviderCatalog, default_registry
from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.logging import configure_logging
from beauty_agent.core.redis import make_redis
from beauty_agent.domain.booking_engine.memory import MemoryLedger
from beauty_agent.domain.booking_engine.service import BookingEntry, BookingService

EXIT_WORDS = {"выход", "exit", "quit", "q"}


class PrintLedger(MemoryLedger):
    async def record(self, entry: BookingEntry) -> None:
        await super().record(entry)
        print(f"  [CRM] запись {entry.status.value}: {entry.slot.starts_at:%d.%m %H:%M}, мастер {entry.slot.master_id}")


def pick_llm(settings: Settings, wanted: str | None) -> LLMClient:
    """Модель из --model; без него — первая настроенная; без ключей — офлайн-модель на правилах."""
    if wanted == "offline":
        return OfflineLLM()
    clients = build_clients(settings.llm)
    order = [wanted] if wanted else [*settings.llm.default_models, *clients]
    for key in order:
        if key in clients:
            return clients[key]
    if wanted is None:
        print("Ключей LLM нет — отвечает офлайн-модель на правилах (понимает только типовые фразы).")
        return OfflineLLM()
    available = ", ".join(clients) or "нет — задайте ключ в .env"
    raise SystemExit(f"Модель {wanted} недоступна. Настроены: {available}")


async def connect_redis(settings: Settings) -> Redis:
    redis = make_redis(settings.redis)
    try:
        await redis.ping()
    except Exception:  # Redis не запущен — для ручной проверки хватит памяти
        await redis.aclose()
        from fakeredis import FakeAsyncRedis  # dev-зависимость: только для локального запуска

        print("Redis недоступен — состояние диалога хранится в памяти.")
        return FakeAsyncRedis(decode_responses=True)
    return redis


def show_debug(result: AgentResult) -> None:
    for outcome in result.tool_calls:
        print(f"  → {outcome.name}({outcome.arguments}) {outcome.duration_ms} мс: {outcome.content()[:300]}")
    usage = result.usage
    line = (
        f"  [{result.model}] шагов {result.steps}, токенов {usage.prompt_tokens}+{usage.completion_tokens}, "
        f"{result.cost_rub} ₽, {result.latency_ms} мс"
    )
    if result.guardrail:
        line += f", guardrail: {result.guardrail}"
    if result.handoff_reason:
        line += f", администратору: {result.handoff_reason}"
    print(line)


async def chat(args: argparse.Namespace, settings: Settings) -> None:
    llm = pick_llm(settings, args.model)
    redis = await connect_redis(settings)
    tz = ZoneInfo(FAKE_SALON.timezone)
    crm = FakeCRM()
    booking = BookingService(
        salon_id="cli", timezone=FAKE_SALON.timezone, provider=crm, redis=redis, ledger=PrintLedger()
    )
    agent = Agent(llm, default_registry())
    state = DialogState()
    conversation_id = uuid.uuid4().hex
    print(f"Салон «{FAKE_SALON.name}» (фейковый CRM), модель {llm.model_key}. Выход — «выход».")
    try:
        while True:
            try:
                text = await asyncio.to_thread(input, "\nВы: ")
            except EOFError:
                break
            if text.strip().lower() in EXIT_WORDS:
                break
            ctx = ToolContext(
                salon_id="cli",
                salon_name=FAKE_SALON.name,
                conversation_id=conversation_id,
                now=datetime.now(tz),
                booking=booking,
                catalog=ProviderCatalog(booking),
                salon_info=CardSalonInfo(crm),
            )
            result = await agent.run(text, ctx, state)
            print(f"Администратор: {result.text}")
            if args.debug:
                show_debug(result)
    finally:
        await redis.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Чат с ИИ-администратором на фейковом CRM")
    parser.add_argument("--model", help="ключ модели: qwen3-235b, gigachat-2-lite, yandexgpt-lite, custom, offline")
    parser.add_argument("--debug", action="store_true", help="показывать инструменты, токены и стоимость")
    args = parser.parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    asyncio.run(chat(args, get_settings()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
