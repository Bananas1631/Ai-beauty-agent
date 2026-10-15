"""Адаптер YCLIENTS по интерфейсу BookingProvider на публичных методах онлайн-записи.

Все запросы идут через одну функцию _send:
ограничитель частоты → circuit breaker → HTTP → разбор конверта {success, data, meta} → доменные ошибки.

- 429, 5xx, таймауты, обрывы → ProviderUnavailableError (и счётчик ошибок circuit breaker);
- «время занято» → SlotTakenError, неверный токен → ProviderAuthError, нужен SMS-код → ConfirmationRequiredError.

Чтение повторяется до 3 раз с растущей паузой и джиттером. Создание записи не повторяется никогда:
после таймаута сначала ищем, не создалась ли запись, иначе клиента можно записать дважды.
Разбор JSON — в функциях parse_*: их проверяют контрактные тесты на сохранённых ответах.
Шпаргалка по API — docs/integrations/yclients.md.
"""

import asyncio
import math
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter
from tenacity.wait import WaitBaseT

from beauty_agent.core.config import YclientsSettings
from beauty_agent.core.resilience import CircuitBreaker, CircuitOpenError, RateLimiter, TokenBucket
from beauty_agent.domain.entities import BookingRequest, BookingResult, MasterInfo, SalonInfo, ServiceInfo, Slot
from beauty_agent.domain.errors import (
    ConfirmationRequiredError,
    DomainValidationError,
    NotFoundError,
    ProviderAuthError,
    ProviderUnavailableError,
    SlotTakenError,
)

logger = structlog.get_logger(__name__)

# Коды 422 онлайн-записи «время недоступно». [сверить с живым API — docs/integrations/yclients.md]
SLOT_TAKEN_CODES = frozenset({433, 434, 435, 436, 437})
DEFAULT_DURATION_MIN = 60


class YclientsTimeoutError(ProviderUnavailableError):
    """YCLIENTS не ответил вовремя: неизвестно, выполнен ли запрос."""


class YclientsBadResponseError(ProviderUnavailableError):
    """Ответ не совпал с ожидаемым форматом: повтор не поможет, нужен разбор контракта."""


# ---------------------------------------------------------------- разбор ответов


def unwrap(response: httpx.Response) -> Any:
    """Достаёт data из конверта или переводит ошибку YCLIENTS в доменную.

    Текст ошибки YCLIENTS пишется в лог, но не уходит клиенту: там бывают внутренние подробности.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        if response.is_success:
            raise YclientsBadResponseError("YCLIENTS вернул не JSON")
        body = {}
    meta = body.get("meta")
    message = str(meta.get("message", "")) if isinstance(meta, dict) else ""
    status = response.status_code

    if response.is_success and body.get("success", True) is not False:
        return body.get("data")
    logger.info("yclients_rejected", status=status, message=message)
    if status in (401, 403):
        raise ProviderAuthError
    if status == 404:
        raise NotFoundError("Не найдено в системе записи салона")
    lowered = message.lower()
    if _error_code(meta) in SLOT_TAKEN_CODES or "занят" in lowered or "недоступ" in lowered:
        raise SlotTakenError
    if "код" in lowered and ("sms" in lowered or "смс" in lowered or "подтвержд" in lowered):
        raise ConfirmationRequiredError
    raise DomainValidationError("Система записи салона отклонила запрос")


def _error_code(meta: Any) -> int | None:
    if not isinstance(meta, dict):
        return None
    errors = meta.get("errors")
    candidate = errors.get("code") if isinstance(errors, dict) else meta.get("code")
    try:
        return int(candidate) if candidate is not None else None
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _minutes(seconds: Any) -> int:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return DEFAULT_DURATION_MIN
    return math.ceil(value / 60) if value > 0 else DEFAULT_DURATION_MIN


def parse_company(data: dict[str, Any]) -> SalonInfo:
    company_id = data["id"]
    return SalonInfo(
        external_id=str(company_id),
        name=_text(data.get("public_title")) or _text(data.get("title")) or f"Филиал {company_id}",
        timezone=_text(data.get("timezone_name")) or "Europe/Moscow",
        address=_text(data.get("address")),
        phone=_text(data.get("phone")),
        site_url=_text(data.get("site")),
        working_hours=_text(data.get("schedule")),
        description=_text(data.get("short_descr")),
    )


def parse_services(data: dict[str, Any]) -> list[ServiceInfo]:
    categories = {item["id"]: _text(item.get("title")) for item in data.get("category") or []}
    services: list[ServiceInfo] = []
    for item in data.get("services") or []:
        if not item.get("active", 1):
            continue  # услуга в архиве салона — агенту её не предлагать
        services.append(
            ServiceInfo(
                external_id=str(item["id"]),
                name=_text(item.get("title")) or f"Услуга {item['id']}",
                duration_min=_minutes(item.get("seance_length")),
                price_min=_money(item.get("price_min")),
                price_max=_money(item.get("price_max")),
                category=categories.get(item.get("category_id")),
                description=_text(item.get("comment")),
            )
        )
    return services


def parse_staff(
    data: list[dict[str, Any]], *, service_id: str | None = None, bookable_only: bool = False
) -> list[MasterInfo]:
    masters: list[MasterInfo] = []
    for item in data:
        if bookable_only and not item.get("bookable", True):
            continue
        position = item.get("position")
        position_title = _text(position.get("title")) if isinstance(position, dict) else None
        masters.append(
            MasterInfo(
                external_id=str(item["id"]),
                name=_text(item.get("name")) or f"Мастер {item['id']}",
                specialization=_text(item.get("specialization")) or position_title,
                service_ids=frozenset({service_id}) if service_id else frozenset(),
            )
        )
    return masters


def parse_dates(data: dict[str, Any], date_from: date, date_to: date) -> list[date]:
    days = {date.fromisoformat(value) for value in data.get("booking_dates") or []}
    return sorted(day for day in days if date_from <= day <= date_to)


def parse_times(data: list[dict[str, Any]], *, master_id: str, service_id: str, day: date, tz: ZoneInfo) -> list[Slot]:
    slots: list[Slot] = []
    for item in data:
        raw = item.get("datetime")
        if raw:
            starts_at = datetime.fromisoformat(raw)
            if starts_at.tzinfo is None:
                starts_at = starts_at.replace(tzinfo=tz)
        else:
            hours, minutes = (int(part) for part in str(item["time"]).split(":")[:2])
            starts_at = datetime(day.year, day.month, day.day, hours, minutes, tzinfo=tz)
        slots.append(Slot(master_id, service_id, starts_at, _minutes(item.get("seance_length"))))
    return slots


def parse_record(data: list[dict[str, Any]], slot: Slot) -> BookingResult:
    if not data:
        raise YclientsBadResponseError("YCLIENTS не вернул ID записи")
    item = data[0]
    return BookingResult(
        external_id=str(item.get("record_id") or item["id"]),
        slot=slot,
        record_hash=_text(item.get("record_hash")),
    )


def find_record(records: list[dict[str, Any]], slot: Slot, phone: str) -> BookingResult | None:
    wanted = _digits(phone)
    for record in records:
        client = record.get("client") or {}
        if (
            str(record.get("staff_id")) == slot.master_id
            and datetime.fromisoformat(record["datetime"]) == slot.starts_at
            and _digits(str(client.get("phone", ""))) == wanted
        ):
            return BookingResult(str(record["id"]), slot, _text(record.get("record_hash")))
    return None


def _digits(phone: str) -> str:
    return "".join(char for char in phone if char.isdigit())


def numeric_id(value: str, what: str) -> int:
    """ID для пути запроса. Только цифры: значение из диалога не должно менять адрес запроса."""
    if not value.isascii() or not value.isdigit() or len(value) > 18:
        raise DomainValidationError(f"Некорректный ID: {what}")
    return int(value)


def _appointment(slot: Slot) -> dict[str, Any]:
    return {
        "id": 1,
        "services": [numeric_id(slot.service_id, "услуга")],
        "staff_id": numeric_id(slot.master_id, "мастер"),
        "datetime": slot.starts_at.isoformat(),
    }


def _parse[**P, T](parser: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Разбор ответа: несовпадение формата — отдельная ошибка, а не KeyError посреди диалога."""
    try:
        return parser(*args, **kwargs)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        logger.error("yclients_bad_payload", parser=parser.__name__, error=type(exc).__name__)
        raise YclientsBadResponseError("Неожиданный формат ответа YCLIENTS") from exc


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, ProviderUnavailableError) and not isinstance(
        exc, CircuitOpenError | YclientsBadResponseError
    )


# ---------------------------------------------------------------- адаптер


class YclientsProvider:
    def __init__(
        self,
        company_id: int,
        settings: YclientsSettings,
        *,
        timezone: str = "Europe/Moscow",
        http: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
        breaker: CircuitBreaker | None = None,
        retry_wait: WaitBaseT | None = None,
    ) -> None:
        if settings.partner_token is None:
            raise ProviderAuthError("Не задан YCLIENTS_PARTNER_TOKEN")
        self.company_id = company_id
        self.settings = settings
        self.tz = ZoneInfo(timezone)
        self._http = http or httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(settings.timeout_s, connect=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            follow_redirects=False,  # редирект на чужой адрес не должен уносить токен
        )
        self._limiter = limiter or TokenBucket(settings.requests_per_second)
        self._breaker = breaker or CircuitBreaker(f"yclients:{company_id}")
        self._wait = retry_wait or wait_exponential_jitter(multiplier=0.2, max=2.0, jitter=0.2)

    async def aclose(self) -> None:
        await self._http.aclose()

    def headers(self) -> dict[str, str]:
        partner = self.settings.partner_token
        if partner is None:  # проверено в __init__; повтор — для mypy и на случай подмены настроек
            raise ProviderAuthError("Не задан YCLIENTS_PARTNER_TOKEN")
        authorization = f"Bearer {partner.get_secret_value()}"
        if self.settings.user_token is not None:
            authorization += f", User {self.settings.user_token.get_secret_value()}"
        return {"Authorization": authorization, "Accept": self.settings.accept, "Content-Type": "application/json"}

    # ------------------------------------------------------------- HTTP

    async def _send(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None
    ) -> Any:
        await self._limiter.acquire()
        self._breaker.before_call()
        try:
            response = await self._http.request(method, path, params=params, json=body, headers=self.headers())
        except httpx.TimeoutException as exc:
            self._breaker.record_failure()
            raise YclientsTimeoutError("YCLIENTS не ответил вовремя") from exc
        except httpx.HTTPError as exc:
            self._breaker.record_failure()
            raise ProviderUnavailableError from exc
        if response.status_code == 429 or response.status_code >= 500:
            self._breaker.record_failure()
            logger.warning("yclients_unavailable", status=response.status_code, method=method)
            raise ProviderUnavailableError
        self._breaker.record_success()
        return unwrap(response)

    async def _retrying(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None
    ) -> Any:
        """Запрос, который безопасно повторить: чтение и проверка слота ничего не создают."""
        retrying = AsyncRetrying(
            stop=stop_after_attempt(3), wait=self._wait, retry=retry_if_exception(_retryable), reraise=True
        )
        async for attempt in retrying:
            with attempt:
                return await self._send(method, path, params=params, body=body)
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------- BookingProvider

    async def get_salon_info(self) -> SalonInfo:
        data = await self._retrying("GET", f"company/{self.company_id}")
        return _parse(parse_company, data)

    async def list_services(self) -> list[ServiceInfo]:
        data = await self._retrying("GET", f"book_services/{self.company_id}")
        return _parse(parse_services, data)

    async def list_masters(self, service_id: str | None = None) -> list[MasterInfo]:
        params = {"service_ids[]": [numeric_id(service_id, "услуга")]} if service_id else None
        data = await self._retrying("GET", f"book_staff/{self.company_id}", params=params)
        return _parse(parse_staff, data, service_id=service_id, bookable_only=True)

    async def find_slots(
        self, service_id: str, date_from: date, date_to: date, master_id: str | None = None
    ) -> list[Slot]:
        service = numeric_id(service_id, "услуга")
        if master_id is not None:
            master_ids = [str(numeric_id(master_id, "мастер"))]
        else:
            master_ids = [master.external_id for master in await self.list_masters(service_id)]
        if not master_ids:
            return []

        params: dict[str, Any] = {
            "service_ids[]": [service],
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
        }
        if master_id is not None:
            params["staff_id"] = master_ids[0]
        dates_data = await self._retrying("GET", f"book_dates/{self.company_id}", params=params)
        days = _parse(parse_dates, dates_data, date_from, date_to)[: self.settings.max_search_days]

        async def day_times(master: str, day: date) -> list[Slot]:
            data = await self._retrying(
                "GET", f"book_times/{self.company_id}/{master}/{day.isoformat()}", params={"service_ids[]": [service]}
            )
            return _parse(parse_times, data, master_id=master, service_id=service_id, day=day, tz=self.tz)

        # Запросы идут параллельно, но не быстрее лимита: их придерживает ограничитель частоты
        batches = await asyncio.gather(*(day_times(master, day) for day in days for master in master_ids))
        slots = [slot for batch in batches for slot in batch]
        return sorted(slots, key=lambda slot: (slot.starts_at, slot.master_id))

    async def check_slot(self, slot: Slot) -> None:
        await self._retrying("POST", f"book_check/{self.company_id}", body={"appointments": [_appointment(slot)]})

    async def create_booking(self, request: BookingRequest) -> BookingResult:
        body = {
            "phone": _digits(request.client.phone),
            "fullname": request.client.name,
            "email": request.client.email or "",
            "comment": request.crm_comment(),
            "api_id": request.idempotency_key[:64],
            "appointments": [_appointment(request.slot)],
        }
        try:
            data = await self._send("POST", f"book_record/{self.company_id}", body=body)
        except YclientsTimeoutError:
            # Запрос мог дойти до YCLIENTS: ищем запись, прежде чем сообщать об ошибке
            existing = await self.find_booking(request.slot, request.client.phone)
            if existing is not None:
                logger.info("yclients_booking_recovered_after_timeout", record_id=existing.external_id)
                return existing
            raise
        return _parse(parse_record, data, request.slot)

    async def find_booking(self, slot: Slot, phone: str) -> BookingResult | None:
        if self.settings.user_token is None:
            return None  # без токена сотрудника список записей недоступен — решит администратор
        day = slot.starts_at.astimezone(self.tz).date()
        params = {
            "staff_id": numeric_id(slot.master_id, "мастер"),
            "start_date": day.isoformat(),
            "end_date": (day + timedelta(days=1)).isoformat(),
        }
        records = await self._retrying("GET", f"records/{self.company_id}", params=params)
        return _parse(find_record, records or [], slot, phone)
