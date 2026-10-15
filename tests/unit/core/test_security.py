import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from beauty_agent.core.config import SecuritySettings
from beauty_agent.core.security import DecryptionError, InvalidTokenError, Security, SecurityConfigError

ACCOUNT = uuid.UUID("01a14169-1127-7108-8cb2-73e7127cdfd0")


def configured(*keys: bytes) -> SecuritySettings:
    return SecuritySettings(
        fernet_keys=SecretStr(",".join(key.decode() for key in keys)),
        webhook_key=SecretStr("webhook-key"),
        jwt_secret=SecretStr("j" * 48),
    )


def test_prod_requires_keys() -> None:
    with pytest.raises(SecurityConfigError):
        Security(SecuritySettings(), env="prod")
    Security(SecuritySettings(), env="local")  # локально — dev-ключи с предупреждением в логе


def test_credentials_roundtrip_and_tamper() -> None:
    security = Security(configured(Fernet.generate_key()), env="prod")

    token = security.encrypt({"token": "123:ABC", "secret": "s"})

    assert b"123:ABC" not in token  # в базе нет открытого токена
    assert security.decrypt(token) == {"token": "123:ABC", "secret": "s"}
    assert security.decrypt(None) == {}
    with pytest.raises(DecryptionError):
        security.decrypt(token[:-4] + b"AAAA")


def test_key_rotation() -> None:
    old, new = Fernet.generate_key(), Fernet.generate_key()
    token = Security(configured(old), env="prod").encrypt({"token": "t"})

    rotated_security = Security(configured(new, old), env="prod")  # новый ключ первым, старый — для чтения

    assert rotated_security.decrypt(token) == {"token": "t"}
    rotated = rotated_security.rotate(token)
    assert Security(configured(new), env="prod").decrypt(rotated) == {"token": "t"}
    with pytest.raises(DecryptionError):
        Security(configured(new), env="prod").decrypt(token)


def test_webhook_secret_per_account() -> None:
    security = Security(configured(Fernet.generate_key()), env="prod")
    secret = security.webhook_secret("telegram", ACCOUNT)

    assert len(secret) == 64
    assert security.verify_webhook("telegram", ACCOUNT, secret)
    assert not security.verify_webhook("telegram", uuid.uuid4(), secret)  # у другого канала — другой секрет
    assert not security.verify_webhook("vk", ACCOUNT, secret)
    assert not security.verify_webhook("telegram", ACCOUNT, None)


def test_admin_jwt() -> None:
    security = Security(configured(Fernet.generate_key()), env="prod")
    salon_id = uuid.uuid4()

    token = security.issue_admin_token(user_id="42", salon_id=salon_id)
    claims = security.verify_admin_token(token)

    assert claims.user_id == "42"
    assert claims.salon_id == salon_id


def test_jwt_rejects_expired_forged_and_none_alg() -> None:
    security = Security(configured(Fernet.generate_key()), env="prod")
    salon_id = uuid.uuid4()
    expired = security.issue_admin_token(user_id="42", salon_id=salon_id, now=datetime.now(UTC) - timedelta(days=2))
    forged = jwt.encode({"sub": "42", "salon_id": str(salon_id)}, "other-secret-" + "x" * 32, algorithm="HS256")
    unsigned = jwt.encode(
        {"sub": "42", "salon_id": str(salon_id), "aud": "beauty-agent-admin", "iss": "beauty-agent"},
        None,  # type: ignore[arg-type]
        algorithm="none",
    )

    for token in (expired, forged, unsigned, "not-a-jwt"):
        with pytest.raises(InvalidTokenError):
            security.verify_admin_token(token)
