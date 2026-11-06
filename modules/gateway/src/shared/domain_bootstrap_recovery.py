"""Protected source qualification for a bootstrap that never acquired authority."""

import re
from datetime import UTC, datetime, timedelta

from src.shared import domain_bootstrap_components_repeat as repeated_components
from src.shared.domain_bootstrap_acquisition import KIND as ACQUISITION_KIND
from src.shared.domain_bootstrap_acquisition import extension_boundary, validate_precomponent_failures, validate_source_prefix
from src.shared.domain_bootstrap_boundary import qualified_source, validate_attempt_extension
from src.shared.domain_bootstrap_components import KIND as COMPONENTS_KIND
from src.shared.domain_bootstrap_components import validate as validate_components
from src.shared.domain_bootstrap_components import validate_source as validate_components_source
from src.shared.domain_bootstrap_denial import KIND as DENIED_KIND
from src.shared.domain_bootstrap_denial import validate_denial
from src.shared.domain_bootstrap_evidence import IDENTITY as IDENTITY
from src.shared.domain_bootstrap_evidence import framing, record_id
from src.shared.domain_bootstrap_retained import KIND as RETAINED_KIND
from src.shared.domain_bootstrap_retained import validate_retained
from src.shared.domain_bootstrap_revoked import KINDS as REVOKED_KINDS
from src.shared.domain_bootstrap_revoked import NAMESPACE_KIND, validate_revoked


def validate(value, *, require_fresh=True):
    from src.internal.domain_operation_store import harness

    harness("bootstrap_provenance_assumption").validate_owner(value)
    framing(value, require_fresh=require_fresh)
    source = qualified_source(value.get("source_image", ""))
    checked = datetime.fromisoformat(value["checked_at"])
    expiry = datetime.fromisoformat(value["expires_at"])
    if (
        value.get("version") != 1
        or value.get("evidence_kind")
        not in {"bootstrap-journal-barrier-v1", DENIED_KIND, RETAINED_KIND, COMPONENTS_KIND, repeated_components.KIND, *REVOKED_KINDS}
        or value.get("record_id") != record_id(value)
        or checked.tzinfo is None
        or expiry.tzinfo is None
        or not checked <= datetime.now(UTC)
        or not checked < expiry <= checked + timedelta(hours=1)
        or (require_fresh and not datetime.now(UTC) < expiry)
        or source is None
        or value.get("source_revision") != source["revision"]
        or value.get("source_hashes") != source["hashes"]
        or value.get("source_pod_uid") != value.get("workload_binding")
        or type(value.get("source_fence")) is not int
        or not 1 <= value["source_fence"] <= 32
        or not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", value.get("source_image", ""))
        or any(
            not value.get(key)
            for key in (
                "source_principal",
                "source_acquired_at",
                "source_pod_name",
                "source_pod_uid",
                "workload_binding",
                "source_audit",
                "installation_id",
                "credential_id",
                "account_id",
                "region",
                "adp_org_id",
                "policy_sha256",
            )
        )
    ):
        raise ValueError("bootstrap source boundary is unavailable or expired")
    if value["evidence_kind"] == repeated_components.KIND:
        repeated_components.validate_source(source)
        repeated_components.validate(value)
    elif value["evidence_kind"] == COMPONENTS_KIND:
        validate_components_source(source)
        validate_components(value)
    elif value["evidence_kind"] in REVOKED_KINDS:
        if source.get("registrar_prefix") is not True:
            raise ValueError("source lacks registrar prefix qualification")
        if value["evidence_kind"] == NAMESPACE_KIND and source.get("namespace_prefix") is not True:
            raise ValueError("source lacks retained namespace qualification")
        if value["evidence_kind"] == ACQUISITION_KIND and source.get("acquisition_prefix") is not True:
            raise ValueError("source lacks grant acquisition qualification")
        validate_revoked(value)
        validate_precomponent_failures(source, value, value["revoked_bootstrap"]["journals"])
        if value["evidence_kind"] == ACQUISITION_KIND:
            for journal in value["revoked_bootstrap"]["journals"]:
                if "acquisition_prefix" in journal:
                    validate_source_prefix(source, journal)
    elif "revoked_bootstrap" in value:
        raise ValueError("revoked bootstrap grants require their distinct boundary")
    elif value["evidence_kind"] == RETAINED_KIND:
        validate_retained(value)
    elif "retained_revocation" in value:
        raise ValueError("retained revocation requires its distinct boundary")
    elif value["evidence_kind"] == DENIED_KIND:
        validate_denial(value)
    elif "denied_grant" in value or "denial_source_hashes" in value:
        raise ValueError("denied grant cannot be presented as an absent journal")
    if "settled_bootstrap" in value:
        from src.internal.domain_operation_store import harness

        closure = harness("settled_bootstrap").validate(value["settled_bootstrap"])
        if (
            value["evidence_kind"] not in {ACQUISITION_KIND, COMPONENTS_KIND, repeated_components.KIND}
            or any(closure["operation"][k] != value[k] for k in ("operation_id", "org_id", "workspace_id", "plan_digest"))
            or closure["call"]["idempotency_key"] != value["source_key"]
            or closure["call"]["fence_token"] != value["source_fence"]
            or closure["source_audit"]["recorded_at"] != value["source_acquired_at"]
        ):
            raise ValueError("settled bootstrap source differs from closure")
    if "attempt_extension" in value:
        acquisition = value["evidence_kind"] == ACQUISITION_KIND
        if acquisition:
            validate_source_prefix(source, extension_boundary(value))
        elif value["evidence_kind"] != RETAINED_KIND:
            raise ValueError("attempt extension requires retained revocation")
        validate_attempt_extension(value["attempt_extension"], acquisition=acquisition)
    return value
