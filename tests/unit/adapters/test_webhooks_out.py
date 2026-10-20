"""Исходящие вебхуки: подпись Ed25519 с меткой времени, проверка у получателя, доставка."""

import json
import uuid

import httpx
import pytest
from pydantic import SecretStr

from beauty_agent.adapters.webhooks_out import (
    SIGNATURE,
    TIMESTAMP,
    WebhookDeliveryError,
    WebhookSender,
    event_body,
    signed_headers,
    verify_signature,
)
from beauty_agent.core.config import SecuritySettings
from beauty_agent.core.security import Security, generate_signing_key
from beauty_agent.rag.safe_http import UnsafeUrlError

SECURITY = Security(SecuritySettings(), env="test")
NOW = 1_800_000_000
EVENT_ID = str(uuid.uuid4())
PAYLOAD: dict[str, object] = {
    "event_id": EVENT_ID,
    "type": "booking.created",
    "salon_id": str(uuid.uuid4()),
    "data": {},
}


def signed(body: bytes, *, now: int = NOW) -> dict[str, str]:
    return signed_headers(SECURITY, body, event_id=EVENT_ID, event_type="booking.created", now=now)


def test_receiver_accepts_genuine_webhook() -> None:
    body = event_body(PAYLOAD)

    assert verify_signature(SECURITY.public_keys, signed(body), body, now=NOW + 10)


def test_forged_body_or_signature_is_rejected() -> None:
    body = event_body(PAYLOAD)
    headers = signed(body)
    forged_body = body.replace(b"booking.created", b"booking.cancelled")
    forged_signature = {**headers, SIGNATURE: headers[SIGNATURE][:-4] + "AAAA"}
    other_key = Security(SecuritySettings(signing_keys=SecretStr(generate_signing_key())), env="test")
    foreign = signed_headers(other_key, body, event_id="x", event_type="booking.created", now=NOW)

    assert not verify_signature(SECURITY.public_keys, headers, forged_body, now=NOW)
    assert not verify_signature(SECURITY.public_keys, forged_signature, body, now=NOW)
    assert not verify_signature(SECURITY.public_keys, foreign, body, now=NOW)  # чужой kid
    assert not verify_signature(SECURITY.public_keys, {}, body, now=NOW)


def test_replay_older_than_five_minutes_is_rejected() -> None:
    body = event_body(PAYLOAD)
    headers = signed(body)
    shifted = {**headers, TIMESTAMP: str(NOW + 3600)}  # метку подменили — подпись не сходится

    assert not verify_signature(SECURITY.public_keys, headers, body, now=NOW + 301)
    assert not verify_signature(SECURITY.public_keys, shifted, body, now=NOW + 3600)


async def test_sender_posts_exact_signed_bytes() -> None:
    received: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(204)

    sender = WebhookSender(httpx.AsyncClient(transport=httpx.MockTransport(handle)), SECURITY)

    await sender.deliver("https://partner.example/hooks/beauty", PAYLOAD)

    request = received[0]
    assert json.loads(request.content) == PAYLOAD
    assert verify_signature(SECURITY.public_keys, dict(request.headers), request.content)
    assert request.headers["X-Beauty-Event-Id"] == EVENT_ID


@pytest.mark.parametrize("code", [400, 500])
async def test_partner_error_raises_for_retry(code: int) -> None:
    sender = WebhookSender(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(code))), SECURITY
    )

    with pytest.raises(WebhookDeliveryError):
        await sender.deliver("https://partner.example/hooks", PAYLOAD)


async def test_internal_address_is_refused() -> None:
    sender = WebhookSender(httpx.AsyncClient(), SECURITY)

    with pytest.raises(UnsafeUrlError):
        await sender.deliver("http://127.0.0.1:8000/admin", PAYLOAD)
