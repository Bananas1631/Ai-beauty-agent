"""Сквозной тест: диалог от приветствия до записи на фейковых CRM и LLM.

Проверяет, что все части агента работают вместе: guardrails, контекст, инструменты, движок записи,
состояние диалога в Redis между сообщениями и сверка фактов в ответах.
"""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from tests.agent_helpers import NOW, make_context

from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.adapters.llm.base import ChatMessage, LLMResponse, ToolSpec
from beauty_agent.adapters.llm.fake import ScriptedLLM, call_tool, reply
from beauty_agent.agent.loop import Agent
from beauty_agent.agent.state import StateStore
from beauty_agent.agent.tools import default_registry


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


def tool_result(messages: Sequence[ChatMessage]) -> dict[str, Any]:
    content = next(message.content for message in reversed(messages) if message.role == "tool")
    data: dict[str, Any] = json.loads(content or "{}")
    return data


def offer_options(messages: Sequence[ChatMessage], _tools: Sequence[ToolSpec]) -> LLMResponse:
    options = tool_result(messages)["options"]
    lines = "; ".join(f"{item['option']}) {item['when']}, мастер {item['master']}" for item in options)
    return reply(f"Есть такие варианты: {lines}. Какой удобен? И подскажите имя и телефон.")


def confirm(messages: Sequence[ChatMessage], _tools: Sequence[ToolSpec]) -> LLMResponse:
    booked = tool_result(messages)
    return reply(f"Записала вас на {booked['service']}: {booked['when']}, мастер {booked['master']}. Ждём вас!")


async def test_dialog_from_greeting_to_booking(redis: FakeAsyncRedis) -> None:
    crm = FakeCRM(clock=lambda: NOW)
    store = StateStore(redis)
    llm = ScriptedLLM(
        [
            # 1. приветствие
            reply("Здравствуйте! Я администратор студии «Лаванда». Чем могу помочь?"),
            # 2. услуга и цена
            call_tool("find_service", {"query": "маникюр с покрытием"}),
            reply("Маникюр с покрытием гель-лак стоит от 2 500 до 3 000 ₽, занимает 90 минут. Подобрать время?"),
            # 3. время
            call_tool("find_free_time", {"service_id": "102", "part_of_day": "evening"}),
            offer_options,
            # 4. запись
            call_tool("book", {"option": 1, "client_name": "Анна", "phone": "8 916 150-22-39"}),
            confirm,
        ]
    )
    agent = Agent(llm, default_registry())
    answers = []

    for text in (
        "Здравствуйте!",
        "Сколько стоит маникюр с покрытием?",
        "Да, хочу вечером на этой неделе",
        "Давайте первый. Анна, 8 916 150-22-39",
    ):
        state = await store.load("salon-1", "dialog-1")
        ctx = make_context(redis, crm=crm)  # контекст инструментов — новый на каждое сообщение, как в сервисе
        result = await agent.run(text, ctx, state)
        await store.save("salon-1", "dialog-1", state)
        assert result.guardrail is None, result.text
        assert result.handoff_reason is None
        answers.append(result.text)

    assert "от 2 500 до 3 000 ₽" in answers[1]
    assert "1)" in answers[2]
    assert answers[3].startswith("Записала вас на Маникюр с покрытием гель-лак")
    [(request, booking)] = crm.bookings.values()
    assert request.client.phone == "+79161502239"
    assert request.slot.starts_at.astimezone(NOW.tzinfo).hour >= 17  # клиент просил вечер
    final_state = await store.load("salon-1", "dialog-1")
    assert len(final_state.turns) == 8
    assert booking.external_id
