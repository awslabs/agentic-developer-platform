"""Durable, scoped execution state and immutable admission inputs."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class Run(Base):
    __tablename__ = "l5_service_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "configuration_id", "configuration_revision"],
            ["l5_service_config_versions.tenant_id", "l5_service_config_versions.configuration_id",
             "l5_service_config_versions.revision"],
        ),
        Index("ix_l5_native_run_checks", "service_id", "next_check_at"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    service_id: Mapped[str] = mapped_column(String(255), nullable=False)
    configuration_id: Mapped[str] = mapped_column(String(36), nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_id: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger_id: Mapped[str] = mapped_column(String(255), nullable=False)
    binding_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input: Mapped[dict] = mapped_column(Payload, nullable=False)
    admission: Mapped[dict | None] = mapped_column(Payload)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="adp_submitted")
    scope: Mapped[dict | None] = mapped_column(Payload)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="admitted")
    cancel_reason: Mapped[str | None] = mapped_column(String(1000))
    provider_state: Mapped[str] = mapped_column(String(32), nullable=False, default="not_started")
    no_effect_proof: Mapped[dict | None] = mapped_column(Payload)
    retry_of: Mapped[str | None] = mapped_column(String(36))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capture_limits: Mapped[dict | None] = mapped_column(Payload)
    manifest: Mapped[dict | None] = mapped_column(Payload)
    monitoring: Mapped[dict | None] = mapped_column(Payload)
    native_job_ref: Mapped[dict | None] = mapped_column(Payload)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_readback_ref: Mapped[dict | None] = mapped_column(Payload)


def view_run(run):
    if isinstance(run, dict):
        return run
    deadline = run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or UTC).astimezone(UTC)
    result = {
        "run_id": run.run_id, "scope": run.scope or run.admission["scope"], "revision": run.revision,
        "state": run.state, "provider_state": run.provider_state,
        "deadline_at": deadline.isoformat().replace("+00:00", "Z"), "origin": run.origin,
    }
    if run.admission is not None:
        result["input"] = run.input
        result["admission"] = run.admission
        if run.service_id == "security":
            # These immutable pins are stored as FK-backed Run columns. Expose
            # them for setup/readback parity, including already admitted runs.
            result["admission"] = {**run.admission, "configuration_id": run.configuration_id,
                                   "configuration_revision": run.configuration_revision}
    else:
        result["service_id"] = run.service_id
        result["attribution"] = "observed" if (run.monitoring or {}).get("attribution") else "unverified"
        if run.input.get("native_event_ref"):
            result["native_event_ref"] = run.input["native_event_ref"]
    if run.native_job_ref is not None:
        result["native_job_ref"] = run.native_job_ref
    if run.service_id == "well_architected" and run.manifest is not None:
        source_manifest = {key: run.manifest.get(key) for key in (
            "archive", "architecture", "provider_source_uri", "provider_coverage")}
        if all(value is not None for value in source_manifest.values()):
            result["source_manifest"] = source_manifest
    if run.provider_readback_ref is not None:
        result["provider_readback_ref"] = run.provider_readback_ref
    if run.monitoring is not None:
        result["monitoring"] = {key: value for key, value in run.monitoring.items()
                                if key not in {"attribution", "native_status", "security_recovery", "devops_recovery"}}
    if run.retry_of is not None:
        result["retry_of"] = run.retry_of
    return result
