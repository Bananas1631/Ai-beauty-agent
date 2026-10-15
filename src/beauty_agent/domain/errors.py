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
