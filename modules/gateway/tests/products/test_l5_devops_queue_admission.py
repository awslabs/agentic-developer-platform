"""Real owner/grant/source boundaries with only provider I/O replaced."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from src.l5_devops_queue_admission import QueueVerificationReader, admission_policies
from src.l5_devops_verification import VerificationReader
from src.l5_integration import GatewayConfigurationBackend
from src.shared.models.budget import BudgetConfig
from src.shared.models.vault import CredentialServiceDelegation
from tests.products.test_l5_devops_queue_fixture import consent_fixture, devops_backend, l5_owner, selected

__all__ = ["consent_fixture", "devops_backend", "l5_owner", "selected"]


@pytest.fixture
async def admission(selected, db_session, monkeypatch):
    fixture = selected
    fixture.version.settings = {
        **fixture.version.settings,
        "devops": {
            **fixture.version.settings["devops"],
            "trigger_policies": [
                {
                    "trigger_id": "verify-queue",
                    "kind": "manual",
                    "enabled": True,
                    "capability": "release_verification",
                    "binding_id": fixture.binding.binding_id,
                    "resource_refs": [],
                    "telemetry_refs": [],
                }
            ],
        },
    }
    await db_session.commit()
    fixture.request = {
        "service_id": "devops",
        "configuration_revision": 1,
        "binding_id": fixture.binding.binding_id,
        "capability": "release_verification",
        "devops_input": fixture.producer.options,
    }
    fixture.verified, _ = await fixture.backend.admission_authority(fixture.product_id, fixture.project_id, "devops", fixture.request)
    fixture.child = await db_session.get(CredentialServiceDelegation, fixture.verified["delegation_id"])
    fixture.aws.clients["sts"].get_caller_identity.return_value["Arn"] = "arn:aws:sts::123456789012:assumed-role/Fixture/readiness"

    def session(**kwargs):
        assert set(kwargs) == {"aws_access_key_id", "aws_secret_access_key", "aws_session_token", "region_name"}
        return fixture.aws

    def ambient(*args, **kwargs):
        raise AssertionError("ambient AWS clients must never be used")

    monkeypatch.setattr("boto3.Session", session)
    monkeypatch.setattr("boto3.client", ambient)
    fixture.reader = QueueVerificationReader(fixture.backend).bind_admission(fixture.verified, fixture.request)
    return fixture


async def test_owner_grant_scopes_observation_and_request_local_evidence(admission):
    fixture = admission
    observed = await fixture.reader.read_deployment(fixture.selection["deployment_ref"], fixture.selection["target_ref"])
    assert observed["deployed_sha"] == fixture.head["record_revision"]
    assert await fixture.reader.read_scenarios(fixture.producer.options["scenarios"]) == fixture.producer.specs()
    assert fixture.aws.invokes == [] and fixture.aws.deleted == []
    assert fixture.assumed.call_count == 2
    assert fixture.reader.reference["version"] != "null"
    for call in fixture.assumed.call_args_list:
        assert call.kwargs["role_arn"] == fixture.binding.provider_identity["role_arn"]
        assert call.kwargs["session_duration_seconds"] == 900
        assert call.kwargs["task_id"] == fixture.backend.admission_key
        assert len(call.kwargs["session_policy"]) <= 2048
    assert fixture.reader.bind_backend(fixture.backend).session is None
    assert fixture.reader.bind_backend(fixture.backend).reference is None


@pytest.mark.parametrize(
    "fault",
    [
        "owner",
        "expired_jwt",
        "parent_revoked",
        "child_revoked",
        "child_version",
        "null_scope",
        "binding",
        "configuration",
        "source",
        "credential",
        "provider",
        "request",
        "admission_key",
    ],
)
async def test_stale_or_foreign_authority_cannot_issue_session_or_evidence(admission, db_session, fault):
    fixture = admission
    if fault == "owner":
        fixture.backend.actor = fixture.backend.actor.model_copy(update={"user_id": "foreign-owner"})
    elif fault == "expired_jwt":
        fixture.backend.actor = fixture.backend.actor.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    elif fault in {"parent_revoked", "child_revoked"}:
        (fixture.parent if fault == "parent_revoked" else fixture.child).revoked_at = datetime.now(UTC)
    elif fault == "child_version":
        fixture.child.revision += 1
    elif fault == "null_scope":
        fixture.child.execution_scope = None
    elif fault == "binding":
        fixture.binding.generation += 1
    elif fault == "configuration":
        fixture.version.hash = "f" * 64
    elif fault == "source":
        fixture.source.status = "unlinked"
        fixture.source.unlinked_at = datetime.now(UTC)
    elif fault == "credential":
        fixture.credential.aws_verified_version_id = "rotated-secret"
    elif fault == "provider":
        fixture.binding.provider_identity = {**fixture.binding.provider_identity, "account_id": "111111111111"}
    elif fault == "request":
        fixture.reader.authority.run_input["devops_input"]["target_ref"]["source_connection_id"] = "foreign-connection"
    elif fault == "admission_key":
        fixture.backend.admission_key = "another-admission"
    await db_session.commit()
    with pytest.raises(HTTPException):
        await fixture.reader.read_deployment(fixture.selection["deployment_ref"], fixture.selection["target_ref"])
    fixture.assumed.assert_not_called()
    assert fixture.aws.objects == {} and fixture.aws.invokes == []


async def test_revocation_after_session_issuance_stops_evidence(admission, db_session):
    fixture = admission
    original = fixture.assumed.side_effect

    def revoke(**kwargs):
        fixture.parent.revoked_at = datetime.now(UTC)
        return fixture.assumed.return_value

    fixture.assumed.side_effect = revoke
    with pytest.raises(HTTPException):
        await fixture.reader.read_deployment(fixture.selection["deployment_ref"], fixture.selection["target_ref"])
    assert fixture.assumed.call_count == 1
    assert fixture.aws.objects == {}
    fixture.assumed.side_effect = original


async def test_secret_rotation_refuses_evidence(admission):
    admission.secrets.current_version_id.return_value = "rotated-secret"
    with pytest.raises(HTTPException) as denied:
        await admission.reader.read_deployment(admission.selection["deployment_ref"], admission.selection["target_ref"])
    assert denied.value.detail["code"] == "selected_connection_unavailable"
    admission.assumed.assert_not_called()
    assert admission.aws.objects == {}


async def test_two_admissions_keep_distinct_signed_references(admission):
    fixture = admission
    other = GatewayConfigurationBackend(fixture.backend.db, fixture.backend.actor, publish=lambda *args: None)
    other.admission_operation, other.admission_key = "retry", "different-retry-00001"
    second = QueueVerificationReader(other).bind_admission(fixture.verified, fixture.request)
    first = fixture.reader
    await first.read_deployment(fixture.selection["deployment_ref"], fixture.selection["target_ref"])
    first_ref = deepcopy(first.reference)
    await second.read_deployment(fixture.selection["deployment_ref"], fixture.selection["target_ref"])
    assert first.reference == first_ref and second.reference != first_ref
    assert first.selected["admission"]["idempotency_key"] == fixture.backend.admission_key
    assert second.selected["admission"] == {"operation": "retry", "idempotency_key": other.admission_key}
    assert await first.read_scenarios(fixture.producer.options["scenarios"]) == fixture.producer.specs()


async def test_legacy_queue_retry_requires_native_profile(admission, db_session):
    fixture = admission
    original = fixture.run
    original.input = fixture.request
    original.state, original.provider_state = "failed", "not_started"
    original.admission = {
        **fixture.verified,
        **original.admission,
        "delegation_id": fixture.child.id,
        "delegation_version": fixture.child.revision,
        "admitted_at": datetime.now(UTC).isoformat(),
        "limits": fixture.verified["limits"],
    }
    original.no_effect_proof = {
        "classification": "definitive_no_effect",
        "evidence_ref": {**fixture.selection["target_ref"], "record_type": "provider_observation", "record_id": "no-native-effect"},
    }
    db_session.add(
        BudgetConfig(
            id=fixture.verified["limits"]["policy_ref"]["record_id"],
            org_id=fixture.backend.actor.org_id,
            entity_type="org",
            entity_id=fixture.backend.actor.org_id,
            period_type="daily",
            budget_amount_usd=Decimal("100"),
            enforcement_mode="hard",
        )
    )
    await db_session.commit()
    fixture.backend.verification_reader = QueueVerificationReader(fixture.backend)
    with pytest.raises(HTTPException) as denied:
        await fixture.backend.retry_run(
            fixture.product_id, fixture.project_id, "devops", original.run_id, "retry exact fixture", "queue-retry-transport-00001"
        )
    assert denied.value.detail["code"] == "native_test_profile_required"
    assert original.input == fixture.request and original.state == "failed"
    assert fixture.backend.verification_reader.reference is None
    assert fixture.assumed.call_count == 0 and fixture.aws.invokes == []


async def test_global_reference_and_ambient_credentials_cannot_enable_reader(admission, monkeypatch):
    monkeypatch.setenv("L5_DEVOPS_VERIFICATION_EVIDENCE_REF", json.dumps({"bucket": "foreign-evidence"}))
    with pytest.raises(HTTPException) as denied:
        await VerificationReader(admission.backend).read_deployment(admission.selection["deployment_ref"], admission.selection["target_ref"])
    assert denied.value.detail["code"] == "deployment_evidence_producer_required"
    admission.assumed.assert_not_called()


async def test_admission_policies_have_no_execution_or_cleanup_authority(admission):
    expiry = datetime.now(UTC) + timedelta(minutes=10)
    policies = [json.loads(item) for item in admission_policies(admission.selection, expiry)]
    actions = {}
    for policy in policies:
        for statement in policy["Statement"]:
            assert statement["Effect"] == "Allow"
            assert statement["Condition"]["DateLessThan"]["aws:CurrentTime"] == expiry.isoformat()
            for action in statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]:
                actions[action] = statement["Resource"]
    assert set(actions) == {
        "lambda:GetFunction",
        "lambda:GetFunctionConcurrency",
        "lambda:GetEventSourceMapping",
        "sqs:GetQueueAttributes",
        "iam:GetRole",
        "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies",
        "iam:GetRolePolicy",
        "ec2:DescribeSecurityGroups",
        "sts:GetCallerIdentity",
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:PutObject",
        "kms:Sign",
        "kms:Verify",
    }
    assert {key for key, resource in actions.items() if resource == "*"} == {"ec2:DescribeSecurityGroups", "sts:GetCallerIdentity"}
    assert actions["lambda:GetFunction"] == admission.selection["function_version_arn"]
    assert actions["s3:PutObject"] == [
        f"arn:aws:s3:::{admission.selection['bucket']}/{admission.selection['prefix']}{suffix}/*" for suffix in ("admission", "observations")
    ]
    assert actions["kms:Sign"] == admission.selection["signing_key_arn"]
