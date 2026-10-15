"""Что отправить модели, в пределах бюджета токенов.

Системный промпт, профиль клиента, резюме начала диалога, данные инструментов прошлых
сообщений, 2–3 фрагмента базы знаний и последние 4 реплики. Меньше токенов — дешевле и быстрее
ответ. Длинный диалог сворачивается в резюме дешёвым вызовом LLM.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from beauty_agent.adapters.llm.base import ChatMessage, LLMClient, LLMResponse
from beauty_agent.agent.state import MAX_STORED_TURNS, DialogState
from beauty_agent.agent.tokens import estimate_tokens
from beauty_agent.domain.errors import LLMBadResponseError, LLMUnavailableError

logger = structlog.get_logger(__name__)

RECENT_TURNS = 4
CONTEXT_BUDGET_TOKENS = 3500

SUMMARY_PROMPT = (
    "Сожми начало переписки администратора салона с клиентом в 2–4 предложения: что хочет клиент, "
    "какая услуга, мастер, даты и время обсуждались, о чём договорились, как зовут клиента. "
    "Только факты из переписки, без оценок."
)


@dataclass(frozen=True, slots=True)
class ClientProfile:
    name: str | None = None
    preferred_master: str | None = None
    visits: int = 0

    def describe(self) -> str | None:
        parts = []
        if self.name:
            parts.append(f"имя — {self.name}")
        if self.visits:
            parts.append(f"уже был(а) в салоне {self.visits} раз(а)")
        if self.preferred_master:
            parts.append(f"обычно ходит к мастеру {self.preferred_master}")
        return "; ".join(parts) if parts else None


def _system_text(
    prompt: str, state: DialogState, profile: ClientProfile | None, knowledge: Sequence[str], facts: Sequence[str]
) -> str:
    sections = [prompt]
    described = profile.describe() if profile else None
    if described:
        sections.append(f"О клиенте: {described}.")
    if state.summary:
        sections.append(f"Начало диалога кратко: {state.summary}")
    if facts:
        sections.append("Данные инструментов из прошлых сообщений:\n" + "\n".join(facts))
    if knowledge:
        sections.append("Сведения о салоне:\n" + "\n".join(knowledge))
    return "\n\n".join(sections)


def build_messages(
    prompt: str,
    state: DialogState,
    user_text: str,
    *,
    profile: ClientProfile | None = None,
    knowledge: Sequence[str] = (),
    budget_tokens: int = CONTEXT_BUDGET_TOKENS,
) -> list[ChatMessage]:
    """Сборка контекста; при превышении бюджета сначала сокращаются старые данные, потом реплики."""
    facts = list(state.facts)
    turns = state.turns[-RECENT_TURNS:]
    knowledge = list(knowledge)[:3]

    def assemble() -> list[ChatMessage]:
        system = ChatMessage("system", _system_text(prompt, state, profile, knowledge, facts))
        history = [ChatMessage(turn.role, turn.content) for turn in turns]
        return [system, *history, ChatMessage("user", user_text)]

    messages = assemble()
    while _tokens(messages) > budget_tokens and (len(facts) > 2 or len(turns) > 2 or len(knowledge) > 1):
        if len(facts) > 2:
            facts = facts[1:]
        elif len(knowledge) > 1:
            knowledge = knowledge[:-1]
        else:
            turns = turns[2:]
        messages = assemble()
    return messages


def _tokens(messages: Sequence[ChatMessage]) -> int:
    return sum(estimate_tokens(message.content or "") + 4 for message in messages)


async def maybe_summarize(state: DialogState, llm: LLMClient) -> LLMResponse | None:
    """Старые реплики (сверх 8) сворачиваются в резюме. Модель недоступна — просто отбрасываются."""
    if len(state.turns) <= MAX_STORED_TURNS:
        return None
    old, recent = state.turns[:-RECENT_TURNS], state.turns[-RECENT_TURNS:]
    transcript = "\n".join(f"{'Клиент' if turn.role == 'user' else 'Администратор'}: {turn.content}" for turn in old)
    if state.summary:
        transcript = f"Ранее: {state.summary}\n{transcript}"
    try:
        response = await llm.complete(
            [ChatMessage("system", SUMMARY_PROMPT), ChatMessage("user", transcript)], temperature=0, max_tokens=200
        )
    except (LLMUnavailableError, LLMBadResponseError) as exc:
        logger.warning("summary_failed", error=type(exc).__name__)
        state.turns = recent
        return None
    if response.text:
        state.summary = response.text
    state.turns = list(recent)
    return response
