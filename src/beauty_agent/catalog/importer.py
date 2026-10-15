"""Импорт каталога салона из CRM: салон, услуги, мастера и связи «мастер — услуга».

Всё в одной транзакции: каталог либо обновился целиком, либо не изменился. Режим dry_run
выполняет те же запросы и откатывает их — видно, что изменится. Логика отделена от CLI:
её переиспользуют генератор демо (scripts/make_demo.py) и ночная синхронизация.
"""

import hashlib
import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.adapters.crm.base import BookingProvider
from beauty_agent.db.models import KnowledgeItem, Salon
from beauty_agent.db.repositories.catalog import MasterServicesRepository, MastersRepository, ServicesRepository
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.domain.entities import SalonInfo
from beauty_agent.domain.enums import KnowledgeSource

logger = structlog.get_logger(__name__)


@dataclass
class ImportReport:
    salon_id: uuid.UUID | None = None
    salon_created: bool = False
    services_created: int = 0
    services_updated: int = 0
    services_deactivated: int = 0
    masters_created: int = 0
    masters_updated: int = 0
    masters_deactivated: int = 0
    links: int = 0
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        prefix = "[dry-run] " if self.dry_run else ""
        return [
            f"{prefix}Салон: {'создан' if self.salon_created else 'обновлён'} ({self.salon_id})",
            f"{prefix}Услуги: +{self.services_created}, обновлено {self.services_updated}, "
            f"выключено {self.services_deactivated}",
            f"{prefix}Мастера: +{self.masters_created}, обновлено {self.masters_updated}, "
            f"выключено {self.masters_deactivated}",
            f"{prefix}Связей «мастер — услуга»: {self.links}",
            *(f"{prefix}Внимание: {warning}" for warning in self.warnings),
        ]


def salon_card_text(info: SalonInfo) -> str:
    """Карточка филиала как документ базы знаний: адрес и часы агент берёт отсюда."""
    parts = [
        f"Салон: {info.name}",
        f"Адрес: {info.address}" if info.address else None,
        f"Телефон: {info.phone}" if info.phone else None,
        f"Часы работы: {info.working_hours}" if info.working_hours else None,
        f"Сайт: {info.site_url}" if info.site_url else None,
        info.description,
    ]
    return "\n".join(part for part in parts if part)


async def _upsert_salon(
    session: AsyncSession, info: SalonInfo, *, company_id: int | None, slug: str, is_demo: bool
) -> tuple[Salon, bool]:
    salons = SalonsRepository(session)
    salon = await salons.get_by_yclients_id(company_id) if company_id is not None else None
    if salon is None:
        salon = await salons.get_by_slug(slug)
    created = salon is None
    if salon is None:
        salon = salons.add(Salon(slug=slug, name=info.name[:200], is_demo=is_demo))
    salon.name = info.name[:200]
    salon.timezone = info.timezone
    salon.yclients_company_id = company_id
    salon.is_demo = is_demo
    await session.flush()
    return salon, created


async def _upsert_salon_card(session: AsyncSession, salon_id: uuid.UUID, info: SalonInfo) -> None:
    content = salon_card_text(info)
    values = {
        "source": KnowledgeSource.CRM,
        "title": f"Карточка салона «{info.name}»"[:500],
        "content": content,
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
    }
    statement = (
        insert(KnowledgeItem)
        .values(salon_id=salon_id, url=f"crm:company/{info.external_id}", **values)
        .on_conflict_do_update(index_elements=["salon_id", "url"], set_=values)
    )
    await session.execute(statement)


async def import_catalog(
    session: AsyncSession,
    provider: BookingProvider,
    *,
    slug: str,
    company_id: int | None = None,
    is_demo: bool = False,
    dry_run: bool = False,
) -> ImportReport:
    report = ImportReport(dry_run=dry_run)
    info = await provider.get_salon_info()
    services = await provider.list_services()
    masters = await provider.list_masters()
    # Связи «мастер — услуга»: по запросу на услугу (лимит 5 rps соблюдает адаптер)
    links: list[tuple[str, str]] = []
    for service in services:
        doers = await provider.list_masters(service.external_id)
        links.extend((master.external_id, service.external_id) for master in doers)

    try:
        salon, report.salon_created = await _upsert_salon(
            session, info, company_id=company_id, slug=slug, is_demo=is_demo
        )
        report.salon_id = salon.id
        await _upsert_salon_card(session, salon.id, info)

        services_repo = ServicesRepository(session, salon.id)
        saved_services = await services_repo.upsert(services)
        report.services_created, report.services_updated = saved_services.created, saved_services.updated
        report.services_deactivated = await services_repo.deactivate_missing(saved_services.ids)

        masters_repo = MastersRepository(session, salon.id)
        saved_masters = await masters_repo.upsert(masters)
        report.masters_created, report.masters_updated = saved_masters.created, saved_masters.updated
        report.masters_deactivated = await masters_repo.deactivate_missing(saved_masters.ids)

        pairs = []
        for master_external, service_external in links:
            master_id = saved_masters.ids.get(master_external)
            service_id = saved_services.ids.get(service_external)
            if master_id is None or service_id is None:
                report.warnings.append(f"мастер {master_external} не найден в общем списке")
                continue
            pairs.append((master_id, service_id))
        report.links = await MasterServicesRepository(session, salon.id).replace_all(pairs)
        if not services:
            report.warnings.append("в CRM нет активных услуг — проверьте онлайн-запись салона")

        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    except BaseException:
        await session.rollback()
        raise
    logger.info("catalog_imported", salon_id=str(report.salon_id), dry_run=dry_run, services=len(services))
    return report
