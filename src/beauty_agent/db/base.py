import os
import time
import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Имена индексов и ограничений строятся по шаблону, а не придумываются PostgreSQL.
# Alembic находит ограничения по имени: без шаблона миграция не сможет их удалить.
NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}


def uuid7() -> uuid.UUID:
    """UUID версии 7 (RFC 9562): первые 48 бит — время в миллисекундах.

    Новые ключи растут со временем, поэтому вставка дописывает индекс с конца,
    а не в случайное место, как UUID4. В Python 3.12 готовой uuid7() нет.
    """
    value = ((time.time_ns() // 1_000_000) << 80) | int.from_bytes(os.urandom(10))
    value = (value & ~(0xF << 76)) | (0x7 << 76)  # 4 бита версии: 7
    value = (value & ~(0x3 << 62)) | (0x2 << 62)  # 2 бита варианта: RFC 9562
    return uuid.UUID(int=value)


class Base(DeclarativeBase):
    """Базовый класс всех моделей: общие метаданные и правила для колонок."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # datetime в моделях — всегда TIMESTAMPTZ: момент времени без путаницы с поясами
    type_annotation_map = {datetime: DateTime(timezone=True)}  # noqa: RUF012
    # Значения, которые ставит БД (now(), DEFAULT), возвращаются сразу через RETURNING.
    # Иначе их чтение запустило бы скрытый запрос к базе, а в async это ошибка.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012


class UUIDPrimaryKey:
    """Первичный ключ id: UUID v7, создаётся в приложении ещё до INSERT."""

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid7,
        sort_order=-10,  # первой колонкой в таблице
    )


class SalonOwned:
    """Строка принадлежит салону; почти каждый запрос фильтрует по salon_id."""

    salon_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("salons.id", ondelete="RESTRICT"),
        index=True,
        sort_order=-5,  # сразу после id
    )


class Timestamps:
    """Время создания и изменения. Значение вычисляет БД функцией now()."""

    created_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        sort_order=10,  # в конце таблицы
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
        sort_order=10,
    )


def str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Колонка-перечисление: VARCHAR с ограничением CHECK, а не тип ENUM в PostgreSQL.

    Новое значение добавляется правкой CHECK в миграции, без ALTER TYPE.
    """
    return Enum(
        enum_cls,
        name=name,  # имя ограничения CHECK
        native_enum=False,
        create_constraint=True,
        length=32,
        # хранить значения ('telegram'), а не имена ('TELEGRAM')
        values_callable=lambda cls: [member.value for member in cls],
    )
