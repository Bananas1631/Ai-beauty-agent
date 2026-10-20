"""Запросы дашборда Grafana (deploy/grafana/clickhouse/analytics.json) выполняются на нашей схеме ClickHouse.

Grafana подставляет макросы сама; здесь — те же подстановки, что делает плагин, чтобы опечатка
в колонке или функции нашлась тестом, а не пустым графиком.
"""

import json
from pathlib import Path

import pytest

from beauty_agent.analytics.clickhouse import ClickHouse

DASHBOARD = Path(__file__).resolve().parents[3] / "deploy" / "grafana" / "clickhouse" / "analytics.json"
MACROS = {
    "$__timeFilter(ts)": "ts >= now() - INTERVAL 30 DAY AND ts <= now()",
    "$salon": "Все",
}


def dashboard_queries() -> list[tuple[str, str]]:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    found = [(panel["title"], target["rawSql"]) for panel in dashboard["panels"] for target in panel["targets"]]
    found += [(variable["name"], variable["query"]) for variable in dashboard["templating"]["list"]]
    return found


def substituted(sql: str) -> str:
    for macro, value in MACROS.items():
        sql = sql.replace(macro, value)
    return sql


@pytest.mark.parametrize(("title", "sql"), dashboard_queries(), ids=lambda value: str(value)[:40])
async def test_dashboard_query_runs(clickhouse: ClickHouse, title: str, sql: str) -> None:
    assert "$" not in substituted(sql), f"неизвестный макрос в «{title}»"

    await clickhouse.rows(substituted(sql))  # синтаксис, колонки и функции — на настоящей схеме
