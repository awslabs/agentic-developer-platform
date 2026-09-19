"""ResearchFinding model — external data scanner results (EPIC-G, US-G2).

Stores structured findings from external ML/AI sources (arXiv, HuggingFace,
GitHub, Twitter/X, Reddit, Hacker News, AWS, NVIDIA, competitor blogs).
Each finding has an agent-generated summary and relevance score.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Valid source identifiers for the scanner
VALID_SOURCES = (
    "arxiv",
    "huggingface",
    "github",
    "twitter",
    "reddit",
    "hackernews",
    "aws_whatsnew",
    "nvidia_blog",
    "competitor_blog",
)


class ResearchFinding(Base):
    """A single research finding discovered by the external data scanner."""

    __tablename__ = "research_findings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id"),
        nullable=True,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevance_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tags: Mapped[str | None] = mapped_column(
        JSONB, nullable=True
    )  # e.g. ["new-model", "inference", "H100"]
    raw_content_json: Mapped[str | None] = mapped_column(JSONB, nullable=True)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
