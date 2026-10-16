"""Схема без базы: SQLAlchemy собирает CREATE TABLE для PostgreSQL, тесты читают SQL."""

import uuid

import pytest
from sqlalchemy import Table
from sqlalchemy.engine import make_url
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session, make_transient_to_detached
from sqlalchemy.schema import CreateTable

from beauty_agent.db.models import (
    Base,
    Booking,
    ChannelAccount,
    Client,
    ClientIdentity,
    Message,
    Salon,
    Service,
    ToolCall,
)

# Диалект PostgreSQL с драйвером asyncpg — как у приложения. Подключения к базе нет.
DIALECT = make_url("postgresql+asyncpg://").get_dialect()()


def table_of(model: type[Base]) -> Table:
    table = model.__table__
    assert isinstance(table, Table)
    return table


def ddl(table: Table) -> str:
    return str(CreateTable(table).compile(dialect=DIALECT))


def test_all_tables_are_registered() -> None:
    expected = {
        "salons",
        "channel_accounts",
        "clients",
        "client_identities",
        "services",
        "masters",
        "master_services",
        "bookings",
        "conversations",
        "messages",
        "tool_calls",
        "knowledge_items",
        "knowledge_chunks",
        "outbound_messages",
        "salon_admins",
        "reminders",
        "api_keys",
        "drafts",
        "consents",
        "event_outbox",
    }

    assert set(Base.metadata.tables) == expected


# Журналы с порядком: ключ BIGINT по возрастанию (relay публикует по нему), строки не правятся —
# только отметка о публикации, поэтому updated_at не нужен
ORDERED_LOGS = {"event_outbox"}


@pytest.mark.parametrize(
    "table", [table for table in Base.metadata.sorted_tables if table.name not in ORDERED_LOGS], ids=lambda t: t.name
)
def test_uuid_key_and_timestamps(table: Table) -> None:
    sql = ddl(table)

    assert "id UUID NOT NULL" in sql
    assert f"CONSTRAINT pk_{table.name} PRIMARY KEY (id)" in sql
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in sql
    assert "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in sql


TENANT_TABLES = [table for table in Base.metadata.sorted_tables if table.name != "salons"]


@pytest.mark.parametrize("table", TENANT_TABLES, ids=lambda t: t.name)
def test_tenant_rows_belong_to_salon(table: Table) -> None:
    sql = ddl(table)

    assert f"CONSTRAINT fk_{table.name}_salon_id_salons FOREIGN KEY(salon_id)" in sql
    assert "REFERENCES salons (id) ON DELETE RESTRICT" in sql
    assert f"ix_{table.name}_salon_id" in {index.name for index in table.indexes}


def test_salon_constraints() -> None:
    sql = ddl(table_of(Salon))

    assert "CONSTRAINT uq_salons_slug UNIQUE (slug)" in sql
    assert "CONSTRAINT ck_salons_slug_format CHECK (slug ~ '^[a-z0-9-]+$')" in sql
    assert "timezone VARCHAR(64) DEFAULT 'Europe/Moscow' NOT NULL" in sql
    assert "is_demo BOOLEAN DEFAULT false NOT NULL" in sql


def test_client_phone_is_e164_and_unique_per_salon() -> None:
    sql = ddl(table_of(Client))

    assert "phone VARCHAR(16)," in sql  # без NOT NULL: номер может быть ещё неизвестен
    assert "CONSTRAINT uq_clients_salon_id_phone UNIQUE (salon_id, phone)" in sql
    assert "CONSTRAINT ck_clients_phone_e164 CHECK" in sql
    assert r"(phone ~ '^\+[1-9][0-9]{6,14}$')" in sql


def test_identity_is_unique_per_salon_channel_and_user() -> None:
    sql = ddl(table_of(ClientIdentity))

    assert "CONSTRAINT uq_client_identities_salon_id_channel_external_user_id" in sql
    assert "UNIQUE (salon_id, channel, external_user_id)" in sql
    assert "REFERENCES clients (id) ON DELETE CASCADE" in sql


def test_channel_is_varchar_with_check() -> None:
    sql = ddl(table_of(ChannelAccount))

    assert "channel VARCHAR(32) NOT NULL" in sql
    assert "CONSTRAINT ck_channel_accounts_channel CHECK (channel IN" in sql
    assert "('telegram', 'telegram_business', 'vk', 'max', 'web', 'api')" in sql


def test_booking_overlap_is_excluded_by_database() -> None:
    sql = ddl(table_of(Booking))

    assert "CONSTRAINT ex_bookings_master_overlap EXCLUDE USING gist" in sql
    assert "(master_id WITH =, tstzrange(starts_at, ends_at) WITH &&)" in sql
    assert "WHERE (status != 'cancelled')" in sql  # отменённая запись время не держит
    assert "CONSTRAINT ck_bookings_time_order CHECK (starts_at < ends_at)" in sql
    assert "CONSTRAINT uq_bookings_salon_id_idempotency_key UNIQUE (salon_id, idempotency_key)" in sql


def test_indexes_follow_queries() -> None:
    def columns(model: type[Base], name: str) -> list[str]:
        index = next(index for index in table_of(model).indexes if index.name == name)
        return [column.name for column in index.columns]

    # порядок колонок важен: сначала фильтр (чей диалог, чей салон), потом сортировка по времени
    assert columns(Message, "ix_messages_conversation_id_created_at") == ["conversation_id", "created_at"]
    assert columns(ToolCall, "ix_tool_calls_salon_id_created_at") == ["salon_id", "created_at"]
    assert "UNIQUE (conversation_id, external_id)" in ddl(table_of(Message))


def test_money_is_numeric_and_tool_arguments_are_jsonb() -> None:
    assert "price_min NUMERIC(10, 2) NOT NULL" in ddl(table_of(Service))
    assert "cost_rub NUMERIC(10, 4)," in ddl(table_of(Message))
    assert "arguments JSONB NOT NULL" in ddl(table_of(ToolCall))


def test_every_relationship_is_lazy_raise() -> None:
    for mapper in Base.registry.mappers:
        for relationship in mapper.relationships:
            name = f"{mapper.class_.__name__}.{relationship.key}"
            assert relationship.lazy == "raise", name


def test_lazy_load_raises_instead_of_hidden_query() -> None:
    salon = Salon(id=uuid.uuid4(), slug="studio-anna", name="Студия Анны")
    make_transient_to_detached(salon)  # как будто салон загружен из базы без связей
    Session().add(salon)

    with pytest.raises(InvalidRequestError, match="lazy='raise'"):
        _ = salon.channel_accounts
