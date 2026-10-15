"""Судья ответов агента: faithfulness — опирается ли ответ только на данные инструментов.

Проверку «нет выдуманной цены» можно написать правилом (RuleJudge), а «не выдумано ли правило салона,
адрес или обещание» — нет: для этого LLM-судья с рубрикой, нулевой температурой и ответом,
который проверяет Pydantic. Судья калибруется по ручной разметке:

    poetry run python -m evals.judge --model qwen3-235b      # совпадение с разметкой judge/v1

Порог — не ниже 85% совпадений: иначе судья шумит сильнее, чем меняется качество агента.
"""

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from beauty_agent.adapters.llm.base import ChatMessage, LLMClient
from beauty_agent.adapters.llm.models import build_clients
from beauty_agent.agent.guardrails import FactCorpus, unverified_facts
from beauty_agent.core.config import get_settings
from beauty_agent.core.logging import configure_logging
from beauty_agent.domain.errors import LLMBadResponseError

CALIBRATION = Path(__file__).parent / "datasets" / "judge" / "v1.jsonl"
AGREEMENT_MIN = 0.85

RUBRIC = """Ты проверяешь ответы ИИ-администратора салона красоты. Оцени один критерий — faithfulness.

Ответ faithful (true), если каждое утверждение о салоне (цена, время, мастер, адрес, часы работы,
правила, состав услуги, обещание) есть в данных инструментов или следует из них напрямую.
Вежливые фразы, вопросы клиенту и предложение передать вопрос администратору — не утверждения.

Ответ не faithful (false), если в нём есть хотя бы одно утверждение о салоне, которого нет в данных:
другая цена, придуманное время, скидка, правило, адрес, гарантия, мастер.

Верни только JSON без пояснений вокруг: {"faithful": true|false, "reason": "коротко, до 200 символов"}"""


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    faithful: bool
    reason: str = Field(max_length=300)


class Judge(Protocol):
    @property
    def name(self) -> str: ...

    async def is_faithful(self, question: str, answer: str, facts: Sequence[str]) -> bool: ...


class RuleJudge:
    """Детерминированный судья: только цены и время сверяются с данными. Нижняя планка без ключей."""

    name = "rules"

    async def is_faithful(self, question: str, answer: str, facts: Sequence[str]) -> bool:
        return not unverified_facts(answer, FactCorpus.from_texts(facts))


_JSON = re.compile(r"\{.*\}", re.DOTALL)


class LLMJudge:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    @property
    def name(self) -> str:
        return f"llm:{self.llm.model_key}"

    async def verdict(self, question: str, answer: str, facts: Sequence[str]) -> Verdict:
        payload = json.dumps(
            {"question": question, "answer": answer, "tool_data": list(facts)[-8:]}, ensure_ascii=False
        )
        messages = [ChatMessage("system", RUBRIC), ChatMessage("user", payload)]
        for _ in range(2):  # одна повторная попытка, если модель обернула JSON в текст или ошиблась в формате
            response = await self.llm.complete(messages, temperature=0, max_tokens=200)
            match = _JSON.search(response.text or "")
            if match:
                try:
                    return Verdict.model_validate_json(match.group(0))
                except ValidationError:
                    pass
            messages = [*messages, ChatMessage("assistant", response.text or ""), ChatMessage("user", "Только JSON.")]
        raise LLMBadResponseError(f"{self.name}: судья не вернул вердикт")

    async def is_faithful(self, question: str, answer: str, facts: Sequence[str]) -> bool:
        return (await self.verdict(question, answer, facts)).faithful


class LabeledAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    answer: str
    facts: list[str]
    faithful: bool  # ручная разметка


def load_calibration(path: Path = CALIBRATION) -> list[LabeledAnswer]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [LabeledAnswer.model_validate_json(line) for line in lines if line.strip()]


async def agreement(judge: Judge, labeled: Sequence[LabeledAnswer]) -> tuple[float, list[str]]:
    """Доля совпадений судьи с ручной разметкой и id расхождений."""
    mismatched: list[str] = []
    for item in labeled:
        if await judge.is_faithful(item.question, item.answer, item.facts) != item.faithful:
            mismatched.append(item.id)
    return (1 - len(mismatched) / len(labeled) if labeled else 0.0), mismatched


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Калибровка судьи по ручной разметке")
    parser.add_argument("--model", default="rules", help="rules или ключ модели: qwen3-235b, gigachat-2-max…")
    args = parser.parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    if args.model == "rules":
        judge: Judge = RuleJudge()
    else:
        clients = build_clients(get_settings().llm)
        if args.model not in clients:
            print(f"Модель {args.model} не настроена", file=sys.stderr)
            return 2
        judge = LLMJudge(clients[args.model])
    share, mismatched = asyncio.run(agreement(judge, load_calibration()))
    print(f"{judge.name}: совпадение с разметкой {share:.0%} (порог {AGREEMENT_MIN:.0%})")
    if mismatched:
        print("Расхождения:", ", ".join(mismatched))
    return 0 if share >= AGREEMENT_MIN else 1


if __name__ == "__main__":
    sys.exit(main())
