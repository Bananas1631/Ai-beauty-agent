"""Импорт каталога из фейкового CRM в настоящий PostgreSQL."""

import argparse
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.adapters.crm.fake import FAKE_SERVICES, FakeCRM
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.core.config import PostgresSettings, Settings
from beauty_agent.db.models import KnowledgeItem, Salon
from beauty_agent.db.repositories.catalog import MasterServicesRepository, MastersRepository, ServicesRepository
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.domain.enums import KnowledgeSource
from scripts import import_catalog as cli

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=ZoneInfo("Europe/Moscow"))


@pytest.fixture
def crm() -> FakeCRM:
    return FakeCRM(clock=lambda: NOW)


async def test_import_creates_catalog(session: AsyncSession, crm: FakeCRM) -> None:
    report = await import_catalog(session, crm, slug="lavanda", company_id=555001)

    assert report.salon_created
    assert (report.services_created, report.masters_created) == (12, 4)
    salon = await SalonsRepository(session).get_by_slug("lavanda")
    assert salon is not None
    assert salon.yclients_company_id == 555001
    assert len(await ServicesRepository(session, salon.id).active()) == 12
    assert report.links == 15  # 5 + 3 + 4 + 3 услуг у четырёх мастеров
    manicure = await ServicesRepository(session, salon.id).by_external_id("102")
    assert manicure is not None
    doers = await MastersRepository(session, salon.id).for_service(manicure.id)
    assert [master.name for master in doers] == ["Марина", "Ольга"]
    card = await session.scalar(select(KnowledgeItem).where(KnowledgeItem.salon_id == salon.id))
    assert card is not None
    assert card.source is KnowledgeSource.CRM
    assert "Ежедневно с 10:00 до 20:00" in card.content


async def test_second_import_updates_without_duplicates(session: AsyncSession, crm: FakeCRM) -> None:
    await import_catalog(session, crm, slug="lavanda", company_id=555001)
    report = await import_catalog(session, crm, slug="lavanda", company_id=555001)

    assert not report.salon_created
    assert (report.services_created, report.services_updated) == (0, 12)
    assert (report.masters_created, report.masters_updated) == (0, 4)
    salon = await SalonsRepository(session).get_by_slug("lavanda")
    assert salon is not None
    assert await ServicesRepository(session, salon.id).count() == 12
    assert len(await MasterServicesRepository(session, salon.id).pairs()) == 15


async def test_missing_service_is_deactivated(
    session: AsyncSession, crm: FakeCRM, monkeypatch: pytest.MonkeyPatch
) -> None:
    await import_catalog(session, crm, slug="lavanda", company_id=555001)
    remaining = [service for service in FAKE_SERVICES if service.external_id != "105"]

    async def fewer_services() -> list[object]:
        return list(remaining)

    monkeypatch.setattr(crm, "list_services", fewer_services)
    report = await import_catalog(session, crm, slug="lavanda", company_id=555001)

    assert report.services_deactivated == 1
    salon = await SalonsRepository(session).get_by_slug("lavanda")
    assert salon is not None
    nails = await ServicesRepository(session, salon.id).by_external_id("105")
    assert nails is not None
    assert nails.is_active is False  # не удалена: на услугу могут ссылаться записи
    assert len(await ServicesRepository(session, salon.id).active()) == 11


async def test_dry_run_changes_nothing(session: AsyncSession, crm: FakeCRM) -> None:
    report = await import_catalog(session, crm, slug="lavanda", company_id=555001, dry_run=True)

    assert report.services_created == 12
    assert report.lines()[0].startswith("[dry-run]")
    assert await session.scalar(select(Salon).where(Salon.slug == "lavanda")) is None


def test_cli_rejects_bad_slug() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--fake", "--slug", "Bad Slug!"])


def test_cli_needs_one_source() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--slug", "lavanda"])


async def test_cli_dry_run_on_fake(migrated: PostgresSettings, capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(fake=True, company_id=None, slug="cli-check", demo=True, dry_run=True)

    report = await cli.run(args, Settings(postgres=migrated))

    assert report.dry_run
    assert report.services_created == 12
    # main() запускает свой цикл событий — внутри работающего цикла это делается в отдельном потоке
    argv = ["--fake", "--slug", "cli-check", "--dry-run"]
    code = await asyncio.to_thread(cli.main, argv, Settings(postgres=migrated))
    assert code == 0
    assert "[dry-run] Услуги: +12" in capsys.readouterr().out
