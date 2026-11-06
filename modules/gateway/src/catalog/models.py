"""Catalog-owned Portfolio identity and immutable dated membership versions."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, new_uuid, utcnow

CatalogJSON = JSON().with_variant(JSONB(), "postgresql")


class PortfolioVersion(Base):
    __tablename__ = "catalog_portfolio_versions"
    __table_args__ = (CheckConstraint("revision > 0"),)

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_connection_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    data: Mapped[dict] = mapped_column(CatalogJSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PortfolioHead(Base):
    __tablename__ = "catalog_portfolio_heads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "source_connection_id", "id", "revision"],
            [
                "catalog_portfolio_versions.tenant_id",
                "catalog_portfolio_versions.source_connection_id",
                "catalog_portfolio_versions.id",
                "catalog_portfolio_versions.revision",
            ],
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_connection_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)


class MembershipVersion(Base):
    __tablename__ = "catalog_membership_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "portfolio_source", "portfolio_id", "portfolio_revision"],
            [
                "catalog_portfolio_versions.tenant_id",
                "catalog_portfolio_versions.source_connection_id",
                "catalog_portfolio_versions.id",
                "catalog_portfolio_versions.revision",
            ],
        ),
        CheckConstraint("relationship_revision > 0"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from"),
        Index("ix_catalog_membership_product", "tenant_id", "product_source", "product_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    relationship_revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    portfolio_source: Mapped[str] = mapped_column(String(255), nullable=False)
    portfolio_id: Mapped[str] = mapped_column(String(255), nullable=False)
    portfolio_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    product_source: Mapped[str] = mapped_column(String(255), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    product_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_connection_id: Mapped[str] = mapped_column(String(255), nullable=False)
    mapping_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_refs: Mapped[list] = mapped_column(CatalogJSON, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_revision: Mapped[int | None] = mapped_column(BigInteger)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class MembershipHead(Base):
    __tablename__ = "catalog_membership_heads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "relationship_id", "relationship_revision"],
            [
                "catalog_membership_versions.tenant_id",
                "catalog_membership_versions.relationship_id",
                "catalog_membership_versions.relationship_revision",
            ],
        ),
        UniqueConstraint("tenant_id", "portfolio_source", "portfolio_id", "product_source", "product_id", name="uq_catalog_membership_pair"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    relationship_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    portfolio_source: Mapped[str] = mapped_column(String(255), nullable=False)
    portfolio_id: Mapped[str] = mapped_column(String(255), nullable=False)
    product_source: Mapped[str] = mapped_column(String(255), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)


class CatalogChange(Base):
    __tablename__ = "catalog_changes"
    __table_args__ = (Index("ix_catalog_changes_record", "tenant_id", "record_type", "record_id", "recorded_at"),)

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_connection_id: Mapped[str] = mapped_column(String(255), nullable=False)
    record_type: Mapped[str] = mapped_column(String(32), nullable=False)
    record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    native_record_revision: Mapped[str] = mapped_column(String(255), nullable=False)
    relationship_revision: Mapped[int | None] = mapped_column(BigInteger)
    mapping_version: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_refs: Mapped[list] = mapped_column(CatalogJSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CatalogReceipt(Base):
    __tablename__ = "catalog_mutation_receipts"

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(80), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[dict] = mapped_column(CatalogJSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class InitiativeVersion(Base):
    __tablename__ = "catalog_initiative_versions"
    __table_args__ = (CheckConstraint("revision > 0"),)

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_connection_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    data: Mapped[dict] = mapped_column(CatalogJSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class InitiativeHead(Base):
    __tablename__ = "catalog_initiative_heads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "source_connection_id", "id", "revision"],
            [
                "catalog_initiative_versions.tenant_id",
                "catalog_initiative_versions.source_connection_id",
                "catalog_initiative_versions.id",
                "catalog_initiative_versions.revision",
            ],
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_connection_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)


class ContributionVersion(Base):
    __tablename__ = "catalog_contribution_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "initiative_source", "initiative_id", "initiative_revision"],
            [
                "catalog_initiative_versions.tenant_id",
                "catalog_initiative_versions.source_connection_id",
                "catalog_initiative_versions.id",
                "catalog_initiative_versions.revision",
            ],
        ),
        CheckConstraint("revision > 0"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from"),
        Index("ix_catalog_contribution_project", "tenant_id", "project_id", "id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_connection_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    initiative_source: Mapped[str] = mapped_column(String(255), nullable=False)
    initiative_id: Mapped[str] = mapped_column(String(255), nullable=False)
    initiative_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    product_source: Mapped[str] = mapped_column(String(255), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    data: Mapped[dict] = mapped_column(CatalogJSON, nullable=False)
    work_links: Mapped[list] = mapped_column(CatalogJSON, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supersedes_revision: Mapped[int | None] = mapped_column(BigInteger)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ContributionHead(Base):
    __tablename__ = "catalog_contribution_heads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "source_connection_id", "id", "revision"],
            [
                "catalog_contribution_versions.tenant_id",
                "catalog_contribution_versions.source_connection_id",
                "catalog_contribution_versions.id",
                "catalog_contribution_versions.revision",
            ],
        ),
        UniqueConstraint(
            "tenant_id", "initiative_source", "initiative_id", "product_source", "product_id", "project_id", name="uq_catalog_contribution_tuple"
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_connection_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    initiative_source: Mapped[str] = mapped_column(String(255), nullable=False)
    initiative_id: Mapped[str] = mapped_column(String(255), nullable=False)
    product_source: Mapped[str] = mapped_column(String(255), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
