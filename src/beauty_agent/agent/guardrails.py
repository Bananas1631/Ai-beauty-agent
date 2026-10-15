"""Защитные проверки агента.

На входе — до вызова LLM:
- слишком длинное сообщение — просьба написать короче (и защита бюджета);
- признаки prompt injection — вежливый отказ без вызова модели;
- темы здоровья (противопоказания, беременность, аллергия…) — сразу администратору.

На выходе:
- символы чужих алфавитов (иероглифы у Qwen) — одна перегенерация;
- цены и время в ответе сверяются с данными инструментов этого диалога:
  неподтверждённый ответ заменяется безопасным. Выдуманная цена не дойдёт до клиента.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

MAX_INPUT_CHARS = 2000

TOO_LONG_ANSWER = "Сообщение получилось очень длинным — напишите, пожалуйста, коротко, чем я могу помочь."
INJECTION_ANSWER = "Я администратор салона и помогаю только с записью и вопросами об услугах. Чем могу помочь?"
HEALTH_ANSWER = (
    "Это вопрос для специалиста — передаю его администратору, он проконсультируется с мастером "
    "и ответит вам. Записаться можно уже сейчас, если хотите."
)
SAFE_ANSWER = (
    "Не хочу ввести вас в заблуждение — уточню точные данные и вернусь с ответом. "
    "Подскажите, пожалуйста, какая услуга вас интересует?"
)
ADMIN_ANSWER = (
    "Понимаю вас. Такие вопросы решает администратор салона — передаю ему наш диалог, "
    "он ответит в ближайшее рабочее время."
)


class InputVerdict(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    INJECTION = "injection"
    HEALTH = "health"
    ADMIN = "admin"  # жалоба, возврат денег, скидка — решает только человек


# Невидимые символы, которыми прячут инструкции от глаз: zero-width, bidi-переключатели
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")

# Шаблоны узкие: «напишите правила отмены записи» или «вы теперь до 21?» — обычные вопросы клиентов
_INJECTION = re.compile(
    r"(игнорир\w*|забудь|забудьте|не\s+следуй\w*)\s+(\w+\s+){0,3}(инструкц|правил|указани|промпт)"
    r"|(ignore|disregard|forget)\s+(\w+\s+){0,3}(instruction|rule|prompt)"
    r"|(систем\w*|system)\s*(промпт|prompt)"
    r"|(покажи|выведи|раскрой|повтори|перескажи)\s+(\w+\s+){0,2}(инструкц|промпт)"
    r"|(ты\s+теперь|теперь\s+ты)\s+(\w+\s+){0,2}(не\s+)?(администратор|ассистент|бот|ии|модель|персонаж|хакер)"
    r"|act\s+as\b|you\s+are\s+now\b|developer\s+mode|режим\s+разработчика|jailbreak"
    r"|<\|im_(start|end)\|>|\[/?inst\]|^\s*#{2,}\s*(system|assistant)",
    re.IGNORECASE | re.MULTILINE,
)

# \b перед основами: «ипотека» не должна срабатывать как «отёк»
_HEALTH = re.compile(
    r"\b(беремен|кормлю\s+грудью|грудн\w+\s+вскармлив|лактац|аллерг|противопоказ|диабет|эпилеп|онколог"
    r"|химиотерап|псориаз|экзем|дерматит|герпес|грибок|воспал|гно[йи]|ожог|отек|кровотеч|осложнен|сыпь|зуд"
    r"|антибиотик|роаккутан|изотретиноин|гормональн\w+\s+препарат|кардиостимулятор|варикоз|тромбоз)",
    re.IGNORECASE,
)

# Жалобы, деньги и скидки модель не решает: обещание скидки или спор о возврате — уже ущерб салону.
# Код передаёт такие сообщения администратору сам, при любой модели
_ADMIN_TOPIC = re.compile(
    r"\b(жалоб|жалова|пожалова|претензи|роспотребнадзор|прокуратур|испорти|изуродова"
    r"|(верн\w*|возврат\w*)\s+(\w+\s+){0,2}(деньг|предоплат|оплат)|компенсац"
    r"|скидк|промокод|скинь\w*\s+(до|цену)|бесплатно\s+за\s+рекламу)",
    re.IGNORECASE,
)

_FOREIGN_SCRIPT = re.compile(
    "[\u0590-\u08ff"  # иврит, арабское письмо
    "\u0e00-\u0e7f"  # тайское
    "\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]"  # CJK, хангыль
)


def normalize(text: str) -> str:
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text)).lower().replace("ё", "е")


def check_input(text: str) -> InputVerdict:
    if not text.strip():
        return InputVerdict.EMPTY
    if len(text) > MAX_INPUT_CHARS:
        return InputVerdict.TOO_LONG
    normalized = normalize(text)
    if _INJECTION.search(normalized):
        return InputVerdict.INJECTION
    if _HEALTH.search(normalized):
        return InputVerdict.HEALTH
    if _ADMIN_TOPIC.search(normalized):
        return InputVerdict.ADMIN
    return InputVerdict.OK


def has_foreign_script(text: str) -> bool:
    return bool(_FOREIGN_SCRIPT.search(text))


# ---------------------------------------------------------------- сверка фактов

_SPACE_IN_NUMBER = re.compile(r"(?<=\d)[\s\u00a0\u202f](?=\d{3}(?!\d))")
_PRICE = re.compile(r"(\d[\d\s\u00a0\u202f]*)(?:[.,]\d{1,2})?\s*(?:₽|руб|р\.|rub)", re.IGNORECASE)
_TIME = re.compile(r"(?<![\d:])([01]?\d|2[0-3])[:.]([0-5]\d)(?![\d:])")
_NUMBER = re.compile(r"\d+")


def _join_thousands(text: str) -> str:
    """«2 500» → «2500»: в ответе модели и в данных инструментов пробелы в числах ставятся по-разному."""
    return _SPACE_IN_NUMBER.sub("", text)


@dataclass(frozen=True, slots=True)
class FactCorpus:
    """Числа и время из результатов инструментов диалога — всё, что модели можно называть."""

    numbers: frozenset[str]
    times: frozenset[str]

    @classmethod
    def from_texts(cls, texts: Iterable[str]) -> "FactCorpus":
        numbers: set[str] = set()
        times: set[str] = set()
        for text in texts:
            joined = _join_thousands(text)
            numbers.update(_NUMBER.findall(joined))
            times.update(f"{int(hour):02d}:{minute}" for hour, minute in _TIME.findall(joined))
        return cls(frozenset(numbers), frozenset(times))


def unverified_facts(answer: str, corpus: FactCorpus) -> list[str]:
    """Цены и время из ответа, которых нет в данных инструментов. Пустой список — ответ подтверждён."""
    problems: list[str] = []
    joined = _join_thousands(answer)
    for raw in _PRICE.findall(joined):
        amount = re.sub(r"\D", "", raw)
        if amount and amount not in corpus.numbers:
            problems.append(f"цена {amount} ₽")
    for hour, minute in _TIME.findall(joined):
        moment = f"{int(hour):02d}:{minute}"
        if moment not in corpus.times:
            problems.append(f"время {moment}")
    return problems
