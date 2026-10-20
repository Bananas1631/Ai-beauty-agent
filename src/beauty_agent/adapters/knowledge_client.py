"""HTTP-клиент основного сервиса к индексатору (services/indexer, ADR 014).

- Поиск фрагментов сайта и версия базы знаний — чтение, поэтому один повтор при сетевой ошибке
  или 502/503/504 безопасен. Таймаут короткий (2 с): клиент ждёт агента, агент ждёт поиск.
- Circuit breaker, как у YCLIENTS: после серии ошибок индексатор не дёргается, агент сразу
  получает «сведений нет» и говорит «уточню у администратора», не дожидаясь таймаутов.
- Доступ — токен OAuth2 со scope knowledge:read. Основной сервис сам издатель токенов,
  поэтому токен для себя выпускает локально, без HTTP к собственному /oauth/token, и держит его
  до минуты до истечения. 401 (индексатор не знает ключ после ротации) — новый токен и один повтор.
- Ответ читается «терпимо» (лишние поля игнорируются): индексатор может добавить поле,
  не ломая основной сервис. Обязательные поля проверяет контрактный тест
  tests/contract/test_indexer_api.py.
"""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from beauty_agent.core import metrics
from beauty_agent.core.resilience import CircuitBreaker
from beauty_agent.core.security import Security
from beauty_agent.domain.errors import ProviderUnavailableError

logger = structlog.get_logger(__name__)

SERVICE_CLIENT_ID = "beauty-agent"
_RETRYABLE_STATUS = frozenset({502, 503, 504})


class KnowledgeUnavailableError(ProviderUnavailableError):
    default_message = "База знаний сайта временно недоступна"


class FragmentOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chunk_id: uuid.UUID
    title: str
    url: str | None = None
    content: str
    score: float
    distance: float | None = None
    text_match: bool = False


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str
    fragments: list[FragmentOut]


class VersionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str


class TokenSource(Protocol):
    async def token(self) -> str: ...

    def invalidate(self) -> None: ...


class ServiceTokens:
    """Токены основного сервиса для вызова других сервисов: выпускаются локально, кэшируются."""

    def __init__(
        self,
        security: Security,
        scopes: tuple[str, ...],
        *,
        subject: str = SERVICE_CLIENT_ID,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.security = security
        self.scopes = scopes
        self.subject = subject
        self.clock = clock or (lambda: datetime.now(UTC))
        self._token: str | None = None
        self._expires: datetime | None = None

    async def token(self) -> str:
        now = self.clock()
        if self._token is None or self._expires is None or self._expires - now < timedelta(minutes=1):
            self._token = self.security.issue_access_token(subject=self.subject, scopes=self.scopes, now=now)
            self._expires = now + self.security.jwt_ttl
        return self._token

    def invalidate(self) -> None:
        self._token = None


class KnowledgeClient:
    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        tokens: TokenSource,
        *,
        timeout_s: float = 2.0,
        breaker: CircuitBreaker | None = None,
        retry_delay_s: float = 0.2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http
        self.tokens = tokens
        self.timeout_s = timeout_s
        self.breaker = breaker or CircuitBreaker("indexer")
        self.retry_delay_s = retry_delay_s

    async def search(self, salon_id: uuid.UUID, question: str, *, k: int = 5) -> SearchResponse:
        data = await self._call("POST", f"/v1/salons/{salon_id}/search", {"question": question, "k": k})
        try:
            return SearchResponse.model_validate(data)
        except ValidationError as exc:
            self.breaker.record_failure()
            raise KnowledgeUnavailableError("Индексатор вернул ответ не по контракту") from exc

    async def version(self, salon_id: uuid.UUID) -> str:
        data = await self._call("GET", f"/v1/salons/{salon_id}/version", None)
        try:
            return VersionResponse.model_validate(data).version
        except ValidationError as exc:
            raise KnowledgeUnavailableError("Индексатор вернул ответ не по контракту") from exc

    async def _call(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        self.breaker.before_call()  # разомкнут — CircuitOpenError, это тоже ProviderUnavailableError
        refreshed = False
        last_error = "ошибка"
        for attempt in (1, 2):
            try:
                response = await self.http.request(
                    method,
                    f"{self.base_url}{path}",
                    json=body,
                    headers={"Authorization": f"Bearer {await self.tokens.token()}"},
                    timeout=self.timeout_s,
                )
            except httpx.TransportError as exc:  # таймаут, отказ соединения
                last_error = type(exc).__name__
            else:
                if response.status_code == 401 and not refreshed:
                    self.tokens.invalidate()  # ключ подписи сменили — выпустить новый токен
                    refreshed = True
                    continue
                if response.status_code < 400:
                    self.breaker.record_success()
                    return response.json()
                last_error = f"HTTP {response.status_code}"
                if response.status_code not in _RETRYABLE_STATUS:
                    break
            if attempt == 1:
                await asyncio.sleep(self.retry_delay_s)
        self.breaker.record_failure()
        metrics.provider_error("indexer", last_error)
        logger.warning("indexer_call_failed", path=path, error=last_error)
        raise KnowledgeUnavailableError
