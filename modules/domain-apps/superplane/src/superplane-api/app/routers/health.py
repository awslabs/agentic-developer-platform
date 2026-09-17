"""Health check endpoint."""

from fastapi import APIRouter

from app.config import settings
from app.schemas.health import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Return application health status and version."""
    return HealthResponse(status="healthy", version=settings.app_version)
