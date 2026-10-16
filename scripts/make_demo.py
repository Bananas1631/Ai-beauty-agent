"""Персональное демо по ID филиала YCLIENTS за 5 минут — для продажи салону.

    poetry run python -m scripts.make_demo --company-id 555001
    poetry run python -m scripts.make_demo --company-id 555001 --site https://salon.example --wait

Импорт каталога (салон с флагом демо) и ссылка на страницу /s/{slug} — сразу. Сайт, если он есть,
индексируется фоновой задачей Celery (workers/indexing.py): медленный сайт больше не держит генератор,
демо отвечает по карточке филиала, пока знания с сайта добавляются. --wait — дождаться индексации
и показать прогресс (не дольше --wait-timeout секунд). Запись в демо только имитируется.
"""

import argparse
import asyncio
import sys
import time
import uuid
from collections.abc import Sequence

from beauty_agent.adapters.crm.yclients import YclientsProvider
from beauty_agent.adapters.llm.models import build_embedder
from beauty_agent.catalog.demo import DemoReport, make_demo
from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.jobs import JobStore, start_reindex
from beauty_agent.core.logging import configure_logging
from beauty_agent.core.redis import make_redis
from beauty_agent.workers.queue import CeleryQueue

POLL_S = 2.0


async def wait_for(jobs: JobStore, job_id: uuid.UUID, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    shown = (-1, -1)
    while time.monotonic() < deadline:
        job = await jobs.get(job_id)
        if job is None:
            return "задача не найдена (истекла?)"
        if (job.done, job.total) != shown:
            shown = (job.done, job.total)
            print(f"  {job.status.value}: страниц {job.done} из {job.total or '?'}", flush=True)
        if job.status.finished:
            return job.error or f"готово: {job.result}"
        await asyncio.sleep(POLL_S)
    return "индексация продолжается в фоне — статус: GET /api/v1/jobs/" + str(job_id)


async def run(args: argparse.Namespace, settings: Settings) -> DemoReport:
    engine = make_engine(settings.postgres)
    redis = make_redis(settings.redis)
    provider = YclientsProvider(args.company_id, settings.yclients)
    jobs = JobStore(redis)

    async def reindex(salon_id: uuid.UUID) -> uuid.UUID:
        job, _ = await start_reindex(jobs, CeleryQueue(), salon_id)
        return job.id

    try:
        async with make_session_factory(engine)() as session:
            report = await make_demo(
                session,
                provider,
                public_url=settings.app.public_url,
                embedder=build_embedder(settings.llm),
                company_id=args.company_id,
                site_url=args.site,
                max_pages=args.max_pages,
                reindex=reindex,
            )
        print("\n".join(report.lines()), flush=True)
        if args.wait and report.site_job:
            print(await wait_for(jobs, report.site_job, args.wait_timeout))
        return report
    finally:
        await provider.aclose()
        await redis.aclose()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Персональное демо салона по ID филиала YCLIENTS")
    parser.add_argument("--company-id", type=int, required=True, help="ID филиала в YCLIENTS")
    parser.add_argument("--site", help="адрес сайта салона, если его нет в карточке филиала")
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--wait", action="store_true", help="дождаться индексации сайта и показать прогресс")
    parser.add_argument("--wait-timeout", type=float, default=600)
    args = parser.parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    asyncio.run(run(args, get_settings()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
