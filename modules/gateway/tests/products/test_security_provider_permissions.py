"""Dependent Security permissions stay inside the selected role and setup intent."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.internal.l5_credential_routes import _security_session_policy, _security_setup_policy, _threat_model_session_policy

ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/project/security-execution"
SPACE = {"record_type": "security_space", "record_id": "space-selected"}
EXPIRY = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=1)
REGISTRATION = {"bucket": "example-security-source", "service_role": ROLE}


def binding():
    return SimpleNamespace(provider_identity={"account_id": ACCOUNT, "region": "us-east-1"}, native_resource_ref=SPACE)


def test_code_review_passes_only_its_observed_role_to_security_before_lease_expiry():
    statements = json.loads(_security_session_policy(binding(), REGISTRATION, EXPIRY))["Statement"]
    iam = [statement for statement in statements if any(action.startswith("iam:") for action in statement["Action"])]
    assert iam == [
        {
            "Effect": "Allow",
            "Action": ["iam:PassRole"],
            "Resource": ROLE,
            "Condition": {
                "DateLessThan": {"aws:CurrentTime": EXPIRY.isoformat()},
                "StringEquals": {"iam:PassedToService": "securityagent.amazonaws.com"},
            },
        }
    ]
    assert statements[0]["Resource"] == f"arn:aws:securityagent:us-east-1:{ACCOUNT}:agent-space/space-selected"
    assert statements[1]["Resource"] == "arn:aws:s3:::example-security-source/security-agent/source/*"
    assert not any("pentest" in action.lower() for item in statements for action in item["Action"])
    threat = json.loads(_threat_model_session_policy(binding(), REGISTRATION, EXPIRY))["Statement"]
    assert [item for item in threat if "iam:PassRole" in item["Action"]] == iam
    assert not any(action.startswith("s3:") for item in threat for action in item["Action"])


@pytest.mark.parametrize(
    "change",
    [
        {"service_role": None},
        {"service_role": "*"},
        {"service_role": ROLE + "*"},
        {"service_role": ROLE.replace(ACCOUNT, "123456789013")},
        {"service_role": f"arn:aws:sts::{ACCOUNT}:assumed-role/role/session"},
        {"bucket": "*"},
        {"bucket": "example-security-source/foreign-prefix"},
        {"unexpected": "caller-supplied"},
    ],
)
def test_code_review_rejects_invalid_or_foreign_registration_before_granting_passrole(change):
    with pytest.raises(HTTPException) as error:
        _security_session_policy(binding(), {**REGISTRATION, **change}, EXPIRY)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException):
        _threat_model_session_policy(binding(), {**REGISTRATION, **change}, EXPIRY)


def test_new_space_tagging_is_scoped_to_the_original_intent_account_region_and_lease():
    intent = SimpleNamespace(
        operation_id="setup-selected",
        tenant_id="tenant-one",
        product_id="product-one",
        project_id="project-one",
        configuration_id="configuration-one",
    )
    operation = SimpleNamespace(action="create")
    policy = json.loads(_security_setup_policy(ACCOUNT, "us-east-1", EXPIRY, intent, operation, None, False))
    tagging = [statement for statement in policy["Statement"] if "securityagent:TagResource" in statement["Action"]]
    assert tagging == [
        {
            "Effect": "Allow",
            "Action": ["securityagent:TagResource"],
            "Resource": f"arn:aws:securityagent:us-east-1:{ACCOUNT}:agent-space/*",
            "Condition": {
                "DateLessThan": {"aws:CurrentTime": EXPIRY.isoformat()},
                "StringEquals": {"aws:RequestedRegion": "us-east-1", "aws:RequestTag/adp-setup-operation": "setup-selected"},
                "ForAllValues:StringEquals": {"aws:TagKeys": ["adp-setup-operation"]},
            },
        }
    ]
    # Already identified resources never need the tag-on-create permission.
    for action in ("create", "check", "update", "resume", "link", "cleanup"):
        operation.action = action
        scoped = _security_setup_policy(ACCOUNT, "us-east-1", EXPIRY, intent, operation, SPACE, True)
        assert "securityagent:TagResource" not in scoped
        assert ("iam:PassRole" in scoped) == (action in {"check", "update", "resume"})
