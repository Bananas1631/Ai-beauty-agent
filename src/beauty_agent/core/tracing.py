"""Трассировка в Langfuse: одно сообщение клиента — одна трасса со всеми шагами.

Внутри трассы: входные guardrails, сборка контекста, каждый вызов LLM (модель, токены, стоимость),
каждый инструмент (аргументы, результат, ошибка), выходные guardrails. В метаданных — салон, диалог,
версия промпта. Телефоны и e-mail маскируются до отправки (функция mask клиента Langfuse).

Без ключей LANGFUSE_* трассировка выключена: тесты и CI работают без Langfuse.
Код агента зависит от этого фасада, а не от SDK: смена версии SDK затрагивает один файл.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Any, Literal, Protocol

import structlog

from beauty_agent import __version__
from beauty_agent.core.config import LangfuseSettings
from beauty_agent.core.logging import mask_value

logger = structlog.get_logger(__name__)

Kind = Literal["span", "generation", "tool", "guardrail", "agent"]


class Observation(Protocol):
    def child(
        self,
        name: str,
        *,
        kind: Kind = "span",
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> "Observation": ...

    def update(
        self,
        *,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_rub: Decimal | None = None,
        error: str | None = None,
    ) -> None: ...

    def end(self) -> None: ...


class Tracer(Protocol):
    def start_trace(self, name: str, *, input: Any, metadata: dict[str, Any]) -> Observation: ...

    def flush(self) -> None: ...


class NoopObservation:
    def child(self, name: str, **_: Any) -> "NoopObservation":
        return self

    def update(self, **_: Any) -> None:
        return None

    def end(self) -> None:
        return None


NOOP = NoopObservation()


class NoopTracer:
    def start_trace(self, name: str, **_: Any) -> NoopObservation:
        return NOOP

    def flush(self) -> None:
        return None


_current: ContextVar[Observation] = ContextVar("trace_observation", default=NOOP)


def current() -> Observation:
    """Текущее наблюдение: LLM-клиент добавляет в него вызов модели, не зная, кто его вызвал."""
    return _current.get()


@contextmanager
def use(observation: Observation) -> Iterator[Observation]:
    token = _current.set(observation)
    try:
        yield observation
    finally:
        _current.reset(token)


@contextmanager
def observe(
    name: str,
    *,
    kind: Kind = "span",
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    model: str | None = None,
) -> Iterator[Observation]:
    """Дочернее наблюдение текущей трассы на время блока; исключение записывается как ошибка."""
    observation = current().child(name, kind=kind, input=input, metadata=metadata, model=model)
    try:
        with use(observation):
            yield observation
    except Exception as exc:
        observation.update(error=type(exc).__name__)
        raise
    finally:
        observation.end()


def _mask(*, data: Any, **_: Any) -> Any:
    """Функция маскировки для клиента Langfuse: вызывается для input, output и metadata."""
    return mask_value(data)


class LangfuseObservation:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def child(
        self,
        name: str,
        *,
        kind: Kind = "span",
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> "LangfuseObservation":
        child = self.inner.start_observation(name=name, as_type=kind, input=input, metadata=metadata, model=model)
        return LangfuseObservation(child)

    def update(
        self,
        *,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_rub: Decimal | None = None,
        error: str | None = None,
    ) -> None:
        fields: dict[str, Any] = {}
        if output is not None:
            fields["output"] = output
        if metadata:
            fields["metadata"] = metadata
        if model:
            fields["model"] = model
        if prompt_tokens is not None or completion_tokens is not None:
            fields["usage_details"] = {"input": prompt_tokens or 0, "output": completion_tokens or 0}
        if cost_rub is not None:
            fields["cost_details"] = {"total": float(cost_rub)}  # рубли: в интерфейсе Langfuse валюта не указана
        if error:
            fields["level"] = "ERROR"
            fields["status_message"] = error
        if fields:
            self.inner.update(**fields)

    def end(self) -> None:
        self.inner.end()


class LangfuseTracer:
    def __init__(self, settings: LangfuseSettings, client: Any = None) -> None:
        if client is None:
            from langfuse import Langfuse  # импорт по требованию: без ключей SDK не загружается

            secret = settings.secret_key.get_secret_value() if settings.secret_key else None
            client = Langfuse(
                public_key=settings.public_key,
                secret_key=secret,
                base_url=settings.host,
                environment=settings.environment,
                release=f"beauty-agent@{__version__}",
                sample_rate=settings.sample_rate,
                mask=_mask,
            )
        self.client = client

    def start_trace(self, name: str, *, input: Any, metadata: dict[str, Any]) -> LangfuseObservation:
        return LangfuseObservation(
            self.client.start_observation(name=name, as_type="agent", input=input, metadata=metadata)
        )

    def flush(self) -> None:
        try:
            self.client.flush()
        except Exception:  # трассировка не должна ронять приложение при остановке
            logger.warning("langfuse_flush_failed")


def make_tracer(settings: LangfuseSettings) -> Tracer:
    return LangfuseTracer(settings) if settings.enabled else NoopTracer()
