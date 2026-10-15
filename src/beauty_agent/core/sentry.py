"""Отправка ошибок в Sentry (или self-hosted GlitchTip в РФ) без персональных данных.

- send_default_pii=False: без IP, cookies и пользователя;
- include_local_variables=False: в локальных переменных стека бывают телефон и текст диалога;
- before_send и before_breadcrumb маскируют телефоны и e-mail во всех строках события,
  а из запроса убирают тело, cookies и заголовок Authorization.
Без DSN ничего не включается: тесты и локальная разработка работают без Sentry.
"""

from typing import Any

import sentry_sdk

from beauty_agent import __version__
from beauty_agent.core.config import SentrySettings
from beauty_agent.core.logging import mask_value

_DROP_REQUEST_FIELDS = ("data", "cookies", "env")
_DROP_HEADERS = {"authorization", "cookie", "x-telegram-bot-api-secret-token"}


def scrub_event(event: Any, _hint: Any = None) -> Any:
    """Очистка события перед отправкой: ПДн маскируются, тело и секреты запроса удаляются."""
    request = event.get("request")
    if isinstance(request, dict):
        for name in _DROP_REQUEST_FIELDS:
            request.pop(name, None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {k: v for k, v in headers.items() if k.lower() not in _DROP_HEADERS}
    return mask_value(event)


def scrub_breadcrumb(crumb: Any, _hint: Any = None) -> Any:
    return mask_value(crumb)


def init_sentry(settings: SentrySettings) -> bool:
    if settings.dsn is None:
        return False
    sentry_sdk.init(
        dsn=settings.dsn.get_secret_value(),
        environment=settings.environment,
        release=f"beauty-agent@{__version__}",
        traces_sample_rate=settings.traces_sample_rate,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        before_send=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
    )
    return True
