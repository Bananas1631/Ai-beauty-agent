"""JWT на Ed25519: токены администраторов и OAuth2, scopes и аудитории, JWKS и ротация ключа."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from beauty_agent.core.config import SecuritySettings
from beauty_agent.core.security import (
    ADMIN_AUDIENCE,
    API_AUDIENCE,
    BILLING_AUDIENCE,
    INDEXER_AUDIENCE,
    InsufficientScopeError,
    InvalidTokenError,
    Security,
    SecurityConfigError,
    UnknownKeyError,
    decode_token,
    generate_signing_key,
    hash_client_secret,
    jwk_public_key,
    needs_rehash,
    verify_client_secret,
)

OLD = generate_signing_key()
NEW = generate_signing_key()
SALON = uuid.uuid4()


def security(keys: str = OLD) -> Security:
    return Security(SecuritySettings(signing_keys=SecretStr(keys)), env="test")


def test_admin_token_is_eddsa_with_kid() -> None:
    issuer = security()

    token = issuer.issue_admin_token(user_id="42", salon_id=SALON)
    header = jwt.get_unverified_header(token)

    assert header["alg"] == "EdDSA"
    assert header["kid"] == issuer.signing_key.kid
    assert issuer.verify_admin_token(token).salon_id == SALON


def test_access_token_audiences_follow_scopes() -> None:
    issuer = security()

    token = issuer.issue_access_token(subject="bac_1", scopes=["knowledge:read", "chat:write"], salon_id=SALON)
    claims = jwt.decode(token, options={"verify_signature": False})

    assert claims["aud"] == sorted([API_AUDIENCE, INDEXER_AUDIENCE])
    assert claims["scope"] == "chat:write knowledge:read"
    verified = issuer.verify_access_token(token, audience=API_AUDIENCE, scope="chat:write")
    assert verified.salon_id == SALON
    assert verified.subject == "bac_1"
    with pytest.raises(InvalidTokenError):  # биллинга среди аудиторий нет
        issuer.verify_access_token(token, audience=BILLING_AUDIENCE)


def test_missing_scope_is_forbidden_not_invalid() -> None:
    issuer = security()
    token = issuer.issue_access_token(subject="bac_1", scopes=["history:read"])

    with pytest.raises(InsufficientScopeError):
        issuer.verify_access_token(token, audience=API_AUDIENCE, scope="chat:write")


def test_unknown_scope_is_not_issued() -> None:
    with pytest.raises(ValueError, match="scopes"):
        security().issue_access_token(subject="bac_1", scopes=["admin:everything"])


def test_admin_token_is_not_an_api_token() -> None:
    issuer = security()
    admin = issuer.issue_admin_token(user_id="42", salon_id=SALON)
    partner = issuer.issue_access_token(subject="bac_1", scopes=["chat:write"], salon_id=SALON)

    with pytest.raises(InvalidTokenError):
        issuer.verify_access_token(admin, audience=API_AUDIENCE)
    with pytest.raises(InvalidTokenError):
        issuer.verify_admin_token(partner)


def test_expired_hs256_none_and_foreign_key_are_rejected() -> None:
    issuer = security()
    expired = issuer.issue_access_token(
        subject="bac_1", scopes=["chat:write"], now=datetime.now(UTC) - timedelta(hours=1)
    )
    payload = {"sub": "x", "aud": API_AUDIENCE, "iss": "beauty-agent", "iat": 1, "exp": 9_999_999_999}
    hs256 = jwt.encode(payload, "secret-" + "x" * 32, algorithm="HS256")
    unsigned = jwt.encode(payload, None, algorithm="none")  # type: ignore[arg-type]
    foreign = jwt.encode(payload, Ed25519PrivateKey.generate(), algorithm="EdDSA", headers={"kid": "someone"})

    for token in (expired, hs256, unsigned, foreign, "not-a-jwt"):
        with pytest.raises(InvalidTokenError):
            issuer.verify_access_token(token, audience=API_AUDIENCE)


def test_rotation_keeps_old_tokens_valid_until_key_is_removed() -> None:
    before = security(OLD)
    old_token = before.issue_access_token(subject="bac_1", scopes=["chat:write"])

    # шаг 1: новый ключ опубликован вторым — подписывает ещё старый
    published = security(f"{OLD},{NEW}")
    assert {key["kid"] for key in published.jwks()["keys"]} == {before.signing_key.kid, security(NEW).signing_key.kid}
    # шаг 2: новый ключ подписывает, старый только проверяет
    rotated = security(f"{NEW},{OLD}")
    new_token = rotated.issue_access_token(subject="bac_1", scopes=["chat:write"])
    assert rotated.verify_access_token(old_token, audience=API_AUDIENCE).subject == "bac_1"
    assert before.signing_key.kid != jwt.get_unverified_header(new_token)["kid"]
    # шаг 3: старый ключ убран — его токены больше не принимаются
    with pytest.raises(UnknownKeyError):
        security(NEW).verify_access_token(old_token, audience=API_AUDIENCE)


def test_jwks_contains_only_public_part() -> None:
    issuer = security()

    (published,) = issuer.jwks()["keys"]

    assert set(published) == {"kty", "crv", "x", "kid", "use", "alg"}
    assert published["kty"] == "OKP"
    assert published["crv"] == "Ed25519"
    token = issuer.issue_admin_token(user_id="1", salon_id=SALON)
    claims = decode_token(
        token, {published["kid"]: jwk_public_key(published)}, issuer="beauty-agent", audience=ADMIN_AUDIENCE
    )
    assert claims["sub"] == "1"


def test_key_derived_from_legacy_jwt_secret_is_stable() -> None:
    """Переход с HS256: без SECURITY_SIGNING_KEYS ключ выводится из старого секрета — одинаково везде."""
    first = Security(SecuritySettings(jwt_secret=SecretStr("j" * 48)), env="prod-like")
    second = Security(SecuritySettings(jwt_secret=SecretStr("j" * 48)), env="prod-like")
    other = Security(SecuritySettings(jwt_secret=SecretStr("k" * 48)), env="prod-like")

    assert first.signing_key.kid == second.signing_key.kid != other.signing_key.kid


@pytest.mark.parametrize("raw", ["not base64!", "c2hvcnQ"])
def test_bad_signing_key_fails_at_start(raw: str) -> None:
    with pytest.raises(SecurityConfigError):
        security(raw)


def test_client_secret_hash() -> None:
    stored = hash_client_secret("s3cret-value")

    assert stored.startswith("$argon2id$")
    assert verify_client_secret(stored, "s3cret-value")
    assert not verify_client_secret(stored, "s3cret-valuE")
    assert not verify_client_secret(None, "s3cret-value")  # неизвестный клиент — та же работа, ответ «нет»
    assert not verify_client_secret("not-a-hash", "s3cret-value")
    assert not needs_rehash(stored)
