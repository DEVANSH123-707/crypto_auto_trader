"""Aggregates every route module into one router that ``main`` includes."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import auth, health, trades, webhook

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(trades.router)
api_router.include_router(webhook.router)
