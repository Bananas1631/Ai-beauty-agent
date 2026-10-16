"""Миграции только обратно совместимые: прошлая версия кода работает с новой схемой.

Деплой сначала применяет миграции, потом меняет код, а при сбое откатывает код на прошлый образ —
без отката схемы. Поэтому в upgrade() запрещено то, что ломает старый код:

- удалить или переименовать таблицу или колонку;
- сделать колонку NOT NULL без значения по умолчанию (старый код вставляет строки без неё);
- сменить тип колонки;
- то же через op.execute("ALTER TABLE … DROP / RENAME …").

Ломающие изменения делаются в два релиза (expand/contract): сначала добавить новое и перевести код,
следующим релизом — убрать старое. Такая миграция помечается комментарием в файле:
    # contract: колонка old_name не используется с версии 0.9 (PR …)

    poetry run python -m scripts.check_migrations          # все миграции
"""

import ast
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "src" / "beauty_agent" / "db" / "migrations" / "versions"
_FORBIDDEN_CALLS = {
    "drop_column": "удаление колонки",
    "drop_table": "удаление таблицы",
    "rename_table": "переименование таблицы",
}
_RAW_SQL = re.compile(
    r"\b(DROP\s+(COLUMN|TABLE)|RENAME\s+(COLUMN|TO)|ALTER\s+COLUMN\s+\w+\s+(TYPE|SET\s+NOT\s+NULL))\b", re.I
)
_APPROVED = re.compile(r"^\s*#\s*contract:\s*\S", re.M)


@dataclass(frozen=True, slots=True)
class Problem:
    file: str
    line: int
    reason: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.reason}"


def _op_calls(function: ast.FunctionDef) -> Iterator[tuple[str, ast.Call]]:
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"op", "batch_op"}
        ):
            yield node.func.attr, node


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)


def check_source(source: str, file: str) -> list[Problem]:
    if _APPROVED.search(source):
        return []  # ломающая часть expand/contract — осознанно, с причиной в комментарии
    tree = ast.parse(source)
    upgrade = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"), None)
    if upgrade is None:
        return []
    problems: list[Problem] = []
    for name, call in _op_calls(upgrade):
        if name in _FORBIDDEN_CALLS:
            problems.append(Problem(file, call.lineno, _FORBIDDEN_CALLS[name]))
        elif name == "alter_column":
            if _keyword(call, "new_column_name") is not None:
                problems.append(Problem(file, call.lineno, "переименование колонки"))
            if _keyword(call, "type_") is not None:
                problems.append(Problem(file, call.lineno, "смена типа колонки"))
            nullable = _keyword(call, "nullable")
            if (
                isinstance(nullable, ast.Constant)
                and nullable.value is False
                and _keyword(call, "server_default") is None
            ):
                problems.append(Problem(file, call.lineno, "NOT NULL без server_default"))
        elif name == "add_column":
            column = call.args[1] if len(call.args) > 1 else None
            if isinstance(column, ast.Call):
                nullable = _keyword(column, "nullable")
                if (
                    isinstance(nullable, ast.Constant)
                    and nullable.value is False
                    and _keyword(column, "server_default") is None
                ):
                    problems.append(Problem(file, call.lineno, "новая NOT NULL колонка без server_default"))
        elif name == "execute" and call.args:
            sql = call.args[0]
            text = sql.value if isinstance(sql, ast.Constant) and isinstance(sql.value, str) else ""
            if _RAW_SQL.search(text):
                problems.append(Problem(file, call.lineno, "ломающий SQL в op.execute"))
    return problems


def main() -> int:
    problems = [
        problem
        for path in sorted(VERSIONS.glob("*.py"))
        for problem in check_source(path.read_text(encoding="utf-8"), path.name)
    ]
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(
            "Миграция ломает прошлую версию кода — см. expand/contract в scripts/check_migrations.py", file=sys.stderr
        )
        return 1
    print(f"Миграции обратно совместимы: {len(list(VERSIONS.glob('*.py')))} файлов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
