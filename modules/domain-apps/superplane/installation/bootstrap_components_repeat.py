"""Source-qualified native cleanup and evidence for retained reader lineage."""

import importlib.util
import json
from pathlib import Path

from .bootstrap_denial import verify_absence
from .bootstrap_recovery import collect, source_contract
from .bootstrap_revoked import acquisition_contract, expired, verify_cleanup_absence
from .provider_authority import require


def contract():
    path = (
        Path(__file__).resolve().parents[4]
        / "modules/gateway/src/shared/domain_bootstrap_components_repeat.py"
    )
    spec = importlib.util.spec_from_file_location(
        "bootstrap_reader_component_contract", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _qualify(
    enrollment,
    original,
    descriptor,
    session,
    read_snapshot,
    read_expiry,
    source_options,
    *,
    complete,
):
    from harness_jobs import settled_bootstrap_repeat as repeat

    before, expiry = read_snapshot(), read_expiry()
    expired(descriptor, expiry)
    require("attempt_extension" not in descriptor)
    closure = repeat.validate(descriptor["settled_bootstrap"])
    evidence = collect(enrollment, original, descriptor, session, **source_options)
    schema = contract()
    schema.validate_source(source_contract().qualified_source(evidence["source_image"]))
    row = repeat.journals_bound(closure, before["journals"])
    if source_options.get("provenance_assumption") is not None:
        source_options["provenance_assumption"].bind_journal(row)
    identity = schema.transition(
        row,
        repeat.component_anchor(closure),
        complete=row["revoked"] if not complete else True,
        grant_identity=acquisition_contract().journal_identity,
    )
    if "inherited_components" in identity:
        require(
            source_contract()
            .qualified_source(evidence["source_image"])
            .get(schema.PARTIAL_SOURCE_FLAG)
            is True
        )
    require(before["journal"] == row)
    require(identity["generation"] == before["revoked_bootstrap"]["current_generation"])
    from harness_jobs import bootstrap_provenance_assumption as assumption

    assumption.validate_owner(
        {
            **evidence,
            "evidence_kind": schema.KIND,
            "revoked_bootstrap": before["revoked_bootstrap"],
        }
    )
    require(before == read_snapshot() and expiry == read_expiry())
    return schema, evidence, before, row, identity, expiry


def qualify_cleanup(
    enrollment,
    original,
    descriptor,
    session,
    *,
    read_snapshot,
    read_expiry,
    **source_options,
):
    schema, evidence, before, _row, identity, _expiry = _qualify(
        enrollment,
        original,
        descriptor,
        session,
        read_snapshot,
        read_expiry,
        source_options,
        complete=False,
    )
    return {
        **identity,
        "evidence_kind": schema.KIND,
        "predecessor": before["revoked_bootstrap"]["predecessor"],
        "settled_bootstrap": evidence["settled_bootstrap"],
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        **(
            {"provenance_assumption": evidence["provenance_assumption"]}
            if "provenance_assumption" in evidence
            else {}
        ),
    }


def cleanup_revalidator(*args, **kwargs):
    from superplane_bootstrap.closed_cleanup_boundary import (
        cleanup_revalidator as native,
    )

    return native(*args, **kwargs)


def collect_components(
    enrollment,
    original,
    descriptor,
    session,
    *,
    read_snapshot,
    read_expiry,
    cleanup_permit=None,
    **source_options,
):
    from harness_jobs import settled_bootstrap_repeat as repeat

    schema, evidence, before, row, identity, expiry = _qualify(
        enrollment,
        original,
        descriptor,
        session,
        read_snapshot,
        read_expiry,
        source_options,
        complete=True,
    )
    evidence.update(
        evidence_kind=schema.KIND, revoked_bootstrap=before["revoked_bootstrap"]
    )
    require(
        schema.validate(
            evidence,
            grant_identity=acquisition_contract().journal_identity,
            repeat_contract=repeat,
        )
        == identity
    )
    eks = session.client("eks", region_name=enrollment["authority"]["region"])
    entries = verify_absence(
        eks,
        json.loads(row["plan_json"]),
        cluster_name=row["cluster_arn"].split("/")[-1],
    )
    # A service verifier must not survive owner proof publication. This is an
    # independent owner observation, not the service's self-reported result.
    require(
        all(
            entry["principalArn"] != enrollment["authority"]["role_arn"]
            and "superplane-cleanup-permit" not in entry.get("tags", {})
            for entry in entries
        )
    )
    verify_cleanup_absence(session, row)
    if cleanup_permit is not None:
        from .closed_cleanup import verify_teardown

        verify_teardown(enrollment, cleanup_permit, session)
    require(before == read_snapshot() and expiry == read_expiry())
    return evidence
