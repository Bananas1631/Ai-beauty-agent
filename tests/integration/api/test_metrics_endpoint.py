"""GET /metrics: задержки по шаблону маршрута, очереди и доля дневного лимита LLM салона."""

from decimal import Decimal

from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.agent.budget import DailyBudget
from beauty_agent.db.models import Salon


async def test_metrics_endpoint(client: AsyncClient, session: AsyncSession, redis: Redis, salon: Salon) -> None:
    salon.settings = {"daily_llm_budget_rub": "200"}
    await session.commit()
    await DailyBudget(redis).add(str(salon.id), Decimal(170))
    await client.get(f"/s/{salon.slug}")

    response = await client.get("/metrics")

    assert response.status_code == 200
    text = response.text
    assert 'route="/s/{slug}"' in text  # шаблон маршрута, а не адрес конкретного салона
    assert f'/s/{salon.slug}"' not in text
    assert 'beauty_agent_queue_messages{queue="messages"}' in text
    assert f'beauty_agent_llm_budget_used_ratio{{salon_id="{salon.id}"}} 0.85' in text
    assert "/metrics" not in [line.split('route="')[1].split('"')[0] for line in text.splitlines() if 'route="' in line]
