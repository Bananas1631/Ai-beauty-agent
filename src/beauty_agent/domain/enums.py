"""Перечисления предметной области: допустимые значения вместо «магических строк»."""

from enum import StrEnum, auto


class Channel(StrEnum):
    """Мессенджер или виджет, через который клиент пишет салону."""

    TELEGRAM = auto()
    TELEGRAM_BUSINESS = auto()
    VK = auto()
    MAX = auto()
    WEB = auto()
    API = auto()  # REST API для сайтов, приложений и партнёрских CRM (api/v1/chat.py)


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


class CrmKind(StrEnum):
    """Откуда салон берёт каталог и свободное время."""

    YCLIENTS = auto()
    DEMO = auto()  # реальные слоты YCLIENTS, но запись только имитируется
    FAKE = auto()  # фейковый CRM: make seed и проверка без доступа к YCLIENTS


class OutboxStatus(StrEnum):
    """Исходящее сообщение в мессенджер."""

    PENDING = auto()  # ждёт отправки
    SENDING = auto()  # взято воркером; если воркер упал, блокировка истечёт и сообщение вернётся в работу
    SENT = auto()
    FAILED = auto()  # мессенджер отказал окончательно: клиент заблокировал бота, чат удалён


class PhotoMode(StrEnum):
    """Что делать с фото-референсом клиента («хочу вот так»)."""

    ADMIN = auto()  # передать администратору: по умолчанию, пока точность не измерена
    GUESS = auto()  # подобрать услугу по фото (workers/photo.py); после evals с точностью ≥ 80%


class ReplyMode(StrEnum):
    """Кто отправляет ответ агента клиенту в мессенджере."""

    AUTOPILOT = auto()  # агент отвечает сам
    SHADOW = auto()  # агент пишет черновик, администратор отправляет его кнопкой (пилот)


class DraftStatus(StrEnum):
    """Черновик ответа в теневом режиме."""

    PENDING = auto()  # ждёт решения администратора
    SENT = auto()  # отправлен клиенту без изменений
    EDITED = auto()  # отправлен исправленный текст
    MANUAL = auto()  # администратор ответил сам, черновик не пригодился


class AdminRole(StrEnum):
    OWNER = auto()
    ADMIN = auto()


class ReminderStatus(StrEnum):
    PENDING = auto()
    SENT = auto()
    CANCELLED = auto()  # запись отменена или перенесена — вместо него поставлено новое
    SKIPPED = auto()  # отправлять некуда (клиент писал с сайта) или запись уже прошла


class MessageStatus(StrEnum):
    """Обработка сообщения из REST API: статус хранится в строке сообщения, а не в Celery."""

    QUEUED = auto()
    PROCESSING = auto()
    DONE = auto()
    FAILED = auto()
