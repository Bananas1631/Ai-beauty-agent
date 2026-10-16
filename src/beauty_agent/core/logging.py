"""Логи в JSON: trace_id и salon_id из контекста, телефоны и e-mail маскируются (152-ФЗ)."""

import logging
import re
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

# Телефон РФ в любой записи: +7 (916) 150-22-39, 89161502239, 7 916 150 22 39.
# (?<!\w) и (?!\w) не дают принять за телефон цифры внутри ID и trace_id.
PHONE_RE = re.compile(r"(?<!\w)(?:\+7|8|7)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\w)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Секрет в адресе вебхука YCLIENTS (YCLIENTS не умеет слать заголовки): в access-логе — звёздочки
WEBHOOK_SECRET_RE = re.compile(r"(/webhooks/[a-z_]+/[0-9a-fA-F-]{36}/)[0-9a-fA-F]{16,128}")


def _mask_phone(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group())
    return f"+7*******{digits[-2:]}"


def _mask_email(match: re.Match[str]) -> str:
    name, domain = match.group().split("@", 1)
    return f"{name[0]}***@{domain}"


def mask_text(text: str) -> str:
    """Заменяет телефоны и e-mail в строке: +7*******39, a***@mail.ru."""
    text = WEBHOOK_SECRET_RE.sub(r"\1***", text)
    return PHONE_RE.sub(_mask_phone, EMAIL_RE.sub(_mask_email, text))


def mask_value(value: Any) -> Any:
    """Маскирует ПДн во всех строках вложенной структуры: логи, события Sentry, трассы."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {key: mask_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [mask_value(item) for item in value]
    return value


def mask_pii(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Процессор structlog: маскирует персональные данные во всех полях события."""
    return {key: mask_value(value) for key, value in event_dict.items()}


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    """Один формат для всех логов: наших (structlog) и чужих (uvicorn, SQLAlchemy)."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,  # trace_id и salon_id из контекста запроса
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False) if json_logs else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,  # то же для записей стандартного logging
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            mask_pii,  # последним перед выводом: маскирует и текст исключений
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn вешает на свои логгеры собственные обработчики — убираем, чтобы записи шли через корневой
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # INFO печатает каждый SQL-запрос с параметрами — включать только для отладки
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
