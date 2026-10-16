"""Генератор персонального демо: каталог, сайт, ссылка — на фейковом CRM и вымышленном сайте."""

import re
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.adapters.crm.demo import DemoProvider
from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.catalog.demo import demo_slug, make_demo, normalize_site_url
from beauty_agent.db.models import KnowledgeChunk
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.domain.entities import BookingRequest, ClientContact
from beauty_agent.domain.enums import CrmKind
from beauty_agent.rag.embeddings import HashingEmbeddings
from evals.site_server import site_client, site_host

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=ZoneInfo("Europe/Moscow"))


def test_demo_slug_is_readable_and_random() -> None:
    first, second = demo_slug("Студия красоты «Лаванда»"), demo_slug("Студия красоты «Лаванда»")

    assert re.fullmatch(r"demo-studiya-krasoty-lavanda-[0-9a-f]{6}", first)
    assert first != second  # ссылку не угадать перебором названий
    assert re.fullmatch(r"demo-salon-[0-9a-f]{6}", demo_slug("🙂🙂"))


async def test_demo_with_site(session: AsyncSession) -> None:
    async with site_client("lavanda") as client:
        report = await make_demo(
            session,
            FakeCRM(clock=lambda: NOW),
            public_url="https://app.example",
            embedder=HashingEmbeddings(),
            company_id=555001,
            site_url=f"https://{site_host('lavanda')}/",
            site_client=client,
            pause_s=0,
        )

    assert report.url == f"https://app.example/s/{report.slug}"
    assert set(report.timings_s) == {"Каталог", "Сайт"}
    assert report.site is not None
    assert report.site.pages_indexed == 5
    salon = await SalonsRepository(session).get_by_slug(report.slug)
    assert salon is not None
    assert salon.is_demo
    assert salon.config.crm_kind is CrmKind.DEMO
    chunks = await session.scalar(select(func.count()).where(KnowledgeChunk.salon_id == salon.id))
    assert chunks is not None
    assert chunks > 5  # страницы сайта и карточка филиала
    assert any(line.startswith("Демо: https://app.example/s/demo-") for line in report.lines())


async def test_second_run_keeps_the_link(session: AsyncSession) -> None:
    crm = FakeCRM(clock=lambda: NOW)
    first = await make_demo(session, crm, public_url="https://app.example", embedder=HashingEmbeddings(), company_id=7)
    second = await make_demo(session, crm, public_url="https://app.example", embedder=HashingEmbeddings(), company_id=7)

    assert second.slug == first.slug  # ссылка, отправленная владельцу салона, не протухает


async def test_unreachable_site_does_not_break_demo(session: AsyncSession) -> None:
    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет связи")

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
        report = await make_demo(
            session,
            FakeCRM(clock=lambda: NOW),
            public_url="https://app.example",
            embedder=HashingEmbeddings(),
            site_url="https://down.example/",
            site_client=client,
            pause_s=0,
        )

    assert report.site is None
    assert report.site_error == "ConnectError"
    assert report.url


async def test_demo_provider_only_simulates_bookings() -> None:
    crm = FakeCRM(clock=lambda: NOW)
    demo = DemoProvider(crm)
    slot = (await demo.find_slots("102", date(2026, 10, 20), date(2026, 10, 20)))[0]
    request = BookingRequest(slot, ClientContact("Анна", "+79161502239"), "demo-key")

    first = await demo.create_booking(request)
    second = await demo.create_booking(request)

    assert first == second
    assert first.external_id.startswith("demo-")
    assert crm.bookings == {}  # в настоящей CRM салона ничего не создано
    assert await demo.find_booking(slot, "+79161502239") is None
    assert len(await demo.list_services()) == 12
    assert (await demo.get_salon_info()).name
    assert await demo.list_masters("102")
    await demo.check_slot(slot)


async def test_site_is_indexed_in_background_job(session: AsyncSession) -> None:
    """Генератор не ждёт обход сайта: ставит задачу, сайт сохранён в настройках салона для воркера."""
    queued: list[uuid.UUID] = []
    job_id = uuid.uuid4()

    async def reindex(salon_id: uuid.UUID) -> uuid.UUID:
        queued.append(salon_id)
        return job_id

    report = await make_demo(
        session,
        FakeCRM(clock=lambda: NOW),
        public_url="https://app.example",
        embedder=HashingEmbeddings(),
        site_url="lavanda.example",
        reindex=reindex,
    )
    salon = await SalonsRepository(session).get_by_slug(report.slug)

    assert salon is not None
    assert queued == [salon.id]
    assert report.site_job == job_id
    assert report.site is None  # в процессе генератора сайт не обходили
    assert "Каталог" in report.timings_s
    assert "Сайт" not in report.timings_s
    assert salon.config.site_url == "https://lavanda.example"  # схема добавлена
    assert any(str(job_id) in line for line in report.lines())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://lavanda.example/", "https://lavanda.example/"),
        ("lavanda.example", "https://lavanda.example"),
        ("  http://lavanda.example/uslugi ", "http://lavanda.example/uslugi"),
        ("javascript:alert(1)", None),
        ("", None),
        (None, None),
    ],
)
def test_site_url_from_crm_card_is_normalized(raw: str | None, expected: str | None) -> None:
    assert normalize_site_url(raw) == expected
