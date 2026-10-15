"""Персональное демо салона: каталог из CRM, база знаний с сайта и ссылка на страницу с чатом.

Демо делается для продажи — без участия салона: провайдер DEMO показывает реальные свободные слоты,
а запись только имитирует. Страница помечена «Демо», закрыта от поисковиков и открывается
по ссылке со случайной частью. Логика — здесь; scripts/make_demo.py и scripts/seed_demo.py — обёртки.
"""

import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.adapters.crm.base import BookingProvider
from beauty_agent.catalog.importer import ImportReport, import_catalog
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.domain.enums import CrmKind
from beauty_agent.domain.errors import DomainError
from beauty_agent.rag.embeddings import EmbeddingsClient
from beauty_agent.rag.ingest import IngestReport, ingest_site

logger = structlog.get_logger(__name__)

_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }
)  # fmt: skip


def demo_slug(name: str) -> str:
    """«Студия «Лаванда»» → demo-studiya-lavanda-3f9a1c: читаемо и не угадывается перебором."""
    words = "".join(char if char.isalnum() else " " for char in name.lower().translate(_TRANSLIT)).split()
    base = "-".join(word for word in words if word.isascii())[:40].strip("-") or "salon"
    return f"demo-{base}-{secrets.token_hex(3)}"


@dataclass
class DemoReport:
    slug: str = ""
    url: str = ""
    catalog: ImportReport | None = None
    site: IngestReport | None = None
    site_error: str | None = None
    timings_s: dict[str, float] = field(default_factory=dict)

    def lines(self) -> list[str]:
        result = [f"{stage}: {seconds:.1f} с" for stage, seconds in self.timings_s.items()]
        if self.catalog:
            result += self.catalog.lines()
        if self.site:
            result.append(f"Сайт: страниц {self.site.pages_indexed}, фрагментов {self.site.chunks}")
        elif self.site_error:
            result.append(f"Сайт не проиндексирован ({self.site_error}) — знания из карточки филиала")
        else:
            result.append("Сайта нет — знания из карточки филиала")
        result.append(f"Демо: {self.url}")
        return result


async def _timed[T](report: DemoReport, stage: str, action: Callable[[], Awaitable[T]]) -> T:
    started = time.perf_counter()
    try:
        return await action()
    finally:
        report.timings_s[stage] = round(time.perf_counter() - started, 2)


async def make_demo(
    session: AsyncSession,
    provider: BookingProvider,
    *,
    public_url: str,
    embedder: EmbeddingsClient,
    company_id: int | None = None,
    crm_kind: CrmKind = CrmKind.DEMO,
    slug: str | None = None,
    site_url: str | None = None,
    site_client: httpx.AsyncClient | None = None,
    max_pages: int = 30,
    pause_s: float = 0.5,
) -> DemoReport:
    report = DemoReport()
    info = await provider.get_salon_info()
    salons = SalonsRepository(session)
    existing = await salons.get_by_yclients_id(company_id) if company_id is not None else None
    report.slug = existing.slug if existing else (slug or demo_slug(info.name))

    report.catalog = await _timed(
        report,
        "Каталог",
        lambda: import_catalog(
            session, provider, slug=report.slug, company_id=company_id, is_demo=True, embedder=embedder
        ),
    )
    salon = await salons.get_by_slug(report.slug)
    if salon is None:  # импорт только что создал салон — его не может не быть
        raise RuntimeError("demo salon vanished")
    salon.settings = {**(salon.settings or {}), "crm_kind": crm_kind.value}
    await session.commit()

    target = site_url or info.site_url
    if target:
        try:
            report.site = await _timed(
                report,
                "Сайт",
                lambda: ingest_site(
                    session, salon.id, target, embedder, client=site_client, max_pages=max_pages, pause_s=pause_s
                ),
            )
        except (DomainError, httpx.HTTPError) as exc:  # сайт недоступен — демо всё равно работает по карточке
            report.site_error = type(exc).__name__
            logger.warning("demo_site_failed", slug=report.slug, error=report.site_error)
    report.url = f"{public_url.rstrip('/')}/s/{report.slug}"
    return report
