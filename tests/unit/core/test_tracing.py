from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from pydantic import SecretStr
from tests.agent_helpers import make_context

from beauty_agent.adapters.llm.fake import ScriptedLLM, call_tool, reply
from beauty_agent.agent.loop import Agent
from beauty_agent.agent.state import DialogState
from beauty_agent.agent.tools import default_registry
from beauty_agent.core.config import LangfuseSettings
from beauty_agent.core.tracing import (
    LangfuseObservation,
    LangfuseTracer,
    NoopTracer,
    _mask,
    make_tracer,
    observe,
    use,
)


class Node:
    """Наблюдение в памяти: имя, вид, вход и все обновления."""

    def __init__(self, name: str, kind: str = "agent", input: Any = None, model: str | None = None) -> None:
        self.name, self.kind, self.input, self.model = name, kind, input, model
        self.children: list[Node] = []
        self.updates: dict[str, Any] = {}
        self.ended = False

    def child(
        self, name: str, *, kind: str = "span", input: Any = None, metadata: Any = None, model: Any = None
    ) -> "Node":
        node = Node(name, kind, input, model)
        self.children.append(node)
        return node

    def update(self, **fields: Any) -> None:
        self.updates.update({key: value for key, value in fields.items() if value is not None})

    def end(self) -> None:
        self.ended = True


class RecordingTracer:
    def __init__(self) -> None:
        self.traces: list[Node] = []

    def start_trace(self, name: str, *, input: Any, metadata: dict[str, Any]) -> Node:
        node = Node(name, input=input)
        node.updates["metadata_start"] = metadata
        self.traces.append(node)
        return node

    def flush(self) -> None:
        return None


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


async def test_one_message_is_one_trace_with_all_steps(redis: FakeAsyncRedis) -> None:
    tracer = RecordingTracer()
    llm = ScriptedLLM([call_tool("find_service", {"query": "педикюр"}), reply("Педикюр с покрытием — 3 200 ₽.")])

    await Agent(llm, default_registry(), tracer=tracer).run(
        "Сколько стоит педикюр?", make_context(redis), DialogState()
    )

    [trace] = tracer.traces
    assert [child.name for child in trace.children] == [
        "guardrails.input",
        "context",
        "llm",
        "tool.find_service",
        "llm",
        "guardrails.output",
    ]
    generation = trace.children[2]
    assert generation.kind == "generation"
    assert generation.updates["prompt_tokens"] == 400
    assert generation.updates["cost_rub"] == Decimal("0.01")
    tool = trace.children[3]
    assert tool.input == {"query": "педикюр"}
    assert "found" in tool.updates["output"]
    assert trace.updates["output"] == "Педикюр с покрытием — 3 200 ₽."
    assert trace.updates["cost_rub"] == Decimal("0.02")
    assert trace.updates["metadata_start"]["prompt_version"] == "v1"
    assert all(child.ended for child in trace.children)
    assert trace.ended


async def test_guardrail_trace_without_llm(redis: FakeAsyncRedis) -> None:
    tracer = RecordingTracer()

    await Agent(ScriptedLLM(), default_registry(), tracer=tracer).run(
        "Я беременна, можно окрашивание?", make_context(redis), DialogState()
    )

    [trace] = tracer.traces
    assert [child.name for child in trace.children] == ["guardrails.input"]
    assert trace.children[0].updates["output"] == "health"
    assert trace.updates["metadata"]["handoff_reason"] == "Медицинский вопрос"


async def test_observe_records_errors() -> None:
    root = Node("root")
    with use(root), pytest.raises(RuntimeError), observe("step"):
        raise RuntimeError("сбой")

    assert root.children[0].updates["error"] == "RuntimeError"
    assert root.children[0].ended


def test_tracing_is_off_without_keys() -> None:
    assert isinstance(make_tracer(LangfuseSettings()), NoopTracer)
    assert not LangfuseSettings(public_key="pk").enabled


def test_mask_hides_personal_data() -> None:
    masked = _mask(data={"input": "Анна, +7 (916) 150-22-39, anna@mail.ru"})

    assert "150-22-39" not in str(masked)
    assert "anna@mail.ru" not in str(masked)


class FakeLangfuseSpan:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def start_observation(self, **fields: Any) -> "FakeLangfuseSpan":
        self.calls.append(("start", fields))
        return self

    def update(self, **fields: Any) -> None:
        self.calls.append(("update", fields))

    def end(self) -> None:
        self.calls.append(("end", {}))


def test_langfuse_fields_mapping() -> None:
    inner = FakeLangfuseSpan()
    observation = LangfuseObservation(inner)

    observation.update(
        model="qwen3-235b", prompt_tokens=100, completion_tokens=20, cost_rub=Decimal("0.01"), error="Timeout"
    )

    _, fields = inner.calls[-1]
    assert fields["usage_details"] == {"input": 100, "output": 20}
    assert fields["cost_details"] == {"total": 0.01}
    assert fields["level"] == "ERROR"
    assert fields["model"] == "qwen3-235b"


def test_langfuse_tracer_with_client() -> None:
    client = FakeLangfuseSpan()
    client.flush = lambda: None  # type: ignore[attr-defined]
    settings = LangfuseSettings(public_key="pk", secret_key=SecretStr("sk"))

    trace = LangfuseTracer(settings, client=client).start_trace("message", input="привет", metadata={"salon_id": "s"})
    trace.child("llm", kind="generation", model="m").end()

    assert client.calls[0] == (
        "start",
        {"name": "message", "as_type": "agent", "input": "привет", "metadata": {"salon_id": "s"}},
    )
    assert client.calls[1][1]["as_type"] == "generation"
