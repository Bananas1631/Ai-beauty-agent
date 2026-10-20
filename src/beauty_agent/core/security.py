"""Секреты каналов, токены доступа и подписи.

- Токены ботов и ключи каналов хранятся в БД только зашифрованными (Fernet = AES-128-CBC + HMAC):
  утечка базы не даёт доступа к мессенджерам салонов. Ключей может быть несколько — первый шифрует,
  остальные расшифровывают: так ключ меняется без простоя (ротация).
- secret_token вебхука не хранится, а выводится из общего ключа и ID канала через HMAC-SHA256:
  у каждого канала свой секрет, сравнение — за постоянное время (без утечки по времени ответа).
- JWT подписываются Ed25519 (EdDSA), а не общим секретом HS256: проверить токен может любой сервис
  по открытому ключу из /.well-known/jwks.json (индексатор, админка на Django, биллинг на Kotlin,
  партнёры), а выпустить — только основной сервис. В заголовке токена — kid ключа подписи.
  Проверяются подпись, срок, издатель и аудитория; алгоритм — только EdDSA (alg=none и подмена
  алгоритма не пройдут).
- Ротация ключа подписи без простоя: новый ключ добавляется вторым в SECURITY_SIGNING_KEYS — он
  появляется в JWKS, и сервисы успевают его закэшировать; затем он ставится первым и подписывает;
  старый убирается, когда истекли выданные им токены (15 минут).
- Токены администраторов (аудитория beauty-agent-admin, роль внутри) и токены OAuth2 client
  credentials (аудитории сервисов по scopes, docs/security.md) устроены одинаково. Access-токен
  живёт 15 минут, refresh-токен администратора — 30 дней и меняется при каждом обновлении
  (ротация). Повторное предъявление уже использованного refresh-токена — признак кражи:
  отзывается вся цепочка токенов этого входа.
- Секреты OAuth-клиентов хранятся только как хеш argon2id: медленный хеш с солью делает перебор
  украденной базы дорогим. Ключи API (256 бит случайности) — по SHA-256: перебирать там нечего.
- Смена ключа шифрования без потери токенов: новый ключ ставится первым в SECURITY_FERNET_KEYS,
  scripts/rotate_keys.py перешифровывает учётные данные каналов, затем старый ключ убирается.
"""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

import jwt
import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from redis.asyncio import Redis

from beauty_agent.core.config import SecuritySettings
from beauty_agent.core.redis import key
from beauty_agent.domain.enums import AdminRole
from beauty_agent.domain.errors import DomainError

logger = structlog.get_logger(__name__)

JWT_ISSUER = "beauty-agent"
JWT_ALGORITHM = "EdDSA"
ADMIN_AUDIENCE = "beauty-agent-admin"  # токены администраторов из админ-бота: инбокс, админка
JWT_AUDIENCE = ADMIN_AUDIENCE
API_AUDIENCE = "beauty-agent-api"  # REST API основного сервиса для партнёров
INDEXER_AUDIENCE = "beauty-indexer"
BILLING_AUDIENCE = "beauty-billing"
# Scope → сервис, который его принимает. Аудитории токена выводятся из выданных scopes:
# токен только на чтение базы знаний не примет API чата, и наоборот
SCOPES: dict[str, str] = {
    "chat:write": API_AUDIENCE,
    "history:read": API_AUDIENCE,
    "reindex:write": API_AUDIENCE,
    "knowledge:read": INDEXER_AUDIENCE,
    "billing:read": BILLING_AUDIENCE,
}
# Известные всем dev-ключи: только для локальной разработки и тестов, в проде create_app их не пустит
_DEV_SECRET = b"beauty-agent-local-development-only"
_HKDF_INFO = b"beauty-agent ed25519 signing key v1"


class SecurityConfigError(RuntimeError):
    """Ключи безопасности не заданы там, где без них нельзя (прод)."""


class DecryptionError(DomainError):
    default_message = "Не удалось расшифровать учётные данные канала"


class InvalidTokenError(DomainError):
    default_message = "Токен доступа недействителен"


class UnknownKeyError(InvalidTokenError):
    """Токен подписан ключом, которого нет среди известных: потребитель JWKS обновит ключи и повторит."""


class InsufficientScopeError(DomainError):
    default_message = "Недостаточно прав для этого действия"


def _dev_fernet_key() -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(_DEV_SECRET + b":fernet").digest())


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True, slots=True)
class AdminClaims:
    user_id: str  # Telegram ID администратора
    salon_id: uuid.UUID
    expires_at: datetime
    role: AdminRole = AdminRole.ADMIN


@dataclass(frozen=True, slots=True)
class AccessClaims:
    """Токен OAuth2 client credentials: кто (client_id), что можно (scopes), для какого салона."""

    subject: str
    scopes: frozenset[str]
    expires_at: datetime
    salon_id: uuid.UUID | None = None  # партнёрский клиент привязан к салону; внутренние сервисы — нет

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise InsufficientScopeError(f"Нужен scope {scope}")


# ---------------------------------------------------------------- ключи подписи Ed25519


def raw_public(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def key_id(key: Ed25519PublicKey) -> str:
    """kid — отпечаток открытого ключа: один и тот же ключ на всех серверах получает один kid."""
    return hashlib.sha256(raw_public(key)).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class SigningKey:
    private: Ed25519PrivateKey
    kid: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kid", key_id(self.private.public_key()))

    @property
    def public(self) -> Ed25519PublicKey:
        return self.private.public_key()

    @classmethod
    def from_seed(cls, seed: bytes) -> "SigningKey":
        return cls(Ed25519PrivateKey.from_private_bytes(seed))


def public_jwk(key: Ed25519PublicKey) -> dict[str, str]:
    """Открытый ключ в формате JWK (RFC 8037): так его берут PyJWT, nimbus-jose-jwt и другие библиотеки."""
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url(raw_public(key)),
        "kid": key_id(key),
        "use": "sig",
        "alg": JWT_ALGORITHM,
    }


def jwk_public_key(jwk_data: Mapping[str, Any]) -> Ed25519PublicKey:
    if jwk_data.get("kty") != "OKP" or jwk_data.get("crv") != "Ed25519":
        raise ValueError("ожидается ключ OKP Ed25519")
    return Ed25519PublicKey.from_public_bytes(b64url_decode(str(jwk_data["x"])))


def generate_signing_key() -> str:
    """Новый ключ подписи для SECURITY_SIGNING_KEYS: 32 случайных байта в base64url."""
    return b64url(secrets.token_bytes(32))


def _signing_keys(settings: SecuritySettings) -> list[SigningKey]:
    if settings.signing_keys:
        keys = []
        for raw in settings.signing_keys.get_secret_value().split(","):
            if not raw.strip():
                continue
            try:
                seed = b64url_decode(raw.strip())
            except (binascii.Error, ValueError) as exc:
                raise SecurityConfigError("SECURITY_SIGNING_KEYS: ключ не в base64url") from exc
            if len(seed) != 32:
                raise SecurityConfigError("SECURITY_SIGNING_KEYS: ключ Ed25519 — ровно 32 байта")
            keys.append(SigningKey.from_seed(seed))
        if keys:
            return keys
    if settings.jwt_secret:
        # Переход с HS256 без новой переменной: ключ Ed25519 выводится из прежнего секрета через HKDF
        hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO)
        return [SigningKey.from_seed(hkdf.derive(settings.jwt_secret.get_secret_value().encode()))]
    return [SigningKey.from_seed(hashlib.sha256(_DEV_SECRET + b":ed25519").digest())]


def decode_token(token: str, keys: Mapping[str, Ed25519PublicKey], *, issuer: str, audience: str) -> dict[str, Any]:
    """Проверить JWT: ключ по kid, только EdDSA, срок, издатель, аудитория. Ошибка — InvalidTokenError."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise InvalidTokenError from exc
    if header.get("alg") != JWT_ALGORITHM:
        raise InvalidTokenError
    key = keys.get(str(header.get("kid")))
    if key is None:
        raise UnknownKeyError
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[JWT_ALGORITHM],  # явный список: токен с alg=none или HS256 не пройдёт
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError from exc
    return claims


def access_claims(claims: Mapping[str, Any]) -> AccessClaims:
    """Поля токена client credentials: scope — строка через пробел (RFC 9068)."""
    try:
        salon = claims.get("salon_id")
        return AccessClaims(
            subject=str(claims["sub"]),
            scopes=frozenset(str(claims.get("scope", "")).split()),
            expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
            salon_id=uuid.UUID(str(salon)) if salon else None,
        )
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError from exc


# ---------------------------------------------------------------- секреты OAuth-клиентов

# argon2id с параметрами RFC 9106 (профиль low-memory: 64 МиБ, 3 прохода) — десятки мс на проверку
_HASHER = PasswordHasher()


def generate_client_secret() -> str:
    return secrets.token_urlsafe(32)


def hash_client_secret(secret: str) -> str:
    return _HASHER.hash(secret)


@cache
def _dummy_hash() -> str:
    return _HASHER.hash(secrets.token_urlsafe(16))


def verify_client_secret(stored_hash: str | None, secret: str) -> bool:
    """Проверка за одинаковое время: неизвестный client_id тоже проходит argon2 (по пустышке)."""
    try:
        return _HASHER.verify(stored_hash or _dummy_hash(), secret) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Параметры argon2 усилили — хеш пересчитывается при следующем успешном входе."""
    return _HASHER.check_needs_rehash(stored_hash)


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
        self._signing = _signing_keys(settings)
        self.issuer = settings.issuer
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

    # ------------------------------------------------------------- подпись и JWKS

    @property
    def signing_key(self) -> SigningKey:
        return self._signing[0]

    @property
    def public_keys(self) -> dict[str, Ed25519PublicKey]:
        """Все опубликованные ключи: активный и те, что ещё проверяют выданные ими токены."""
        return {key.kid: key.public for key in self._signing}

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        return {"keys": [public_jwk(key.public) for key in self._signing]}

    def sign(self, data: bytes) -> tuple[str, bytes]:
        """Подписать данные активным ключом (исходящие вебхуки): kid и подпись."""
        key = self.signing_key
        return key.kid, key.private.sign(data)

    def _encode(self, payload: dict[str, Any]) -> str:
        key = self.signing_key
        return jwt.encode(payload, key.private, algorithm=JWT_ALGORITHM, headers={"kid": key.kid})

    # ------------------------------------------------------------- токены администраторов

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
            "iss": self.issuer,
            "aud": ADMIN_AUDIENCE,
            "iat": issued,
            "exp": issued + self.jwt_ttl,
            "jti": uuid.uuid4().hex,
        }
        return self._encode(payload)

    def verify_admin_token(self, token: str) -> AdminClaims:
        claims = decode_token(token, self.public_keys, issuer=self.issuer, audience=ADMIN_AUDIENCE)
        try:
            return AdminClaims(
                user_id=str(claims["sub"]),
                salon_id=uuid.UUID(str(claims["salon_id"])),
                expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
                role=AdminRole(claims.get("role", AdminRole.ADMIN.value)),
            )
        except (KeyError, ValueError) as exc:
            raise InvalidTokenError from exc

    # ------------------------------------------------------------- OAuth2 client credentials

    def issue_access_token(
        self,
        *,
        subject: str,
        scopes: Iterable[str],
        salon_id: uuid.UUID | None = None,
        now: datetime | None = None,
        ttl: timedelta | None = None,
    ) -> str:
        granted = sorted(set(scopes))
        unknown = [scope for scope in granted if scope not in SCOPES]
        if not granted or unknown:
            raise ValueError(f"неизвестные scopes: {unknown or 'пусто'}")
        issued = now or datetime.now(UTC)
        payload: dict[str, Any] = {
            "sub": subject,
            "client_id": subject,
            "scope": " ".join(granted),
            "iss": self.issuer,
            "aud": sorted({SCOPES[scope] for scope in granted}),
            "iat": issued,
            "exp": issued + (ttl or self.jwt_ttl),
            "jti": uuid.uuid4().hex,
        }
        if salon_id is not None:
            payload["salon_id"] = str(salon_id)
        return self._encode(payload)

    def verify_access_token(self, token: str, *, audience: str, scope: str | None = None) -> AccessClaims:
        claims = access_claims(decode_token(token, self.public_keys, issuer=self.issuer, audience=audience))
        if scope is not None:
            claims.require(scope)
        return claims


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
