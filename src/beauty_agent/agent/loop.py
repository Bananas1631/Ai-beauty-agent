"""Сердце агента: «вызвать LLM → выполнить инструменты → снова LLM», не больше 6 шагов.

1. Входные guardrails: опасная тема — сразу администратору, injection — вежливый отказ без LLM.
2. Контекст в пределах бюджета (agent/context.py); длинный диалог — в резюме.
3. Шаг: вызов LLM. Нет вызовов инструментов — это ответ. Иначе аргументы проверяет Pydantic:
   при ошибке модель получает её текст и одну попытку исправиться, затем инструмент выполняется.
4. Шаги кончились, модель недоступна или аргументы снова неверны — передача администратору.
5. Выходные guardrails: чужой алфавит — одна перегенерация; цены и время сверяются с данными
   инструментов диалога, неподтверждённый ответ заменяется безопасным.
6. Кэш (agent/cache.py): вопрос о салоне без цен и записи сначала ищется в кэше ответов; ответ,
   прошедший выходные guardrails без цен и персональных данных, сохраняется туда — только если
   модель не видела ничего о клиенте: ни истории диалога, ни профиля (иначе в ответе может быть имя).
"""

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal

import structlog

from beauty_agent.adapters.llm.base import ChatMessage, LLMClient, LLMResponse, Usage
from beauty_agent.adapters.llm.traced import TracedLLM
from beauty_agent.agent.cache import AnswerCache, Scope, cacheable_answer, normalize
from beauty_agent.agent.context import ClientProfile, build_messages, maybe_summarize
from beauty_agent.agent.guardrails import (
    ADMIN_ANSWER,
    HEALTH_ANSWER,
    INJECTION_ANSWER,
    SAFE_ANSWER,
    TOO_LONG_ANSWER,
    FactCorpus,
    InputVerdict,
    check_input,
    has_foreign_script,
    unverified_facts,
)
from beauty_agent.agent.prompts import DEFAULT_PROMPT_VERSION, render_system_prompt
from beauty_agent.agent.registry import ToolArgumentsError, ToolContext, ToolOutcome, ToolRegistry
from beauty_agent.agent.state import DialogState
from beauty_agent.core import metrics
from beauty_agent.core.tracing import NoopTracer, Tracer, observe, use
from beauty_agent.domain.errors import LLMBadResponseError, LLMUnavailableError

logger = structlog.get_logger(__name__)

MAX_STEPS = 6
HANDOFF_ANSWER = "Передаю ваш вопрос администратору — он ответит в ближайшее рабочее время."
EMPTY_ANSWER = "Слушаю вас! Подскажите, на какую услугу хотите записаться?"
_RUSSIAN_ONLY = "Перепиши свой последний ответ только на русском языке, без иероглифов и иностранных слов."


@dataclass
class AgentResult:
    text: str
    prompt_version: str
    model: str | None = None
    steps: int = 0
    handoff_reason: str | None = None
    guardrail: str | None = None
    tool_calls: list[ToolOutcome] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_rub: Decimal = Decimal(0)
    latency_ms: int = 0
    llm_latency_ms: int = 0
    cache_hit: bool = False
    cache_saved_rub: Decimal = Decimal(0)  # сколько стоил бы ответ модели, если бы не кэш

    @property
    def handed_off(self) -> bool:
        return self.handoff_reason is not None

    def add(self, response: LLMResponse) -> None:
        self.usage = self.usage + response.usage
        self.cost_rub += response.cost_rub
        self.llm_latency_ms += response.latency_ms
        self.model = response.model or self.model


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        *,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        max_steps: int = MAX_STEPS,
        summarizer: LLMClient | None = None,
        tracer: Tracer | None = None,
        cache: AnswerCache | None = None,
    ) -> None:
        # Каждый вызов модели — наблюдение generation в трассе: модель, токены, стоимость
        self.llm = TracedLLM(llm)
        self.registry = registry
        self.prompt_version = prompt_version
        self.max_steps = max_steps
        self.summarizer = TracedLLM(summarizer or llm, name="llm.summary")
        self.tracer = tracer or NoopTracer()
        self.cache = cache

    async def run(
        self,
        user_text: str,
        ctx: ToolContext,
        state: DialogState,
        *,
        profile: ClientProfile | None = None,
        knowledge: tuple[str, ...] = (),
    ) -> AgentResult:
        started = time.perf_counter()
        result = AgentResult(text="", prompt_version=self.prompt_version)
        trace = self.tracer.start_trace(
            "message",
            input=user_text,
            metadata={
                "salon_id": ctx.salon_id,
                "conversation_id": ctx.conversation_id,
                "prompt_version": self.prompt_version,
            },
        )
        try:
            with use(trace):
                await self._run(user_text, ctx, state, result, profile, knowledge)
        finally:
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            trace.update(
                output=result.text,
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cost_rub=result.cost_rub,
                metadata={
                    "steps": result.steps,
                    "handoff_reason": result.handoff_reason,
                    "guardrail": result.guardrail,
                    "latency_ms": result.latency_ms,
                    "cache_hit": result.cache_hit,
                    "cache_saved_rub": str(result.cache_saved_rub),
                },
            )
            trace.end()
        # Текст атаки не сохраняем в историю: иначе он попал бы в контекст следующих сообщений
        remembered = "[сообщение отклонено фильтром]" if result.guardrail == InputVerdict.INJECTION else user_text
        state.remember(remembered, result.text, ctx.facts)
        logger.info(
            "agent_answered",
            steps=result.steps,
            tools=[outcome.name for outcome in result.tool_calls],
            handoff=result.handoff_reason,
            guardrail=result.guardrail,
            tokens=result.usage.total,
            cost_rub=str(result.cost_rub),
            latency_ms=result.latency_ms,
        )
        return result

    async def _run(
        self,
        user_text: str,
        ctx: ToolContext,
        state: DialogState,
        result: AgentResult,
        profile: ClientProfile | None,
        knowledge: tuple[str, ...],
    ) -> None:
        with observe("guardrails.input", kind="guardrail", input=user_text) as check:
            verdict = check_input(user_text)
            check.update(output=verdict.value)
        if verdict is not InputVerdict.OK:
            self._answer_without_llm(verdict, ctx, result)
            return

        # Модель не знает о клиенте ничего: её ответ можно отдать другому клиенту
        anonymous = not state.turns and state.summary is None and not knowledge and not (profile and profile.describe())
        scope, normalized = await self._cache_scope(user_text, ctx)
        if scope is not None and normalized is not None and self.cache is not None:
            with observe("cache", kind="span", input=normalized) as span:
                hit = await self.cache.get(scope, normalized)
                span.update(
                    output={
                        "hit": hit is not None,
                        "semantic": bool(hit and hit.semantic),
                        "saved_rub": str(hit.cost_rub) if hit else "0",
                    }
                )
            if hit is not None:
                result.text = hit.text
                result.cache_hit = True
                result.cache_saved_rub = hit.cost_rub
                metrics.llm_cache("hit", salon_id=ctx.salon_id, saved_rub=hit.cost_rub)
                return
            metrics.llm_cache("miss")

        summary = await maybe_summarize(state, self.summarizer)
        if summary is not None:
            result.add(summary)
        with observe("context") as context:
            prompt = render_system_prompt(salon_name=ctx.salon_name, now=ctx.now, version=self.prompt_version)
            messages = build_messages(prompt, state, user_text, profile=profile, knowledge=knowledge)
            context.update(
                output={"messages": len(messages), "facts": len(state.facts), "summary": bool(state.summary)}
            )

        try:
            text = await self._loop(messages, ctx, result)
        except (LLMUnavailableError, LLMBadResponseError) as exc:
            logger.warning("agent_llm_failed", error=str(exc))
            self._handoff(result, ctx, "Модель недоступна")
            return
        if text is None:  # шаги кончились, инструмент передал администратору или аргументы снова неверны
            if result.handoff_reason is None:
                self._handoff(result, ctx, ctx.handoff_reason or "Агент не справился за отведённые шаги")
            return
        with observe("guardrails.output", kind="guardrail", input=text) as check:
            result.text = await self._checked_output(text, messages, ctx, state, result)
            check.update(output={"text": result.text, "guardrail": result.guardrail})
        if (
            anonymous
            and scope is not None
            and normalized is not None
            and self.cache is not None
            and cacheable_answer(
                result.text, result.tool_calls, handed_off=result.handed_off, guardrail=result.guardrail
            )
        ):
            await self.cache.put(scope, normalized, result.text, result.cost_rub)
            metrics.llm_cache("store")

    async def _cache_scope(self, user_text: str, ctx: ToolContext) -> tuple[Scope | None, str | None]:
        """Ключ кэша для вопроса о салоне; (None, None) — вопрос не кэшируется или кэша нет."""
        normalized = normalize(user_text) if self.cache is not None else None
        if normalized is None or ctx.knowledge_version is None:
            return None, None
        version = await ctx.knowledge_version()
        if ctx.release is not None:
            await ctx.release()  # запрос версии не держит соединение, пока агент ждёт модель
        return Scope(ctx.salon_id, self.prompt_version, self.llm.model_key, version), normalized

    def _answer_without_llm(self, verdict: InputVerdict, ctx: ToolContext, result: AgentResult) -> None:
        result.guardrail = verdict.value
        if verdict is InputVerdict.HEALTH:
            ctx.handoff_reason = "Медицинский вопрос"
            result.handoff_reason = ctx.handoff_reason
            result.text = HEALTH_ANSWER
        elif verdict is InputVerdict.ADMIN:
            ctx.handoff_reason = "Жалоба, возврат или скидка"
            result.handoff_reason = ctx.handoff_reason
            result.text = ADMIN_ANSWER
        elif verdict is InputVerdict.INJECTION:
            result.text = INJECTION_ANSWER
        elif verdict is InputVerdict.TOO_LONG:
            result.text = TOO_LONG_ANSWER
        else:
            result.text = EMPTY_ANSWER

    def _handoff(self, result: AgentResult, ctx: ToolContext, reason: str) -> None:
        ctx.handoff_reason = ctx.handoff_reason or reason
        result.handoff_reason = ctx.handoff_reason
        result.text = HANDOFF_ANSWER

    async def _loop(self, messages: list[ChatMessage], ctx: ToolContext, result: AgentResult) -> str | None:
        specs = self.registry.specs()
        retried_arguments = False
        for _ in range(self.max_steps):
            response = await self.llm.complete(messages, specs)
            result.steps += 1
            result.add(response)
            if not response.tool_calls:
                return response.text or EMPTY_ANSWER
            messages.append(ChatMessage("assistant", response.text, response.tool_calls))
            for call in response.tool_calls:
                try:
                    tool, args = self.registry.parse(call)
                except ToolArgumentsError as exc:
                    if retried_arguments:  # вторая ошибка подряд — модель не справляется
                        self._handoff(result, ctx, f"Неверные аргументы инструмента {call.name}")
                        return None
                    retried_arguments = True
                    error = json.dumps({"error": str(exc)}, ensure_ascii=False)
                    messages.append(ChatMessage("tool", error, tool_call_id=call.id, name=call.name))
                    continue
                with observe(f"tool.{tool.name}", kind="tool", input=args.model_dump(mode="json")) as span:
                    outcome = await self.registry.execute(tool, args, call.id, ctx)
                    span.update(output=outcome.result, error=outcome.error, metadata={"ms": outcome.duration_ms})
                result.tool_calls.append(outcome)
                messages.append(ChatMessage("tool", outcome.content(), tool_call_id=call.id, name=call.name))
            if ctx.handoff_reason is not None:
                self._handoff(result, ctx, ctx.handoff_reason)
                return None
        return None

    async def _checked_output(
        self, text: str, messages: list[ChatMessage], ctx: ToolContext, state: DialogState, result: AgentResult
    ) -> str:
        if has_foreign_script(text):
            retry = await self._regenerate(text, messages, result)
            if retry is None or has_foreign_script(retry):
                result.guardrail = "foreign_script"
                return SAFE_ANSWER
            text = retry
        problems = unverified_facts(text, FactCorpus.from_texts([*state.facts, *ctx.facts]))
        if problems:
            logger.warning("agent_unverified_facts", problems=problems)
            result.guardrail = "unverified_fact"
            return SAFE_ANSWER
        return text

    async def _regenerate(self, text: str, messages: list[ChatMessage], result: AgentResult) -> str | None:
        try:
            response = await self.llm.complete(
                [*messages, ChatMessage("assistant", text), ChatMessage("user", _RUSSIAN_ONLY)]
            )
        except (LLMUnavailableError, LLMBadResponseError):
            return None
        result.add(response)
        return response.text
