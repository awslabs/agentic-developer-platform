"""Retained exclusive claim on a native context-bearing Agent Profile."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ProfileClaim(Base):
    __tablename__ = "l5_service_profile_claims"
    service_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    native_identity_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
