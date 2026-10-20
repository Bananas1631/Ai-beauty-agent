"""Проверка JWT основного сервиса в других сервисах — по открытым ключам из JWKS.

Индексатор (и любой сервис на Python) не спрашивает основной сервис про каждый токен: ключи
берутся с /.well-known/jwks.json и кэшируются на 10 минут. Токен с незнакомым kid (ключ только что
сменили) — повод перечитать JWKS, но не чаще раза в 30 секунд: поток мусорных токенов с выдуманными
kid не превратится в поток запросов к основному сервису. Если JWKS недоступен, а ключи уже есть —
работаем со старыми; если ключей нет совсем — KeysUnavailableError (503, а не 401: токен может
быть хорошим).
"""

import asyncio
import time
from collections.abc import Callable

import httpx
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from beauty_agent.core.security import AccessClaims, UnknownKeyError, access_claims, decode_token, jwk_public_key
from beauty_agent.domain.errors import ProviderUnavailableError

logger = structlog.get_logger(__name__)


class KeysUnavailableError(ProviderUnavailableError):
    default_message = "Ключи проверки токенов недоступны"


class JwksVerifier:
    def __init__(
        self,
        jwks_url: str,
        http: httpx.AsyncClient,
        *,
        issuer: str,
        audience: str,
        ttl_s: float = 600.0,
        min_refresh_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.jwks_url = jwks_url
        self.http = http
        self.issuer = issuer
        self.audience = audience
        self.ttl_s = ttl_s
        self.min_refresh_s = min_refresh_s
        self._clock = clock
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def _fetch(self) -> None:
        response = await self.http.get(self.jwks_url, timeout=5.0)
        response.raise_for_status()
        keys: dict[str, Ed25519PublicKey] = {}
        for item in response.json().get("keys", []):
            try:
                keys[str(item["kid"])] = jwk_public_key(item)
            except (KeyError, ValueError):
                continue  # чужой тип ключа — не наш, пропускаем
        self._keys = keys
        self._fetched_at = self._clock()
        logger.info("jwks_refreshed", keys=sorted(keys))

    async def keys(self, *, force: bool = False) -> dict[str, Ed25519PublicKey]:
        async with self._lock:
            age = None if self._fetched_at is None else self._clock() - self._fetched_at
            stale = age is None or age >= self.ttl_s
            may_refresh = age is None or age >= self.min_refresh_s
            if stale or (force and may_refresh):
                try:
                    await self._fetch()
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("jwks_fetch_failed", error=type(exc).__name__)
                    if not self._keys:
                        raise KeysUnavailableError from exc
            return dict(self._keys)

    async def verify(self, token: str, *, scope: str | None = None) -> AccessClaims:
        """Проверить токен client credentials; scope — обязательное право (InsufficientScopeError)."""
        try:
            claims = decode_token(token, await self.keys(), issuer=self.issuer, audience=self.audience)
        except UnknownKeyError:
            # ключ могли только что сменить: перечитать JWKS и проверить ещё раз
            claims = decode_token(token, await self.keys(force=True), issuer=self.issuer, audience=self.audience)
        result = access_claims(claims)
        if scope is not None:
            result.require(scope)
        return result
