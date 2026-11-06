# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Real shared operation and domain journals gate an original bootstrap retry."""

import json

import pytest

from tests.internal.test_domain_bootstrap_retry import bootstrap_interrupted as bootstrap_interrupted
from tests.internal.test_domain_bootstrap_retry import denied_bootstrap as denied_bootstrap
from tests.internal.test_domain_bootstrap_retry import unstarted_bootstrap as unstarted_bootstrap
from tests.internal.test_domain_dispatch_reauthorization import recovery as recovery
from tests.internal.test_paid_domain_operation_postgres import (
    ORG,
)
from tests.internal.test_paid_domain_operation_postgres import (
    kubernetes as kubernetes,
)
from tests.internal.test_paid_domain_operation_postgres import (
    paid as paid,
)
from tests.internal.test_paid_domain_operation_postgres import (
    pg_server as pg_server,
)
from tests.internal.test_paid_domain_operation_postgres import (
    pg_url as pg_url,
)
from tests.internal.test_paid_domain_operation_postgres import (
    store as store,
)


def registrar_row(previous):
    import hashlib

    claim = "d" * 64
    generation = hashlib.sha256((previous["operation_id"] + ":" + claim).encode()).hexdigest()
    entry = json.loads(previous["plan_json"])["grants"][0]
    stem = "sp-bootstrap-" + generation[:24]
    entry.update(
        generation=generation,
        groups=[stem + ":registrar"],
        username=stem + ":registrar:{{SessionName}}",
        client_token=hashlib.sha256((generation + ":registrar").encode()).hexdigest(),
    )
    policy = {
        **entry,
        "key": "registrar-policy",
        "kind": "eks-policy",
        "policy_arn": "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
        "scope": {"type": "cluster"},
    }
    namespace = {
        "key": "workspace-namespace",
        "kind": "kubernetes",
        "generation": generation,
        "cluster_arn": entry["cluster_arn"],
        "body": {"kind": "Namespace"},
    }
    identity = {k: entry[k] for k in ("generation", "groups", "username")}
    identity["arn"] = entry["cluster_arn"].replace(":cluster/", ":access-entry/") + "/role/123456789012/registrar/immutable"
    progress = {
        "phase": "revoked",
        "complete": True,
        "registrar-entry": {"phase": "revoked", "identity": identity},
        "registrar-policy": {
            "phase": "revoked",
            "identity": {
                **identity,
                "policy_arn": policy["policy_arn"],
                "scope": {"type": "cluster", "namespaces": []},
                "associated_at": "2026-10-13T10:15:58Z",
            },
        },
    }
    return {
        **previous,
        "claim": claim,
        "generation": generation,
        "plan_json": json.dumps({"version": 1, "grants": [entry, policy, namespace]}),
        "progress_json": json.dumps(progress),
    }


def acquisition_row(row, *, complete=False):
    """Synthetic native grant prefix with a positively revoked lost acknowledgement."""
    import hashlib

    from src.shared.domain_bootstrap_acquisition import PREFIX

    plan, progress = json.loads(row["plan_json"]), json.loads(row["progress_json"])
    generation = row["generation"]
    stem = "sp-bootstrap-" + generation[:24]
    namespace = plan["grants"][2]
    namespace["body"]["metadata"]["labels"] = {"superplane.aws-e/bootstrap-owner": row["workspace_id"]}
    progress["workspace-namespace"] = {
        "phase": "adopted",
        "identity": {"uid": "retained-namespace", "generation": "a" * 64, "digest": "b" * 64, "labels": namespace["body"]["metadata"]["labels"]},
    }
    for key in PREFIX[3:]:
        actor = key.split("-")[0]
        lifetime = "workspace" if actor == "supervisor" else "temporary"
        if key.endswith("-entry"):
            spec = {
                **plan["grants"][0],
                "key": key,
                "actor": actor,
                "lifetime": lifetime,
                "principal_arn": "arn:aws:iam::123456789012:role/" + actor,
                "groups": [stem + ":" + actor],
                "username": stem + ":" + actor + ":{{SessionName}}",
                "client_token": hashlib.sha256((generation + ":" + actor).encode()).hexdigest(),
            }
            identity = {k: spec[k] for k in ("generation", "groups", "username")}
            identity["arn"] = row["cluster_arn"].replace(":cluster/", ":access-entry/") + "/role/123456789012/" + actor + "/immutable"
        else:
            spec = {
                "key": key,
                "kind": "kubernetes",
                "actor": actor,
                "lifetime": lifetime,
                "generation": generation,
                "cluster_arn": row["cluster_arn"],
                "body": {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": ("ClusterRole" if "-cluster-" in key else "Role") + ("Binding" if key.endswith("binding") else ""),
                    "metadata": {"name": stem + "-" + key},
                },
            }
            identity = {"uid": key + "-uid", "generation": generation, "digest": "e" * 64}
        plan["grants"].append(spec)
        if complete or len(plan["grants"]) <= 14:
            progress[key] = {"phase": "revoked", "identity": None if key == "installer-namespace-role" else identity}
            if complete:
                progress[key]["identity"] = identity
    if complete:
        from src.shared.domain_bootstrap_acquisition import CLEANUP_PREFIX

        for key in CLEANUP_PREFIX:
            metadata = {"name": stem + "-retirement", "annotations": {"superplane.aws-e/authority-generation": generation}}
            if key == "retirement-fence-policy":
                body = {
                    "apiVersion": "admissionregistration.k8s.io/v1",
                    "kind": "ValidatingAdmissionPolicy",
                    "metadata": metadata,
                    "spec": {"validations": [{"expression": "true", "message": "dedicated workspace retirement has closed workload admission"}]},
                }
            elif key == "retirement-fence-binding":
                body = {
                    "apiVersion": "admissionregistration.k8s.io/v1",
                    "kind": "ValidatingAdmissionPolicyBinding",
                    "metadata": metadata,
                    "spec": {"policyName": stem + "-retirement", "validationActions": ["Deny"]},
                }
            else:
                scope, kind = key.removeprefix("cleanup-").split("-")
                metadata["name"] = stem + "-cleanup-" + scope
                if scope != "cluster":
                    metadata["namespace"] = "superplane-system" if scope == "namespace" else "kube-system"
                body = {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": ("ClusterRole" if scope == "cluster" else "Role") + ("Binding" if kind == "binding" else ""),
                    "metadata": metadata,
                }
            plan["grants"].append(
                {
                    "key": key,
                    "kind": "kubernetes",
                    "actor": "registrar",
                    "lifetime": "workspace",
                    "original_allocation_id": "original-allocation",
                    "generation": generation,
                    "cluster_arn": row["cluster_arn"],
                    "body": body,
                }
            )
            progress[key] = {"phase": "revoked", "identity": {"uid": key + "-uid", "generation": generation, "digest": "e" * 64}}
    return {**row, "plan_json": json.dumps(plan), "progress_json": json.dumps(progress)}


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing-old",
        "extra",
        "namespace",
        "unrevoked",
        "reservation",
        "membership",
        "receipt",
        "source",
        "absent-label",
        "changed-old",
        "artifact",
    ],
)
@pytest.mark.parametrize(
    "source_image",
    [
        "sha256:973f97e714f49acd37a9ad00aa761845fd12a17a9d26fd01282c7a60797c0008",
        "sha256:9b77966b012974cd2610d1dc45ed1faaf2ebb33b2e68a7ba09d7c1c8883b4827",
        "sha256:042f6e3dbb6e7ac7336b0bab10e9b6992ae2e7b343add2d2984a71a1e0916adc",
        "sha256:5d2a7e122882b6590fce923bda9134ab1343271d5fe556e82e35cb56f5ad21ca",
        "sha256:dbbf2eb4b51c61e387e6e4d13a2b141625ade8fa265d1d293fa6fcf0b968f5f1",
    ],
)
async def test_registrar_retry_preserves_all_prior_journals_and_refuses_kubernetes_intent(denied_bootstrap, monkeypatch, fault, source_image):
    from src.internal import domain_bootstrap_recovery as bootstrap
    from src.internal.domain_operation_dispatch import paid_operation
    from src.internal.domain_operation_store import binding_for, harness
    from src.shared.domain_bootstrap_boundary import QUALIFIED_SOURCES
    from src.shared.domain_bootstrap_recovery import record_id, validate
    from src.shared.domain_bootstrap_revoked import ACQUISITION_KIND, KIND, NAMESPACE_KIND

    paid, body, evidence, old = denied_bootstrap
    response = await paid.post("/recover-unstarted-bootstrap", body, role="producer")
    assert response.status_code == 200, response.text
    binding = binding_for("superplane", ORG)
    monkeypatch.setattr(bootstrap, "operation_connect", paid.connect)
    operation = await paid_operation(binding, body["operation_id"])
    row = registrar_row(old)
    kind = KIND
    namespace_retained = QUALIFIED_SOURCES[source_image].get("namespace_prefix") is True
    if namespace_retained:
        plan = json.loads(row["plan_json"])
        plan["grants"][2].update(actor="registrar", lifetime="resource")
        plan["grants"][2]["body"].update(apiVersion="v1", metadata={"name": "superplane-system"})
        row["plan_json"] = json.dumps(plan)
        progress = json.loads(row["progress_json"])
        progress["workspace-namespace"] = {"phase": "grant_intent"}
        row["progress_json"] = json.dumps(progress)
        kind = NAMESPACE_KIND
    acquisition = QUALIFIED_SOURCES[source_image].get("acquisition_prefix") is True
    if acquisition:
        row = acquisition_row(row)
        kind = ACQUISITION_KIND
    async with paid.connect() as c:
        prior = dict(await c.fetchrow("SELECT * FROM harness_provider_retries"))
        await c.execute(
            "INSERT INTO workspace_bootstrap_authority VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            *[
                row[k]
                for k in ("workspace_id", "operation_id", "org_id", "cluster_arn", "generation", "claim", "revoked", "plan_json", "progress_json")
            ],
        )
    key, fence = prior["replacement_key"], prior["source_fence"] + 2
    snapshot = await bootstrap.registrar_snapshot(binding, operation, key, fence, retained_namespace=namespace_retained, acquisition=acquisition)
    assert len(snapshot["revoked_bootstrap"]["journals"]) == 2
    source = QUALIFIED_SOURCES[source_image]
    assert source["registrar_prefix"] is True
    current = {
        **evidence,
        "evidence_kind": kind,
        "source_key": key,
        "source_fence": fence,
        "source_image": "qualified@" + source_image,
        "source_revision": source["revision"],
        "source_hashes": source["hashes"],
        "revoked_bootstrap": snapshot["revoked_bootstrap"],
    }
    current.pop("denied_grant")
    current.pop("denial_source_hashes")
    current["record_id"] = record_id(current)
    request = harness("identity").decode_payload(operation["request_payload"])
    async with paid.connect() as c:
        if fault == "missing-old":
            await c.execute("DELETE FROM workspace_bootstrap_authority WHERE generation=$1", old["generation"])
        elif fault == "extra":
            await c.execute(
                "INSERT INTO workspace_bootstrap_authority SELECT * FROM workspace_bootstrap_authority WHERE generation=$1", row["generation"]
            )
        elif fault == "namespace":
            progress = json.loads(row["progress_json"])
            progress["workspace-namespace"] = {"phase": "revoked"}
            await c.execute("UPDATE workspace_bootstrap_authority SET progress_json=$1 WHERE generation=$2", json.dumps(progress), row["generation"])
        elif fault == "unrevoked":
            await c.execute("UPDATE workspace_bootstrap_authority SET revoked=false WHERE generation=$1", row["generation"])
        elif fault == "changed-old":
            await c.execute("UPDATE workspace_bootstrap_authority SET plan_json='{}' WHERE generation=$1", old["generation"])
        elif fault in {"reservation", "membership"}:
            table = "workspace_bootstrap_reservations" if fault == "reservation" else "cluster_memberships"
            await c.execute("INSERT INTO " + table + " VALUES($1)", body["workspace_id"])
        elif fault == "artifact":
            await c.execute("INSERT INTO workspace_lifecycle_artifacts(source_operation_id) VALUES($1)", body["operation_id"])
    if fault == "receipt":
        current["revoked_bootstrap"]["predecessor"]["proof_sha256"] = "0" * 64
    elif fault == "source":
        image, source = next(iter(QUALIFIED_SOURCES.items()))
        current.update(source_image="qualified@" + image, source_revision=source["revision"], source_hashes=source["hashes"])
    elif fault == "absent-label":
        current["evidence_kind"] = "bootstrap-journal-barrier-v1"
    if fault:
        with pytest.raises(Exception):
            validate(current)
            await bootstrap.untouched(binding, operation, request, current)
    else:
        validate(current)
        assert await bootstrap.untouched(binding, operation, request, current)
        proof = {
            "evidence_kind": kind,
            "owner_evidence": current,
            "revoked_bootstrap": current["revoked_bootstrap"],
            "bootstrap_mutations_started": True,
            "bootstrap_grants_created": True,
            "bootstrap_journal_revoked": True,
            "bootstrap_components_started": namespace_retained,
        }
        harness("provider_retry").bootstrap_revoked_predecessor(proof, prior)
    async with paid.connect() as c:
        assert await c.fetchval("SELECT proof FROM harness_provider_retries") == prior["proof"]
        assert await c.fetchval("SELECT attempts FROM harness_operation_leases") == 1
