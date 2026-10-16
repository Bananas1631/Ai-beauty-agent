"""Журнал согласий: строки только добавляются, текущее состояние — по последней записи."""

import uuid

from sqlalchemy import func, insert, select

from beauty_agent.db.models import Consent
from beauty_agent.db.repositories.base import SalonRepository
from beauty_agent.domain.consent import CONSENT_VERSION, ConsentState
from beauty_agent.domain.enums import Channel, ConsentType


class ConsentsRepository(SalonRepository[Consent]):
    model = Consent

    async def state(self, client_id: uuid.UUID, kind: ConsentType = ConsentType.PERSONAL_DATA) -> ConsentState:
        statement = (
            select(Consent.granted)
            .where(Consent.salon_id == self.salon_id, Consent.client_id == client_id, Consent.kind == kind)
            .order_by(Consent.created_at.desc(), Consent.id.desc())
            .limit(1)
        )
        granted = await self.session.scalar(statement)
        if granted is None:
            return ConsentState.NONE
        return ConsentState.GRANTED if granted else ConsentState.WITHDRAWN

    async def record(
        self,
        client_id: uuid.UUID,
        channel: Channel,
        *,
        granted: bool,
        kind: ConsentType = ConsentType.PERSONAL_DATA,
        version: str = CONSENT_VERSION,
    ) -> None:
        # clock_timestamp, а не now(): согласие и отзыв в одной транзакции различаются по времени
        await self.session.execute(
            insert(Consent).values(
                salon_id=self.salon_id,
                client_id=client_id,
                kind=kind,
                granted=granted,
                document_version=version,
                channel=channel,
                created_at=func.clock_timestamp(),
                updated_at=func.clock_timestamp(),
            )
        )
