"""Эксперимент (ветка experiment/langgraph): тот же агент, но цикл «модель → инструменты» — граф LangGraph.

Для честного сравнения меняется только оркестрация, а всё остальное — общее с agent/loop.py:
входные guardrails, контекст и промпт, проверка аргументов Pydantic с одной попыткой исправиться,
лимит шагов, передача администратору, выходные guardrails, трассы. Модель вызывается через наш
LLMClient: офлайн-модели в LangChain нет, а для Qwen, GigaChat и YandexGPT пришлось бы брать
сторонние интеграции LangChain — это уже сравнение адаптеров, а не цикла.

Граф без супервизора: model → (tools → model)* → END. Компилируется один раз на процесс;
агент, контекст инструментов и результат передаются через config — лучший для LangGraph случай.
"""

import json
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from beauty_agent.adapters.llm.base import ChatMessage
from beauty_agent.agent.loop import EMPTY_ANSWER, Agent, AgentResult
from beauty_agent.agent.registry import ToolArgumentsError, ToolContext
from beauty_agent.core.tracing import observe


class LoopState(TypedDict):
    messages: list[ChatMessage]
    steps: int
    retried_arguments: bool
    answer: str | None
    stopped: bool  # передача администратору или шаги кончились


def _run_parts(config: RunnableConfig) -> tuple["LangGraphAgent", ToolContext, AgentResult]:
    parts = config["configurable"]
    return parts["agent"], parts["ctx"], parts["result"]


async def _model(state: LoopState, config: RunnableConfig) -> dict[str, Any]:
    agent, _, result = _run_parts(config)
    response = await agent.llm.complete(state["messages"], agent.registry.specs())
    result.steps += 1
    result.add(response)
    if not response.tool_calls:
        return {"answer": response.text or EMPTY_ANSWER, "steps": state["steps"] + 1}
    message = ChatMessage("assistant", response.text, response.tool_calls)
    return {"messages": [*state["messages"], message], "steps": state["steps"] + 1}


async def _tools(state: LoopState, config: RunnableConfig) -> dict[str, Any]:
    agent, ctx, result = _run_parts(config)
    history = list(state["messages"])
    retried = state["retried_arguments"]
    for call in history[-1].tool_calls:
        try:
            tool, args = agent.registry.parse(call)
        except ToolArgumentsError as exc:
            if retried:
                agent._handoff(result, ctx, f"Неверные аргументы инструмента {call.name}")
                return {"messages": history, "stopped": True}
            retried = True
            error = json.dumps({"error": str(exc)}, ensure_ascii=False)
            history.append(ChatMessage("tool", error, tool_call_id=call.id, name=call.name))
            continue
        with observe(f"tool.{tool.name}", kind="tool", input=args.model_dump(mode="json")) as span:
            outcome = await agent.registry.execute(tool, args, call.id, ctx)
            span.update(output=outcome.result, error=outcome.error, metadata={"ms": outcome.duration_ms})
        result.tool_calls.append(outcome)
        history.append(ChatMessage("tool", outcome.content(), tool_call_id=call.id, name=call.name))
    if ctx.handoff_reason is not None:
        agent._handoff(result, ctx, ctx.handoff_reason)
        return {"messages": history, "retried_arguments": retried, "stopped": True}
    return {"messages": history, "retried_arguments": retried}


def _after_model(state: LoopState) -> Literal["tools", "__end__"]:
    return "__end__" if state["answer"] is not None else "tools"


def _after_tools(state: LoopState, config: RunnableConfig) -> Literal["model", "__end__"]:
    agent, _, _ = _run_parts(config)
    if state["stopped"] or state["steps"] >= agent.max_steps:
        return "__end__"
    return "model"


def _build() -> CompiledStateGraph[Any]:
    graph = StateGraph(LoopState)
    graph.add_node("model", _model)
    graph.add_node("tools", _tools)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", _after_model)
    graph.add_conditional_edges("tools", _after_tools)
    return graph.compile()


GRAPH = _build()


class LangGraphAgent(Agent):
    async def _loop(self, messages: list[ChatMessage], ctx: ToolContext, result: AgentResult) -> str | None:
        final = await GRAPH.ainvoke(
            LoopState(messages=messages, steps=0, retried_arguments=False, answer=None, stopped=False),
            {
                "recursion_limit": 2 * self.max_steps + 2,
                "configurable": {"agent": self, "ctx": ctx, "result": result},
            },
        )
        messages[:] = final["messages"]  # выходные guardrails перегенерируют ответ с этой историей
        answer: str | None = final["answer"]
        return answer
