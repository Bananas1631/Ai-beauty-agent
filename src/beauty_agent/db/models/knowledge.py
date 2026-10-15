"""База знаний салона: страницы сайта, FAQ, карточка филиала. Фрагменты с векторами — 26 октября."""

from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from beauty_agent.db.base import Base, SalonOwned, Timestamps, UUIDPrimaryKey, str_enum
from beauty_agent.domain.enums import KnowledgeSource


class KnowledgeItem(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Документ базы знаний, по которому агент отвечает на общие вопросы."""

    __tablename__ = "knowledge_items"
    __table_args__ = (UniqueConstraint("salon_id", "url"),)  # страница сайта — один документ

    source: Mapped[KnowledgeSource] = mapped_column(str_enum(KnowledgeSource, "source"))
    url: Mapped[str | None] = mapped_column(String(2048))  # у FAQ адреса нет
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    # SHA-256 текста: страницу, которая не изменилась, повторно не индексируем
    content_hash: Mapped[str] = mapped_column(String(64))
