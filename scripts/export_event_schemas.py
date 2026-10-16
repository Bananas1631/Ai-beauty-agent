"""JSON Schema доменных событий из domain/events.py → docs/events/schemas/<тип>.v<версия>.json.

    poetry run python -m scripts.export_event_schemas

Схемы читают другие сервисы (биллинг на Kotlin, шаг 29) и Kafka UI. Источник правды — Pydantic-модели;
тест tests/unit/events/test_event_schemas.py падает, если выгрузка устарела.
"""

import json
import sys
from pathlib import Path

from beauty_agent.domain.events import EVENT_TYPES

SCHEMAS = Path(__file__).resolve().parents[1] / "docs" / "events" / "schemas"


def schema_files() -> dict[str, str]:
    """Имя файла → содержимое, детерминированно (сортировка ключей, перевод строки в конце)."""
    files: dict[str, str] = {}
    for model in EVENT_TYPES:
        event_type = str(model.model_fields["type"].default)
        version = int(model.model_fields["schema_version"].default)
        schema = model.model_json_schema(mode="serialization")
        schema["$id"] = (
            f"https://github.com/Bananas1631/Ai-beauty-agent/docs/events/schemas/{event_type}.v{version}.json"
        )
        files[f"{event_type}.v{version}.json"] = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return files


def main() -> int:
    SCHEMAS.mkdir(parents=True, exist_ok=True)
    for name, content in schema_files().items():
        (SCHEMAS / name).write_text(content, encoding="utf-8", newline="\n")
        print(SCHEMAS / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
