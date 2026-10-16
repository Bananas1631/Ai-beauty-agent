"""Секреты каналов и доступ к API администратора.

- Токены ботов и ключи каналов хранятся в БД только зашифрованными (Fernet = AES-128-CBC + HMAC):
  утечка базы не даёт доступа к мессенджерам салонов. Ключей может быть несколько — первый шифрует,
  остальные расшифровывают: так ключ меняется без простоя (ротация).
- secret_token вебхука не хранится, а выводится из общего ключа и ID канала через HMAC-SHA256:
  у каждого канала свой секрет, сравнение — за постоянное время (без утечки по времени ответа).
- JWT для API инбокса подписывается HS256, проверяются срок, издатель и аудитория; в токене роль
  («владелец» или «администратор»). Access-токен живёт 15 минут, refresh-токен — 30 дней и
  меняется при каждом обновлении (ротация). Повторное предъявление уже использованного
  refresh-токена — признак кражи: отзывается вся цепочка токенов этого входа.
- Смена ключа шифрования без потери токенов: новый ключ ставится первым в SECURITY_FERNET_KEYS,
  scripts/rotate_keys.py перешифровывает учётные данные каналов, затем старый ключ убирается.
"""

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import structlog
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from redis.asyncio import Redis

from beauty_agent.core.config import SecuritySettings
from beauty_agent.core.redis import key
from beauty_agent.domain.enums import AdminRole
from beauty_agent.domain.errors import DomainError

logger = structlog.get_logger(__name__)

JWT_ISSUER = "beauty-agent"
JWT_AUDIENCE = "beauty-agent-admin"
# Известные всем dev-ключи: только для локальной разработки и тестов, в проде create_app их не пустит
_DEV_SECRET = b"beauty-agent-local-development-only"


class SecurityConfigError(RuntimeError):
    """Ключи безопасности не заданы там, где без них нельзя (прод)."""


class DecryptionError(DomainError):
    default_message = "Не удалось расшифровать учётные данные канала"


class InvalidTokenError(DomainError):
    default_message = "Токен доступа недействителен"


def _dev_fernet_key() -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(_DEV_SECRET + b":fernet").digest())


@dataclass(frozen=True, slots=True)
class AdminClaims:
    user_id: str  # Telegram ID администратора
    salon_id: uuid.UUID
    expires_at: datetime
    role: AdminRole = AdminRole.ADMIN


class Security:
    def __init__(self, settings: SecuritySettings, *, env: str) -> None:
        if not settings.configured:
            if env == "prod":
                raise SecurityConfigError("Задайте SECURITY_FERNET_KEYS, SECURITY_WEBHOOK_KEY и SECURITY_JWT_SECRET")
            logger.warning("security_dev_keys_in_use")
        keys = (
            [key.strip().encode() for key in settings.fernet_keys.get_secret_value().split(",") if key.strip()]
            if settings.fernet_keys
            else [_dev_fernet_key()]
        )
        self._fernet = MultiFernet([Fernet(key) for key in keys])
        self._webhook_key = (
            settings.webhook_key.get_secret_value().encode() if settings.webhook_key else _DEV_SECRET + b":webhook"
        )
        self._jwt_secret = settings.jwt_secret.get_secret_value() if settings.jwt_secret else "dev-jwt-" + "x" * 32
        self.jwt_ttl = timedelta(seconds=settings.jwt_ttl_s)
        self.refresh_ttl = timedelta(seconds=settings.refresh_ttl_s)

    # ------------------------------------------------------------- шифрование учётных данных

    def encrypt(self, data: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(data, ensure_ascii=False).encode())

    def decrypt(self, token: bytes | None) -> dict[str, Any]:
        if not token:
            return {}
        try:
            data = json.loads(self._fernet.decrypt(token))
        except (InvalidToken, ValueError) as exc:
            raise DecryptionError from exc
        if not isinstance(data, dict):
            raise DecryptionError
        return data

    def rotate(self, token: bytes) -> bytes:
        """Перешифровать первым (новым) ключом — для фоновой ротации."""
        return self._fernet.rotate(token)

    # ------------------------------------------------------------- вебхуки

    def webhook_secret(self, channel: str, account_id: uuid.UUID) -> str:
        """Секрет вебхука канала: 64 hex-символа, подходит под ограничения Telegram и MAX."""
        message = f"{channel}:{account_id}".encode()
        return hmac.new(self._webhook_key, message, hashlib.sha256).hexdigest()

    def verify_webhook(self, channel: str, account_id: uuid.UUID, provided: str | None) -> bool:
        if not provided:
            return False
        return hmac.compare_digest(self.webhook_secret(channel, account_id), provided)

    # ------------------------------------------------------------- JWT для API инбокса

    def issue_admin_token(
        self,
        *,
        user_id: str,
        salon_id: uuid.UUID,
        role: AdminRole = AdminRole.ADMIN,
        now: datetime | None = None,
    ) -> str:
        issued = now or datetime.now(UTC)
        payload = {
            "sub": user_id,
            "salon_id": str(salon_id),
            "role": role.value,
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "iat": issued,
            "exp": issued + self.jwt_ttl,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(payload, self._jwt_secret, algorithm="HS256")

    def verify_admin_token(self, token: str) -> AdminClaims:
        try:
            claims = jwt.decode(
                token,
                self._jwt_secret,
                algorithms=["HS256"],  # явный список: токен с alg=none или RS256 не пройдёт
                audience=JWT_AUDIENCE,
                issuer=JWT_ISSUER,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
            return AdminClaims(
                user_id=str(claims["sub"]),
                salon_id=uuid.UUID(str(claims["salon_id"])),
                expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
                role=AdminRole(claims.get("role", AdminRole.ADMIN.value)),
            )
        except (jwt.PyJWTError, KeyError, ValueError) as exc:
            raise InvalidTokenError from exc


# ---------------------------------------------------------------- refresh-токены


@dataclass(frozen=True, slots=True)
class RefreshGrant:
    user_id: str
    salon_id: uuid.UUID
    family: str  # цепочка токенов одного входа: при краже отзывается целиком


class RefreshTokens:
    """Непрозрачные refresh-токены в Redis: хранится только SHA-256, сам токен — у клиента.

    Обновление одноразовое (GETDEL): токен меняется на новый. Использованный токен помнится
    до конца срока; если его предъявят снова — кто-то скопировал токен, и вся цепочка отзывается.
    """

    def __init__(self, redis: Redis, *, ttl: timedelta) -> None:
        self.redis = redis
        self.ttl_s = int(ttl.total_seconds())

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def issue(self, user_id: str, salon_id: uuid.UUID, *, family: str | None = None) -> str:
        token = secrets.token_urlsafe(32)
        family = family or uuid.uuid4().hex
        digest = self._hash(token)
        payload = json.dumps({"user_id": user_id, "salon_id": str(salon_id), "family": family})
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(key("refresh", digest), payload, ex=self.ttl_s)
            pipe.sadd(key("refresh_family", family), digest)
            pipe.expire(key("refresh_family", family), self.ttl_s)
            await pipe.execute()
        return token

    async def use(self, token: str) -> RefreshGrant:
        """Обменять токен: старый сгорает. Неизвестный или повторный — InvalidTokenError."""
        if not 20 <= len(token) <= 100:
            raise InvalidTokenError
        digest = self._hash(token)
        raw = await self.redis.getdel(key("refresh", digest))
        if raw is None:
            family = await self.redis.get(key("refresh_used", digest))
            if family is not None:
                await self.revoke_family(str(family))
                logger.warning("refresh_token_reuse_detected")
            raise InvalidTokenError
        data = json.loads(raw)
        await self.redis.set(key("refresh_used", digest), data["family"], ex=self.ttl_s)
        await self.redis.srem(key("refresh_family", data["family"]), digest)
        return RefreshGrant(str(data["user_id"]), uuid.UUID(data["salon_id"]), str(data["family"]))

    async def revoke_family(self, family: str) -> None:
        members = await self.redis.smembers(key("refresh_family", family))
        async with self.redis.pipeline(transaction=True) as pipe:
            for digest in members:
                pipe.delete(key("refresh", digest))
            pipe.delete(key("refresh_family", family))
            await pipe.execute()
