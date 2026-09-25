"""Exact original workload submission; a captured POST UID is separate evidence."""

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ControllerWorkloadSubmission(Base):
    __tablename__ = "controller_workload_submissions"

    operation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(63), primary_key=True)
    name: Mapped[str] = mapped_column(String(253), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(255), nullable=False)
    allocation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    step_key: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(255), nullable=False)
    fence_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
