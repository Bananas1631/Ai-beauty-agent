"""Единый формат ошибок API — Problem Details (RFC 9457).

Доменные ошибки превращаются в 404, 409 или 422 с понятным текстом; неожиданные — в 500
без подробностей: стек и текст исключения остаются в логах, клиент получает только trace_id.
"""

from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from beauty_agent.api.middleware import TRACE_HEADER
from beauty_agent.domain.errors import (
    ConflictError,
    DomainError,
    DomainValidationError,
    NotFoundError,
    ProviderUnavailableError,
)

logger = structlog.get_logger(__name__)

PROBLEM_JSON = "application/problem+json"

# Порядок важен: первым совпадает самый конкретный класс
_STATUS_BY_ERROR: tuple[tuple[type[DomainError], HTTPStatus], ...] = (
    (NotFoundError, HTTPStatus.NOT_FOUND),
    (ConflictError, HTTPStatus.CONFLICT),
    (DomainValidationError, HTTPStatus.UNPROCESSABLE_ENTITY),
    (ProviderUnavailableError, HTTPStatus.SERVICE_UNAVAILABLE),
)


def status_for(error: DomainError) -> HTTPStatus:
    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(error, error_type):
            return status
    return HTTPStatus.BAD_REQUEST


def problem(request: Request, status: int, *, detail: str | None = None, **extra: Any) -> JSONResponse:
    trace_id: str | None = getattr(request.state, "trace_id", None)
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "instance": request.url.path,
    }
    if detail:
        body["detail"] = detail
    if trace_id:
        body["trace_id"] = trace_id
    body.update(extra)
    headers = {TRACE_HEADER: trace_id} if trace_id else None
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return problem(request, status_for(exc), detail=exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Только где и что не так. Поля input и ctx не возвращаем: там может быть телефон клиента.
        errors = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
        return problem(request, HTTPStatus.UNPROCESSABLE_ENTITY, detail="Запрос не прошёл проверку", errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        response = problem(request, exc.status_code, detail=detail)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", None)
        logger.exception("unhandled_error", trace_id=trace_id, path=request.url.path, error_type=type(exc).__name__)
        return problem(request, HTTPStatus.INTERNAL_SERVER_ERROR, detail="Внутренняя ошибка сервера")
