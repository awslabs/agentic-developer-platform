"""Owner evidence after native revocation of a pre-namespace registrar prefix."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

from .bootstrap_denial import verify_absence
from .bootstrap_recovery import collect, source_contract
from .provider_authority import require


def revoked_contract():
    path = (
        Path(__file__).resolve().parents[4]
        / "modules/gateway/src/shared/domain_bootstrap_revoked.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_revoked_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def acquisition_contract():
    path = (
        Path(__file__).resolve().parents[4]
        / "modules/gateway/src/shared/domain_bootstrap_acquisition.py"
    )
    spec = importlib.util.spec_from_file_location(
        "bootstrap_acquisition_contract", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_cleanup_absence(session, row):
    """The disposable cleanup principal must never survive into a new attempt."""
    from botocore.exceptions import ClientError

    name = "sp-acquisition-cleanup-" + row["generation"][:24]
    arn = "arn:aws:iam::" + row["cluster_arn"].split(":")[4] + ":role/" + name
    checks = (
        (session.client("iam").get_role, {"RoleName": name}, "NoSuchEntity"),
        (
            session.client(
                "eks", region_name=row["cluster_arn"].split(":")[3]
            ).describe_access_entry,
            {"clusterName": row["cluster_arn"].split("/")[-1], "principalArn": arn},
            "ResourceNotFoundException",
        ),
    )
    for call, arguments, code in checks:
        try:
            call(**arguments)
        except ClientError as error:
            require(error.response.get("Error", {}).get("Code") == code)
        else:
            require(False)


def expired(descriptor, value):
    require(
        value.get("original_authority_expired") is True
        and value.get("issued_sessions_expired") is True
        and datetime.now(UTC)
        >= datetime.fromisoformat(value["earliest_owner_evidence_at"])
        and all(
            value[k] == descriptor[k]
            for k in ("operation_id", "source_principal", "source_key", "source_fence")
        )
    )
    require(descriptor.get("settled_bootstrap") == value.get("settled_bootstrap"))


def permission_failures(enrollment, evidence, source, journals, session):
    """Fresh attributed log evidence is required only for the qualified marker."""
    marked = [j for j in journals if "prerequisite_inventory" in j]
    if not marked:
        return
    from .bootstrap_permission_failure import collect as collect_failure

    contract = acquisition_contract()
    for identity in marked:
        contract.validate_source_prefix(source, identity)
    evidence["precomponent_failures"] = {
        identity["generation"]: collect_failure(
            session,
            evidence=evidence,
            identity=identity,
            source=source,
            management_cluster=enrollment["management_cluster"],
            management_region=enrollment.get(
                "management_region", enrollment["authority"]["region"]
            ),
        )
        for identity in marked
    }
    contract.validate_precomponent_failures(source, evidence, journals)


def collect_revoked(
    enrollment,
    original,
    descriptor,
    session,
    *,
    read_snapshot,
    read_expiry,
    retained_namespace=False,
    acquisition=False,
    **source_options,
):
    before, expiry = read_snapshot(), read_expiry()
    expired(descriptor, expiry)
    evidence = collect(enrollment, original, descriptor, session, **source_options)
    require(
        source_contract()
        .qualified_source(evidence["source_image"])
        .get("registrar_prefix")
        is True
    )
    schema = revoked_contract()
    if acquisition:
        require(
            source_contract()
            .qualified_source(evidence["source_image"])
            .get("acquisition_prefix")
            is True
        )
    evidence.update(
        evidence_kind=schema.ACQUISITION_KIND
        if acquisition
        else schema.NAMESPACE_KIND
        if retained_namespace
        else schema.KIND,
        revoked_bootstrap=before["revoked_bootstrap"],
    )
    permission_failures(
        enrollment,
        evidence,
        source_contract().qualified_source(evidence["source_image"]),
        evidence["revoked_bootstrap"]["journals"],
        session,
    )
    if "attempt_extension" in descriptor:
        require(acquisition)
        boundary = acquisition_contract().extension_boundary(evidence)
        acquisition_contract().validate_source_prefix(
            source_contract().qualified_source(evidence["source_image"]), boundary
        )
        source_contract().validate_attempt_extension(
            descriptor["attempt_extension"],
            subject=enrollment["authority"]["subject"],
            attempts=expiry["attempts"],
            ceiling=expiry["max_attempts"],
            acquisition=True,
        )
        evidence["attempt_extension"] = descriptor["attempt_extension"]
    schema.validate_revoked(evidence)
    if acquisition:
        source = source_contract().qualified_source(evidence["source_image"])
        for identity in evidence["revoked_bootstrap"]["journals"]:
            if "acquisition_prefix" in identity:
                acquisition_contract().validate_source_prefix(source, identity)
    rows = before["journals"]
    if "settled_bootstrap" in evidence:
        from harness_jobs.settled_bootstrap import journal_bound

        require(len(rows) == 1)
        journal_bound(evidence["settled_bootstrap"], rows[0])
    require(
        {r["generation"] for r in rows}
        == {j["generation"] for j in evidence["revoked_bootstrap"]["journals"]}
    )
    eks = session.client("eks", region_name=enrollment["authority"]["region"])
    for row in rows:
        require(row["revoked"] is True)
        verify_absence(
            eks,
            json.loads(row["plan_json"]),
            cluster_name=row["cluster_arn"].split("/")[-1],
        )
        if acquisition:
            verify_cleanup_absence(session, row)
    require(before == read_snapshot() and expiry == read_expiry())
    return evidence


def qualify_cleanup(
    enrollment,
    original,
    descriptor,
    session,
    *,
    read_snapshot,
    read_expiry,
    acquisition=False,
    **source_options,
):
    """Read-only source qualification before a native cleanup composition starts.

    No recovery evidence is published here. The caller must re-read the expired
    execution and snapshot on every cleanup boundary while holding its native lock.
    """
    before, expiry = read_snapshot(), read_expiry()
    expired(descriptor, expiry)
    evidence = collect(enrollment, original, descriptor, session, **source_options)
    require(
        source_contract()
        .qualified_source(evidence["source_image"])
        .get("registrar_prefix")
        is True
    )
    row = before["journal"]
    if "settled_bootstrap" in evidence:
        from harness_jobs.settled_bootstrap import journal_bound

        journal_bound(evidence["settled_bootstrap"], row)
    schema = acquisition_contract() if acquisition else revoked_contract()
    if acquisition:
        require(
            source_contract()
            .qualified_source(evidence["source_image"])
            .get("acquisition_prefix")
            is True
        )
    identity = schema.journal_identity(row, complete=False)
    if acquisition:
        schema.validate_source_prefix(
            source_contract().qualified_source(evidence["source_image"]), identity
        )
        permission_failures(
            enrollment,
            evidence,
            source_contract().qualified_source(evidence["source_image"]),
            [identity],
            session,
        )
    require(
        all(
            identity[k] == descriptor[k]
            for k in ("operation_id", "org_id", "workspace_id")
        )
    )
    require(before == read_snapshot() and expiry == read_expiry())
    return {
        **identity,
        **(
            {
                "precomponent_failures": evidence["precomponent_failures"],
                "precomponent_source": {
                    k: evidence[k]
                    for k in (
                        "source_image",
                        "source_revision",
                        "source_hashes",
                        "source_pod_name",
                        "source_pod_uid",
                        "source_audit",
                    )
                },
            }
            if "prerequisite_inventory" in identity
            else {}
        ),
        "evidence_kind": schema.KIND,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
    }
