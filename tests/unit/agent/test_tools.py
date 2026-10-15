import json
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from fakeredis import FakeAsyncRedis
from tests.agent_helpers import NOW, make_context

from beauty_agent.adapters.crm.fake import FAKE_SERVICES, FakeCRM
from beauty_agent.adapters.llm.base import ToolCallRequest
from beauty_agent.agent.prompts import render_system_prompt
from beauty_agent.agent.registry import ToolArgumentsError, ToolContext, clean_schema
from beauty_agent.agent.tokens import compact_json, estimate_tokens
from beauty_agent.agent.tools import TOOLS, default_registry, format_local, normalize_phone, search_services

registry = default_registry()


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def ctx(redis: FakeAsyncRedis) -> ToolContext:
    return make_context(redis)


async def run(ctx: ToolContext, name: str, **arguments: object) -> dict[str, object]:
    tool, args = registry.parse(ToolCallRequest("c1", name, json.dumps(arguments, ensure_ascii=False)))
    outcome = await registry.execute(tool, args, "c1", ctx)
    result: dict[str, object] = json.loads(outcome.content())
    return result


# ---------------------------------------------------------------- схемы и реестр


def test_six_tools_with_strict_schemas() -> None:
    specs = registry.specs()

    assert [spec.name for spec in specs] == [
        "find_service",
        "masters_for_service",
        "find_free_time",
        "book",
        "salon_info",
        "handoff_to_admin",
    ]
    for spec in specs:
        assert spec.parameters["type"] == "object"
        assert spec.parameters["additionalProperties"] is False  # лишние поля запрещены
        assert "title" not in json.dumps(spec.parameters)
        assert spec.description
    book = next(spec for spec in specs if spec.name == "book")
    assert set(book.parameters["required"]) == {"option", "client_name", "phone"}


def test_clean_schema_keeps_field_named_title() -> None:
    schema = {"title": "Args", "properties": {"title": {"title": "Title", "type": "string"}}}

    assert clean_schema(schema) == {"properties": {"title": {"type": "string"}}}


def test_extra_argument_is_rejected() -> None:
    with pytest.raises(ToolArgumentsError, match="discount"):
        registry.parse(ToolCallRequest("c1", "find_service", '{"query": "маникюр", "discount": 50}'))


def test_invalid_json_and_unknown_tool() -> None:
    with pytest.raises(ToolArgumentsError, match="не JSON"):
        registry.parse(ToolCallRequest("c1", "find_service", "{query: маникюр"))
    with pytest.raises(ToolArgumentsError, match="Доступны"):
        registry.parse(ToolCallRequest("c1", "delete_all", "{}"))
    with pytest.raises(ToolArgumentsError, match="объектом"):
        registry.parse(ToolCallRequest("c1", "find_service", "[1, 2]"))


def test_master_id_cannot_carry_a_path() -> None:
    with pytest.raises(ToolArgumentsError):
        registry.parse(ToolCallRequest("c1", "masters_for_service", '{"service_id": "../company/1"}'))


# ---------------------------------------------------------------- данные


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("маникюр гель лак", "Маникюр с покрытием гель-лак"),
        ("педикюр", "Педикюр с покрытием"),
        ("брови", "Коррекция и окрашивание бровей"),
        ("ресницы 2д", "Наращивание ресниц 2D"),
        ("покраска волос", "Окрашивание в один тон"),
        ("шеллак", "Маникюр с покрытием гель-лак"),
        ("бровки", "Коррекция и окрашивание бровей"),
    ],
)
def test_fuzzy_service_search(query: str, expected: str) -> None:
    found = search_services(list(FAKE_SERVICES), query)

    assert found
    assert found[0].name == expected


def test_unknown_service_is_not_found() -> None:
    assert search_services(list(FAKE_SERVICES), "татуировка дракона") == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("8 (916) 150-22-39", "+79161502239"), ("+7 916 150 22 39", "+79161502239"), ("9161502239", "+79161502239")],
)
def test_phone_is_normalized_to_e164(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["123", "позвоните мне", "+7 000 000 00 00"])
def test_bad_phone_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match=r"номер|телефон"):
        normalize_phone(raw)


def test_format_local() -> None:
    assert format_local(NOW + timedelta(days=1, hours=1)) == "вт, 20 октября, 10:00"


def test_compact_json_trims_long_lists() -> None:
    data = {"options": [{"text": "x" * 100} for _ in range(30)]}

    text = compact_json(data, max_tokens=300)

    assert estimate_tokens(text) <= 300
    assert json.loads(text)["truncated"] is True


def test_system_prompt_is_rendered() -> None:
    prompt = render_system_prompt(salon_name="Лаванда {$x}", now=NOW)

    assert "«Лаванда {$x}»" in prompt
    assert "пн, 19 октября, 09:00" in prompt
    assert "Europe/Moscow" in prompt
    assert "$" not in prompt.replace("{$x}", "")


# ---------------------------------------------------------------- обработчики


async def test_find_service(ctx: ToolContext) -> None:
    result = await run(ctx, "find_service", query="маникюр с покрытием")

    first = result["found"][0]  # type: ignore[index]
    assert first == {
        "service_id": "102",
        "name": "Маникюр с покрытием гель-лак",
        "price": "от 2 500 до 3 000 ₽",
        "duration_min": 90,
        "category": "Ногтевой сервис",
    }


async def test_masters_for_service(ctx: ToolContext) -> None:
    result = await run(ctx, "masters_for_service", service_id="301")

    assert result == {"masters": [{"master_id": "31", "name": "Светлана", "specialization": "Бровист, лэшмейкер"}]}


async def test_free_time_gives_at_most_three_options(ctx: ToolContext) -> None:
    result = await run(ctx, "find_free_time", service_id="102")

    options = result["options"]
    assert isinstance(options, list)
    assert 1 <= len(options) <= 3
    assert [option["option"] for option in options] == list(range(1, len(options) + 1))
    assert options[0]["price"] == "от 2 500 до 3 000 ₽"


async def test_free_time_never_in_the_past(ctx: ToolContext) -> None:
    result = await run(ctx, "find_free_time", service_id="102", date_from="2020-01-01")

    assert result["options"]


async def test_booking_by_option_number(ctx: ToolContext) -> None:
    await run(ctx, "find_free_time", service_id="102")

    result = await run(ctx, "book", option=1, client_name="Анна", phone="8 916 150-22-39")

    assert result["status"] == "confirmed"
    crm = ctx.booking.provider
    assert isinstance(crm, FakeCRM)
    [(request, _)] = crm.bookings.values()
    assert request.client.phone == "+79161502239"


async def test_stale_option_gives_clear_error(ctx: ToolContext) -> None:
    result = await run(ctx, "book", option=2, client_name="Анна", phone="+79161502239")

    assert result == {"error": "Предложенный вариант устарел — подберу время заново"}


async def test_option_out_of_range_is_rejected() -> None:
    with pytest.raises(ToolArgumentsError, match="option"):
        registry.parse(ToolCallRequest("c1", "book", '{"option": 4, "client_name": "Анна", "phone": "+79161502239"}'))


async def test_salon_info_from_card(ctx: ToolContext) -> None:
    result = await run(ctx, "salon_info", question="Где вы находитесь?")

    assert "ул. Примерная" in str(result["fragments"])


async def test_handoff_sets_reason(ctx: ToolContext) -> None:
    result = await run(ctx, "handoff_to_admin", reason="Вопрос о противопоказаниях")

    assert result["status"] == "handed_off"
    assert ctx.handoff_reason == "Вопрос о противопоказаниях"


async def test_tool_results_are_recorded_as_facts(ctx: ToolContext) -> None:
    await run(ctx, "find_service", query="педикюр")

    assert "3 200 ₽" in ctx.facts[-1]


async def test_unexpected_error_is_hidden_from_model(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken() -> list[object]:
        raise RuntimeError("password=secret")

    monkeypatch.setattr(ctx.catalog, "services", broken)

    result = await run(ctx, "find_service", query="маникюр")

    assert "secret" not in json.dumps(result)
    assert "администратору" in str(result["error"])


def test_every_tool_has_handler() -> None:
    assert all(callable(tool.handler) for tool in TOOLS)
