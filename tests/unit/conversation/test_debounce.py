import uuid
from collections.abc import AsyncIterator

import pytest
from fakeredis import FakeAsyncRedis

from beauty_agent.adapters.channels.base import ChannelUpdate, UpdateKind
from beauty_agent.conversation.debounce import Debouncer
from beauty_agent.domain.enums import Channel

ACCOUNT = uuid.UUID("01a14169-1127-7108-8cb2-73e7127cdfd0")


def update(number: int, text: str) -> ChannelUpdate:
    return ChannelUpdate(Channel.TELEGRAM, str(number), "555", "555", str(number), UpdateKind.TEXT, text)


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


async def test_three_quick_messages_make_one_batch(redis: FakeAsyncRedis) -> None:
    debouncer = Debouncer(redis)
    tokens = [
        await debouncer.push(ACCOUNT, "555", update(1, "Здравствуйте")),
        await debouncer.push(ACCOUNT, "555", update(2, "хочу на маникюр")),
        await debouncer.push(ACCOUNT, "555", update(3, "в пятницу")),
    ]

    assert await debouncer.take(ACCOUNT, "555", tokens[0]) is None  # после неё пришли новые — пропуск
    assert await debouncer.take(ACCOUNT, "555", tokens[1]) is None
    batch = await debouncer.take(ACCOUNT, "555", tokens[2])

    assert batch is not None
    assert [item.text for item in batch] == ["Здравствуйте", "хочу на маникюр", "в пятницу"]


async def test_retry_gets_the_same_batch(redis: FakeAsyncRedis) -> None:
    debouncer = Debouncer(redis)
    token = await debouncer.push(ACCOUNT, "555", update(1, "Привет"))

    first = await debouncer.take(ACCOUNT, "555", token)
    retry = await debouncer.take(ACCOUNT, "555", token)  # задача упала и повторилась

    assert first == retry
    await debouncer.done(ACCOUNT, "555", token)
    assert await debouncer.take(ACCOUNT, "555", token) is None


async def test_new_message_after_take_starts_new_batch(redis: FakeAsyncRedis) -> None:
    debouncer = Debouncer(redis)
    first_token = await debouncer.push(ACCOUNT, "555", update(1, "Привет"))
    await debouncer.take(ACCOUNT, "555", first_token)

    second_token = await debouncer.push(ACCOUNT, "555", update(2, "Ещё вопрос"))
    batch = await debouncer.take(ACCOUNT, "555", second_token)

    assert batch is not None
    assert [item.text for item in batch] == ["Ещё вопрос"]


async def test_chats_are_independent(redis: FakeAsyncRedis) -> None:
    debouncer = Debouncer(redis)
    token_a = await debouncer.push(ACCOUNT, "555", update(1, "A"))
    token_b = await debouncer.push(ACCOUNT, "777", update(2, "B"))

    assert [item.text for item in await debouncer.take(ACCOUNT, "555", token_a) or []] == ["A"]
    assert [item.text for item in await debouncer.take(ACCOUNT, "777", token_b) or []] == ["B"]
