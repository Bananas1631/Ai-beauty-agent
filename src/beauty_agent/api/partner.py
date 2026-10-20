"""Доступ партнёрских интеграций к REST API: ключ API салона или токен OAuth2.

- X-API-Key — как раньше (шаг 21): ключ салона даёт весь партнёрский API этого салона.
- Authorization: Bearer <JWT> — токен client credentials (POST /oauth/token): подпись Ed25519,
  срок 15 минут, аудитория beauty-agent-api и нужный scope эндпоинта: chat:write — отправить
  сообщение и узнать ответ, history:read — история клиента, reindex:write — переиндексация сайта.
  Чужой scope — 403, истёкший или поддельный токен — 401. Салон берётся из токена (клиент
  привязан к салону при выпуске), а не из запроса.
- Лимит частоты — на ключ или на клиента OAuth, как и раньше на ключ.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated

import structlog
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from beauty_agent.api.deps import SessionDep, enforce
from beauty_agent.core.rate_limit import API_KEY_LIMIT
from beauty_agent.core.security import API_AUDIENCE, InsufficientScopeError, InvalidTokenError, Security
from beauty_agent.db.models import Salon
from beauty_agent.db.repositories.api_keys import ApiKeysRepository

_bearer = HTTPBearer(auto_error=False)
_UNAUTHORIZED = HTTPException(
    status.HTTP_401_UNAUTHORIZED,
    "Нужен ключ API салона (X-API-Key) или токен OAuth2 (Authorization: Bearer)",
    headers={"WWW-Authenticate": "Bearer"},
)


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_403_FORBIDDEN, detail, headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'}
    )


def salon_for(scope: str) -> Callable[..., Awaitable[Salon]]:
    """Зависимость: салон партнёра с правом scope (по ключу API — с любым правом)."""

    async def dependency(
        request: Request,
        session: SessionDep,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
        api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> Salon:
        if api_key:
            found = await ApiKeysRepository(session).authenticate(api_key)
            if found is None:
                raise _UNAUTHORIZED
            key, salon = found
            await enforce(request, API_KEY_LIMIT, str(key.id))
        elif credentials is not None and credentials.scheme.lower() == "bearer":
            security: Security = request.app.state.security
            try:
                claims = security.verify_access_token(credentials.credentials, audience=API_AUDIENCE, scope=scope)
            except InvalidTokenError as exc:
                raise _UNAUTHORIZED from exc
            except InsufficientScopeError as exc:
                raise _forbidden(f"Токену не выдан scope {scope}") from exc
            if claims.salon_id is None:
                raise _forbidden("Токен не привязан к салону")
            found_salon = await session.get(Salon, claims.salon_id)
            if found_salon is None:
                raise _UNAUTHORIZED
            salon = found_salon
            await enforce(request, API_KEY_LIMIT, f"oauth:{claims.subject}")
        else:
            raise _UNAUTHORIZED
        structlog.contextvars.bind_contextvars(salon_id=str(salon.id))
        return salon

    return dependency


ChatWriter = Annotated[Salon, Depends(salon_for("chat:write"))]
HistoryReader = Annotated[Salon, Depends(salon_for("history:read"))]
