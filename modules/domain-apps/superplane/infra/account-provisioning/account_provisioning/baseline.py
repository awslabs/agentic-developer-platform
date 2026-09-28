"""Durable managed-policy and S3 setup, with authoritative baseline reads."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import unquote

from account_factory.recovery import StepState

from .ports import PolicyAbsent, ProviderDenied, ProviderUnavailable

PUBLIC_ACCESS_BLOCK = {
    "BlockPublicAcls": True,
    "IgnorePublicAcls": True,
    "BlockPublicPolicy": True,
    "RestrictPublicBuckets": True,
}


@dataclass(frozen=True)
class ControlResult:
    state: StepState
    detail: str
    durable_key: str | None = None


def canonical(document):
    if isinstance(document, str):
        document = json.loads(unquote(document))
    if not isinstance(document, dict) or not isinstance(document.get("Statement"), list):
        raise ValueError("managed policy must be a policy document")
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def policy_target(arn, document):
    return arn + "#" + hashlib.sha256(canonical(document).encode()).hexdigest()


def read_policy(iam, arn, document):
    try:
        policy = iam.get_policy(PolicyArn=arn)["Policy"]
        if policy.get("Arn") != arn or not policy.get("DefaultVersionId"):
            return ControlResult(StepState.NOT_CHECKED, "policy identity/version was not established")
        version = iam.get_policy_version(PolicyArn=arn, VersionId=policy["DefaultVersionId"])["PolicyVersion"]
        if canonical(version.get("Document")) != canonical(document):
            return ControlResult(StepState.CONFLICT, "managed policy differs from the reviewed document")
        return ControlResult(StepState.ESTABLISHED, "default managed-policy version matches the reviewed document")
    except PolicyAbsent:
        return ControlResult(StepState.ABSENT, "managed policy is absent")
    except ProviderDenied:
        return ControlResult(StepState.DENIED, "managed-policy read was denied")
    except (ProviderUnavailable, AttributeError, KeyError, TypeError, ValueError):
        return ControlResult(StepState.NOT_CHECKED, "managed-policy contents could not be verified")


async def settle_verified(executor, *, provider, kinds, target, reference):
    """Persist authoritative read-back of an interrupted deterministic write."""
    for kind in kinds:
        for call in await executor.provider_calls(provider=provider, operation_kind=kind):
            if call.target != target:
                raise ValueError("baseline history target differs from reviewed inputs")
            if call.outcome is None:
                await executor.observe_success(
                    idempotency_key=call.idempotency_key,
                    detail="authoritative baseline read-back verified the requested state",
                    provider_ref=reference,
                )


async def ensure_policy(executor, iam, *, step, arn, document, allow_update=False, refusal_types=()):
    result = read_policy(iam, arn, document)
    if result.state is StepState.ESTABLISHED:
        await settle_verified(
            executor,
            provider="aws-iam",
            kinds=(f"managed-policy/{step}/create", f"managed-policy/{step}/version"),
            target=policy_target(arn, document),
            reference=arn,
        )
    if result.state is not StepState.ABSENT and not (allow_update and result.state is StepState.CONFLICT):
        return result
    action = "create" if result.state is StepState.ABSENT else "version"
    key = f"{executor.operation_id}:managed-policy:{step}"
    try:
        await executor.execute_provider(
            idempotency_key=key, provider="aws-iam", operation_kind=f"managed-policy/{step}/{action}", target=policy_target(arn, document)
        )
    except refusal_types:
        pass  # A prior intent is evidence to re-read, never permission to repeat.
    result = read_policy(iam, arn, document)
    if result.state is StepState.ESTABLISHED:
        await settle_verified(
            executor,
            provider="aws-iam",
            kinds=(f"managed-policy/{step}/create", f"managed-policy/{step}/version"),
            target=policy_target(arn, document),
            reference=arn,
        )
    return ControlResult(result.state, result.detail, key)


def read_public_access_block(child, account_id):
    try:
        observed = child.s3control.get_public_access_block(AccountId=account_id)["PublicAccessBlockConfiguration"]
        if all(observed.get(key) is True for key in PUBLIC_ACCESS_BLOCK):
            return ControlResult(StepState.ESTABLISHED, "all four account public-access protections verified")
        return ControlResult(StepState.ABSENT, "account public-access block is incomplete")
    except PolicyAbsent:  # Port maps only NoSuchPublicAccessBlockConfiguration.
        return ControlResult(StepState.ABSENT, "account public-access block is absent")
    except ProviderDenied:
        return ControlResult(StepState.DENIED, "account public-access block read was denied")
    except (ProviderUnavailable, AttributeError, KeyError, TypeError):
        return ControlResult(StepState.NOT_CHECKED, "account public-access block was not observed")


async def ensure_public_access_block(executor, child, account_id, *, refusal_types=()):
    result = read_public_access_block(child, account_id)
    if result.state is StepState.ESTABLISHED:
        await settle_verified(
            executor,
            provider="aws-s3control",
            kinds=("baseline-public-access-block",),
            target=account_id + "#public-access-block-v1",
            reference=account_id,
        )
    if result.state is not StepState.ABSENT:
        return result
    key = f"{executor.operation_id}:baseline-public-access-block"
    try:
        await executor.execute_provider(
            idempotency_key=key,
            provider="aws-s3control",
            operation_kind="baseline-public-access-block",
            target=account_id + "#public-access-block-v1",
        )
    except refusal_types:
        pass
    result = read_public_access_block(child, account_id)
    if result.state is StepState.ESTABLISHED:
        await settle_verified(
            executor,
            provider="aws-s3control",
            kinds=("baseline-public-access-block",),
            target=account_id + "#public-access-block-v1",
            reference=account_id,
        )
    return ControlResult(result.state, result.detail, key)


def read_audit(child, account_id, trail_arn):
    if not trail_arn:
        return ControlResult(StepState.NOT_CHECKED, "reviewed audit trail ARN is required")
    try:
        trails = child.cloudtrail.describe_trails(trailNameList=[trail_arn], includeShadowTrails=True)["trailList"]
        if len(trails) != 1:
            return ControlResult(StepState.ABSENT, "reviewed audit trail was not found")
        trail = trails[0]
        if (
            trail.get("TrailARN") != trail_arn
            or not trail.get("HomeRegion")
            or trail.get("IsMultiRegionTrail") is not True
            or trail.get("IncludeGlobalServiceEvents") is not True
            or trail.get("LogFileValidationEnabled") is not True
            or (trail.get("IsOrganizationTrail") is not True and trail_arn.split(":")[4] != account_id)
        ):
            return ControlResult(StepState.CONFLICT, "audit trail does not provide the reviewed account coverage")
        status = child.cloudtrail.get_trail_status(Name=trail_arn)
        if status.get("IsLogging") is not True or status.get("LatestDeliveryError"):
            return ControlResult(StepState.NOT_CHECKED, "audit delivery is not healthy and logging")
        return ControlResult(StepState.ESTABLISHED, "reviewed multi-region audit trail is logging with integrity validation")
    except ProviderDenied:
        return ControlResult(StepState.DENIED, "audit trail read was denied")
    except (ProviderUnavailable, AttributeError, KeyError, TypeError, IndexError):
        return ControlResult(StepState.NOT_CHECKED, "audit trail coverage could not be verified")


async def control_hook(call, credentials, *, account_id, policy_arns, policy_documents, policy_update_arns, outcomes):
    """Return None for role steps; all control writes are confined to reviewed inputs."""
    kind = call.operation_kind
    if not kind.startswith("managed-policy/") and kind != "baseline-public-access-block":
        return None
    try:
        child = await credentials.child_account(operation_id=call.operation_id, account_id=account_id)
        if kind == "baseline-public-access-block":
            if call.target != account_id + "#public-access-block-v1":
                return outcomes.FAILED, "public-access block target differs from the operation", None
            child.s3control.put_public_access_block(AccountId=account_id, PublicAccessBlockConfiguration=dict(PUBLIC_ACCESS_BLOCK))
            return outcomes.SUCCEEDED, "public-access block requested; verify through authoritative read", account_id
        _, step, action = kind.split("/")
        arn, document = policy_arns[step], policy_documents[step]
        if call.target != policy_target(arn, document) or not arn.startswith(f"arn:aws:iam::{account_id}:policy/"):
            return outcomes.FAILED, "managed policy target differs from the operation", None
        if action == "create":
            path_and_name = arn.split(":policy/", 1)[1]
            path, _, name = path_and_name.rpartition("/")
            child.iam.create_policy(PolicyName=name or path_and_name, Path="/" + path + "/" if path else "/", PolicyDocument=canonical(document))
        elif action == "version" and arn in policy_update_arns:
            child.iam.create_policy_version(PolicyArn=arn, PolicyDocument=canonical(document), SetAsDefault=True)
        else:
            return outcomes.FAILED, "managed policy version replacement was not approved", None
        return outcomes.SUCCEEDED, "managed policy requested; verify default version", arn
    except ProviderDenied:
        return outcomes.FAILED, "baseline control write was denied", None
    except (ProviderUnavailable, AttributeError, KeyError, TypeError, ValueError):
        return outcomes.UNKNOWN, "baseline control write outcome could not be established", None
