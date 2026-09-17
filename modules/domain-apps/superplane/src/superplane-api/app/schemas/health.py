"""Pydantic response models for the health endpoint."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response for GET /health."""

    status: str
    version: str
