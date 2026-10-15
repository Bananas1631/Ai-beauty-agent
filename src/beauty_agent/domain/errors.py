"""Доменные ошибки. Код реагирует на смысл ошибки, а не на HTTP-коды конкретной CRM или LLM.

api/errors.py переводит их в Problem Details: NotFound → 404, Conflict → 409, Validation → 422.
Текст ошибки (message) можно показать человеку: внутренних подробностей в нём нет.
"""


class DomainError(Exception):
    """Базовая доменная ошибка."""

    default_message = "Не удалось выполнить действие"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


class NotFoundError(DomainError):
    default_message = "Не найдено"


class ConflictError(DomainError):
    default_message = "Действие конфликтует с текущим состоянием"


class DomainValidationError(DomainError):
    default_message = "Данные не прошли проверку"


# ---------------------------------------------------------------- запись и CRM


class SlotTakenError(ConflictError):
    default_message = "Это время уже занято"


class OfferExpiredError(ConflictError):
    default_message = "Предложенный вариант устарел — подберу время заново"


class BookingInProgressError(ConflictError):
    default_message = "Эта запись уже оформляется"


class ProviderUnavailableError(DomainError):
    """CRM недоступна: таймаут, 5xx, лимит запросов или разомкнут circuit breaker."""

    default_message = "Система записи салона временно недоступна"


class ProviderAuthError(DomainError):
    """CRM отклонила токен: нет доступа к филиалу."""

    default_message = "Нет доступа к системе записи салона"


class ConfirmationRequiredError(DomainError):
    """CRM требует подтвердить телефон кодом из SMS."""

    default_message = "Нужен код подтверждения из SMS"
