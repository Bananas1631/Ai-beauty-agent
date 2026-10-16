"""Реестр инструментов агента: имя → модель аргументов и обработчик; список tools для LLM.

Новый инструмент добавляется в одном месте (agent/tools.py). Аргументы от модели проверяет Pydantic
(лишние поля запрещены): при ошибке модель получает понятный текст и шанс исправиться.
Доменные ошибки обработчика (время заняли, вариант устарел) возвращаются модели как результат,
неожиданные — пишутся в лог, а модели уходит нейтральное «не удалось».
"""

import json
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, ValidationError

from beauty_agent.adapters.llm.base import ToolCallRequest, ToolSpec
from beauty_agent.agent.tokens import compact_json
from beauty_agent.domain.booking_engine.service import BookingService
from beauty_agent.domain.entities import MasterInfo, ServiceInfo
from beauty_agent.domain.errors import DomainError

logger = structlog.get_logger(__name__)

MAX_RESULT_TOKENS = 300


class Catalog(Protocol):
    """Каталог салона для инструментов: в проде — из БД (импорт из CRM), в тестах — из фейкового CRM."""

    async def services(self) -> list[ServiceInfo]: ...

    async def masters_for(self, service_id: str) -> list[MasterInfo]: ...


class SalonInfoSource(Protocol):
    """Сведения о салоне для ответа на общие вопросы: адрес, часы, правила (с 3 ноября — RAG)."""

    async def search(self, question: str) -> list[dict[str, str]]: ...


@dataclass
class ToolContext:
    salon_id: str
    salon_name: str
    conversation_id: str
    now: datetime  # «сейчас» в часовом поясе салона: от него считаются «сегодня» и «завтра»
    booking: BookingService
    catalog: Catalog
    salon_info: SalonInfoSource
    client_id: uuid.UUID | None = None
    preferred_master_id: str | None = None
    handoff_reason: str | None = None  # заполняет инструмент handoff_to_admin
    facts: list[str] = field(default_factory=list)  # тексты результатов: по ним guardrails сверяют ответ
    # Вызывается после каждого инструмента: вернуть соединение с БД в пул, чтобы оно не простаивало,
    # пока агент ждёт ответ модели (docs/performance.md, нагрузочный тест)
    release: Callable[[], Awaitable[None]] | None = None
    # Версия базы знаний салона — часть ключа кэша ответов (agent/cache.py); None — кэш не используется
    knowledge_version: Callable[[], Awaitable[str]] | None = None


Handler = Callable[[Any, ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    handler: Handler

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, clean_schema(self.args.model_json_schema()))


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    call_id: str
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    duration_ms: int

    def content(self) -> str:
        """Что увидит модель в сообщении role=tool."""
        payload = self.result if self.error is None else {"error": self.error}
        return compact_json(payload or {}, max_tokens=MAX_RESULT_TOKENS)


class ToolArgumentsError(Exception):
    """Аргументы от модели не прошли проверку — текст ошибки уходит модели для исправления."""


def clean_schema(node: Any) -> Any:
    """JSON Schema без служебных title: GigaChat и YandexGPT строже к схеме, чем OpenAI.

    Внутри properties ключи — имена полей: их не трогаем, даже если поле называется title.
    """
    if isinstance(node, list):
        return [clean_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title":
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {name: clean_schema(schema) for name, schema in value.items()}
        else:
            cleaned[key] = clean_schema(value)
    return cleaned


def _validation_message(error: ValidationError) -> str:
    problems = [
        f"{'.'.join(str(part) for part in item['loc']) or 'аргументы'}: {item['msg']}" for item in error.errors()
    ]
    return "Неверные аргументы: " + "; ".join(problems[:5])


class ToolRegistry:
    def __init__(self, tools: Sequence[Tool]) -> None:
        self.tools = {tool.name: tool for tool in tools}

    def specs(self) -> list[ToolSpec]:
        return [tool.spec() for tool in self.tools.values()]

    def parse(self, call: ToolCallRequest) -> tuple[Tool, BaseModel]:
        tool = self.tools.get(call.name)
        if tool is None:
            raise ToolArgumentsError(f"Инструмента {call.name!r} нет. Доступны: {', '.join(self.tools)}")
        try:
            raw = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ToolArgumentsError(f"Аргументы — не JSON: {exc.msg}") from exc
        if not isinstance(raw, dict):
            raise ToolArgumentsError("Аргументы должны быть JSON-объектом")
        try:
            return tool, tool.args.model_validate(raw)
        except ValidationError as exc:
            raise ToolArgumentsError(_validation_message(exc)) from exc

    async def execute(self, tool: Tool, args: BaseModel, call_id: str, context: ToolContext) -> ToolOutcome:
        started = time.perf_counter()
        result: dict[str, Any] | None = None
        error: str | None = None
        try:
            result = await tool.handler(args, context)
        except DomainError as exc:
            error = exc.message
        except Exception:
            logger.exception("tool_failed", tool=tool.name)
            error = "Не удалось выполнить действие. Предложи клиенту передать вопрос администратору."
        finally:
            if context.release is not None:
                await context.release()
        outcome = ToolOutcome(
            call_id=call_id,
            name=tool.name,
            arguments=args.model_dump(mode="json"),
            result=result,
            error=error,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        context.facts.append(outcome.content())
        return outcome
