"""Реальные диалоги салона в датасет evals — обезличенные и только в приватное хранилище.

    poetry run python -m scripts.export_dialogs --slug studio-lavanda --query "наращивание" \\
        --date-from 2026-09-01 --date-to 2026-09-30 --out D:/private-evals/lavanda-naraschivanie.jsonl

Файл внутри репозитория не пишется (репозиторий публичный). Соль для ID сценариев — переменная
окружения EXPORT_SALT: без неё по ID нельзя сопоставить сценарий с диалогом. Ожидаемый итог
каждого сценария заполняет человек (тег needs_review). Подробности — rag/history_export.py.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from beauty_agent.adapters.llm.models import build_embedder
from beauty_agent.core.config import get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.rag.history_export import export_dialogs

REPOSITORY = Path(__file__).resolve().parents[1]


class UnsafeOutputError(ValueError):
    """Путь внутри репозитория: реальные диалоги не должны попасть в git."""


def check_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == REPOSITORY or REPOSITORY in resolved.parents:
        raise UnsafeOutputError(f"{resolved} внутри репозитория — укажите приватное хранилище вне его")
    return resolved


async def collect(args: argparse.Namespace, salt: str) -> list[dict[str, Any]] | None:
    settings = get_settings()
    engine = make_engine(settings.postgres)
    try:
        async with make_session_factory(engine)() as session:
            salon = await SalonsRepository(session).get_by_slug(args.slug)
            if salon is None:
                print(f"Салон {args.slug} не найден", file=sys.stderr)
                return None
            tz = ZoneInfo(salon.timezone)
            since = datetime.combine(args.date_from, time(0), tzinfo=tz) if args.date_from else None
            until = datetime.combine(args.date_to + timedelta(days=1), time(0), tzinfo=tz) if args.date_to else None
            return await export_dialogs(
                session,
                salon.id,
                build_embedder(settings.llm),
                args.query,
                salt=salt,
                since=since,
                until=until,
                limit=args.limit,
            )
    finally:
        await engine.dispose()


def write_scenarios(out: Path, scenarios: list[dict[str, Any]]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(scenario, ensure_ascii=False) + "\n" for scenario in scenarios)
    out.write_text(payload, encoding="utf-8", newline="\n")
    out.chmod(0o600)  # на POSIX — только владелец; на Windows влияет лишь на «только чтение»


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Реальные диалоги в датасет evals (обезличенные)")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--query", required=True, help="о чём диалоги: «наращивание», «перенос записи»")
    parser.add_argument("--date-from", type=date.fromisoformat)
    parser.add_argument("--date-to", type=date.fromisoformat)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out", type=Path, required=True, help="файл JSONL вне репозитория")
    args = parser.parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    salt = os.environ.get("EXPORT_SALT", "")
    if len(salt) < 16:
        print("Задайте EXPORT_SALT (не короче 16 символов) — секрет для необратимых ID", file=sys.stderr)
        return 2
    try:
        out = check_output(args.out)
    except UnsafeOutputError as exc:
        print(exc, file=sys.stderr)
        return 2
    scenarios = asyncio.run(collect(args, salt))
    if scenarios is None:
        return 1
    write_scenarios(out, scenarios)
    print(f"Сценариев: {len(scenarios)} → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
