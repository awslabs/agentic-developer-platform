from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantMixin, new_uuid, utcnow


class UsageLog(Base, TenantMixin):
    __tablename__ = "usage_logs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    department_id: Mapped[str] = mapped_column(String(255), nullable=False)
    team_id: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    account_type: Mapped[str] = mapped_column(String(20), nullable=False, default="human")  # human/service
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(255))
    bedrock_account_id: Mapped[str | None] = mapped_column(String(12))
    # Issue #1616: Per-run cost traceability
    agent_run_id: Mapped[str | None] = mapped_column(String(255), index=True)
    chat_log_s3_key: Mapped[str | None] = mapped_column(String(1024))
    # Issue #4180: prompt-cache token accounting. Nullable on purpose —
    # NULL means "provider did not report this counter", 0 means "provider
    # reported zero cache activity". Collapsing the two would make the cache
    # hit-rate query silently wrong. Never give these a default.
    cache_read_input_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(Integer)
    # Issue #4207: the orchestration graph address (`flow/epic/wave/node`) this
    # call is attributable to. Nullable with NO default, and never backfilled —
    # a null means "this row is not addressed to a graph node", which is the
    # truth for every pre-feature row and for every non-gateway Bedrock path
    # (ADP_BEDROCK_VIA=direct|user writes no row at all). A default would stamp a
    # fabricated address onto historical rows and land them in some EPIC's total.
    graph_address: Mapped[str | None] = mapped_column(String(512), index=True)
    # Issue #4398: which client tool made the request (claude_code, codex_cli,
    # cursor, web_chat, sdk — the closed set in src/proxy/client_tool.py).
    # Nullable with NO default, and never backfilled: NULL means "NOT CAPTURED",
    # never "unknown tool". That distinction is the whole contract (FR-6.2) —
    # every pre-migration row is NULL, and a default or sentinel here would make
    # those rows read as a real tool and corrupt any future per-tool breakdown.
    # Never holds a raw User-Agent; only a normalised member of that closed set.
    client_tool: Mapped[str | None] = mapped_column(String(32), index=True)


class RateLimitConfig(Base, TenantMixin):
    __tablename__ = "rate_limit_configs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    rpm: Mapped[int | None] = mapped_column(Integer)
    tpm: Mapped[int | None] = mapped_column(Integer)
    concurrent_requests: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BedrockPoolAccount(Base):
    __tablename__ = "bedrock_pool_accounts"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    account_id: Mapped[str] = mapped_column(String(12), nullable=False)
    role_arn: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    region: Mapped[str] = mapped_column(String(20), nullable=False)
    is_healthy: Mapped[bool] = mapped_column(default=True)
    last_health_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelAlias(Base, TenantMixin):
    __tablename__ = "model_aliases"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    alias_name: Mapped[str] = mapped_column(String(255), nullable=False)
    bedrock_model_id: Mapped[str] = mapped_column(String(255), nullable=False)


class ModelPricing(Base):
    """
    Stores per-model pricing for cost calculation.

    This table is populated by the pricing-refresh Lambda and read by:
    - usage-tracker Lambda (for accurate cost recording)
    - Gateway pods (optional future enhancement for real-time cost display)

    Issue #234: Updated to add source field for tracking pricing source.
    """

    __tablename__ = "model_pricing"

    model_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    input_price_per_1k_tokens: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    output_price_per_1k_tokens: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="pricing_api")  # 'pricing_api' or 'fallback'
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
