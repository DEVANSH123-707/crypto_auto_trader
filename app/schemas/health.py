"""Schemas for the health endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    """Is the process up? Answered without touching any dependency."""

    status: Literal["ok"]
    app: str
    environment: str


class ReadinessResponse(BaseModel):
    """Is the process able to serve traffic (i.e. can it reach PostgreSQL)?"""

    status: Literal["ready", "degraded"]
    database: Literal["up", "down"]
    binance_host: str
