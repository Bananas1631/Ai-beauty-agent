"""Бизнес-API версии 1: всё под префиксом /api/v1."""

from fastapi import APIRouter

from beauty_agent.api.v1 import admin, chat, clients, salons, webhooks

router = APIRouter(prefix="/api/v1")
router.include_router(salons.router)
router.include_router(admin.router)
router.include_router(chat.router)
router.include_router(clients.router)
router.include_router(webhooks.router)
