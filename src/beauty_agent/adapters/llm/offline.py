"""Офлайн-модель на правилах: тот же интерфейс LLMClient и те же инструменты, но без облачной LLM.

Зачем: чат в терминале, демо-страница после make seed и evals в CI работают без ключей API.
В evals это нижняя планка (baseline): живая модель должна быть заметно лучше правил.
Для продакшена не предназначена: понимает только типовые фразы.

Модель видит то же, что и настоящая: системный промпт (в нём данные инструментов прошлых
сообщений), последние реплики и результаты инструментов текущего шага.
"""

import json
import re
from collections.abc import Sequence
from itertools import count
from typing import Any

from beauty_agent.adapters.llm.base import ChatMessage, LLMResponse, ToolCallRequest, ToolSpec, Usage
from beauty_agent.agent.tokens import estimate_tokens

_ids = count(1)

_GREETING = re.compile(r"^\W*(здравствуй\w*|привет\w*|добр\w+\s+(день|утро|вечер)|доброго\s+\w+)\W*$")
_HUMAN = re.compile(r"администратор|менеджер|оператор|живо\w+\s+человек|жалоб|претензи|скидк|верните|возврат")
_INFO = re.compile(
    r"адрес|где\s+вы|где\s+наход|как\s+(до\s+вас\s+)?добрат|часы|режим\s+работ|график|работаете|до\s+скольки|"
    r"оплат|картой|парковк"
)
_PRICE = re.compile(r"сколько|цен[аы]|стоит|стоимост|почем|прайс")
# «запис» и «запиш»: записаться, но запишите — чередование с/ш
_BOOKING = re.compile(
    r"запис|запиш|хочу|хачу|нужн|нужен|надо|давайте|можно|свобод|окн|время|когда|завтра|сегодня|выходн|утр|вечер"
    r"|днем|днём"
)
_CANCEL = re.compile(r"отмен|перенес|перенест")
_YES = re.compile(r"^\W*(да|верно|все\s+верно|всё\s+верно|подтверждаю|ок|хорошо|записывайте)\b")
_PHONE = re.compile(r"(?:\+7|8)[\s(\-]*\d{3}[\s)\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}|(?<!\d)9\d{9}(?!\d)")
_NAME = re.compile(
    r"(?:меня\s+зовут|зовут|я\s*[—-]?)\s+([А-ЯЁ][а-яё]{1,20})|^([А-ЯЁ][а-яё]{1,20})(?=[,.\s]+(?:\+7|8|9|тел))"
)
_OPTION_WORDS = {"перв": 1, "втор": 2, "трет": 3}
_OPTION_DIGIT = re.compile(
    r"(?:вариант\w*|номер|№)\s*(\d)|(?<![\d:+])([123])(?![\d:])\s*(?:-?(?:й|ый|ой|ий)\b|\)|$|\s*вариант)"
)
_PART_OF_DAY = {
    "утр": "morning",
    "днем": "day",
    "днём": "day",
    "обед": "day",
    "вечер": "evening",
    "после работы": "evening",
}
# Служебные слова и слова о времени: после их удаления в запросе остаётся название услуги
_FILLER = re.compile(
    r"\b(здравствуйте|привет|добрый|день|хочу|хотела|хотел|бы|записаться|запишите|записать|меня|мне|на|можно|ли"
    r"|сколько|стоит|стоимость|цена|а|у|вас|есть|подскажите|пожалуйста|какая|какие|сделать|нужно|надо|давайте"
    r"|в|и|с|по|к|для|это|этой|эту|эта|этот|следующ\w*|недел\w*|понедельник\w*|вторник\w*|сред\w*|четверг\w*"
    r"|пятниц\w*|суббот\w*|воскресень\w*|выходн\w*|завтра|сегодня|послезавтра|утр\w*|вечер\w*|днем|днём"
    r"|после|работы|время|свободн\w*|окн\w*|когда|получится|удобно|хачу|нужен|нужна|хорошо|ок|ладно|да"
    r"|телефон\w*|номер|зовут)\b"
)
# Имя и телефон в запросе на поиск услуги не нужны
_CONTACTS = re.compile(r"(?:меня\s+)?зовут\s+\w+|(?:\+7|8)[\d\s()\-]{9,}|\d+")


def _lower(text: str | None) -> str:
    return (text or "").lower().replace("ё", "е")


class OfflineLLM:
    def __init__(self, *, model_key: str = "offline") -> None:
        self._model_key = model_key

    @property
    def model_key(self) -> str:
        return self._model_key

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> LLMResponse:
        prompt_tokens = sum(estimate_tokens(message.content or "") for message in messages)
        if not tools:  # резюме диалога и другие служебные вызовы без инструментов
            return self._text("Клиент обсуждает запись в салон.", prompt_tokens)
        last = messages[-1]
        if last.role == "tool":
            return self._after_tool(messages, prompt_tokens)
        return self._on_user(messages, prompt_tokens)

    # ------------------------------------------------------------- ответы

    def _text(self, text: str, prompt_tokens: int) -> LLMResponse:
        return LLMResponse(text, (), Usage(prompt_tokens, estimate_tokens(text)), model=self.model_key)

    def _call(self, name: str, arguments: dict[str, Any], prompt_tokens: int) -> LLMResponse:
        call = ToolCallRequest(f"offline_{next(_ids)}", name, json.dumps(arguments, ensure_ascii=False))
        return LLMResponse(None, (call,), Usage(prompt_tokens, 20), model=self.model_key)

    # ------------------------------------------------------------- новое сообщение клиента

    def _on_user(self, messages: Sequence[ChatMessage], tokens: int) -> LLMResponse:
        text = messages[-1].content or ""
        lowered = _lower(text)
        facts = _facts(messages)
        users = [_lower(message.content) for message in messages if message.role == "user"]

        if _HUMAN.search(lowered) or _CANCEL.search(lowered):
            return self._call("handoff_to_admin", {"reason": "Клиент просит администратора"}, tokens)
        if "[фото-референс:" in lowered:
            return self._call("photo_services", {}, tokens)
        if _GREETING.match(lowered):
            return self._text("Здравствуйте! Подскажите, на какую услугу хотите записаться?", tokens)
        if _INFO.search(lowered):
            return self._call("salon_info", {"question": text[:200]}, tokens)

        options = _latest(facts, "options")
        phone = _phone(text) or _phone_from(users[:-1])
        option = _option(lowered) or (_option_from(users[:-1]) if options else None)
        name = _name(text) or _name_from([message.content or "" for message in messages if message.role == "user"][:-1])
        if options and option and phone and name:
            return self._call("book", {"option": option, "client_name": name, "phone": phone}, tokens)
        if options and option and _YES.match(lowered) and not (phone and name):
            return self._text("Подскажите, пожалуйста, ваше имя и номер телефона.", tokens)
        if options and (option or phone):
            missing = "имя и телефон" if not phone else "ваше имя"
            if option and phone and not name:
                missing = "ваше имя"
            return self._text(f"Отлично! Подскажите, пожалуйста, {missing} для записи.", tokens)

        service = _latest(facts, "found")
        query = _FILLER.sub(" ", _CONTACTS.sub(" ", lowered))
        query = re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", query)).strip()
        if len(query) >= 3:
            return self._call("find_service", {"query": query[:100]}, tokens)
        if service and (_BOOKING.search(lowered) or _YES.match(lowered)):
            return self._call("find_free_time", _time_args(service[0]["service_id"], lowered), tokens)
        return self._text("Подскажите, на какую услугу хотите записаться?", tokens)

    # ------------------------------------------------------------- результат инструмента

    def _after_tool(self, messages: Sequence[ChatMessage], tokens: int) -> LLMResponse:
        name = _last_tool_name(messages)
        result = _json(messages[-1].content)
        user = _lower(next((m.content for m in reversed(messages) if m.role == "user"), ""))
        if "error" in result:
            return self._text(f"{result['error']}. Давайте попробуем ещё раз?", tokens)
        if name == "find_service":
            found = result.get("found") or []
            if not found:
                return self._text("Такой услуги я не нашла. Уточните, пожалуйста, что именно хотите сделать?", tokens)
            first = found[0]
            if _BOOKING.search(user) and not _PRICE.search(user):
                return self._call("find_free_time", _time_args(first["service_id"], user), tokens)
            return self._text(
                f"{first['name']} — {first['price']}, длится {first['duration_min']} минут. Подобрать время?", tokens
            )
        if name in ("find_free_time", "book") and result.get("options"):
            lines = "; ".join(
                f"{item['option']}) {item['when']}, мастер {item['master']}" for item in result["options"]
            )
            prefix = "Это время только что заняли. " if result.get("status") == "slot_taken" else ""
            return self._text(f"{prefix}Есть варианты: {lines}. Какой удобен? Подскажите имя и телефон.", tokens)
        if name == "find_free_time":
            return self._text("В эти дни свободного времени нет. Посмотреть другие даты?", tokens)
        if name == "book":
            if result.get("status") == "confirmed":
                return self._text(f"Записала вас: {result['when']}, мастер {result['master']}. Ждём вас!", tokens)
            return self._text(str(result.get("message", "Передала заявку администратору.")), tokens)
        if name == "photo_services":
            found = result.get("found") or []
            if not found:
                return self._call("handoff_to_admin", {"reason": "Не нашла услугу по фото"}, tokens)
            first = found[0]
            return self._text(
                f"Похоже на {result.get('photo', 'вашу работу')} — это «{first['name']}», {first['price']}. "
                "Подобрать время?",
                tokens,
            )
        if name == "salon_info":
            fragments = result.get("fragments") or []
            text = fragments[0]["text"] if fragments else "Уточню у администратора и вернусь с ответом."
            return self._text(text[:400], tokens)
        return self._text("Готово. Чем ещё помочь?", tokens)


# ---------------------------------------------------------------- разбор сообщений


def _json(content: str | None) -> dict[str, Any]:
    try:
        data = json.loads(content or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _facts(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    """Данные инструментов из системного промпта и текущего шага, от старых к новым."""
    facts: list[dict[str, Any]] = []
    system = messages[0].content or "" if messages and messages[0].role == "system" else ""
    for line in system.splitlines():
        if line.startswith("{"):
            facts.append(_json(line))
    facts.extend(_json(message.content) for message in messages if message.role == "tool")
    return facts


def _latest(facts: list[dict[str, Any]], field: str) -> list[dict[str, Any]] | None:
    for fact in reversed(facts):
        value = fact.get(field)
        if isinstance(value, list) and value:
            return value
    return None


def _last_tool_name(messages: Sequence[ChatMessage]) -> str | None:
    for message in reversed(messages):
        if message.role == "assistant" and message.tool_calls:
            return message.tool_calls[-1].name
    return None


def _phone(text: str) -> str | None:
    match = _PHONE.search(text)
    return match.group(0) if match else None


def _phone_from(texts: Sequence[str]) -> str | None:
    return next((phone for text in reversed(texts) if (phone := _phone(text))), None)


def _name(text: str) -> str | None:
    match = _NAME.search(text.strip())
    if not match:
        return None
    return match.group(1) or match.group(2)


def _name_from(texts: Sequence[str]) -> str | None:
    return next((name for text in reversed(texts) if (name := _name(text))), None)


def _option(lowered: str) -> int | None:
    for stem, number in _OPTION_WORDS.items():
        if re.search(rf"\b{stem}\w*", lowered):
            return number
    match = _OPTION_DIGIT.search(lowered)
    if match:
        return int(match.group(1) or match.group(2))
    return None


def _option_from(texts: Sequence[str]) -> int | None:
    return next((option for text in reversed(texts) if (option := _option(text))), None)


def _time_args(service_id: str, lowered: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {"service_id": service_id}
    for stem, part in _PART_OF_DAY.items():
        if stem in lowered:
            arguments["part_of_day"] = part
            break
    return arguments
