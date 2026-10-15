"""Перечисления предметной области: допустимые значения вместо «магических строк»."""

from enum import StrEnum, auto


class Channel(StrEnum):
    """Мессенджер или виджет, через который клиент пишет салону."""

    TELEGRAM = auto()
    TELEGRAM_BUSINESS = auto()
    VK = auto()
    MAX = auto()
    WEB = auto()


class BookingStatus(StrEnum):
    """Статус записи клиента."""

    PENDING = auto()  # ждёт подтверждения, например кода из SMS
    CONFIRMED = auto()
    CANCELLED = auto()
    COMPLETED = auto()
    NO_SHOW = auto()  # клиент не пришёл


class ConversationStatus(StrEnum):
    """Кто сейчас ведёт диалог."""

    ACTIVE = auto()  # отвечает агент
    HANDED_OFF = auto()  # передан администратору
    CLOSED = auto()


class ConsentType(StrEnum):
    """На что клиент дал согласие."""

    PERSONAL_DATA = auto()  # обработка персональных данных (152-ФЗ)
    MARKETING = auto()  # рассылки


class MessageRole(StrEnum):
    """Кто написал сообщение в диалоге."""

    CLIENT = auto()
    AGENT = auto()  # ИИ-администратор
    ADMIN = auto()  # живой администратор салона после передачи диалога


class KnowledgeSource(StrEnum):
    """Откуда взят документ базы знаний."""

    WEBSITE = auto()  # страница сайта салона
    FAQ = auto()  # вопросы и ответы, которые салон написал сам
    CRM = auto()  # карточка филиала в YCLIENTS: адрес, часы работы, описание
