"""Бизнес-API версии 1: всё под префиксом /api/v1."""

from fastapi import APIRouter

from beauty_agent.api.v1 import salons

router = APIRouter(prefix="/api/v1")
router.include_router(salons.router)
