"""Режим Telegram Business: бот отвечает клиентам от имени аккаунта салона.

Владелец аккаунта (нужен Telegram Premium) подключает бота в настройках «Telegram для бизнеса».
Сообщения приходят обновлениями business_message с business_connection_id, а ответ отправляется
sendMessage с тем же business_connection_id — клиент видит его от имени салона.

В бизнес-чат попадают и сообщения самого владельца: администратор может ответить клиенту руками.
В личном чате ID чата совпадает с ID клиента, поэтому сообщение, у которого отправитель не совпадает
с чатом, написал владелец — агент на него не отвечает.
"""

from typing import Any


def business_client_message(message: Any) -> dict[str, Any] | None:
    """Сообщение клиента в бизнес-чате или None (сообщение владельца, бота или без подключения)."""
    if not isinstance(message, dict) or not message.get("business_connection_id"):
        return None
    if message.get("sender_business_bot"):
        return None  # это ответ нашего же бота
    chat_id = (message.get("chat") or {}).get("id")
    sender_id = (message.get("from") or {}).get("id")
    if chat_id is None or sender_id != chat_id:
        return None  # пишет владелец аккаунта салона, а не клиент
    return message


def parse_business_connection(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Событие подключения бота к бизнес-аккаунту: для онбординга — кто владелец и может ли бот отвечать."""
    connection = payload.get("business_connection")
    if not isinstance(connection, dict):
        return None
    rights = connection.get("rights") or {}
    return {
        "connection_id": str(connection.get("id")),
        "owner_id": str((connection.get("user") or {}).get("id")),
        "enabled": bool(connection.get("is_enabled")),
        "can_reply": bool(rights.get("can_reply", connection.get("can_reply", False))),
    }
