"""HTML-страницы сервиса: страница салона /s/{slug} с услугами, ценами и чатом на всю страницу
и политика обработки персональных данных /privacy/{slug}, на которую ведёт ссылка из текста согласия.

Шаблоны Jinja2 с автоэкранированием: название услуги «<script>» из CRM выведется как текст.
Демо-страница помечена «Демо» и закрыта от поисковиков: это страница реального салона,
сделанная без его участия, и открывается только по ссылке со случайной частью.
"""

from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from beauty_agent.api.deps import SalonDep, SessionDep
from beauty_agent.catalog.lookup import DbCatalog
from beauty_agent.domain.consent import CONSENT_VERSION
from beauty_agent.domain.entities import ServiceInfo

router = APIRouter(tags=["pages"], include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@router.get("/s/{slug}", response_class=HTMLResponse)
async def salon_page(request: Request, salon: SalonDep, session: SessionDep) -> HTMLResponse:
    services = await DbCatalog(session, salon.id).services()
    by_category: dict[str, list[ServiceInfo]] = defaultdict(list)
    for service in services:
        by_category[service.category or "Услуги"].append(service)
    response = templates.TemplateResponse(
        request,
        "salon.html",
        {"salon": salon, "categories": dict(by_category)},
    )
    if salon.is_demo:
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/privacy/{slug}", response_class=HTMLResponse)
async def privacy_page(request: Request, salon: SalonDep) -> HTMLResponse:
    config = salon.config
    response = templates.TemplateResponse(
        request,
        "privacy.html",
        {
            "salon": salon,
            "privacy": config.privacy,
            "retention_days": config.retention_days,
            "version": CONSENT_VERSION,
        },
    )
    response.headers["X-Robots-Tag"] = "noindex"
    return response
