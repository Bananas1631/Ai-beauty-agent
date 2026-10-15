"""6 инструментов агента: через них модель получает факты и действует.

У каждого — Pydantic-модель аргументов (лишние поля запрещены, строки ограничены по длине),
обработчик и описание для модели. Цены, время и мастеров модель видит только здесь.
"""

import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Annotated, Any

import phonenumbers
from pydantic import BaseModel, ConfigDict, Field, field_validator
from rapidfuzz import fuzz

from beauty_agent.adapters.crm.base import BookingProvider
from beauty_agent.agent.registry import Tool, ToolContext, ToolRegistry
from beauty_agent.catalog.importer import salon_card_text
from beauty_agent.domain.booking_engine.offers import Offer, PartOfDay
from beauty_agent.domain.booking_engine.service import BookingService, Outcome
from beauty_agent.domain.entities import ClientContact, MasterInfo, ServiceInfo
from beauty_agent.domain.errors import DomainValidationError

MAX_SEARCH_DAYS = 14
DEFAULT_SEARCH_DAYS = 7
SERVICE_MATCH_CUTOFF = 60

ExternalId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")]

_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip


def format_local(moment: datetime) -> str:
    """«вт, 20 октября, 10:00» — время уже в часовом поясе салона."""
    return f"{_WEEKDAYS[moment.weekday()]}, {moment.day} {_MONTHS[moment.month - 1]}, {moment:%H:%M}"


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    return re.sub(r"[\W_]+", " ", text).strip()


# Как клиенты называют услуги → как они называются в каталогах салонов
SYNONYMS = {
    "покраска": "окрашивание",
    "покрасить": "окрашивание",
    "шеллак": "гель лак",
    "гельлак": "гель лак",
    "бровки": "брови",
    "реснички": "ресницы",
    "ноготочки": "маникюр",
    "подстричься": "стрижка",
    "подстричь": "стрижка",
    "ламинация": "ламинирование",
}
# «2д» и «2D» — одно и то же: латиницу, похожую на кириллицу, приводим к одному виду
_LOOKALIKES = str.maketrans({"d": "д"})


def _words(text: str) -> list[str]:
    return normalize_text(text).translate(_LOOKALIKES).split()


def _stem(word: str) -> str:
    """Грубая основа слова: без последних двух букв, но не короче трёх («бровей» → «бров»)."""
    return word[: max(3, len(word) - 2)] if len(word) > 3 else word


def _word_score(token: str, words: list[str]) -> float:
    best = 0.0
    for word in words:
        if word.startswith(_stem(token)) or (len(word) > 3 and token.startswith(_stem(word))):
            return 100.0
        best = max(best, fuzz.ratio(token, word))
    return best


def _service_score(query_words: list[str], service: ServiceInfo) -> float:
    """Среднее по словам запроса: совпадение в названии весит больше, чем в категории."""
    name_words = _words(service.name)
    category_words = _words(service.category or "")
    total = 0.0
    for token in query_words:
        total += max(_word_score(token, name_words), 0.9 * _word_score(token, category_words))
    return total / len(query_words)


def search_services(services: list[ServiceInfo], query: str, *, limit: int = 3) -> list[ServiceInfo]:
    """Нечёткий поиск: «маникюр гель лак» найдёт «Маникюр с покрытием гель-лак», «брови» — «…бровей»."""
    query_words = [word for token in _words(query) for word in SYNONYMS.get(token, token).split()]
    if not query_words:
        return []
    scored = [(_service_score(query_words, service), index) for index, service in enumerate(services)]
    ranked = sorted((item for item in scored if item[0] >= SERVICE_MATCH_CUTOFF), key=lambda item: (-item[0], item[1]))
    return [services[index] for _, index in ranked[:limit]]


def normalize_phone(raw: str) -> str:
    """Телефон в E.164 (+79161234567). Номер без кода страны считается российским."""
    try:
        number = phonenumbers.parse(raw, "RU")
    except phonenumbers.NumberParseException as exc:
        raise ValueError("не похоже на номер телефона") from exc
    if not phonenumbers.is_valid_number(number):
        raise ValueError("такого номера не существует — уточни у клиента")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FindServiceArgs(StrictArgs):
    query: str = Field(
        min_length=2, max_length=100, description="Как клиент назвал услугу, например «маникюр с покрытием»"
    )


class MastersArgs(StrictArgs):
    service_id: ExternalId = Field(description="service_id из результата find_service")


class FreeTimeArgs(StrictArgs):
    service_id: ExternalId = Field(description="service_id из результата find_service")
    date_from: date | None = Field(None, description="С какой даты искать, ГГГГ-ММ-ДД; по умолчанию сегодня")
    date_to: date | None = Field(None, description="По какую дату искать включительно; по умолчанию неделя")
    master_id: ExternalId | None = Field(None, description="master_id, если клиент хочет конкретного мастера")
    part_of_day: PartOfDay | None = Field(None, description="morning — до 12, day — 12–17, evening — после 17")


class BookArgs(StrictArgs):
    option: int = Field(ge=1, le=3, description="Номер варианта из find_free_time, который выбрал клиент")
    client_name: str = Field(min_length=1, max_length=60, description="Имя клиента")
    phone: str = Field(min_length=5, max_length=32, description="Телефон клиента, как он его написал")

    @field_validator("client_name")
    @classmethod
    def _name(cls, value: str) -> str:
        if any(unicodedata.category(char).startswith("C") for char in value) or not any(c.isalpha() for c in value):
            raise ValueError("имя должно состоять из букв")
        return value

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return normalize_phone(value)


class SalonInfoArgs(StrictArgs):
    question: str = Field(min_length=2, max_length=200, description="Вопрос клиента о салоне: адрес, часы, оплата…")


class HandoffArgs(StrictArgs):
    reason: str = Field(min_length=2, max_length=300, description="Коротко, почему нужен администратор")


# ---------------------------------------------------------------- обработчики


def _service_json(service: ServiceInfo) -> dict[str, Any]:
    return {
        "service_id": service.external_id,
        "name": service.name,
        "price": service.price_text(),
        "duration_min": service.duration_min,
        "category": service.category,
    }


def _offer_json(offer: Offer, tz: Any) -> dict[str, Any]:
    return {
        "option": offer.number,
        "when": format_local(offer.slot.starts_at.astimezone(tz)),
        "master": offer.master_name,
        "service": offer.service_name,
        "price": offer.price_text,
        "duration_min": offer.slot.duration_min,
    }


async def find_service(args: FindServiceArgs, ctx: ToolContext) -> dict[str, Any]:
    services = await ctx.catalog.services()
    found = search_services(services, args.query)
    if not found:
        categories = sorted({service.category for service in services if service.category})
        return {
            "found": [],
            "hint": "Такой услуги нет в каталоге. Не придумывай её: уточни у клиента, что он имеет в виду.",
            "categories": categories,
        }
    return {"found": [_service_json(service) for service in found]}


async def masters_for_service(args: MastersArgs, ctx: ToolContext) -> dict[str, Any]:
    masters = await ctx.catalog.masters_for(args.service_id)
    return {
        "masters": [
            {"master_id": master.external_id, "name": master.name, "specialization": master.specialization}
            for master in masters
        ]
    }


async def find_free_time(args: FreeTimeArgs, ctx: ToolContext) -> dict[str, Any]:
    today = ctx.now.date()
    date_from = max(args.date_from or today, today)
    date_to = args.date_to or date_from + timedelta(days=DEFAULT_SEARCH_DAYS - 1)
    if date_to < date_from:
        raise DomainValidationError("date_to раньше date_from")
    date_to = min(date_to, date_from + timedelta(days=MAX_SEARCH_DAYS - 1))
    offers = await ctx.booking.propose(
        ctx.conversation_id,
        args.service_id,
        date_from=date_from,
        date_to=date_to,
        master_id=args.master_id,
        preferred_master_id=ctx.preferred_master_id,
        part_of_day=args.part_of_day,
    )
    if not offers:
        return {"options": [], "hint": "В эти даты свободного времени нет. Предложи другие даты или другого мастера."}
    return {
        "options": [_offer_json(offer, ctx.now.tzinfo) for offer in offers],
        "hint": "Предложи варианты клиенту. Для записи нужны номер варианта, имя и телефон.",
    }


async def book(args: BookArgs, ctx: ToolContext) -> dict[str, Any]:
    outcome = await ctx.booking.confirm(
        ctx.conversation_id, args.option, ClientContact(args.client_name, args.phone), client_id=ctx.client_id
    )
    details = _offer_json(outcome.offer, ctx.now.tzinfo)
    if outcome.status is Outcome.CONFIRMED:
        return {"status": "confirmed", **details, "message": "Запись создана."}
    if outcome.status is Outcome.PENDING_ADMIN:
        return {
            "status": "pending_admin",
            **details,
            "message": "Система записи салона недоступна. Заявка сохранена: администратор подтвердит запись.",
        }
    return {
        "status": "slot_taken",
        "message": "Это время только что заняли. Вот новые варианты.",
        "options": [_offer_json(offer, ctx.now.tzinfo) for offer in outcome.alternatives],
    }


async def salon_info(args: SalonInfoArgs, ctx: ToolContext) -> dict[str, Any]:
    fragments = await ctx.salon_info.search(args.question)
    if not fragments:
        return {
            "found": False,
            "hint": "Сведений нет. Скажи, что уточнишь у администратора, и вызови handoff_to_admin.",
        }
    return {"found": True, "fragments": fragments[:3]}


async def handoff_to_admin(args: HandoffArgs, ctx: ToolContext) -> dict[str, Any]:
    ctx.handoff_reason = args.reason
    return {"status": "handed_off", "message": "Администратор получил диалог и ответит в рабочее время."}


TOOLS: tuple[Tool, ...] = (
    Tool(
        "find_service",
        "Найти услугу салона по словам клиента. Возвращает service_id, цену и длительность.",
        FindServiceArgs,
        find_service,
    ),
    Tool("masters_for_service", "Мастера, которые делают услугу.", MastersArgs, masters_for_service),
    Tool(
        "find_free_time",
        "Подобрать 2–3 варианта свободного времени на услугу. Варианты пронумерованы.",
        FreeTimeArgs,
        find_free_time,
    ),
    Tool(
        "book",
        "Записать клиента на выбранный вариант по его номеру. Вызывать только после согласия клиента.",
        BookArgs,
        book,
    ),
    Tool(
        "salon_info",
        "Сведения о салоне: адрес, часы работы, оплата, правила, подготовка к процедуре.",
        SalonInfoArgs,
        salon_info,
    ),
    Tool("handoff_to_admin", "Передать диалог живому администратору.", HandoffArgs, handoff_to_admin),
)


def default_registry() -> ToolRegistry:
    return ToolRegistry(TOOLS)


# ---------------------------------------------------------------- источники данных без БД


class ProviderCatalog:
    """Каталог прямо из CRM через кэш движка записи: для фейкового CRM, чата в терминале и evals."""

    def __init__(self, booking: BookingService) -> None:
        self.booking = booking

    async def services(self) -> list[ServiceInfo]:
        return await self.booking.services()

    async def masters_for(self, service_id: str) -> list[MasterInfo]:
        return await self.booking.provider.list_masters(service_id)


class CardSalonInfo:
    """Сведения о салоне из карточки филиала в CRM — пока нет RAG по сайту (с 3 ноября)."""

    def __init__(self, provider: BookingProvider) -> None:
        self.provider = provider
        self._card: str | None = None

    async def search(self, question: str) -> list[dict[str, str]]:
        if self._card is None:
            self._card = salon_card_text(await self.provider.get_salon_info())
        return [{"title": "Карточка салона", "text": self._card}]
