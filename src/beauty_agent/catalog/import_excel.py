"""Импорт прайса салона без YCLIENTS из Excel (.xlsx) или CSV.

Салон присылает таблицу «как есть»: колонки называются по-разному, CSV из Excel для Windows — в
cp1251 и с «;», цены — «1 500 ₽», «от 2000», «1500,50», длительность — «90», «1:30», «1 ч 30 мин».
Модуль приводит это к услугам каталога или объясняет, что не так, — с номерами строк, как их видит
салон в Excel (заголовок — строка 1).

- Обязательные колонки: услуга, цена, длительность; необязательная — категория. Нет колонки —
  одна ошибка про файл, строки не разбираются.
- Цена — Decimal, без float: «1 500,50» → 1500.50. Диапазон «2000–2500» → от и до, «от 2000» — от.
- Ошибки строк копятся все сразу: салон исправляет файл один раз, а не по одной ошибке за загрузку.
- Импорт атомарный: с ошибками каталог не меняется. Услуги, которых нет в файле, выключаются
  (на них ссылаются прошлые записи) — как при синхронизации с CRM.
"""

import csv
import hashlib
import io
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.repositories.catalog import ServicesRepository
from beauty_agent.domain.entities import ServiceInfo

MAX_ROWS = 2000
MAX_PRICE = Decimal(1_000_000)
COLUMNS: dict[str, tuple[str, ...]] = {
    "name": ("услуга", "название", "наименование", "название услуги", "наименование услуги", "услуги"),
    "price": ("цена", "стоимость", "цена руб", "стоимость руб", "прайс"),
    "duration": ("длительность", "длительность мин", "время", "время мин", "продолжительность", "минут"),
    "category": ("категория", "раздел", "группа", "направление"),
}
REQUIRED = ("name", "price", "duration")
TITLES = {"name": "Услуга", "price": "Цена", "duration": "Длительность", "category": "Категория"}

_SPACES = re.compile(r"[\s  ]+")
_HEADER_NOISE = re.compile(r"[^a-zа-яё0-9 ]")
_RANGE = re.compile(r"^(\d+(?:[.,]\d{1,2})?)\s*[-–—]\s*(\d+(?:[.,]\d{1,2})?)$")
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
_HOURS_MINUTES = re.compile(r"^(?:(\d+(?:[.,]\d+)?)\s*ч[а-я]*)?\s*(?:(\d+)\s*м[а-я]*)?$")


@dataclass(frozen=True, slots=True)
class RowError:
    row: int  # номер строки в Excel: заголовок — 1
    message: str

    def __str__(self) -> str:
        return f"строка {self.row}: {self.message}" if self.row else self.message


@dataclass(frozen=True, slots=True)
class PriceItem:
    row: int
    name: str
    price_min: Decimal
    price_max: Decimal
    duration_min: int
    category: str | None = None

    @property
    def external_id(self) -> str:
        """Стабильный ID услуги из файла: повторная загрузка обновит услугу, а не создаст дубль."""
        normalized = " ".join(self.name.lower().split())
        return "xls:" + hashlib.sha256(normalized.encode()).hexdigest()[:20]


@dataclass
class PriceList:
    items: list[PriceItem] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.items)


def normalize_header(raw: object) -> str:
    text = _SPACES.sub(" ", str(raw).lower().replace("ё", "е"))
    return " ".join(_HEADER_NOISE.sub(" ", text).split())


def _decode_csv(data: bytes) -> str:
    """UTF-8 (с BOM или без), иначе cp1251 — так сохраняет CSV Excel для Windows."""
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("не удалось определить кодировку: сохраните CSV в UTF-8 или Windows-1251")


def read_table(data: bytes, filename: str) -> pd.DataFrame:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        return pd.read_excel(io.BytesIO(data), dtype=str, engine="openpyxl", nrows=MAX_ROWS + 1)
    if name.endswith(".csv"):
        text = _decode_csv(data)
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=";,\t").delimiter
        except csv.Error:
            delimiter = ";"
        return pd.read_csv(io.StringIO(text), dtype=str, sep=delimiter, nrows=MAX_ROWS + 1, keep_default_na=False)
    raise ValueError("нужен файл .xlsx или .csv")


def parse_money(raw: str) -> tuple[Decimal, Decimal]:
    text = _SPACES.sub("", raw.lower()).replace("₽", "").replace("руб.", "").replace("руб", "").replace("р.", "")
    text = text.removeprefix("от")
    if match := _RANGE.fullmatch(text):
        low, high = (Decimal(part.replace(",", ".")) for part in match.groups())
    else:
        try:
            low = high = Decimal(text.replace(",", "."))
        except InvalidOperation as exc:
            raise ValueError(f"цена «{raw}» — не число") from exc
    if low <= 0 or high < low:
        raise ValueError(f"цена «{raw}» должна быть больше нуля, «от» не больше «до»")
    cents = Decimal("0.01")
    if high > MAX_PRICE or low != low.quantize(cents) or high != high.quantize(cents):
        raise ValueError(f"цена «{raw}» вне допустимого диапазона или с долями копейки")
    return low.quantize(cents), high.quantize(cents)


def parse_duration(raw: str) -> int:
    text = _SPACES.sub(" ", raw.lower()).strip().removesuffix(".")
    minutes: int | None = None
    if text.isdigit():
        minutes = int(text)
    elif match := _CLOCK.fullmatch(text):
        minutes = int(match[1]) * 60 + int(match[2])
    elif (match := _HOURS_MINUTES.fullmatch(text)) and (match[1] or match[2]):
        hours = Decimal(match[1].replace(",", ".")) if match[1] else Decimal(0)
        minutes = int(hours * 60) + int(match[2] or 0)
    if minutes is None:
        raise ValueError(f"длительность «{raw}» — укажите минуты: 90, 1:30 или «1 ч 30 мин»")
    if not 5 <= minutes <= 720:
        raise ValueError(f"длительность «{raw}» — от 5 минут до 12 часов")
    return minutes


def parse_price_list(data: bytes, filename: str) -> PriceList:
    result = PriceList()
    try:
        table = read_table(data, filename)
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:  # битый xlsx, формат, кодировка
        reason = "файл повреждён или это не .xlsx" if isinstance(exc, (zipfile.BadZipFile, KeyError)) else str(exc)
        result.errors.append(RowError(0, f"Файл не прочитан: {reason}"))
        return result
    columns: dict[str, str] = {}
    for column in table.columns:
        header = normalize_header(column)
        for key, names in COLUMNS.items():
            if key not in columns and (header in names or header.split(" ")[0] in names):
                columns[key] = str(column)
    missing = [TITLES[key] for key in REQUIRED if key not in columns]
    if missing:
        result.errors.append(RowError(1, f"нет колонок: {', '.join(missing)}"))
        return result
    if len(table) > MAX_ROWS:
        result.errors.append(RowError(0, f"больше {MAX_ROWS} строк — разделите файл"))
        return result

    seen: dict[str, int] = {}
    for index, record in enumerate(table.to_dict("records")):
        row = index + 2  # строка 1 — заголовок
        values = {key: str(record.get(column) or "").strip() for key, column in columns.items()}
        values = {key: "" if value.lower() == "nan" else value for key, value in values.items()}
        if not any(values.values()):
            continue  # пустая строка-разделитель
        name = " ".join(values["name"].split())
        problems: list[str] = []
        if not name:
            problems.append("пустое название услуги")
        elif len(name) > 200:
            problems.append("название длиннее 200 символов")
        elif (first := seen.get(name.lower())) is not None:
            problems.append(f"услуга «{name}» уже есть в строке {first}")
        price: tuple[Decimal, Decimal] | None = None
        duration: int | None = None
        try:
            price = parse_money(values["price"]) if values["price"] else None
            if price is None:
                problems.append("не указана цена")
        except ValueError as exc:
            problems.append(str(exc))
        try:
            duration = parse_duration(values["duration"]) if values["duration"] else None
            if duration is None:
                problems.append("не указана длительность")
        except ValueError as exc:
            problems.append(str(exc))
        if problems or price is None or duration is None:
            result.errors.extend(RowError(row, problem) for problem in problems)
            continue
        seen[name.lower()] = row
        category = " ".join(values.get("category", "").split()) or None
        result.items.append(PriceItem(row, name, price[0], price[1], duration, category[:200] if category else None))
    if not result.items and not result.errors:
        result.errors.append(RowError(0, "в файле нет ни одной услуги"))
    return result


@dataclass(frozen=True, slots=True)
class PriceImportReport:
    created: int
    updated: int
    deactivated: int


async def import_price_list(session: AsyncSession, salon_id: uuid.UUID, price_list: PriceList) -> PriceImportReport:
    """Записать услуги из разобранного файла; с ошибками — ValueError, каталог не трогается."""
    if not price_list.ok:
        raise ValueError("прайс с ошибками не импортируется")
    services = ServicesRepository(session, salon_id)
    result = await services.upsert(
        ServiceInfo(
            external_id=item.external_id,
            name=item.name,
            duration_min=item.duration_min,
            price_min=item.price_min,
            price_max=item.price_max,
            category=item.category,
        )
        for item in price_list.items
    )
    deactivated = await services.deactivate_missing(result.ids.keys())
    return PriceImportReport(result.created, result.updated, deactivated)
