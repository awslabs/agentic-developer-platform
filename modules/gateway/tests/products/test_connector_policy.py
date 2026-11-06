"""Policy closures use pinned requirements rather than producer visibility labels."""

import hashlib
import json

import pytest

from src.products.connector_policy import PolicyDependency, PolicyScopeError, derive_policy_envelope
from src.products.models import ConnectorPolicyRealm


def identity(record_id, *, tenant_id="tenant-one", project_id="project-one"):
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "source_connection_id": "connection-one",
        "record_type": "native_record",
        "record_id": record_id,
        "record_revision": "revision-one",
    }


def policy(ref, visibility, audience):
    return {"ref": ref, "revision": 1, "visibility": visibility, "audience_expression": audience}


def realm(*, private=False):
    descriptor = {
        "version": 1,
        "audience_expression": "current:team-readers" if not private else "current:owner",
        "policies": [
            policy("source-readers", "shared", "current:team-readers"),
            policy("execution-evidence", "private" if private else "shared", "current:owner" if private else "current:team-readers"),
            policy("ci-report", "private", "current:ci-maintainers"),
            policy("artifact", "private", "current:artifact-readers"),
            policy("execution-cost", "private", "current:billing-readers"),
        ],
    }
    digest = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ConnectorPolicyRealm(
        tenant_id="tenant-one",
        project_id="project-one",
        id="private-realm" if private else "shared-realm",
        revision=2 if private else 1,
        descriptor_digest=digest,
        descriptor=descriptor,
        source_policy_ref="source-readers",
        evidence_policy_ref="execution-evidence",
        state="active",
    )


def test_shared_source_and_evidence_require_both_policy_expressions():
    root = identity("record-one")
    envelope = derive_policy_envelope(realm(), root, [PolicyDependency(root, ())], asserted_audience="shared")
    assert envelope["identity"] == root
    assert envelope["dependency_identities"] == [root]
    assert envelope["realm_revision"] == 1 and envelope["audience"] == "shared"
    assert [(item["ref"], item["revision"], item["audience_expression"]) for item in envelope["required_policies"]] == [
        ("execution-evidence", 1, "current:team-readers"),
        ("source-readers", 1, "current:team-readers"),
    ]


def test_nested_private_ci_artifacts_and_cost_conjoin_without_changing_identity():
    root, ci, artifact, cost = [identity(record_id) for record_id in ("record-one", "ci-one", "artifact-one", "charge-one")]
    graph = [
        PolicyDependency(root, (), (ci, cost)),
        PolicyDependency(ci, ("ci-report",), (artifact,)),
        PolicyDependency(artifact, ("artifact",)),
        PolicyDependency(cost, ("execution-cost",)),
    ]
    envelope = derive_policy_envelope(realm(), root, graph)
    assert envelope["audience"] == "private" and envelope["identity"] == root
    assert envelope["dependency_identities"] == [artifact, cost, ci, root]
    assert [entry["ref"] for entry in envelope["required_policies"]] == [
        "artifact",
        "ci-report",
        "execution-cost",
        "execution-evidence",
        "source-readers",
    ]
    assert {entry["audience_expression"] for entry in envelope["required_policies"]} == {
        "current:team-readers",
        "current:ci-maintainers",
        "current:artifact-readers",
        "current:billing-readers",
    }
    with pytest.raises(PolicyScopeError, match="audience_mismatch"):
        derive_policy_envelope(realm(), root, graph, asserted_audience="shared")
    private = derive_policy_envelope(realm(private=True), root, graph, asserted_audience="private")
    assert private["identity"] == envelope["identity"]
    assert (private["realm_id"], private["realm_revision"]) == ("private-realm", 2)


@pytest.mark.parametrize(
    "nodes,error",
    [
        ([PolicyDependency(identity("record-one"), (), (identity("missing"),))], "missing_dependency"),
        ([PolicyDependency(identity("record-one"), ("unregistered",))], "unknown_policy"),
        ([PolicyDependency(identity("record-one"), (), (identity("record-one"),))], "invalid_dependency_graph"),
        ([PolicyDependency(identity("record-one"), (), (identity("foreign", tenant_id="tenant-other"),))], "foreign_dependency"),
    ],
)
def test_unknown_or_cross_scope_dependency_fails_closed(nodes, error):
    with pytest.raises(PolicyScopeError, match=error):
        derive_policy_envelope(realm(), identity("record-one"), nodes)


def test_descriptor_integrity_and_version_are_required():
    registered = realm()
    registered.descriptor = {**registered.descriptor, "audience_expression": "shared-to-everyone"}
    with pytest.raises(PolicyScopeError, match="invalid_realm_descriptor"):
        derive_policy_envelope(registered, identity("record-one"), [PolicyDependency(identity("record-one"), ())])


def test_shared_labels_with_different_reader_conditions_are_not_compatible():
    registered = realm()
    registered.descriptor["policies"][1]["audience_expression"] = "current:other-team"
    registered.descriptor_digest = hashlib.sha256(json.dumps(registered.descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = derive_policy_envelope(registered, identity("record-one"), [PolicyDependency(identity("record-one"), ())])
    assert result["audience"] == "private"
    assert {policy["audience_expression"] for policy in result["required_policies"]} == {
        "current:team-readers",
        "current:other-team",
    }
