"""Движок записи: «код предлагает и проверяет, модель только разговаривает».

propose — берёт слоты у CRM через кэш (60 секунд), выбирает 2–3 варианта и сохраняет их
за диалогом под номерами 1–3. Модель получает короткий нумерованный список.

confirm — принимает номер варианта, а не время. Занимает ключ идемпотентности, заново проверяет
слот в CRM без кэша, создаёт запись ровно один раз и сохраняет её в БД. Время заняли —
новые варианты; CRM недоступна — запись в статусе «администратор подтвердит».

client_bookings / cancel / reschedule — «мои записи», отмена и перенос. Список — только записи,
сделанные из этого диалога-клиента (наша БД), сверенные с CRM: администратор мог перенести или
отменить их в YCLIENTS. Поиск по телефону не используется: телефон, который назвал клиент,
не подтверждён, и по чужому номеру нельзя увидеть или отменить чужую запись. Записи в списке
пронумерованы, отмена и перенос — по номеру, с ключом идемпотентности.

Почему не SERIALIZABLE: от гонки защищают ограничение исключения в bookings и уникальный
ключ идемпотентности, а не уровень изоляции (docs/adr/006-hybrid-code-llm.md).
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import structlog
from redis.asyncio import Redis

from beauty_agent.adapters.crm.base import BookingProvider
from beauty_agent.core.redis import IdempotencyStore, JsonCache, key
from beauty_agent.domain.booking_engine.offers import Offer, OfferStore, PartOfDay, pick_offers
from beauty_agent.domain.entities import BookingRequest, ClientContact, ServiceInfo, Slot
from beauty_agent.domain.enums import BookingStatus
from beauty_agent.domain.errors import (
    BookingInProgressError,
    ConfirmationRequiredError,
    DomainValidationError,
    NotFoundError,
    OfferExpiredError,
    ProviderAuthError,
    ProviderUnavailableError,
    SlotTakenError,
)

logger = structlog.get_logger(__name__)

SLOTS_TTL_S = 60
CATALOG_TTL_S = 300
BOOKING_KEY_TTL_S = 24 * 3600  # результат записи помним сутки: повторное «да» не создаст вторую
CLIENT_BOOKINGS_TTL_S = 30 * 60


@dataclass(frozen=True, slots=True)
class BookingEntry:
    """Что сохраняется в нашей БД о записи."""

    conversation_id: str
    client_id: uuid.UUID | None
    slot: Slot
    status: BookingStatus
    idempotency_key: str
    external_id: str | None
    price: Decimal | None
    client: ClientContact | None = None  # имя и телефон — в карточку клиента (согласие уже дано)


@dataclass(frozen=True, slots=True)
class StoredBooking:
    """Запись из нашей БД для «моих записей»."""

    external_id: str | None  # None — CRM была недоступна, запись ждёт подтверждения администратора
    slot: Slot
    status: BookingStatus


class BookingLedger(Protocol):
    """Куда движок сохраняет записи: в проде — PostgreSQL (db/repositories/bookings.py)."""

    async def record(self, entry: BookingEntry) -> None: ...

    async def upcoming(self, client_id: uuid.UUID, now: datetime) -> list[StoredBooking]:
        """Будущие неотменённые записи клиента по времени."""
        ...

    async def cancel(self, external_id: str) -> None: ...

    async def move(self, external_id: str, slot: Slot) -> None: ...


class Outcome(StrEnum):
    CONFIRMED = "confirmed"
    PENDING_ADMIN = "pending_admin"  # CRM недоступна: администратор подтвердит вручную
    SLOT_TAKEN = "slot_taken"  # пока клиент думал, время заняли — предлагаем новое
    CANCELLED = "cancelled"
    RESCHEDULED = "rescheduled"


@dataclass(frozen=True, slots=True)
class BookingOutcome:
    status: Outcome
    offer: Offer
    external_id: str | None = None
    alternatives: tuple[Offer, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "offer": self.offer.to_json(),
            "external_id": self.external_id,
            "alternatives": [offer.to_json() for offer in self.alternatives],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "BookingOutcome":
        return cls(
            status=Outcome(data["status"]),
            offer=Offer.from_json(data["offer"]),
            external_id=data.get("external_id"),
            alternatives=tuple(Offer.from_json(item) for item in data.get("alternatives", [])),
        )


@dataclass(frozen=True, slots=True)
class ClientBooking:
    """Запись клиента в списке «мои записи» под номером."""

    number: int
    external_id: str | None
    slot: Slot
    service_name: str
    master_name: str
    pending: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "external_id": self.external_id,
            "slot": _slot_to_json(self.slot),
            "service_name": self.service_name,
            "master_name": self.master_name,
            "pending": self.pending,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ClientBooking":
        return cls(
            number=int(data["number"]),
            external_id=data.get("external_id"),
            slot=_slot_from_json(data["slot"]),
            service_name=data["service_name"],
            master_name=data["master_name"],
            pending=bool(data.get("pending")),
        )


@dataclass(frozen=True, slots=True)
class ChangeOutcome:
    """Результат отмены или переноса."""

    status: Outcome
    booking: ClientBooking
    new_offer: Offer | None = None
    alternatives: tuple[Offer, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "booking": self.booking.to_json(),
            "new_offer": self.new_offer.to_json() if self.new_offer else None,
            "alternatives": [offer.to_json() for offer in self.alternatives],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ChangeOutcome":
        return cls(
            status=Outcome(data["status"]),
            booking=ClientBooking.from_json(data["booking"]),
            new_offer=Offer.from_json(data["new_offer"]) if data.get("new_offer") else None,
            alternatives=tuple(Offer.from_json(item) for item in data.get("alternatives", [])),
        )


class ClientBookingStore:
    """Список «мои записи» под номерами — как варианты времени: модель передаёт номер, а не ID записи."""

    def __init__(self, redis: Redis, *, ttl_s: int = CLIENT_BOOKINGS_TTL_S) -> None:
        self.redis = redis
        self.ttl_s = ttl_s

    @staticmethod
    def _key(salon_id: str, conversation_id: str) -> str:
        return key("client_bookings", salon_id, conversation_id)

    async def save(self, salon_id: str, conversation_id: str, bookings: list[ClientBooking]) -> None:
        payload = json.dumps([booking.to_json() for booking in bookings], ensure_ascii=False)
        await self.redis.set(self._key(salon_id, conversation_id), payload, ex=self.ttl_s)

    async def get(self, salon_id: str, conversation_id: str, number: int) -> ClientBooking | None:
        raw = await self.redis.get(self._key(salon_id, conversation_id))
        for item in json.loads(raw) if raw else []:
            if item["number"] == number:
                return ClientBooking.from_json(item)
        return None


def _service_to_json(service: ServiceInfo) -> dict[str, Any]:
    return {
        "external_id": service.external_id,
        "name": service.name,
        "duration_min": service.duration_min,
        "price_min": None if service.price_min is None else str(service.price_min),
        "price_max": None if service.price_max is None else str(service.price_max),
        "category": service.category,
        "description": service.description,
    }


def _service_from_json(data: dict[str, Any]) -> ServiceInfo:
    return ServiceInfo(
        external_id=data["external_id"],
        name=data["name"],
        duration_min=int(data["duration_min"]),
        price_min=None if data["price_min"] is None else Decimal(data["price_min"]),
        price_max=None if data["price_max"] is None else Decimal(data["price_max"]),
        category=data.get("category"),
        description=data.get("description"),
    )


def _slot_to_json(slot: Slot) -> dict[str, Any]:
    return {
        "master_id": slot.master_id,
        "service_id": slot.service_id,
        "starts_at": slot.starts_at.isoformat(),
        "duration_min": slot.duration_min,
    }


def _slot_from_json(data: dict[str, Any]) -> Slot:
    return Slot(data["master_id"], data["service_id"], datetime.fromisoformat(data["starts_at"]), data["duration_min"])


class BookingService:
    def __init__(
        self,
        *,
        salon_id: str,
        timezone: str,
        provider: BookingProvider,
        redis: Redis,
        ledger: BookingLedger,
    ) -> None:
        self.salon_id = salon_id
        self.tz = ZoneInfo(timezone)
        self.provider = provider
        self.ledger = ledger
        self.cache = JsonCache(redis)
        self.offers = OfferStore(redis)
        self.client_bookings_store = ClientBookingStore(redis)
        self.idempotency = IdempotencyStore(redis, ttl_s=BOOKING_KEY_TTL_S)
        self.redis = redis

    # ------------------------------------------------------------- каталог и слоты через кэш

    async def services(self) -> list[ServiceInfo]:
        name = key("catalog", self.salon_id, "services")
        cached = await self.cache.get(name)
        if cached is not None:
            return [_service_from_json(item) for item in cached]
        services = await self.provider.list_services()
        await self.cache.set(name, [_service_to_json(service) for service in services], CATALOG_TTL_S)
        return services

    async def service(self, service_id: str) -> ServiceInfo:
        for service in await self.services():
            if service.external_id == service_id:
                return service
        raise NotFoundError("Услуга не найдена")

    async def master_names(self, service_id: str) -> dict[str, str]:
        name = key("catalog", self.salon_id, "masters", service_id)
        cached = await self.cache.get(name)
        if cached is None:
            cached = {master.external_id: master.name for master in await self.provider.list_masters(service_id)}
            await self.cache.set(name, cached, CATALOG_TTL_S)
        return dict(cached)

    async def _slots_version(self) -> int:
        raw = await self.redis.get(key("slots_version", self.salon_id))
        return int(raw) if raw else 0

    async def invalidate_slots(self) -> None:
        """Сбрасывает кэш слотов салона: старые ключи с прежней версией больше не читаются."""
        await self.redis.incr(key("slots_version", self.salon_id))

    async def slots(self, service_id: str, date_from: date, date_to: date, master_id: str | None) -> list[Slot]:
        version = await self._slots_version()
        name = key("slots", self.salon_id, version, service_id, master_id or "*", date_from, date_to)
        cached = await self.cache.get(name)
        if cached is not None:
            return [_slot_from_json(item) for item in cached]
        found = await self.provider.find_slots(service_id, date_from, date_to, master_id)
        await self.cache.set(name, [_slot_to_json(slot) for slot in found], SLOTS_TTL_S)
        return found

    # ------------------------------------------------------------- предложение

    async def propose(
        self,
        conversation_id: str,
        service_id: str,
        *,
        date_from: date,
        date_to: date,
        master_id: str | None = None,
        preferred_master_id: str | None = None,
        part_of_day: PartOfDay | None = None,
    ) -> list[Offer]:
        service = await self.service(service_id)
        slots = await self.slots(service_id, date_from, date_to, master_id)
        if part_of_day is not None:
            matching = [slot for slot in slots if PartOfDay.of(slot.starts_at.astimezone(self.tz)) == part_of_day]
            slots = matching or slots  # в это время дня окон нет — показываем ближайшие
        masters = await self.master_names(service_id) if slots else {}
        local = [slot.starts_at.astimezone(self.tz) for slot in slots]
        indices = pick_offers(slots, local, preferred_master_id=preferred_master_id)
        offers = [
            Offer(
                number=number,
                slot=slots[index],
                master_name=masters.get(slots[index].master_id, "мастер"),
                service_name=service.name,
                price_text=service.price_text(),
            )
            for number, index in enumerate(indices, start=1)
        ]
        await self.offers.save(self.salon_id, conversation_id, offers)
        return offers

    # ------------------------------------------------------------- подтверждение

    def booking_key(self, conversation_id: str, slot: Slot) -> str:
        """64 символа: столько вмещают api_id в YCLIENTS и колонка idempotency_key."""
        raw = f"{self.salon_id}|{conversation_id}|{slot.key}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def confirm(
        self, conversation_id: str, number: int, client: ClientContact, *, client_id: uuid.UUID | None = None
    ) -> BookingOutcome:
        offer = await self.offers.get(self.salon_id, conversation_id, number)
        if offer is None:
            raise OfferExpiredError
        idempotency_key = self.booking_key(conversation_id, offer.slot)
        claim_name = key("idem", "booking", idempotency_key)
        claim = await self.idempotency.begin(claim_name)
        if claim.state == "done" and claim.result is not None:
            return BookingOutcome.from_json(claim.result)  # повторное «да»: та же запись, вторая не создаётся
        if claim.state == "in_progress":
            raise BookingInProgressError

        try:
            outcome = await self._book(conversation_id, offer, client, client_id, idempotency_key)
        except BaseException:
            await self.idempotency.release(claim_name)  # иначе клиент не сможет повторить попытку
            raise
        if outcome.status is Outcome.SLOT_TAKEN:
            await self.idempotency.release(claim_name)
        return outcome

    async def _book(
        self,
        conversation_id: str,
        offer: Offer,
        client: ClientContact,
        client_id: uuid.UUID | None,
        idempotency_key: str,
    ) -> BookingOutcome:
        service = await self.service(offer.slot.service_id)
        price = service.price_min if service.price_min == service.price_max else None
        claim_name = key("idem", "booking", idempotency_key)

        def entry(status: BookingStatus, external_id: str | None) -> BookingEntry:
            return BookingEntry(
                conversation_id, client_id, offer.slot, status, idempotency_key, external_id, price, client
            )

        try:
            await self.provider.check_slot(offer.slot)  # без кэша: окно могли занять минуту назад
            result = await self.provider.create_booking(BookingRequest(offer.slot, client, idempotency_key))
        except SlotTakenError:
            await self.invalidate_slots()
            alternatives = await self._alternatives(conversation_id, offer)
            return BookingOutcome(Outcome.SLOT_TAKEN, offer, alternatives=alternatives)
        except (ProviderUnavailableError, ProviderAuthError, ConfirmationRequiredError) as exc:
            logger.warning("booking_pending_admin", reason=type(exc).__name__, conversation_id=conversation_id)
            await self.ledger.record(entry(BookingStatus.PENDING, None))
            outcome = BookingOutcome(Outcome.PENDING_ADMIN, offer)
            await self.idempotency.complete(claim_name, outcome.to_json())
            return outcome

        # Запись уже есть в CRM. Дальше ни одна ошибка не должна выйти наружу: confirm снял бы
        # ключ идемпотентности, и повтор создал бы вторую запись. Сначала запоминаем результат,
        # потом пишем в нашу БД — её сбой разбирается по логам, а клиент получает подтверждение.
        outcome = BookingOutcome(Outcome.CONFIRMED, offer, external_id=result.external_id)
        try:
            await self.idempotency.complete(claim_name, outcome.to_json())
            await self.invalidate_slots()
        except Exception:
            logger.exception("booking_cache_update_failed", external_id=result.external_id)
        try:
            await self.ledger.record(entry(BookingStatus.CONFIRMED, result.external_id))
        except Exception:
            logger.exception("booking_ledger_failed", external_id=result.external_id)
        logger.info("booking_created", external_id=result.external_id, conversation_id=conversation_id)
        return outcome

    async def _alternatives(self, conversation_id: str, offer: Offer) -> tuple[Offer, ...]:
        day = offer.slot.starts_at.astimezone(self.tz).date()
        try:
            offers = await self.propose(
                conversation_id, offer.slot.service_id, date_from=day, date_to=day + timedelta(days=7)
            )
        except ProviderUnavailableError:
            return ()
        return tuple(offers)

    # ------------------------------------------------------------- мои записи, отмена, перенос

    async def _names(self, slot: Slot) -> tuple[str, str]:
        try:
            service_name = (await self.service(slot.service_id)).name
            master_name = (await self.master_names(slot.service_id)).get(slot.master_id, "мастер")
        except (NotFoundError, ProviderUnavailableError):
            return "услуга", "мастер"
        return service_name, master_name

    async def client_bookings(
        self, conversation_id: str, client_id: uuid.UUID | None, now: datetime
    ) -> list[ClientBooking]:
        """Будущие записи клиента, сверенные с CRM, под номерами 1…N."""
        stored = await self.ledger.upcoming(client_id, now) if client_id is not None else []
        found: list[ClientBooking] = []
        for item in stored:
            slot = item.slot
            if item.external_id is not None:
                try:
                    in_crm = await self.provider.get_booking(item.external_id)
                except (ProviderUnavailableError, ProviderAuthError):
                    in_crm = None  # CRM недоступна — показываем то, что знаем
                    verified = False
                else:
                    verified = True
                if verified and (in_crm is None or not in_crm.active):
                    await self.ledger.cancel(item.external_id)  # отменили в YCLIENTS
                    continue
                if in_crm is not None and in_crm.slot.key != slot.key:
                    await self.ledger.move(item.external_id, in_crm.slot)  # перенесли в YCLIENTS
                    slot = in_crm.slot
                if slot.starts_at <= now:
                    continue
            service_name, master_name = await self._names(slot)
            found.append(
                ClientBooking(
                    number=len(found) + 1,
                    external_id=item.external_id,
                    slot=slot,
                    service_name=service_name,
                    master_name=master_name,
                    pending=item.status is BookingStatus.PENDING,
                )
            )
        await self.client_bookings_store.save(self.salon_id, conversation_id, found)
        return found

    async def _client_booking(self, conversation_id: str, number: int, now: datetime) -> ClientBooking:
        booking = await self.client_bookings_store.get(self.salon_id, conversation_id, number)
        if booking is None:
            raise OfferExpiredError("Список записей устарел — запросите его заново")
        if booking.slot.starts_at <= now:
            raise DomainValidationError("Эта запись уже прошла")
        return booking

    async def _change(
        self,
        claim_name: str,
        booking: ClientBooking,
        action: Callable[[], Awaitable[None]],
        done: ChangeOutcome,
        apply: Callable[[], Awaitable[None]],
    ) -> ChangeOutcome:
        """Общий порядок отмены и переноса: ключ → CRM → (после успеха ничего не бросать) → БД."""
        claim = await self.idempotency.begin(claim_name)
        if claim.state == "done" and claim.result is not None:
            return ChangeOutcome.from_json(claim.result)
        if claim.state == "in_progress":
            raise BookingInProgressError
        try:
            await action()
        except (ProviderUnavailableError, ProviderAuthError) as exc:
            await self.idempotency.release(claim_name)
            logger.warning("booking_change_pending_admin", reason=type(exc).__name__)
            return ChangeOutcome(Outcome.PENDING_ADMIN, booking, done.new_offer)
        except BaseException:
            await self.idempotency.release(claim_name)
            raise
        try:
            await self.idempotency.complete(claim_name, done.to_json())
            await self.invalidate_slots()
        except Exception:
            logger.exception("booking_cache_update_failed", external_id=booking.external_id)
        try:
            await apply()
        except Exception:
            logger.exception("booking_ledger_failed", external_id=booking.external_id)
        return done

    async def cancel(self, conversation_id: str, number: int, now: datetime) -> ChangeOutcome:
        booking = await self._client_booking(conversation_id, number, now)
        external_id = booking.external_id
        if external_id is None:
            return ChangeOutcome(Outcome.PENDING_ADMIN, booking)

        async def action() -> None:
            await self.provider.cancel_booking(external_id)

        async def apply() -> None:
            await self.ledger.cancel(external_id)

        outcome = await self._change(
            key("idem", "cancel", self.salon_id, external_id),
            booking,
            action,
            ChangeOutcome(Outcome.CANCELLED, booking),
            apply,
        )
        logger.info("booking_change", status=outcome.status.value, external_id=external_id)
        return outcome

    async def reschedule(self, conversation_id: str, number: int, option: int, now: datetime) -> ChangeOutcome:
        booking = await self._client_booking(conversation_id, number, now)
        offer = await self.offers.get(self.salon_id, conversation_id, option)
        if offer is None:
            raise OfferExpiredError
        if offer.slot.service_id != booking.slot.service_id:
            raise DomainValidationError("Перенести можно на ту же услугу; другую услугу — новой записью")
        external_id = booking.external_id
        if external_id is None:
            return ChangeOutcome(Outcome.PENDING_ADMIN, booking, offer)
        move_key = hashlib.sha256(f"{external_id}|{offer.slot.key}".encode()).hexdigest()

        async def action() -> None:
            await self.provider.reschedule_booking(external_id, offer.slot)

        async def apply() -> None:
            await self.ledger.move(external_id, offer.slot)

        try:
            outcome = await self._change(
                key("idem", "move", self.salon_id, move_key),
                booking,
                action,
                ChangeOutcome(Outcome.RESCHEDULED, booking, offer),
                apply,
            )
        except SlotTakenError:
            await self.invalidate_slots()
            alternatives = await self._alternatives(conversation_id, offer)
            return ChangeOutcome(Outcome.SLOT_TAKEN, booking, offer, alternatives)
        logger.info("booking_change", status=outcome.status.value, external_id=external_id)
        return outcome
