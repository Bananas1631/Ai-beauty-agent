"""Ночная синхронизация каталога с YCLIENTS: агент не должен называть устаревшую цену или услугу.

Каждый салон с ID филиала синхронизируется отдельно: сбой одного салона (CRM недоступна, отозван
токен) пишется в лог и не мешает остальным. Импорт — тот же, что при онбординге
(catalog/importer.py): цены и длительности обновляются, услуги и мастера, которых больше нет
в CRM, деактивируются, карточка филиала переиндексируется в базе знаний.
Кэш каталога и слотов в Redis живёт 5 минут и 60 секунд — после синхронизации отдельно не сбрасывается.
"""

import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.db.models import Salon
from beauty_agent.domain.enums import CrmKind
from beauty_agent.rag.embeddings import EmbeddingsClient
from beauty_agent.workers.celery_app import celery, run, runtime

logger = structlog.get_logger(__name__)


@dataclass
class SyncReport:
    synced: list[uuid.UUID] = field(default_factory=list)
    failed: dict[uuid.UUID, str] = field(default_factory=dict)
    services_updated: int = 0


async def sync_catalogs(
    session_factory: async_sessionmaker[AsyncSession],
    providers: ProviderRegistry,
    embedder: EmbeddingsClient | None = None,
) -> SyncReport:
    async with session_factory() as session:
        salons = list(await session.scalars(select(Salon).where(Salon.yclients_company_id.is_not(None))))
    report = SyncReport()
    for salon in salons:
        if salon.config.crm_kind is CrmKind.FAKE:
            continue
        try:
            async with session_factory() as session:
                imported = await import_catalog(
                    session,
                    providers.for_salon(salon),
                    slug=salon.slug,
                    company_id=salon.yclients_company_id,
                    is_demo=salon.is_demo,
                    embedder=embedder,
                )
        except Exception as exc:  # один салон не должен останавливать синхронизацию остальных
            logger.warning("catalog_sync_failed", salon_id=str(salon.id), error=type(exc).__name__)
            report.failed[salon.id] = type(exc).__name__
            continue
        report.synced.append(salon.id)
        report.services_updated += imported.services_updated
        logger.info(
            "catalog_synced",
            salon_id=str(salon.id),
            services_updated=imported.services_updated,
            services_deactivated=imported.services_deactivated,
        )
    return report


@celery.task(name="periodic.catalog_sync")
def catalog_sync_task() -> int:
    current = runtime()
    report = run(sync_catalogs(current.session_factory, current.providers, current.conversations.embedder))
    return len(report.synced)
