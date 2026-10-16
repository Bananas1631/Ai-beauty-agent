"""Keyset-пагинация: курсор — непрозрачная строка с (временем, id) последней строки страницы.

Следующая страница — строго «раньше» курсора по индексу (время, id): запрос не замедляется на
длинной истории, в отличие от OFFSET (docs/performance.md). Подделанный курсор — 400, а не 500.
"""

import base64
import json
import uuid
from datetime import datetime

from fastapi import HTTPException, status

from beauty_agent.db.repositories.conversations import Cursor


def encode_cursor(at: datetime, id_: uuid.UUID) -> str:
    raw = json.dumps({"t": at.isoformat(), "id": str(id_)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None) -> Cursor | None:
    if not value:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        at = datetime.fromisoformat(data["t"])
        if at.tzinfo is None:
            raise ValueError("время курсора без часового пояса")
        return Cursor(at, uuid.UUID(data["id"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный курсор") from exc
