"""A proven pre-install permission failure retains its attributed inventory."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from superplane_bootstrap.acquisition_recovery import recover_acquisition
from superplane_bootstrap.errors import BootstrapRefused

from installation.bootstrap_recovery import source_contract
from installation.bootstrap_revoked import acquisition_contract

from .test_acquisition_recovery_postgres import acquisition  # noqa: F401
from .test_authority_journal_postgres import authority  # noqa: F401
from .test_registry_postgres import database, loop, schema_ddl, server  # noqa: F401


@pytest.fixture
def marked(acquisition):  # noqa: F811
    journal, registry, backend, resources, _, row = acquisition
    contract = acquisition_contract()
    progress = json.loads(row["progress_json"])
    marker = {
        "workspace_id": journal.target.workspace_id,
        "prerequisites": [
            {
                "kind": kind,
                "identifier": identifier,
                "workspace_id": journal.target.workspace_id,
                "ownership": ownership,
                "reason": "verified original prerequisite",
            }
            for kind, identifier, ownership in (
                (
                    "EksAccessEntry",
                    progress["supervisor-entry"]["identity"]["arn"],
                    "adp-created",
                ),
                ("EksPublicEndpoint", journal.target.cluster_arn, "adopted"),
                ("SecurityGroupRule/private-sts", "sgr-0123456789abcdef0", "adopted"),
            )
        ],
    }
    progress["prerequisite_inventory"] = marker
    with journal.fenced():
        journal.write_locked(progress)
    row["progress_json"] = json.dumps(progress)
    identity = contract.journal_identity(row, complete=False)
    image = "qualified@sha256:9fd2a13be7c5e147fe00cf5433fcd792ab751d27cd5ce86206c1d9bc326212bb"
    source = source_contract().qualified_source(image)
    now = datetime.now(UTC)
    outer = {
        "source_image": image,
        "source_revision": source["revision"],
        "source_hashes": source["hashes"],
        "source_pod_name": "original-worker",
        "source_pod_uid": "original-worker-uid",
        "source_audit": {
            "log_group": "/aws/eks/management/cluster",
            "creation_event_id": "created",
            "terminal_event_ids": ["ended"],
        },
        "source_acquired_at": (now - timedelta(hours=2)).isoformat(),
        "checked_at": now.isoformat(),
    }
    proof = {
        "kind": contract.PERMISSION_FAILURE,
        **{
            k: outer[k]
            for k in (
                "source_image",
                "source_revision",
                "source_pod_name",
                "source_pod_uid",
            )
        },
        **{
            k: identity[k]
            for k in (
                "operation_id",
                "org_id",
                "workspace_id",
                "generation",
                "plan_sha256",
                "precomponent_progress_sha256",
            )
        },
        "source_hashes_sha256": contract.digest(source["hashes"]),
        "source_audit_sha256": contract.digest(outer["source_audit"]),
        "prerequisite_inventory_sha256": contract.digest(marker),
        "log_group": "/aws/containerinsights/management/application",
        "failure_event_id": "failure",
        "failure_log_stream": "original-worker-stream",
        "failure_message_sha256": "f" * 64,
        "stopped_event_id": "stopped",
        "stopped_message_sha256": "e" * 64,
        "failed_at": (now - timedelta(hours=1)).isoformat(),
        "stopped_at": (now - timedelta(minutes=59)).isoformat(),
    }
    outer["precomponent_failures"] = {journal.generation: proof}
    contract.validate_precomponent_failures(source, outer, [identity])
    evidence = {
        **identity,
        "evidence_kind": contract.KIND,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "precomponent_failures": outer["precomponent_failures"],
        "precomponent_source": {
            k: outer[k]
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
    return journal, registry, backend, resources, evidence, row, outer, source


@pytest.mark.parametrize("acquisition", [(25, "revoke_intent")], indirect=True)
@pytest.mark.parametrize(
    "fault",
    [None, "missing-proof", "proof-source", "proof-progress", "marker-during-cleanup"],
)
def test_native_cleanup_preserves_marker_and_requires_original_proof(marked, fault):
    journal, registry, backend, resources, evidence, _row, outer, source = marked
    marker = copy.deepcopy(evidence["prerequisite_inventory"])
    if fault == "missing-proof":
        evidence.pop("precomponent_failures")
    elif fault == "proof-source":
        evidence["precomponent_source"]["source_pod_uid"] = "replacement-pod"
    elif fault == "proof-progress":
        evidence["precomponent_failures"][journal.generation][
            "precomponent_progress_sha256"
        ] = "b" * 64
    reads = 0

    def revalidate():
        nonlocal reads
        reads += 1
        if fault == "marker-during-cleanup" and reads == 2:
            _, progress = journal.read_locked()
            progress["prerequisite_inventory"]["prerequisites"][-1]["reason"] = (
                "changed"
            )
            journal.write_locked(progress)
        return copy.deepcopy(evidence)

    def run():
        return recover_acquisition(
            journal,
            registry,
            backend,
            backend,
            revalidate=revalidate,
            verify_absence=lambda _: set(resources) == {"workspace-namespace"},
        )

    if fault:
        with pytest.raises(BootstrapRefused):
            run()
        assert backend.deleted == []
        assert journal.store.execute(
            "SELECT * FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
            journal.key,
        )
        return
    assert run()["revoked"] is True
    assert set(resources) == {"workspace-namespace"}
    rows = journal.store.execute(
        "SELECT * FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id",
        journal.key,
    )
    assert len(rows) == 1
    completed = acquisition_contract().journal_identity(dict(rows[0]), complete=True)
    assert json.loads(rows[0]["progress_json"])["prerequisite_inventory"] == marker
    assert completed["prerequisite_inventory"] == marker
    assert (
        completed["precomponent_progress_sha256"]
        == evidence["acquiring_progress_sha256"]
    )
    acquisition_contract().validate_precomponent_failures(source, outer, [completed])


@pytest.mark.parametrize("acquisition", [(25, "revoke_intent")], indirect=True)
@pytest.mark.parametrize(
    "fault",
    [
        "owned-network",
        "different-supervisor",
        "different-cluster",
        "duplicate",
        "unknown-field",
        "partial",
        "components",
        "retain-workspace",
        "already-cleaned",
        "earlier-revoke-intent",
    ],
)
def test_marker_does_not_relax_original_journal_boundary(marked, fault):
    *_, row, _, _ = marked
    progress = json.loads(row["progress_json"])
    items = progress["prerequisite_inventory"]["prerequisites"]
    if fault == "owned-network":
        items[-1]["ownership"] = "adp-created"
    elif fault == "different-supervisor":
        items[0]["identifier"] += "replacement"
    elif fault == "different-cluster":
        items[1]["identifier"] += "replacement"
    elif fault == "duplicate":
        items[-1] = items[0]
    elif fault == "unknown-field":
        items[-1]["removable"] = True
    elif fault == "partial":
        del progress["cleanup-system-binding"]
    elif fault == "components":
        progress["components"] = {}
    elif fault == "retain-workspace":
        progress["retain_workspace"] = False
    elif fault == "already-cleaned":
        progress["cleanup-system-binding"]["phase"] = "revoked"
    else:
        progress["cleanup-system-role"]["phase"] = "revoke_intent"
    row["progress_json"] = json.dumps(progress)
    with pytest.raises(ValueError):
        acquisition_contract().journal_identity(row, complete=False)


@pytest.mark.parametrize("acquisition", [(25, "revoke_intent")], indirect=True)
@pytest.mark.parametrize(
    "fault",
    [
        "unqualified-source",
        "missing",
        "foreign-pod",
        "foreign-journal",
        "changed-inventory",
        "changed-audit",
        "wrong-log-group",
        "future-failure",
    ],
)
def test_gateway_requires_proof_bound_to_original_source_and_journal(marked, fault):
    journal, _, _, _, evidence, _, outer, source = marked
    proof = outer["precomponent_failures"][journal.generation]
    if fault == "unqualified-source":
        source.pop("precomponent_permission_refusal")
    elif fault == "missing":
        outer.pop("precomponent_failures")
    else:
        key, value = {
            "foreign-pod": ("source_pod_uid", "other-pod"),
            "foreign-journal": ("generation", "a" * 64),
            "changed-inventory": ("prerequisite_inventory_sha256", "a" * 64),
            "changed-audit": ("source_audit_sha256", "a" * 64),
            "wrong-log-group": (
                "log_group",
                "/aws/containerinsights/other/application",
            ),
            "future-failure": (
                "failed_at",
                (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            ),
        }[fault]
        proof[key] = value
    with pytest.raises(ValueError):
        acquisition_contract().validate_precomponent_failures(source, outer, [evidence])
