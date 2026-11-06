"""Native claim/journal cleanup with real PostgreSQL and EKS response shapes."""

import copy
import json

import pytest
from botocore.exceptions import ClientError

from superplane_bootstrap.denied_grant import digest
from superplane_bootstrap.eks_grants import EksGrants
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.grant_plan import BootstrapRelease, compile_grants
from superplane_bootstrap.registrar_recovery import KIND, recover_registrar

from .test_authority_journal_postgres import authority  # noqa: F401
from .test_registry_postgres import database, loop, schema_ddl, server  # noqa: F401


@pytest.fixture
def registrar(authority):  # noqa: F811
    journal, registry, _ = authority
    principals = {
        a: f"arn:aws:iam::{journal.target.account_id}:role/{a}"
        for a in ("registrar", "installer", "supervisor")
    }
    plan = compile_grants(
        journal,
        BootstrapRelease(
            "workspace", "worker", "controller", "v1.35", ("jobs.example.com",)
        ),
        principals,
    )
    first, policy = plan["grants"][:2]
    arn = (
        journal.target.cluster_arn.replace(":cluster/", ":access-entry/")
        + "/role/"
        + journal.target.account_id
        + "/registrar/immutable"
    )
    entry = {
        "clusterName": journal.target.cluster_name,
        "principalArn": first["principal_arn"],
        "accessEntryArn": arn,
        "type": "STANDARD",
        "kubernetesGroups": first["groups"],
        "username": first["username"],
        "tags": {"superplane-generation": journal.generation},
    }

    class Client:
        def __init__(self):
            self.entry = entry
            self.policies = [
                {
                    "policyArn": policy["policy_arn"],
                    "accessScope": {"type": "cluster", "namespaces": []},
                    "associatedAt": "2026-10-13T10:15:58Z",
                }
            ]
            self.deleted = []
            self.residual = False

        def describe_access_entry(self, **kwargs):
            if self.entry is None:
                raise ClientError(
                    {"Error": {"Code": "ResourceNotFoundException"}},
                    "DescribeAccessEntry",
                )
            return {"accessEntry": self.entry}

        def list_associated_access_policies(self, **kwargs):
            return {"associatedAccessPolicies": self.policies}

        def disassociate_access_policy(self, **kwargs):
            self.deleted.append("policy")
            if not self.residual:
                self.policies = []

        def delete_access_entry(self, **kwargs):
            assert not self.policies
            self.deleted.append("entry")
            self.entry = None

    client = Client()

    def narrow(value):
        assert value == arn
        return client

    eks = EksGrants(client, journal.target, entry_client=narrow)
    journal.begin(plan)
    with journal.fenced():
        journal.write_locked(
            {
                "phase": "revoking",
                "registrar-entry": {"phase": "granted", "identity": eks.observe(first)},
                "registrar-policy": {"phase": "revoke_intent"},
            }
        )
    evidence = {
        "evidence_kind": KIND,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "operation_id": journal.binding.operation_id,
        "org_id": journal.target.org_id,
        "workspace_id": journal.target.workspace_id,
        "cluster_arn": journal.target.cluster_arn,
        "generation": journal.generation,
        "claim": journal.claim,
        "plan_sha256": digest(plan),
    }
    return journal, registry, eks, client, evidence


def run(value, **options):
    journal, registry, eks, client, evidence = value
    return recover_registrar(
        journal,
        registry,
        eks,
        revalidate=options.get("revalidate", lambda: evidence),
        verify_absence=lambda _: client.entry is None and not client.policies,
    )


def test_native_cleanup_preserves_created_identities_and_releases_claim(registrar):
    journal, _, _, client, _ = registrar
    original = journal.read()[0]
    result = run(registrar)
    assert result["revoked"] and result["claim_released"]
    assert client.deleted == ["policy", "entry"]
    rows = journal.store.execute(
        "SELECT * FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id",
        journal.key,
    )
    assert len(rows) == 1 and rows[0]["revoked"]
    assert json.loads(rows[0]["plan_json"]) == original
    progress = json.loads(rows[0]["progress_json"])
    assert progress["complete"] and progress["phase"] == "revoked"
    assert progress["registrar-policy"]["identity"]["scope"] == {
        "type": "cluster",
        "namespaces": [],
    }
    assert (
        progress["registrar-entry"]["identity"]["arn"]
        == progress["registrar-policy"]["identity"]["arn"]
    )
    assert not journal.store.execute(
        "SELECT * FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
        journal.key,
    )
    with pytest.raises(BootstrapRefused, match="stale"):
        run(registrar)


@pytest.mark.parametrize(
    "fault",
    [
        "components",
        "namespace",
        "phase",
        "claim",
        "generation",
        "plan_sha256",
        "original_authority_expired",
        "issued_sessions_expired",
        "resolver",
        "replacement",
        "scope",
        "residual",
    ],
)
def test_uncertain_cleanup_never_releases_claim(registrar, fault):
    journal, _, _, client, evidence = registrar
    if fault in {"components", "namespace", "phase"}:
        with journal.fenced():
            _, progress = journal.read_locked()
            if fault == "phase":
                progress["phase"] = "active"
            else:
                progress["workspace-namespace" if fault == "namespace" else fault] = {
                    "phase": "grant_intent"
                }
            journal.write_locked(progress)
    elif fault in evidence:
        evidence[fault] = False if isinstance(evidence[fault], bool) else "changed"
    elif fault == "replacement":
        client.entry = {
            **client.entry,
            "accessEntryArn": client.entry["accessEntryArn"] + "-new",
        }
    elif fault == "scope":
        client.policies[0]["accessScope"] = {
            "type": "namespace",
            "namespaces": ["other"],
        }
    elif fault == "residual":
        client.residual = True
    count = 0

    def resolve():
        nonlocal count
        count += 1
        value = copy.deepcopy(evidence)
        if fault == "resolver" and count > 1:
            value["claim"] = "changed"
        return value

    with pytest.raises(BootstrapRefused):
        run(registrar, revalidate=resolve)
    assert journal.store.execute(
        "SELECT * FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
        journal.key,
    )
    assert not journal.store.execute(
        "SELECT * FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id AND revoked=true",
        journal.key,
    )
