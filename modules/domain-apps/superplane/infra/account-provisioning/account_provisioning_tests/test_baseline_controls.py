"""Fresh-account baseline execution through real hooks and durable PostgreSQL."""

from types import SimpleNamespace

import pytest
from harness_jobs.execution import CallOutcome, ProviderCallRefused

from account_factory.bootstrap import bootstrap_plan
from account_factory.recovery import StepState
from account_provisioning.baseline import PUBLIC_ACCESS_BLOCK
from account_provisioning.bootstrap_runner import BootstrapRefused, bootstrap_account, bootstrap_hook
from account_provisioning.ports import ProviderDenied, ProviderUnavailable

from .conftest import (
    FIXTURE_CREATED_ACCOUNT,
    FIXTURE_PERMISSION_ARNS,
    FIXTURE_PERMISSION_DOCUMENTS,
    FIXTURE_TRUST_POLICIES,
    RecordingCredentials,
    RecordingIam,
    matching_authorization,
    new_account_request,
    verified_placement,
)
from .test_durability_postgres import _executor, _leased

TRAIL = f"arn:aws:cloudtrail:us-west-2:{FIXTURE_CREATED_ACCOUNT}:trail/reviewed"


class S3Control:
    def __init__(self):
        self.configuration = {}
        self.writes = []
        self.denied_read = False
        self.denied_write = False
        self.lost_write = False

    def get_public_access_block(self, **kwargs):
        assert kwargs == {"AccountId": FIXTURE_CREATED_ACCOUNT}
        if self.denied_read:
            raise ProviderDenied("synthetic denial")
        return {"PublicAccessBlockConfiguration": dict(self.configuration)}

    def put_public_access_block(self, **kwargs):
        if self.denied_write:
            raise ProviderDenied("synthetic denial")
        self.writes.append(kwargs)
        self.configuration = dict(kwargs["PublicAccessBlockConfiguration"])
        if self.lost_write:
            raise ProviderUnavailable("synthetic lost response")
        return {}


class Trail:
    def __init__(self):
        self.logging = True
        self.denied = False

    def describe_trails(self, **kwargs):
        assert kwargs == {"trailNameList": [TRAIL], "includeShadowTrails": True}
        if self.denied:
            raise ProviderDenied("synthetic denial")
        return {
            "trailList": [
                {
                    "TrailARN": TRAIL,
                    "HomeRegion": "us-west-2",
                    "IsMultiRegionTrail": True,
                    "IncludeGlobalServiceEvents": True,
                    "LogFileValidationEnabled": True,
                    "IsOrganizationTrail": False,
                }
            ]
        }

    def get_trail_status(self, **kwargs):
        assert kwargs == {"Name": TRAIL}
        return {"IsLogging": self.logging}


class Credentials(RecordingCredentials):
    def __init__(self, iam):
        super().__init__(iam=iam)
        self.s3 = S3Control()
        self.trail = Trail()

    def _clients(self):
        return SimpleNamespace(iam=self._iam, organizations=self._organizations, s3control=self.s3, cloudtrail=self.trail)


async def composed(pool, key, iam=None, **options):
    iam = iam or RecordingIam()
    credentials = Credentials(iam)
    request = new_account_request()
    plan = bootstrap_plan(request, matching_authorization(request))
    arguments = dict(
        account_id=FIXTURE_CREATED_ACCOUNT,
        trust_policies=FIXTURE_TRUST_POLICIES,
        permission_policy_arns=FIXTURE_PERMISSION_ARNS,
        permission_policy_documents=FIXTURE_PERMISSION_DOCUMENTS,
        audit_trail_arn=TRAIL,
        **options,
    )
    lease = await _leased(pool, key=key)
    hook = bootstrap_hook(credentials, plan, outcomes=CallOutcome, **arguments)
    executor = _executor(lease, pool, hook=hook)

    async def run(**extra):
        return await bootstrap_account(
            executor, credentials, plan, placement=verified_placement(), refusal_types=(ProviderCallRefused,), **arguments, **extra
        )

    return run, executor, credentials, iam


@pytest.mark.asyncio
async def test_fresh_account_establishes_policies_and_s3_then_replays_without_writes(pool):
    iam = RecordingIam()
    iam.permission_documents = {}
    run, executor, credentials, iam = await composed(pool, "baseline-fresh", iam)
    report = await run()
    assert report.complete, [(outcome.step, outcome.state, outcome.detail) for outcome in report.outcomes]
    assert len([call for call, _ in iam.calls if call == "create_policy"]) == 3
    assert credentials.s3.configuration == PUBLIC_ACCESS_BLOCK
    for step in FIXTURE_PERMISSION_ARNS:
        rows = await executor.provider_calls(provider="aws-iam", operation_kind=f"managed-policy/{step}/create")
        assert len(rows) == 1 and rows[0].outcome is CallOutcome.SUCCEEDED
    before = len(iam.writes), len(credentials.s3.writes)
    assert (await run()).complete
    assert before == (len(iam.writes), len(credentials.s3.writes))


@pytest.mark.parametrize("which", ["s3-read", "s3-write", "audit-read", "audit-stopped"])
@pytest.mark.asyncio
async def test_missing_baseline_evidence_cannot_report_ready(pool, which):
    run, _, credentials, _ = await composed(pool, "baseline-denied-" + which)
    credentials.s3.denied_read = which == "s3-read"
    credentials.s3.denied_write = which == "s3-write"
    credentials.trail.denied = which == "audit-read"
    credentials.trail.logging = which != "audit-stopped"
    report = await run()
    assert not report.complete
    if which == "s3-read":
        assert credentials.s3.writes == []


@pytest.mark.asyncio
async def test_lost_s3_response_is_durable_and_reobserved_without_duplicate_write(pool):
    run, executor, credentials, _ = await composed(pool, "baseline-lost-s3")
    credentials.s3.lost_write = True
    assert (await run()).complete
    rows = await executor.provider_calls(provider="aws-s3control", operation_kind="baseline-public-access-block")
    assert rows[0].outcome is CallOutcome.SUCCEEDED
    assert (await run()).complete
    assert len(credentials.s3.writes) == 1


@pytest.mark.asyncio
async def test_policy_drift_requires_explicit_version_update_authority(pool):
    iam = RecordingIam()
    arn = FIXTURE_PERMISSION_ARNS["controller-role"]
    iam.permission_documents[arn] = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
    run, _, _, _ = await composed(pool, "baseline-policy-conflict", iam)
    report = await run()
    assert not report.complete
    assert not any(name == "create_policy_version" for name, _ in iam.writes)
    run, executor, _, _ = await composed(pool, "baseline-policy-approved-version", iam, policy_update_arns=frozenset({arn}))
    assert (await run()).complete
    assert iam.permission_documents[arn] == FIXTURE_PERMISSION_DOCUMENTS["controller-role"]
    rows = await executor.provider_calls(provider="aws-iam", operation_kind="managed-policy/controller-role/version")
    assert len(rows) == 1 and rows[0].outcome is CallOutcome.SUCCEEDED


@pytest.mark.asyncio
async def test_caller_asserted_baseline_cannot_replace_provider_observation(pool):
    run, _, credentials, _ = await composed(pool, "baseline-caller")
    with pytest.raises(BootstrapRefused, match="caller-supplied baseline"):
        await run(observed={"baseline-audit-logging": StepState.ESTABLISHED})
    assert credentials.child_calls == []


@pytest.mark.parametrize("fault", ["read-denied", "create-denied", "create-lost", "version-lost"])
@pytest.mark.asyncio
async def test_policy_write_failures_require_authoritative_readback(pool, fault):
    class FaultIam(RecordingIam):
        def get_policy(self, **kwargs):
            if fault == "read-denied":
                raise ProviderDenied("synthetic policy read denial")
            return super().get_policy(**kwargs)

        def create_policy(self, **kwargs):
            if fault == "create-denied":
                raise ProviderDenied("synthetic policy create denial")
            result = super().create_policy(**kwargs)
            if fault == "create-lost":
                raise ProviderUnavailable("synthetic lost policy response")
            return result

        def create_policy_version(self, **kwargs):
            result = super().create_policy_version(**kwargs)
            if fault == "version-lost":
                raise ProviderUnavailable("synthetic lost version response")
            return result

    iam = FaultIam()
    step = "controller-role" if fault == "version-lost" else "bootstrap-role"
    arn = FIXTURE_PERMISSION_ARNS[step]
    options = {}
    if fault == "version-lost":
        iam.permission_documents[arn] = {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
        options["policy_update_arns"] = frozenset({arn})
    else:
        iam.permission_documents = {}
    run, executor, _, _ = await composed(pool, "baseline-policy-" + fault, iam, **options)
    report = await run()
    if fault.endswith("denied"):
        assert not report.complete
        assert not any(name in {"create_policy", "create_policy_version", "create_role", "attach_role_policy"} for name, _ in iam.writes)
    else:
        assert report.complete
        kind = "version" if fault == "version-lost" else "create"
        rows = await executor.provider_calls(provider="aws-iam", operation_kind=f"managed-policy/{step}/{kind}")
        assert len(rows) == 1 and rows[0].outcome is CallOutcome.SUCCEEDED
        before = len(iam.writes)
        assert (await run()).complete
        assert len(iam.writes) == before
