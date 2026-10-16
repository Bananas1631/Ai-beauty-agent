"""Схемы событий: выгрузка JSON Schema не устарела, каталог описывает каждый тип, разбор строгий."""

import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from beauty_agent.domain.enums import Channel
from beauty_agent.domain.events import EVENT_TYPES, TOPICS, MessageReceived, MessageReceivedData, parse_event
from scripts.export_event_schemas import SCHEMAS, schema_files

CATALOG = Path(__file__).resolve().parents[3] / "docs" / "events.md"


def test_exported_schemas_are_up_to_date() -> None:
    for name, content in schema_files().items():
        path = SCHEMAS / name
        assert path.exists(), f"нет {name}: python -m scripts.export_event_schemas"
        assert path.read_text(encoding="utf-8") == content, f"{name} устарела: python -m scripts.export_event_schemas"


def test_catalog_lists_every_event_and_topic() -> None:
    catalog = CATALOG.read_text(encoding="utf-8")

    for model in EVENT_TYPES:
        assert f"`{model.model_fields['type'].default}`" in catalog
    for topic in set(TOPICS.values()):
        assert f"`{topic}`" in catalog


def test_every_type_has_a_topic() -> None:
    assert {str(model.model_fields["type"].default) for model in EVENT_TYPES} == set(TOPICS)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"type": "booking.deleted", "salon_id": "00000000-0000-0000-0000-000000000000"}',  # неизвестный тип
        b"{not json",
    ],
)
def test_unknown_or_broken_events_are_rejected(raw: bytes) -> None:
    with pytest.raises(ValidationError):
        parse_event(raw)


def test_extra_fields_are_rejected() -> None:
    event = MessageReceived(
        salon_id=uuid.uuid4(),
        data=MessageReceivedData(
            message_id=uuid.uuid4(), conversation_id=uuid.uuid4(), client_id=uuid.uuid4(), channel=Channel.VK
        ),
    )
    raw = event.model_dump_json().replace('"channel":"vk"', '"channel":"vk","text":"Здравствуйте"')

    with pytest.raises(ValidationError):  # текст сообщения в событие не пролезет
        parse_event(raw)
