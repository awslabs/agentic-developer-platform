"""Deletion must not require creation-only outputs or permission to create grants."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import workspace_kms as kms
from workspace_ownership import WorkspaceOwnershipError

ACCOUNT = "123456789012"


def plan(actions=None):
    return {
        "variables": {
            "account_id": {"value": ACCOUNT},
            "aws_region": {"value": "us-east-1"},
            "kms_key_arn": {"value": f"arn:aws:kms:us-east-1:{ACCOUNT}:key/retained"},
        },
        "planned_values": {"outputs": {}},
        "resource_changes": [{"change": {"actions": actions or ["delete"]}}],
    }


def test_destroy_binds_operator_without_kms_or_provisioning_outputs(monkeypatch):
    calls = []

    def aws(service, operation, *args, **kwargs):
        calls.append((service, operation))
        assert service in ("sts", "iam")
        if service == "sts":
            return {
                "Account": ACCOUNT,
                "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/operator/session",
                "UserId": "AROAORIGINAL:session",
            }
        return {
            "Role": {
                "Arn": f"arn:aws:iam::{ACCOUNT}:role/operator",
                "RoleId": "AROAORIGINAL",
            }
        }

    monkeypatch.setattr(kms, "_aws", aws)
    receipt = kms.verify_destroy_principal(plan())
    assert receipt["principal_id"] == "AROAORIGINAL"
    assert kms.verify_destroy_principal(plan(), approved=receipt) == receipt
    with pytest.raises(WorkspaceOwnershipError, match="changed after plan review"):
        kms.verify_destroy_principal(
            plan(), approved=dict(receipt, principal_id="AROAOTHER")
        )
    assert set(calls) == {("sts", "get-caller-identity"), ("iam", "get-role")}


@pytest.mark.parametrize(
    "actions",
    [["create"], ["update"], ["delete", "create"], ["create", "delete"], ["no-op"]],
)
def test_creation_and_replacement_cannot_use_destroy_preflight(monkeypatch, actions):
    monkeypatch.setattr(
        kms, "_aws", lambda *a, **kw: pytest.fail("AWS called before action validation")
    )
    with pytest.raises(WorkspaceOwnershipError, match="delete-only"):
        kms.verify_destroy_principal(plan(actions))


def test_foreign_account_is_refused(monkeypatch):
    monkeypatch.setattr(
        kms,
        "_aws",
        lambda *a, **kw: {
            "Account": "222233334444",
            "Arn": "arn:aws:iam::222233334444:user/operator",
            "UserId": "AIDAOTHER",
        },
    )
    with pytest.raises(WorkspaceOwnershipError, match="selected account"):
        kms.verify_destroy_principal(plan())
