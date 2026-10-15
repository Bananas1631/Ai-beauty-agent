"""Оценка числа токенов без токенизатора конкретной модели.

У Qwen, GigaChat и YandexGPT разные токенизаторы, а точность нужна только для бюджета контекста:
для русского текста ~3 символа на токен — оценка с запасом.
"""

import json
import math
from typing import Any

CHARS_PER_TOKEN = 3.0


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def compact_json(data: dict[str, Any], *, max_tokens: int = 300) -> str:
    """JSON результата инструмента не длиннее max_tokens: длинные списки укорачиваются с пометкой.

    Длинный результат дорог (его модель читает на каждом следующем шаге) и размывает внимание.
    """
    text = _dump(data)
    if estimate_tokens(text) <= max_tokens:
        return text
    trimmed = dict(data)
    for name, value in data.items():
        if isinstance(value, list) and len(value) > 1:
            items = list(value)
            while len(items) > 1 and estimate_tokens(_dump({**trimmed, name: items})) > max_tokens:
                items.pop()
            trimmed[name] = items
            trimmed["truncated"] = True
    text = _dump(trimmed)
    limit = int(max_tokens * CHARS_PER_TOKEN)
    return text if len(text) <= limit else text[: limit - 1] + "…"
