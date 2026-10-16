"""Фоновые задачи с прогрессом: переиндексация сайта, демо — то, что дольше HTTP-запроса.

API ставит задачу и сразу отвечает 202 с её ID; воркер Celery пишет сюда статус и прогресс
(«обработано 12 страниц из 30»), GET /api/v1/jobs/{id} читает. Хранится в Redis неделю: это
состояние выполнения, а не данные салона — результат работы задачи лежит в своих таблицах.

- Задача принадлежит салону: чужой салон её не увидит, даже зная ID.
- Одна активная задача одного вида на салон: повторный POST вернёт уже идущую, а не запустит вторую
  (две переиндексации одного сайта мешали бы друг другу и тратили бы деньги на эмбеддинги дважды).
- Повтор после сбоя виден: attempts растёт, статус «retrying» с текстом ошибки, затем «done» или «failed».
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT

from beauty_agent.core.redis import key

JOB_TTL_S = 7 * 24 * 3600
ACTIVE_TTL_S = 3600  # зависшая задача (воркер убит) не блокирует новую дольше часа
MAX_ERROR_CHARS = 300


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    DONE = "done"
    FAILED = "failed"

    @property
    def finished(self) -> bool:
        return self in (JobStatus.DONE, JobStatus.FAILED)


class JobKind(StrEnum):
    REINDEX = "reindex"


@dataclass(frozen=True, slots=True)
class Job:
    id: uuid.UUID
    salon_id: uuid.UUID
    kind: JobKind
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    done: int = 0
    total: int = 0
    attempts: int = 0
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)


class JobStore:
    def __init__(self, redis: Redis, *, clock: Callable[[], datetime] | None = None) -> None:
        self.redis = redis
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _key(job_id: uuid.UUID) -> str:
        return key("job", job_id)

    @staticmethod
    def _active_key(salon_id: uuid.UUID, kind: JobKind) -> str:
        return key("job", "active", kind.value, salon_id)

    async def create(self, salon_id: uuid.UUID, kind: JobKind) -> tuple[Job, bool]:
        """Новая задача или уже идущая того же вида; второе значение — создана ли новая.

        Сначала записывается задача, потом занимается отметка «активная»: второй одновременный
        запрос, проигравший SET NX, всегда найдёт задачу победителя.
        """
        job_id = uuid.uuid4()
        now = self.clock().isoformat()
        fields: dict[FieldT, EncodableT] = {
            "salon_id": str(salon_id),
            "kind": kind.value,
            "status": JobStatus.QUEUED.value,
            "created_at": now,
            "updated_at": now,
            "done": 0,
            "total": 0,
            "attempts": 0,
        }
        await self.redis.hset(self._key(job_id), mapping=fields)
        await self.redis.expire(self._key(job_id), JOB_TTL_S)
        active = self._active_key(salon_id, kind)
        if not await self.redis.set(active, str(job_id), nx=True, ex=ACTIVE_TTL_S):
            current = await self.redis.get(active)
            existing = await self.get(uuid.UUID(_text(current))) if current else None
            if existing is not None and not existing.status.finished:
                await self.redis.delete(self._key(job_id))
                return existing, False
            await self.redis.set(active, str(job_id), ex=ACTIVE_TTL_S)  # прежняя завершилась или истекла
        job = await self.get(job_id)
        if job is None:  # только что записали — не может пропасть, разве что Redis очистили
            raise RuntimeError("job vanished")
        return job, True

    async def get(self, job_id: uuid.UUID, *, salon_id: uuid.UUID | None = None) -> Job | None:
        raw = await self.redis.hgetall(self._key(job_id))
        data = {_text(name): _text(value) for name, value in raw.items()}
        if "salon_id" not in data or (salon_id is not None and data["salon_id"] != str(salon_id)):
            return None
        return Job(
            id=job_id,
            salon_id=uuid.UUID(data["salon_id"]),
            kind=JobKind(data["kind"]),
            status=JobStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            done=int(data.get("done", 0)),
            total=int(data.get("total", 0)),
            attempts=int(data.get("attempts", 0)),
            error=data.get("error") or None,
            result=json.loads(data["result"]) if data.get("result") else {},
        )

    async def _update(self, job_id: uuid.UUID, **values: EncodableT) -> None:
        name = self._key(job_id)
        if not await self.redis.exists(name):  # истекла или удалена — не воскрешаем без полей
            return
        fields: dict[FieldT, EncodableT] = {field_name: value for field_name, value in values.items()}
        fields["updated_at"] = self.clock().isoformat()
        await self.redis.hset(name, mapping=fields)

    async def start(self, job_id: uuid.UUID) -> None:
        name = self._key(job_id)
        if await self.redis.exists(name):
            await self.redis.hincrby(name, "attempts", 1)
        await self._update(job_id, status=JobStatus.RUNNING.value, error="")

    async def progress(self, job_id: uuid.UUID, done: int, total: int) -> None:
        await self._update(job_id, done=done, total=max(total, done))

    async def finish(self, job_id: uuid.UUID, result: dict[str, Any]) -> None:
        await self._update(job_id, status=JobStatus.DONE.value, result=json.dumps(result, ensure_ascii=False))
        await self._release(job_id)

    async def fail(self, job_id: uuid.UUID, error: str, *, final: bool) -> None:
        status = JobStatus.FAILED if final else JobStatus.RETRYING
        await self._update(job_id, status=status.value, error=error[:MAX_ERROR_CHARS])
        if final:
            await self._release(job_id)

    async def _release(self, job_id: uuid.UUID) -> None:
        job = await self.get(job_id)
        if job is None:
            return
        active = self._active_key(job.salon_id, job.kind)
        current = await self.redis.get(active)
        if current is not None and _text(current) == str(job_id):  # не снимаем отметку чужой, более новой задачи
            await self.redis.delete(active)


class ReindexQueue(Protocol):
    async def reindex_site(self, salon_id: uuid.UUID, job_id: uuid.UUID) -> None: ...


class QueueUnavailableError(RuntimeError):
    """Задачу не удалось поставить в очередь: она уже помечена «failed», повтор запроса пробует снова."""


async def start_reindex(jobs: JobStore, queue: ReindexQueue, salon_id: uuid.UUID) -> tuple[Job, bool]:
    """Поставить переиндексацию сайта салона или вернуть уже идущую (API и генератор демо)."""
    job, created = await jobs.create(salon_id, JobKind.REINDEX)
    if created:
        try:
            await queue.reindex_site(salon_id, job.id)
        except Exception as exc:
            await jobs.fail(job.id, "Очередь задач недоступна", final=True)
            raise QueueUnavailableError(type(exc).__name__) from exc
    return job, created
