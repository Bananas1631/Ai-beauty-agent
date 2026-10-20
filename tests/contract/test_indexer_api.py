"""Контракт индексатора и основного сервиса: ответ /search и /version читается клиентом основного сервиса.

Сервисы деплоятся отдельно, поэтому договорённость о формате проверяется здесь, а не в проде:
- каждое поле, которое основной сервис считает обязательным, индексатор отдаёт всегда и того же
  типа (по OpenAPI-схеме индексатора);
- пример ответа индексатора, построенный его же моделями, разбирается моделями клиента.
Индексатор может добавлять поля (клиент их игнорирует), но не удалять и не переименовывать.
"""

import uuid
from typing import Any

import pytest
from pydantic import BaseModel

from beauty_agent.adapters.knowledge_client import FragmentOut as ClientFragment
from beauty_agent.adapters.knowledge_client import SearchResponse, VersionResponse
from beauty_indexer.api import FragmentOut, SearchOut, VersionOut


def required_fields(model: type[BaseModel]) -> set[str]:
    return {name for name, field in model.model_fields.items() if field.is_required()}


def json_types(schema: dict[str, Any]) -> set[str]:
    if "anyOf" in schema:
        return {item.get("type", "object") for item in schema["anyOf"]}
    return {schema.get("type", "object")}


@pytest.mark.parametrize(
    ("server", "client"),
    [(SearchOut, SearchResponse), (FragmentOut, ClientFragment), (VersionOut, VersionResponse)],
)
def test_server_always_sends_what_client_requires(server: type[BaseModel], client: type[BaseModel]) -> None:
    server_schema = server.model_json_schema()
    client_schema = client.model_json_schema()

    missing = required_fields(client) - set(server_schema.get("required", []))

    assert not missing, f"индексатор не обязан отдавать {missing}"
    for name in client.model_fields:
        assert name in server_schema["properties"], f"поле {name} пропало из ответа индексатора"
        server_types = json_types(server_schema["properties"][name])
        client_types = json_types(client_schema["properties"][name])
        # null индексатор может отдать, только если клиент его принимает
        assert server_types <= client_types, f"{name}: индексатор отдаёт {server_types}, клиент ждёт {client_types}"


def test_example_response_round_trip() -> None:
    response = SearchOut(
        version="12:2026-12-26T10:00:00+00:00",
        fragments=[
            FragmentOut(
                chunk_id=uuid.uuid4(),
                title="Цены — Маникюр",
                url="https://lavanda.example/prices.html",
                content="Shellac — 1500 ₽",
                score=0.0325,
                distance=None,
                text_match=True,
            )
        ],
    )

    parsed = SearchResponse.model_validate_json(response.model_dump_json())

    assert parsed.fragments[0].distance is None
    assert parsed.fragments[0].url == "https://lavanda.example/prices.html"
    assert VersionResponse.model_validate_json(VersionOut(version="0:-").model_dump_json()).version == "0:-"
