"""Fresh owner qualification of a later unstarted attempt after native revocation."""

from datetime import datetime, timezone

from .bootstrap_recovery import collect, source_contract
from .provider_authority import require


def collect_retained(
    enrollment,
    original,
    descriptor,
    session,
    *,
    read_retained,
    read_expiry,
    **source_options,
):
    # Trusted live readers are supplied by the installation service composition;
    # caller-provided descriptors alone never establish protected DB state.
    before = read_retained()
    expiry = read_expiry()
    require(
        expiry.get("original_authority_expired") is True
        and expiry.get("issued_sessions_expired") is True
        and datetime.now(timezone.utc)
        >= datetime.fromisoformat(expiry["earliest_owner_evidence_at"])
        and all(
            expiry[k] == descriptor[k]
            for k in ("operation_id", "source_principal", "source_key", "source_fence")
        )
    )
    evidence = collect(enrollment, original, descriptor, session, **source_options)
    require(before == read_retained())
    extension = descriptor.get("attempt_extension")
    if "attempt_extension" in descriptor:
        source_contract().validate_attempt_extension(
            extension,
            subject=enrollment["authority"]["subject"],
            attempts=expiry["attempts"],
            ceiling=expiry["max_attempts"],
        )
        require(expiry == read_expiry())
    require(
        before["source_fence"] < descriptor["source_fence"]
        and all(
            before["journal"][k] == descriptor[k]
            for k in ("operation_id", "org_id", "workspace_id")
        )
    )
    return {
        **evidence,
        "evidence_kind": "bootstrap-retained-revocation-barrier-v1",
        "retained_revocation": before,
        **({"attempt_extension": extension} if extension is not None else {}),
    }
