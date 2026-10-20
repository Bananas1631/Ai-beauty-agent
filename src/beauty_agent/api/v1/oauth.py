"""OAuth2 client credentials (RFC 6749, раздел 4.4) и открытые ключи подписи (JWKS).

    POST /oauth/token            grant_type=client_credentials, client_id, client_secret[, scope]
    GET  /.well-known/jwks.json  открытые ключи Ed25519 для проверки токенов и подписей вебхуков

- Клиент передаёт секрет в заголовке Authorization: Basic (предпочтительно) или в теле формы.
  Тело — application/x-www-form-urlencoded, как требует RFC 6749; повтор параметра — ошибка.
- Токен — JWT на 15 минут: sub и client_id — клиент, scope — выданные права (запрошенные, но не
  шире разрешённых клиенту), aud — сервисы этих прав, salon_id — у партнёрского клиента.
  Сервисы проверяют токен сами по JWKS, без запроса к основному сервису.
- Ошибки — в формате RFC 6749 (error, error_description), без подробностей: неизвестный клиент
  и неверный секрет неразличимы ни по ответу, ни по времени (argon2 считается и для неизвестного).
- Ответ с токеном не кэшируется (Cache-Control: no-store); JWKS кэшируется на 5 минут.
- Лимит — 30 запросов в минуту с IP: проверка argon2 дорогая, перебор секретов бессмысленен.
"""

import base64
import binascii
from urllib.parse import parse_qs, unquote

import structlog
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from beauty_agent.api.deps import SessionDep, client_ip, enforce
from beauty_agent.core.rate_limit import OAUTH_TOKEN_LIMIT
from beauty_agent.core.security import Security
from beauty_agent.db.repositories.oauth_clients import OAuthClientsRepository

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["oauth"])

MAX_BODY = 4096
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105 — тип токена по RFC 6750, не секрет
    expires_in: int
    scope: str


class OAuthError(BaseModel):
    error: str
    error_description: str


def _error(code: int, error: str, description: str) -> JSONResponse:
    headers = dict(_NO_STORE)
    if code == status.HTTP_401_UNAUTHORIZED:
        headers["WWW-Authenticate"] = 'Basic realm="beauty-agent"'
    return JSONResponse({"error": error, "error_description": description}, status_code=code, headers=headers)


def _basic_credentials(header: str | None) -> tuple[str, str] | None:
    """client_id и секрет из Authorization: Basic — оба url-кодированы (RFC 6749, 2.3.1)."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None
    client_id, separator, secret = decoded.partition(":")
    return (unquote(client_id), unquote(secret)) if separator else None


_RESPONSES: dict[int | str, dict[str, object]] = {
    400: {"model": OAuthError, "description": "invalid_request, unsupported_grant_type, invalid_scope"},
    401: {"model": OAuthError, "description": "invalid_client"},
}


@router.post("/oauth/token", response_model=TokenOut, responses=_RESPONSES)
async def issue_token(request: Request, session: SessionDep) -> JSONResponse:
    await enforce(request, OAUTH_TOKEN_LIMIT, client_ip(request))
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/x-www-form-urlencoded":
        return _error(400, "invalid_request", "Тело — application/x-www-form-urlencoded")
    body = await request.body()
    if len(body) > MAX_BODY:
        return _error(400, "invalid_request", "Слишком большой запрос")
    form = parse_qs(body.decode("utf-8", errors="replace"))
    if any(len(values) > 1 for values in form.values()):
        return _error(400, "invalid_request", "Параметр повторяется")
    params = {name: values[0] for name, values in form.items()}
    if params.get("grant_type") != "client_credentials":
        return _error(400, "unsupported_grant_type", "Поддерживается только client_credentials")

    basic = _basic_credentials(request.headers.get("authorization"))
    if basic is not None and ("client_id" in params or "client_secret" in params):
        return _error(400, "invalid_request", "Секрет — либо в заголовке, либо в теле, не в обоих")
    client_id, secret = basic or (params.get("client_id", ""), params.get("client_secret", ""))
    if not client_id or not secret:
        return _error(401, "invalid_client", "Нужны client_id и client_secret")

    client = await OAuthClientsRepository(session).authenticate(client_id, secret)
    if client is None:
        logger.info("oauth_client_rejected", ip=client_ip(request))
        return _error(401, "invalid_client", "Клиент не найден или секрет неверный")

    allowed = set(client.scopes)
    requested = set(params["scope"].split()) if params.get("scope") else allowed
    if not requested or not requested <= allowed:
        return _error(400, "invalid_scope", "Запрошены права, которые клиенту не выданы")

    security: Security = request.app.state.security
    token = security.issue_access_token(subject=client.client_id, scopes=requested, salon_id=client.salon_id)
    logger.info("oauth_token_issued", client_id=client.client_id, scope=" ".join(sorted(requested)))
    out = TokenOut(
        access_token=token, expires_in=int(security.jwt_ttl.total_seconds()), scope=" ".join(sorted(requested))
    )
    return JSONResponse(out.model_dump(), headers=_NO_STORE)


@router.get("/.well-known/jwks.json")
async def jwks(request: Request) -> JSONResponse:
    security: Security = request.app.state.security
    return JSONResponse(security.jwks(), headers={"Cache-Control": "public, max-age=300"})
