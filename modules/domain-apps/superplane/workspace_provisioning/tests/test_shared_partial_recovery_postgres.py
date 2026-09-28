"""Read original member recovery journals without granting cleanup or network effects."""
# ruff: noqa: F811

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from harness_jobs.identity import OperationRefused
from superplane_bootstrap.kube_grants import GENERATION_ANNOTATION
from superplane_bootstrap.membership import SharedMembership, registration_fields
from superplane_bootstrap.state import claim_fingerprint
from workspace_bootstrap.tests import conftest as identities

from workspace_provisioning.recovery_partial import PartialLifecycleRecovery

from .postgres_bridge import requires_harness_postgres
from .test_bootstrap_runtime_postgres import bootstrap_harness  # noqa: F401

pytestmark = requires_harness_postgres


async def seed(harness):
    member = SharedMembership.create(
        org_id=identities.ORG_ID,
        workspace_id=str(uuid4()),
        cluster_id=str(uuid4()),
        request_id=str(uuid4()),
        cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/shared",
        endpoint="https://shared.example",
    )
    lease = SimpleNamespace(
        org_id=member.org_id,
        workspace_id=member.workspace_id,
        operation_id=str(uuid4()),
    )
    parameters = {
        "shared_membership": member.encode(),
        "lifecycle_inputs": json.dumps(
            {
                "cluster_placement": "shared",
                "shared_cluster_id": member.cluster_id,
            }
        ),
        "lifecycle_request": json.dumps(
            {
                "mode": "bring-existing-cluster",
                "workspace_id": member.workspace_id,
                "target_account_id": "123456789012",
                "region": "us-east-1",
                "existing_cluster_name": "shared",
            }
        ),
    }
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=lease),
        request=SimpleNamespace(parameters=parameters),
    )
    token = "original-registration-token"
    claim = claim_fingerprint(token)
    generation = hashlib.sha256((lease.operation_id + ":" + claim).encode()).hexdigest()
    plan = {
        "mode": "shared-namespace",
        "membership": member.encode(),
        "cluster_authority_entry": "installed-cluster-owned-entry",
        "grants": [
            {
                "key": "workspace-namespace",
                "kind": "kubernetes",
                "cluster_arn": member.cluster_arn,
                "generation": member.generation,
                "body": {
                    "kind": "Namespace",
                    "metadata": {
                        "name": member.namespace,
                        "annotations": {GENERATION_ANNOTATION: member.generation},
                    },
                },
            }
        ],
    }
    progress = {
        "phase": "active",
        "workspace-namespace": {
            "phase": "granted",
            "identity": {"uid": "member-ns-uid"},
        },
        "member_credentials": {"reader": 1},
    }
    identity = {
        "org_id": member.org_id,
        "workspace_id": member.workspace_id,
        "cluster_arn": member.cluster_arn,
        "endpoint": member.endpoint,
        "namespace": member.namespace,
        **registration_fields(member),
    }
    member_id = uuid4()
    async with harness.connect() as c:
        await c.execute(
            "INSERT INTO clusters(id,org_id,name,status,sharing_enabled,eks_cluster_arn,endpoint) VALUES($1,$2,'shared','Ready',true,$3,$4)",
            UUID(member.cluster_id),
            UUID(member.org_id),
            member.cluster_arn,
            member.endpoint,
        )
        await c.execute(
            "INSERT INTO workspaces(id,org_id,name,status,isolation_mode,is_default) VALUES($1,$2,'member','Provisioning','namespace',false)",
            UUID(member.workspace_id),
            UUID(member.org_id),
        )
        await c.execute(
            "INSERT INTO cluster_memberships(id,org_id,workspace_id,cluster_id,generation,namespace,namespace_uid,operation_id) VALUES($1,$2,$3,$4,$5,$6,'member-ns-uid',$7)",
            member_id,
            UUID(member.org_id),
            UUID(member.workspace_id),
            UUID(member.cluster_id),
            member.generation,
            member.namespace,
            UUID(member.request_id),
        )
        await c.execute(
            "INSERT INTO workspace_bootstrap_reservations(workspace_id,state,identity_json,attempt_token) VALUES($1,'reserved',$2,$3)",
            member.workspace_id,
            json.dumps(identity),
            token,
        )
        await c.execute(
            "INSERT INTO workspace_bootstrap_authority(workspace_id,generation,operation_id,org_id,cluster_arn,claim,plan_json,progress_json) VALUES($1,$2,$3,$4,$5,$6,$7,$8)",
            member.workspace_id,
            generation,
            lease.operation_id,
            member.org_id,
            member.cluster_arn,
            claim,
            json.dumps(plan),
            json.dumps(progress),
        )
        # Revision 2 belongs to a different controller, never this bootstrap claim.
        await c.execute(
            "INSERT INTO membership_credentials(membership_id,revision,scope,namespace_uid,state,service_account_uid) VALUES($1,1,'reader','member-ns-uid','reserved','original-sa'),($1,2,'reader','member-ns-uid','reserved','renewal-sa')",
            member_id,
        )
    recovery = PartialLifecycleRecovery(SimpleNamespace(domain_connect=harness.connect))
    source = {
        "artifact_id": "original-discovery",
        "artifact_metadata_json": json.dumps(
            {
                "outputs": {
                    "cluster_arn": {"value": member.cluster_arn},
                    "cluster_endpoint": {"value": member.endpoint},
                },
            }
        ),
    }
    return SimpleNamespace(
        member=member,
        operation=operation,
        plan=plan,
        progress=progress,
        recovery=recovery,
        source=source,
        generation=generation,
    )


async def inventory(case):
    return await case.recovery.inventory(
        case.operation, {}, None, None, case.source, "bootstrap-workspace"
    )


def forbid_network(*args, **kwargs):
    raise AssertionError("shared member recovery cannot construct dedicated networking")


def test_shared_partial_inventory_keeps_original_claim_and_never_authorizes_cleanup(
    bootstrap_harness,
    monkeypatch,
):
    monkeypatch.setattr("workspace_provisioning.network.network_recipe", forbid_network)

    async def scenario():
        case = await seed(bootstrap_harness)
        result = await inventory(case)
        assert result["cleanup_authorized"] is False
        assert result["provider_absence_verified"] is False
        assert result["workflow_complete"] is False
        assert result["confirmed_effect_keys"] == result["uncertain_effect_keys"] == []
        assert result["shared_membership"]["cluster_resources_included"] is False
        assert result["shared_membership"]["generation"] == case.member.generation
        (authority,) = result["authority_generations"]
        assert authority["generation"] == case.generation
        assert authority["namespace_uid"] == "member-ns-uid"
        assert [
            (row["revision"], row["service_account_uid"])
            for row in authority["credentials"]
        ] == [(1, "original-sa")]
        assert "original-registration-token" not in json.dumps(result)
        async with bootstrap_harness.connect() as c:
            assert (
                await c.fetchval(
                    "SELECT count(*) FROM membership_credentials WHERE state='reserved'"
                )
                == 2
            )
            assert await c.fetchval(
                "SELECT progress_json FROM workspace_bootstrap_authority"
            ) == json.dumps(case.progress)

    bootstrap_harness.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        "claim",
        "generation",
        "org",
        "member_request",
        "namespace",
        "credential_namespace",
        "cluster_component",
        "foreign_membership",
        "discovery",
    ],
)
def test_shared_partial_inventory_refuses_stale_or_foreign_journal_scope(
    bootstrap_harness,
    monkeypatch,
    change,
):
    monkeypatch.setattr("workspace_provisioning.network.network_recipe", forbid_network)

    async def scenario():
        case = await seed(bootstrap_harness)
        async with bootstrap_harness.connect() as c:
            if change == "claim":
                await c.execute(
                    "UPDATE workspace_bootstrap_reservations SET attempt_token='replacement'"
                )
            elif change == "generation":
                await c.execute(
                    "UPDATE workspace_bootstrap_authority SET generation=$1", "a" * 64
                )
            elif change == "org":
                await c.execute(
                    "UPDATE workspace_bootstrap_authority SET org_id=$1", str(uuid4())
                )
            elif change == "member_request":
                await c.execute(
                    "UPDATE cluster_memberships SET operation_id=$1", uuid4()
                )
            elif change == "credential_namespace":
                await c.execute(
                    "UPDATE membership_credentials SET namespace_uid='other-namespace' WHERE revision=1"
                )
            elif change in {"namespace", "foreign_membership"}:
                plan = deepcopy(case.plan)
                if change == "namespace":
                    plan["grants"][0]["body"]["metadata"]["name"] = "another-member"
                else:
                    plan["membership"] = "{}"
                await c.execute(
                    "UPDATE workspace_bootstrap_authority SET plan_json=$1",
                    json.dumps(plan),
                )
            elif change == "cluster_component":
                progress = deepcopy(case.progress)
                progress["components"] = {
                    "cluster-network": {"desired": {"kind": "ClusterRole"}}
                }
                await c.execute(
                    "UPDATE workspace_bootstrap_authority SET progress_json=$1",
                    json.dumps(progress),
                )
            else:
                metadata = json.loads(case.source["artifact_metadata_json"])
                metadata["outputs"]["cluster_endpoint"]["value"] = (
                    "https://foreign.example"
                )
                case.source["artifact_metadata_json"] = json.dumps(metadata)
        with pytest.raises(OperationRefused):
            await inventory(case)

    bootstrap_harness.run(scenario())


def test_revoked_original_claim_is_historical_evidence_not_cleanup_authority(
    bootstrap_harness,
):
    async def scenario():
        case = await seed(bootstrap_harness)
        async with bootstrap_harness.connect() as c:
            await c.execute("UPDATE workspace_bootstrap_authority SET revoked=true")
            await c.execute("DELETE FROM workspace_bootstrap_reservations")
        result = await inventory(case)
        assert result["authority_generations"][0]["revoked"] is True
        assert result["cleanup_authorized"] is False
        assert result["provider_absence_verified"] is False

    bootstrap_harness.run(scenario())
