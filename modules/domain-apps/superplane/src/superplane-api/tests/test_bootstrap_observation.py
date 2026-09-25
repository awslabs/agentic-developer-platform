"""Readiness consumes the exact manager fence and the exact bootstrap claim."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest

from app.config import settings
from app.models.observation import ObservationLease
from app.services.bootstrap_observation import verified_observation
from app.services.bootstrap_tokens import token_digest
from tests.conftest import async_session_test


async def setup_observation(monkeypatch):
    org = SimpleNamespace(id=uuid.uuid4())
    instance = str(uuid.uuid4())
    now = datetime.now(UTC)
    target = {
        "workspace_id": str(uuid.uuid4()),
        "bootstrap_org_id": "bound-org",
        "bootstrap_operation_id": "actual-operation",
        "registration_claim": "a" * 64,
        "cluster_arn": "arn:aws:eks:us-east-1:000000000002:cluster/fixture",
        "namespace": "fixture",
    }
    snapshot = {
        "mode": "management",
        "registry_ready": True,
        "instance_id": instance,
        "fence_token": 7,
        "last_reconciled": now.isoformat(),
        "lease_expires_at": (now + timedelta(seconds=40)).isoformat(),
        "targets": {target["workspace_id"]: "observed_execution_unavailable"},
        "bootstrap_observations": {
            target["workspace_id"]: {
                key: target[key]
                for key in (
                    "bootstrap_operation_id",
                    "registration_claim",
                    "cluster_arn",
                    "namespace",
                )
            }
        },
    }
    monkeypatch.setattr(
        settings, "controller_observation_submitter_id", "registry-fixture"
    )
    async with async_session_test() as db:
        db.add(
            ObservationLease(
                scope=f"controller_management/{org.id}",
                holder="registry-fixture:" + instance,
                expires_at=now + timedelta(seconds=40),
                fence_token=7,
                acquire_count=1,
            )
        )
        await db.commit()
    return org, target, snapshot


async def test_current_exact_observation_does_not_expose_other_targets(monkeypatch):
    org, target, snapshot = await setup_observation(monkeypatch)
    snapshot["targets"][str(uuid.uuid4())] = "credential_unavailable"
    async with async_session_test() as db:
        result = await verified_observation(
            db, org=org, target=target, snapshot=snapshot
        )
    assert result["workspace_id"] == target["workspace_id"]
    assert result["registration_claim"] == target["registration_claim"]
    assert "targets" not in result and "credential" not in result
    assert result["org_id"] == "bound-org"


@pytest.mark.parametrize(
    "changed",
    [
        "claim",
        "operation",
        "namespace",
        "instance",
        "fence",
        "stale",
        "unobserved",
        "registry",
    ],
)
async def test_a_new_claim_or_manager_takeover_invalidates_old_readiness(
    monkeypatch, changed
):
    org, target, original = await setup_observation(monkeypatch)
    snapshot = deepcopy(original)
    binding = snapshot["bootstrap_observations"][target["workspace_id"]]
    if changed in {"claim", "operation", "namespace"}:
        binding[
            {
                "claim": "registration_claim",
                "operation": "bootstrap_operation_id",
                "namespace": "namespace",
            }[changed]
        ] = "another"
    elif changed == "instance":
        snapshot["instance_id"] = str(uuid.uuid4())
    elif changed == "fence":
        snapshot["fence_token"] += 1
    elif changed == "stale":
        snapshot["last_reconciled"] = (
            datetime.now(UTC) - timedelta(minutes=1)
        ).isoformat()
    elif changed == "unobserved":
        snapshot["targets"][target["workspace_id"]] = "pending"
    else:
        snapshot["registry_ready"] = False
    async with async_session_test() as db:
        with pytest.raises(HTTPException) as error:
            await verified_observation(db, org=org, target=target, snapshot=snapshot)
    assert error.value.status_code == 409


async def test_broad_machine_or_user_credentials_are_not_bootstrap_read_tokens(client):
    path = f"/api/v1/workspaces/{uuid.uuid4()}/bootstrap-observation"
    assert (await client.get(path)).status_code == 401
    assert (
        await client.get(path, headers={"Authorization": "broad-registry-fixture"})
    ).status_code == 401


def test_token_storage_is_domain_separated_digest():
    token = "sp-bootstrap-read-" + "fixture" * 8
    assert len(token_digest(token)) == 64
    assert token not in token_digest(token)


async def test_shared_reader_acknowledgement_binds_exact_revision_and_namespace(
    monkeypatch,
):
    org, target, snapshot = await setup_observation(monkeypatch)
    target["shared_membership"] = True
    target["membership_credential"] = {
        "org_id": str(org.id),
        "workspace_id": target["workspace_id"],
        "cluster_id": str(uuid.uuid4()),
        "cluster_arn": target["cluster_arn"],
        "generation": "b" * 64,
        "namespace": target["namespace"],
        "namespace_uid": "namespace-uid",
        "service_account_uid": "reader-sa-uid",
        "revision": 1,
        "scope": "reader",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    async with async_session_test() as db:
        with pytest.raises(HTTPException):
            await verified_observation(db, org=org, target=target, snapshot=snapshot)
        snapshot["bootstrap_observations"][target["workspace_id"]][
            "membership_credential"
        ] = deepcopy(target["membership_credential"])
        result = await verified_observation(
            db, org=org, target=target, snapshot=snapshot
        )
        assert result["membership_credential"] == target["membership_credential"]
        for field, replacement in (
            ("revision", 2),
            ("namespace_uid", "replacement"),
            ("service_account_uid", "replacement"),
        ):
            changed = deepcopy(snapshot)
            changed["bootstrap_observations"][target["workspace_id"]][
                "membership_credential"
            ][field] = replacement
            with pytest.raises(HTTPException):
                await verified_observation(db, org=org, target=target, snapshot=changed)
