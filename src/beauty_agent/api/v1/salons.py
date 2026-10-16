"""Публичные данные салона: то, что видит клиент на странице салона."""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from beauty_agent.api.deps import PublicRateLimit, SalonDep

router = APIRouter(prefix="/salons", tags=["salons"], dependencies=[PublicRateLimit])


class SalonOut(BaseModel):
    """Только публичные поля: ID филиала в CRM и служебные флаги наружу не отдаём."""

    model_config = ConfigDict(from_attributes=True)

    slug: str
    name: str
    timezone: str


@router.get("/{slug}")
async def get_salon(salon: SalonDep) -> SalonOut:
    return SalonOut.model_validate(salon)
