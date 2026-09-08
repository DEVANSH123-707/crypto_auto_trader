"""Health endpoints.

Two different questions, deliberately separated:

* ``/health``       - is the process alive? Answered with zero dependencies, so
                      an orchestrator never restarts a healthy app just because
                      the database blinked.
* ``/health/ready`` - can it actually serve traffic? Touches PostgreSQL, and
                      returns 503 when it cannot, so a load balancer can take
                      the instance out of rotation.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.core.config import settings
from app.db.database import check_database_connection
from app.schemas.health import LivenessResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=LivenessResponse, summary="Liveness")
async def health() -> LivenessResponse:
    """Liveness probe. Always cheap, never touches a dependency."""
    return LivenessResponse(
        status="ok", app=settings.APP_NAME, environment=settings.ENVIRONMENT
    )


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness (checks PostgreSQL)",
    responses={503: {"description": "A dependency is unavailable"}},
)
async def readiness(response: Response) -> ReadinessResponse:
    """Readiness probe: reports 503 when PostgreSQL is unreachable."""
    database_up = await check_database_connection()
    if not database_up:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if database_up else "degraded",
        database="up" if database_up else "down",
        # Surfaced so it is obvious at a glance that this deployment is
        # pointed at the testnet.
        binance_host=settings.binance_host,
    )
