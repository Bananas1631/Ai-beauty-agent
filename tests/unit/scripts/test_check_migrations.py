"""Проверка обратной совместимости миграций (expand/contract)."""

import pytest

from scripts.check_migrations import VERSIONS, check_source

HEADER = "import sqlalchemy as sa\nfrom alembic import op\n\n\ndef upgrade() -> None:\n"


def reasons(body: str, extra: str = "") -> list[str]:
    return [problem.reason for problem in check_source(extra + HEADER + body, "0099_test.py")]


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ('    op.drop_column("bookings", "price")\n', "удаление колонки"),
        ('    op.drop_table("drafts")\n', "удаление таблицы"),
        ('    op.alter_column("clients", "name", new_column_name="full_name")\n', "переименование колонки"),
        ('    op.alter_column("clients", "name", type_=sa.Text())\n', "смена типа колонки"),
        ('    op.alter_column("clients", "name", nullable=False)\n', "NOT NULL без server_default"),
        (
            '    op.add_column("clients", sa.Column("tier", sa.String(), nullable=False))\n',
            "новая NOT NULL колонка без server_default",
        ),
        ('    op.execute("ALTER TABLE clients DROP COLUMN name")\n', "ломающий SQL в op.execute"),
    ],
)
def test_breaking_changes_are_rejected(body: str, reason: str) -> None:
    assert reasons(body) == [reason]


def test_additive_changes_pass() -> None:
    body = (
        '    op.add_column("clients", sa.Column("tier", sa.String(), nullable=True))\n'
        '    op.add_column("clients", sa.Column("vip", sa.Boolean(), server_default=sa.false(), nullable=False))\n'
        '    op.create_index("ix_x", "clients", ["tier"])\n'
        '    op.alter_column("clients", "name", nullable=False, server_default="")\n'
    )
    assert reasons(body) == []


def test_downgrade_may_drop() -> None:
    source = HEADER + "    pass\n\n\ndef downgrade() -> None:\n    op.drop_table('drafts')\n"
    assert check_source(source, "0099_test.py") == []


def test_contract_step_is_allowed_with_reason() -> None:
    assert reasons('    op.drop_column("bookings", "legacy")\n', extra="# contract: legacy не читается с 0.9\n") == []


def test_repository_migrations_are_compatible() -> None:
    problems = [p for path in VERSIONS.glob("*.py") for p in check_source(path.read_text("utf-8"), path.name)]
    assert problems == []
