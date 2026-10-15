import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from beauty_agent.api.middleware import TRACE_HEADER, TraceIdMiddleware, pick_trace_id


@pytest.mark.parametrize(
    "incoming",
    [None, "", "short", "bad\ninjected log line", "x" * 65, "трассировка-1234"],
)
def test_unsafe_trace_id_is_replaced(incoming: str | None) -> None:
    trace_id = pick_trace_id(incoming)

    assert trace_id != incoming
    assert len(trace_id) == 32  # uuid4().hex


def test_safe_trace_id_is_kept() -> None:
    assert pick_trace_id("a1b2c3d4-e5f6.7890_ab") == "a1b2c3d4-e5f6.7890_ab"


async def test_trace_id_is_in_log_context_and_response() -> None:
    app = FastAPI()
    seen: dict[str, object] = {}

    @app.get("/")
    async def index() -> dict[str, str]:
        seen.update(structlog.contextvars.get_contextvars())
        return {}

    app.add_middleware(TraceIdMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")

    assert seen["trace_id"] == response.headers[TRACE_HEADER]
    assert "trace_id" not in structlog.contextvars.get_contextvars()  # контекст очищен после запроса
