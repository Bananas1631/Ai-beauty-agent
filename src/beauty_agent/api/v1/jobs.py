"""Фоновые задачи салона: переиндексация сайта и её прогресс.

    POST /api/v1/salons/{salon_id}/reindex  → 202 {id, status: queued, status_url}
    GET  /api/v1/jobs/{job_id}              → статус, «обработано N из M», результат или ошибка

- Доступ — по JWT администратора из админ-бота (как инбокс); запустить переиндексацию может и
  партнёр с токеном OAuth2 со scope reindex:write. Салон в адресе должен совпадать с салоном из
  токена: чужой салон — 404, будто его нет; чужая задача — тоже 404.
- Куда уходит задача — зависит от KNOWLEDGE_BACKEND: Celery (свой обход сайта) или событие
  site.reindex.requested индексатору (ADR 014); статус в обоих случаях — здесь.
- Повторный POST, пока переиндексация идёт, возвращает ту же задачу: второй обход того же сайта
  только тратил бы деньги на эмбеддинги. Неизменённые страницы и так пропускаются по хешу.
- Очередь недоступна — задача сразу помечается «failed», ответ 503: клиент API не ждёт вечно.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from beauty_agent.api.deps import SessionDep, enforce
from beauty_agent.api.v1.admin import AdminDep, current_admin
from beauty_agent.core.jobs import Job, JobKind, JobStatus, JobStore, QueueUnavailableError, start_reindex
from beauty_agent.core.rate_limit import API_KEY_LIMIT
from beauty_agent.core.security import API_AUDIENCE, InsufficientScopeError, InvalidTokenError, Security
from beauty_agent.db.models import Salon

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["jobs"])
_bearer = HTTPBearer(auto_error=False)


async def reindex_caller(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> tuple[uuid.UUID, str]:
    """Салон и кто запускает: партнёр с reindex:write или администратор из админ-бота."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        return await _admin_caller(request, session, credentials)
    security: Security = request.app.state.security
    try:
        access = security.verify_access_token(credentials.credentials, audience=API_AUDIENCE, scope="reindex:write")
    except InsufficientScopeError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Токену не выдан scope reindex:write") from exc
    except InvalidTokenError:  # не токен партнёра — значит, токен администратора (или плохой)
        return await _admin_caller(request, session, credentials)
    if access.salon_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Токен не привязан к салону")
    await enforce(request, API_KEY_LIMIT, f"oauth:{access.subject}")
    return access.salon_id, f"oauth:{access.subject}"


async def _admin_caller(
    request: Request, session: SessionDep, credentials: HTTPAuthorizationCredentials | None
) -> tuple[uuid.UUID, str]:
    admin = await current_admin(request, session, credentials)
    return admin.salon_id, admin.user_id


ReindexCaller = Annotated[tuple[uuid.UUID, str], Depends(reindex_caller)]


class JobProgress(BaseModel):
    done: int
    total: int
    percent: int


class JobOut(BaseModel):
    id: uuid.UUID
    kind: JobKind
    status: JobStatus
    progress: JobProgress
    attempts: int
    error: str | None
    result: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    status_url: str


def job_out(job: Job) -> JobOut:
    percent = 100 if job.status is JobStatus.DONE else (job.done * 100 // job.total if job.total else 0)
    return JobOut(
        id=job.id,
        kind=job.kind,
        status=job.status,
        progress=JobProgress(done=job.done, total=job.total, percent=percent),
        attempts=job.attempts,
        error=job.error,
        result=job.result,
        created_at=job.created_at,
        updated_at=job.updated_at,
        status_url=f"/api/v1/jobs/{job.id}",
    )


@router.post("/salons/{salon_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex(
    salon_id: uuid.UUID, caller: ReindexCaller, session: SessionDep, request: Request, response: Response
) -> JobOut:
    caller_salon, who = caller
    salon = await session.get(Salon, salon_id) if salon_id == caller_salon else None
    if salon is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Салон не найден")
    if salon.config.site_url is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "У салона не указан сайт (настройка site_url)")
    try:
        job, created = await start_reindex(
            JobStore(request.app.state.redis), request.app.state.runtime.reindexer, salon.id
        )
    except QueueUnavailableError as exc:
        logger.error("reindex_enqueue_failed", error=str(exc))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Очередь задач недоступна") from exc
    if created:
        logger.info("reindex_queued", job_id=str(job.id), caller=who)
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    return job_out(job)


@router.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, admin: AdminDep, request: Request) -> JobOut:
    job = await JobStore(request.app.state.redis).get(job_id, salon_id=admin.salon_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задача не найдена")
    return job_out(job)
