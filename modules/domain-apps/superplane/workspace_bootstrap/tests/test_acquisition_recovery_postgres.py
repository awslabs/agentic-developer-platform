"""A stopped RBAC acquisition revokes only its own grants under real SQL locks."""

import copy
import json
from dataclasses import replace

import pytest

from installation.bootstrap_revoked import acquisition_contract
from superplane_bootstrap.acquisition_recovery import KIND, recover_acquisition
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.grant_plan import BootstrapRelease, compile_grants
from superplane_bootstrap.kube_grants import _digest

from .test_authority_journal_postgres import authority  # noqa: F401
from .test_registry_postgres import database, loop, schema_ddl, server  # noqa: F401


@pytest.fixture(
    params=[
        (14, "grant_intent"),
        (25, "grant_intent"),
        (25, "revoke_intent"),
        (18, "grant_intent", True),
        (21, "revoke_intent", True),
        (29, "revoke_intent", True),
    ]
)
def acquisition(authority, request):  # noqa: F811
    journal, registry, _ = authority
    count, interrupted_phase = request.param[:2]
    reader = len(request.param) == 3 and request.param[2]
    if count > (21 if reader else 17):
        journal = replace(journal, original_allocation_id="original-managed-allocation")
    plan = compile_grants(
        journal,
        BootstrapRelease(
            "workspace",
            "superplane-controller" if reader else "worker",
            "controller",
            "v1.35",
            ("jobs.example.com",),
            reader_renewal=reader,
        ),
        {
            a: f"arn:aws:iam::{journal.target.account_id}:role/{a}"
            for a in ("registrar", "installer", "supervisor")
        },
    )
    progress = {"phase": "acquiring"}
    resources = {}
    for spec in plan["grants"][:count]:
        key = spec["key"]
        if spec["kind"].startswith("eks-"):
            identity = {
                "generation": journal.generation,
                "groups": spec["groups"],
                "username": spec["username"],
                "arn": journal.target.cluster_arn.replace(":cluster/", ":access-entry/")
                + "/role/"
                + journal.target.account_id
                + "/"
                + spec["actor"]
                + "/immutable",
            }
            if spec["kind"] == "eks-policy":
                identity.update(
                    policy_arn=spec["policy_arn"],
                    scope=spec["scope"],
                    associated_at="2026-10-14T10:00:00Z",
                )
        else:
            identity = {
                "generation": journal.generation,
                "uid": key + "-uid",
                "digest": _digest(spec["body"]),
            }
        phase = "granted"
        if key == "workspace-namespace":
            identity.update(
                generation="a" * 64, labels=spec["body"]["metadata"]["labels"]
            )
            phase = "adopted"
        resources[key] = identity
        progress[key] = {"phase": phase, "identity": copy.deepcopy(identity)}
    # The final create's acknowledgement was lost. Its real identity must be
    # observed and generation-verified before it can be deleted.
    last = plan["grants"][count - 1]["key"]
    progress[last] = {"phase": interrupted_phase}
    if interrupted_phase == "revoke_intent":
        progress["phase"] = "revoking"
        progress[last]["identity"] = copy.deepcopy(resources[last])
    journal.begin(plan)
    with journal.fenced():
        journal.write_locked(progress)
    row = {
        **{
            k: getattr(journal.target, k)
            for k in ("org_id", "workspace_id", "cluster_arn")
        },
        "operation_id": journal.binding.operation_id,
        "generation": journal.generation,
        "claim": journal.claim,
        "revoked": False,
        "plan_json": json.dumps(plan),
        "progress_json": json.dumps(progress),
    }
    evidence = {
        **acquisition_contract().journal_identity(row, complete=False),
        "evidence_kind": KIND,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
    }

    class Backend:
        def __init__(self):
            self.deleted = []
            self.residual = False

        def observe(self, spec):
            return copy.deepcopy(resources.get(spec["key"]))

        def verify(self, spec, identity):
            if identity.get("generation") != spec["generation"]:
                raise BootstrapRefused("foreign grant")

        def delete(self, spec, identity):
            assert resources[spec["key"]] == identity
            self.deleted.append(spec["key"])
            if not self.residual:
                del resources[spec["key"]]

    return journal, registry, Backend(), resources, evidence, row


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "changed-uid",
        "lost-ack-foreign",
        "changed-namespace",
        "residual",
        "live",
        "resolver",
        "component",
        "plan",
        "late-grant",
    ],
)
def test_native_acquisition_recovery_retains_namespace_and_history(acquisition, fault):
    journal, registry, backend, resources, evidence, row = acquisition
    if fault == "changed-uid":
        resources["supervisor-cluster-role"]["uid"] = "replacement"
    elif fault == "lost-ack-foreign":
        resources[evidence["acquisition_prefix"][-1]["key"]]["generation"] = "foreign"
    elif fault == "changed-namespace":
        resources["workspace-namespace"]["uid"] = "replacement"
    elif fault == "residual":
        backend.residual = True
    elif fault == "live":
        evidence["issued_sessions_expired"] = False
    elif fault in {"component", "late-grant", "plan"}:
        with journal.fenced():
            _, progress = journal.read_locked()
            progress[
                "components" if fault == "component" else "retirement-fence-policy"
            ] = {"phase": "grant_intent"}
            journal.write_locked(progress)
        if fault == "plan":
            evidence["plan_sha256"] = "b" * 64
    reads = 0

    def revalidate():
        nonlocal reads
        reads += 1
        value = copy.deepcopy(evidence)
        if fault == "resolver" and reads > 1:
            value["claim"] = "changed"
        return value

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
        assert journal.store.execute(
            "SELECT * FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
            journal.key,
        )
        assert not journal.store.execute(
            "SELECT * FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id AND revoked=true",
            journal.key,
        )
    else:
        assert run()["namespace_retained"] is True
        assert backend.deleted == [
            g["key"]
            for g in reversed(evidence["acquisition_prefix"])
            if g["key"] != "workspace-namespace"
        ]
        rows = journal.store.execute(
            "SELECT * FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id",
            journal.key,
        )
        assert len(rows) == 1 and rows[0]["plan_json"] == json.dumps(
            json.loads(row["plan_json"]), sort_keys=True, separators=(",", ":")
        )
        identity = acquisition_contract().journal_identity(dict(rows[0]), complete=True)
        assert identity["retained_namespace"] == evidence["retained_namespace"]
        assert not journal.store.execute(
            "SELECT * FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
            journal.key,
        )
        with pytest.raises(BootstrapRefused, match="stale"):
            run()


@pytest.mark.parametrize(
    "acquisition",
    [(21, "revoke_intent", True), (29, "revoke_intent", True)],
    indirect=True,
)
@pytest.mark.parametrize(
    "fault",
    [None, "old-source", "rules", "subject", "lifetime", "namespace", "account-name"],
)
def test_reader_issuer_acquisition_requires_exact_plan_and_separate_source(
    acquisition, fault
):
    *_, row = acquisition
    row = copy.deepcopy(row)
    plan = json.loads(row["plan_json"])
    source = {
        "acquisition_prefix": True,
        "cleanup_acquisition_prefix": True,
        "reader_renewal_acquisition": fault != "old-source",
        "hashes": {
            "workspace_bootstrap/superplane_bootstrap/grant_plan.py": "a" * 64,
            "workspace_bootstrap/superplane_bootstrap/retirement_fence.py": "b" * 64,
        },
    }
    if fault == "rules":
        plan["grants"][3]["body"]["rules"][0]["verbs"].append("create")
    elif fault == "subject":
        plan["grants"][4]["body"]["subjects"][0]["name"] = "foreign"
    elif fault == "lifetime":
        plan["grants"][3]["lifetime"] = "temporary"
    elif fault == "namespace":
        plan["grants"][5]["body"]["metadata"]["namespace"] = "foreign"
    elif fault == "account-name":
        plan["grants"][5]["body"]["rules"][0]["resourceNames"] = ["foreign"]
    row["plan_json"] = json.dumps(plan)

    def validate():
        contract = acquisition_contract()
        identity = contract.journal_identity(row, complete=False)
        contract.validate_source_prefix(source, identity)
        return identity

    if fault:
        with pytest.raises(ValueError):
            validate()
    else:
        assert validate()["reader_renewal"] is True


@pytest.mark.parametrize(
    "fault",
    [
        "active",
        "components",
        "later-intent",
        "hole",
        "adopted-rbac",
        "foreign-namespace",
        "foreign-plan",
        "foreign-identity",
        "retain-workspace",
    ],
)
def test_source_boundary_rejects_unqualified_progress(acquisition, fault):
    *_, row = acquisition
    row = copy.deepcopy(row)
    plan, progress = json.loads(row["plan_json"]), json.loads(row["progress_json"])
    if fault == "active":
        progress["phase"] = "active"
    elif fault in {"components", "later-intent", "retain-workspace"}:
        progress[
            {
                "components": "components",
                "later-intent": "retirement-fence-policy",
                "retain-workspace": "retain_workspace",
            }[fault]
        ] = True
    elif fault == "hole":
        del progress["supervisor-cluster-role"]
    elif fault == "adopted-rbac":
        progress["installer-cluster-role"]["phase"] = "adopted"
    elif fault == "foreign-namespace":
        progress["workspace-namespace"]["identity"]["labels"][
            "superplane.aws-e/bootstrap-owner"
        ] = "foreign"
    elif fault == "foreign-plan":
        plan["grants"][4]["cluster_arn"] = "foreign"
    elif fault == "foreign-identity":
        progress["installer-cluster-role"]["identity"]["generation"] = "foreign"
    row.update(plan_json=json.dumps(plan), progress_json=json.dumps(progress))
    with pytest.raises(ValueError):
        acquisition_contract().journal_identity(row, complete=False)


@pytest.mark.parametrize("acquisition", [(25, "revoke_intent")], indirect=True)
@pytest.mark.parametrize(
    "fault",
    [
        "active-fence",
        "foreign-binding",
        "wrong-api",
        "wrong-name",
        "wrong-kind",
        "allocation",
        "namespace",
        "unknown-grant",
        "beyond-plan",
    ],
)
def test_full_acquisition_refuses_changed_retirement_grants(acquisition, fault):
    *_, original = acquisition
    row = copy.deepcopy(original)
    plan = json.loads(row["plan_json"])
    progress = json.loads(row["progress_json"])
    grants = plan["grants"]
    policy, binding, cleanup = grants[17], grants[18], grants[19]
    if fault == "active-fence":
        policy["body"]["spec"]["validations"][0]["expression"] = "false"
    elif fault == "foreign-binding":
        binding["body"]["spec"]["policyName"] = "foreign-policy"
    elif fault == "wrong-api":
        policy["body"]["apiVersion"] = "example.com/v1"
    elif fault == "wrong-name":
        cleanup["body"]["metadata"]["name"] = "foreign-cluster-role"
    elif fault == "wrong-kind":
        policy["body"]["kind"] = "MutatingAdmissionPolicy"
    elif fault == "allocation":
        cleanup["original_allocation_id"] = "different-allocation"
    elif fault == "namespace":
        grants[21]["body"]["metadata"]["namespace"] = "foreign-namespace"
    elif fault == "unknown-grant":
        grants[-1]["key"] = "unrecognized-grant"
    else:
        progress["component-installation"] = {"phase": "grant_intent"}
    row.update(plan_json=json.dumps(plan), progress_json=json.dumps(progress))
    with pytest.raises(ValueError):
        acquisition_contract().journal_identity(row, complete=False)
