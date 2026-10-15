"""Системные промпты — отдельные файлы с версией: их меняют и сравнивают в evals без правки кода."""

from datetime import datetime
from functools import cache
from importlib.resources import files
from string import Template

from beauty_agent.agent.tools import format_local

DEFAULT_PROMPT_VERSION = "v1"


@cache
def load_prompt(version: str = DEFAULT_PROMPT_VERSION) -> Template:
    if not version.isalnum():  # версия приходит из настроек салона — не путь к файлу
        raise ValueError(f"Некорректная версия промпта: {version!r}")
    return Template(files(__package__).joinpath(f"system_{version}.md").read_text(encoding="utf-8"))


def render_system_prompt(*, salon_name: str, now: datetime, version: str = DEFAULT_PROMPT_VERSION) -> str:
    """Подстановка через Template: фигурные скобки и $ в названии салона ничего не ломают."""
    return load_prompt(version).safe_substitute(salon_name=salon_name, now=format_local(now), timezone=str(now.tzinfo))
