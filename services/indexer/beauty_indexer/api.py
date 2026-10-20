"""REST API индексатора: гибридный поиск фрагментов сайта и версия базы знаний салона.

    POST /v1/salons/{salon_id}/search   {"question": "...", "k": 5} → {"version", "fragments": [...]}
    GET  /v1/salons/{salon_id}/version  → {"version": "..."}
    GET  /health, /ready

Запуск: uvicorn beauty_indexer.api:create_app --factory --port 8001

- Доступ — токен OAuth2 основного сервиса со scope knowledge:read и аудиторией beauty-indexer.
  Токен проверяется по открытым ключам из JWKS основного сервиса (кэш 10 минут): индексатор не
  спрашивает основной сервис про каждый запрос. Токен партнёра привязан к салону — чужой салон 404.
- Ответ — контракт с основным сервисом (adapters/knowledge_client.py): поле можно добавить,
  удалить или переименовать — нельзя; это проверяет tests/contract/test_indexer_api.py.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.adapters.llm.models import build_embedder
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.jwks import JwksVerifier, KeysUnavailableError
from beauty_agent.core.logging import configure_logging
from beauty_agent.core.security import INDEXER_AUDIENCE, AccessClaims, InsufficientScopeError, InvalidTokenError
from beauty_agent.rag.embeddings import EmbeddingsClient
from beauty_indexer import __version__
from beauty_indexer.config import Settings, get_settings
from beauty_indexer.store import KnowledgeStore

logger = structlog.get_logger(__name__)
_bearer = HTTPBearer(auto_error=False)
SCOPE = "knowledge:read"


class SearchIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    k: int = Field(5, ge=1, le=10)


class FragmentOut(BaseModel):
    chunk_id: uuid.UUID
    title: str
    url: str
    content: str
    score: float
    distance: float | None
    text_match: bool


class SearchOut(BaseModel):
    version: str
    fragments: list[FragmentOut]


class VersionOut(BaseModel):
    version: str


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


async def caller(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> AccessClaims:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Нужен токен OAuth2", headers={"WWW-Authenticate": "Bearer"})
    verifier: JwksVerifier = request.app.state.verifier
    try:
        return await verifier.verify(credentials.credentials, scope=SCOPE)
    except InsufficientScopeError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Нужен scope {SCOPE}") from exc
    except KeysUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Ключи проверки токенов недоступны") from exc
    except InvalidTokenError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Токен недействителен", headers={"WWW-Authenticate": "Bearer"}
        ) from exc


SessionDep = Annotated[AsyncSession, Depends(get_session)]
CallerDep = Annotated[AccessClaims, Depends(caller)]


def own_salon(claims: AccessClaims, salon_id: uuid.UUID) -> None:
    """Токен партнёра — только свой салон; токен основного сервиса (без salon_id) — любой."""
    if claims.salon_id is not None and claims.salon_id != salon_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Салон не найден")


def create_app(
    settings: Settings | None = None,
    *,
    embedder: EmbeddingsClient | None = None,
    http: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.app.log_level, json_logs=settings.app.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.postgres)
        client = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0))
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        app.state.embedder = embedder or build_embedder(settings.llm)
        app.state.verifier = JwksVerifier(
            settings.indexer.jwks_url, client, issuer=settings.indexer.issuer, audience=INDEXER_AUDIENCE
        )
        try:
            yield
        finally:
            if http is None:
                await client.aclose()
            await engine.dispose()

    app = FastAPI(
        title="beauty-indexer",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.app.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.app.docs_enabled else None,
    )

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        try:
            async with asyncio.timeout(settings.app.ready_timeout_s), request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            logger.warning("indexer_not_ready", error=type(exc).__name__)
            return JSONResponse({"status": "unavailable", "postgres": "down"}, status_code=503)
        return JSONResponse({"status": "ok", "postgres": "ok"})

    @app.post("/v1/salons/{salon_id}/search", response_model=SearchOut, tags=["knowledge"])
    async def search(
        salon_id: uuid.UUID, body: SearchIn, claims: CallerDep, session: SessionDep, request: Request
    ) -> SearchOut:
        own_salon(claims, salon_id)
        model: EmbeddingsClient = request.app.state.embedder
        embedded = await model.embed([body.question])
        store = KnowledgeStore(session, salon_id)
        found = await store.hybrid_search(body.question, embedded.vectors[0], model.model_key, k=body.k)
        return SearchOut(
            version=await store.version(),
            fragments=[
                FragmentOut(
                    chunk_id=item.chunk_id,
                    title=item.title,
                    url=item.url,
                    content=item.content,
                    score=item.score,
                    distance=item.distance,
                    text_match=item.text_match,
                )
                for item in found
            ],
        )

    @app.get("/v1/salons/{salon_id}/version", response_model=VersionOut, tags=["knowledge"])
    async def version(salon_id: uuid.UUID, claims: CallerDep, session: SessionDep) -> VersionOut:
        own_salon(claims, salon_id)
        return VersionOut(version=await KnowledgeStore(session, salon_id).version())

    return app
