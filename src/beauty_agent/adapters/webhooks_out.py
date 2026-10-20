"""Исходящие вебхуки партнёрам: события о записях салона с подписью Ed25519 и меткой времени.

Партнёр (CRM, сайт или приложение салона) получает POST с телом события (domain/events.py —
персональных данных в событиях нет) и заголовками:

    X-Beauty-Event-Id    event_id — получатель отсеивает повторы (доставка at-least-once)
    X-Beauty-Event-Type  booking.created, booking.rescheduled, booking.cancelled
    X-Beauty-Timestamp   время отправки, секунды Unix
    X-Beauty-Key-Id      kid ключа подписи — открытый ключ в /.well-known/jwks.json
    X-Beauty-Signature   base64url(Ed25519(«<timestamp>.» + тело))

Подписывается метка времени вместе с телом: перехваченный запрос нельзя повторить позже —
получатель отвергает подпись старше 5 минут, а внутри окна — повтор по event_id. Асимметричная
подпись, а не общий секрет HMAC: у партнёра нет ключа, которым можно подделать наш вебхук,
и один открытый ключ подходит всем партнёрам. Адрес партнёра проверяется как внешний (без SSRF).
"""

import json
import time
from collections.abc import Mapping
from typing import Any

import httpx
import structlog
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from beauty_agent.core.security import Security, b64url, b64url_decode
from beauty_agent.domain.errors import ProviderUnavailableError
from beauty_agent.rag.safe_http import check_url

logger = structlog.get_logger(__name__)

TOLERANCE_S = 300
EVENT_ID = "X-Beauty-Event-Id"
EVENT_TYPE = "X-Beauty-Event-Type"
TIMESTAMP = "X-Beauty-Timestamp"
KEY_ID = "X-Beauty-Key-Id"
SIGNATURE = "X-Beauty-Signature"


class WebhookDeliveryError(ProviderUnavailableError):
    default_message = "Партнёр не принял вебхук"


def _signed_message(timestamp: int, body: bytes) -> bytes:
    return f"{timestamp}.".encode() + body


def signed_headers(
    security: Security, body: bytes, *, event_id: str, event_type: str, now: int | None = None
) -> dict[str, str]:
    timestamp = int(time.time()) if now is None else now
    kid, signature = security.sign(_signed_message(timestamp, body))
    return {
        "Content-Type": "application/json",
        EVENT_ID: event_id,
        EVENT_TYPE: event_type,
        TIMESTAMP: str(timestamp),
        KEY_ID: kid,
        SIGNATURE: b64url(signature),
    }


def verify_signature(
    public_keys: Mapping[str, Ed25519PublicKey],
    headers: Mapping[str, str],
    body: bytes,
    *,
    now: int | None = None,
    tolerance_s: int = TOLERANCE_S,
) -> bool:
    """Проверка на стороне получателя (и в тестах): ключ по kid, свежесть метки, подпись тела."""
    lowered = {name.lower(): value for name, value in headers.items()}
    try:
        timestamp = int(lowered[TIMESTAMP.lower()])
        key = public_keys[lowered[KEY_ID.lower()]]
        signature = b64url_decode(lowered[SIGNATURE.lower()])
    except (KeyError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_s:
        return False
    try:
        key.verify(signature, _signed_message(timestamp, body))
    except InvalidSignature:
        return False
    return True


def event_body(payload: Mapping[str, Any]) -> bytes:
    """Тело запроса — компактный JSON: подписываются ровно те байты, которые уйдут."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


class WebhookSender:
    def __init__(self, http: httpx.AsyncClient, security: Security, *, timeout_s: float = 10.0) -> None:
        self.http = http
        self.security = security
        self.timeout_s = timeout_s

    async def deliver(self, url: str, payload: Mapping[str, Any]) -> None:
        """Отправить событие; не 2xx или сеть — WebhookDeliveryError (потребитель повторит)."""
        check_url(url)
        body = event_body(payload)
        headers = signed_headers(
            self.security, body, event_id=str(payload["event_id"]), event_type=str(payload["type"])
        )
        try:
            response = await self.http.post(url, content=body, headers=headers, timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            raise WebhookDeliveryError(f"Сеть: {type(exc).__name__}") from exc
        if not 200 <= response.status_code < 300:
            raise WebhookDeliveryError(f"HTTP {response.status_code}")
        logger.info("webhook_delivered", event_type=payload["type"], status=response.status_code)
