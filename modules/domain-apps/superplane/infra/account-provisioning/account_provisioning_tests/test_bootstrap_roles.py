"""A bootstrapped role is one that is USABLE, not one that exists — Issue #5531 (w6-08).

## The defect these tests exist to prevent

The reviewed revision established a role by calling `create_role` and nothing else, and it
decided a role was already established by calling `get_role` and nothing else. Both halves are
wrong in the same way, and together they make the wrongness permanent:

1. A role created with a trust policy and no permission policy **can be assumed and can do
   nothing**. It is not a step toward a working account; it is an identity that fails later and
   somewhere else — as a workspace build that cannot reach the account it was given.
2. On the next pass, `get_role` for that role SUCCEEDS. So the step reported `ESTABLISHED` and
   was skipped. Nothing in the system would ever finish it.

The window between `create_role` and `attach_role_policy` is small but it is real: a denied
attach, a throttle, a killed worker, a lost lease. Any of them lands in it. So "existence is
not usability" needs a read (`list_attached_role_policies`) and a way to act on what that read
says, and the acting is the part with the sharp edge — see `TestRecoveringAHalfBuiltRole`.

## Why these drive the executor and the real hook

Every write below goes through `bootstrap_hook` invoked by `HookExecutor`, which commits intent
under the key before the hook runs and refuses a key that already has a row — the two executor
behaviours this package's correctness rests on. Asserting on a scripted outcome instead would
make the tests agree with a re-implementation of the runner rather than with the runner.

The evidence is the recorded call lists: which IAM call, with which arguments, in which order,
plus which durable keys were consumed. "It raised" and "it returned established" are both far
too weak here, because the failure being prevented is a step that reports success.

## What these tests do NOT establish

That AWS accepts the policies, that the roles work, or that any account was bootstrapped. No
AWS is contacted anywhere in this file and no live account vending is exercised or authorized
by it. Applying any of this to a real child account is a Wave 6 operations-gate activity with
its own named authorization.
"""

from __future__ import annotations

import json

import pytest
from harness_jobs.execution import CallOutcome as AuthoritativeCallOutcome

from account_factory.bootstrap import bootstrap_plan
from account_factory.cleanup import plan as cleanup_plan
from account_factory.creation import (
    AttemptDecision,
    AttemptDisposition,
    CreateAccountAttempt,
    CreateAccountStatus,
    account_identity_key,
)
from account_factory.recovery import StepState, recovery_report
from account_provisioning.bootstrap_runner import (
    ATTACH_SUFFIX,
    BootstrapRefused,
    bootstrap_account,
    bootstrap_hook,
)
from account_provisioning.ports import ProviderDenied, ProviderUnavailable

from .conftest import (
    FIXTURE_CREATED_ACCOUNT,
    FIXTURE_ORG_ID,
    FIXTURE_ORGANIZATIONAL_UNIT,
    FIXTURE_PERMISSION_ARNS,
    FIXTURE_PERMISSION_DOCUMENTS,
    FIXTURE_ROLE_NAMES,
    FIXTURE_TRUST_POLICIES,
    FIXTURE_WORKSPACE,
    HookExecutor,
    RecordingCredentials,
    RecordingIam,
    bootstrapped_trust_documents,
    fully_bootstrapped_roles,
    matching_authorization,
    new_account_request,
    request_for_mode,
    verified_placement,
)

# The three role steps, and the role each one establishes. Paired rather than derived, so a
# test that parametrizes over them is asserting the pairing too: the runner reads the role name
# out of the plan's own `--role-name` argument, and a step silently establishing a differently
# named role is a role nobody creates and a step that reads "absent" forever.
ROLE_STEPS = (
    ("bootstrap-role", "AdpAccountBootstrap"),
    ("controller-role", "AdpWorkspaceController"),
    ("workload-role", "AdpWorkspaceWorkload"),
)


def _authorized_plan(**overrides):
    """A fully authorized bootstrap plan — the only kind `bootstrap_account` will act on."""
    request = new_account_request(**overrides)
    return bootstrap_plan(request, matching_authorization(request))


async def _run(
    iam: RecordingIam,
    *,
    already_recorded: tuple[str, ...] = (),
    permission_policy_arns: dict[str, str] | None = FIXTURE_PERMISSION_ARNS,
    trust_policies: dict[str, str] | None = FIXTURE_TRUST_POLICIES,
    plan=None,
):
    """Drive a real bootstrap against `iam`, through the real hook and the executor double.

    Returns `(report, executor)`: the report is what a caller reads, and the executor holds
    which durable keys were consumed — which is the half that says whether recovery was even
    dispatchable.
    """
    plan = plan if plan is not None else _authorized_plan()
    credentials = RecordingCredentials(iam=iam)
    hook = bootstrap_hook(
        credentials,
        plan,
        account_id=FIXTURE_CREATED_ACCOUNT,
        trust_policies=trust_policies,
        permission_policy_arns=permission_policy_arns,
        permission_policy_documents=FIXTURE_PERMISSION_DOCUMENTS,
        # The REAL harness enum, as the composer passes in production. The executor
        # `isinstance`-checks the hook's answer against this class and substitutes `UNKNOWN`
        # for anything else, so a test that let the hook answer in this package's own copy
        # would read `UNKNOWN` on every path and call it a pass.
        outcomes=AuthoritativeCallOutcome,
    )
    executor = HookExecutor(hook=hook, already_recorded=already_recorded)
    report = await bootstrap_account(
        executor,
        credentials,
        plan,
        account_id=FIXTURE_CREATED_ACCOUNT,
        # The account is in its approved organizational unit, as an authoritative read
        # established. Required rather than defaulted: these steps write account-wide roles,
        # and `TestBootstrapRequiresAVerifiedPlacement` is what covers omitting it.
        placement=verified_placement(),
        trust_policies=trust_policies,
        permission_policy_arns=permission_policy_arns,
        permission_policy_documents=FIXTURE_PERMISSION_DOCUMENTS,
        # The store's refusal of an already-recorded key. Supplied by the composer in
        # production for the reason `execution.py` gives — this package does not import
        # `harness_jobs` — so a test that omitted it would silently test the wrong branch.
        refusal_types=(Exception,),
    )
    return report, executor


def _outcome(report, step_name: str):
    return next(outcome for outcome in report.outcomes if outcome.step == step_name)


def _calls(iam: RecordingIam, name: str) -> list[dict]:
    return [kwargs for called, kwargs in iam.calls if called == name]


def _created_decision() -> AttemptDecision:
    """A recorded, settled creation for the account `_authorized_plan` describes.

    Needed to build a `recovery_report`, which refuses a plan and an attempt about different
    accounts — so the identity key is derived from the same request the plan is built from
    rather than written as a literal.
    """
    request = new_account_request()
    return AttemptDecision(
        disposition=AttemptDisposition.ALREADY_CREATED,
        reason="a recorded attempt succeeded and its account id is known",
        attempt=CreateAccountAttempt(
            workspace_id=FIXTURE_WORKSPACE,
            organization_id=FIXTURE_ORG_ID,
            organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
            account_email=request.account_email,
            idempotency_key=account_identity_key(request),
            create_account_request_id="car-fixture0000001",
            status=CreateAccountStatus.SUCCEEDED,
            account_id=FIXTURE_CREATED_ACCOUNT,
        ),
    )


class TestAnEmptyAccountGetsCompleteRoles:
    """The positive control: a fresh account, and every role created WITH its permissions."""

    @pytest.mark.asyncio
    async def test_every_role_is_created_and_immediately_attached(self) -> None:
        """Both calls, for all three roles, in that order, in one dispatch each.

        The ordering assertion is the substance. A create and an attach in separate dispatches
        would double the window in which a crash leaves a half-built role, and the whole reason
        the hook performs both is to keep that window as small as it can be made.
        """
        iam = RecordingIam()
        report, executor = await _run(iam)

        for step_name, role_name in ROLE_STEPS:
            outcome = _outcome(report, step_name)
            assert outcome.state is StepState.ESTABLISHED, outcome.detail
            assert outcome.created
            assert outcome.durable_key == f"op-fixture:{step_name}"

        creates = [kwargs["RoleName"] for kwargs in _calls(iam, "create_role")]
        attaches = [(kwargs["RoleName"], kwargs["PolicyArn"]) for kwargs in _calls(iam, "attach_role_policy")]
        assert creates == list(FIXTURE_ROLE_NAMES)
        assert attaches == [(role, FIXTURE_PERMISSION_ARNS[step]) for step, role in ROLE_STEPS]

        # Per role: the create is immediately followed by its own attach. Asserted on the
        # interleaving rather than on the two lists separately, because two correct lists are
        # also consistent with "create all three, then attach all three" — which leaves three
        # half-built roles live at once instead of one at a time.
        writes = [(name, kwargs.get("RoleName")) for name, kwargs in iam.writes]
        for _, role_name in ROLE_STEPS:
            index = writes.index(("create_role", role_name))
            assert writes[index + 1] == ("attach_role_policy", role_name), writes

    @pytest.mark.parametrize(("step_name", "role_name"), ROLE_STEPS)
    @pytest.mark.asyncio
    async def test_the_role_name_comes_from_the_plans_own_command(self, step_name: str, role_name: str) -> None:
        """A role read from one name and created under another is never established.

        The runner takes the name from the `--role-name` argument in the step's command — the
        command an operator would run — rather than deriving it from the step name. This asserts
        the two agree, which is what keeps the manual and automated paths on the same role.
        """
        iam = RecordingIam()
        await _run(iam)

        assert {"RoleName": role_name} in _calls(iam, "get_role")
        assert [kwargs["RoleName"] for kwargs in _calls(iam, "create_role") if kwargs["RoleName"] == role_name] == [role_name]

    @pytest.mark.asyncio
    async def test_a_created_role_is_verifiable_as_complete_afterwards(self) -> None:
        """A second pass over the account the first pass built reads it as done, and writes
        nothing. Stated as an end-to-end property because it is the one that matters: the first
        pass's work has to be recognisable BY THE SAME CODE, or every pass re-does it."""
        iam = RecordingIam()
        await _run(iam)
        first_pass_writes = len(iam.writes)

        second = RecordingIam(
            present_roles=set(iam.present_roles),
            # Carried from the first pass rather than re-stated: the property under test is
            # that the SAME code recognises what it itself created, and a fixture that
            # supplied the reviewed documents independently would test something weaker.
            trust_documents=dict(iam.trust_documents),
            attached_policies=dict(iam.attached_policies),
        )
        report, executor = await _run(second)

        assert second.writes == [], f"a second pass over a bootstrapped account wrote: {second.writes}"
        assert executor.dispatches == [], "a reuse-only pass consumed a durable key"
        assert first_pass_writes > 0, "fixture precondition: the first pass must actually have written"
        for step_name, _ in ROLE_STEPS:
            assert _outcome(report, step_name).state is StepState.ESTABLISHED


class TestExistingRolesAreReusedNotRewritten:
    """ "Reuse if present" means present AND usable, which is the corrected meaning."""

    @pytest.mark.asyncio
    async def test_complete_roles_are_reused_with_no_write_and_no_durable_key(self) -> None:
        iam = RecordingIam(
            present_roles={*FIXTURE_ROLE_NAMES, "AWSServiceRoleForAutoScaling"},
            trust_documents=bootstrapped_trust_documents(),
            attached_policies=fully_bootstrapped_roles(),
        )
        report, executor = await _run(iam)

        assert iam.writes == [], f"an already-bootstrapped account was written to: {iam.writes}"
        assert executor.dispatches == [], "reuse consumed a durable key it did not need"
        for step_name, _ in ROLE_STEPS:
            outcome = _outcome(report, step_name)
            assert outcome.state is StepState.ESTABLISHED
            assert not outcome.created, "a reused role was reported as created by this pass"
            assert outcome.durable_key is None

    @pytest.mark.asyncio
    async def test_reuse_reads_both_existence_and_usability(self) -> None:
        """Two reads per role, not one. The second read IS the repair.

        A pass that asked only `get_role` is exactly the pass that reported a half-built role
        established, so the absence of the attachment read is the defect, and asserting the read
        happens is how it stays absent-proof.
        """
        iam = RecordingIam(
            present_roles={*FIXTURE_ROLE_NAMES, "AWSServiceRoleForAutoScaling"},
            trust_documents=bootstrapped_trust_documents(),
            attached_policies=fully_bootstrapped_roles(),
        )
        await _run(iam)

        for _, role_name in ROLE_STEPS:
            assert {"RoleName": role_name} in _calls(iam, "get_role")
            assert {"RoleName": role_name} in _calls(iam, "list_attached_role_policies"), (
                f"{role_name} was reused on its existence alone, so a role with no permissions would have passed"
            )

    @pytest.mark.asyncio
    async def test_the_detail_names_the_policy_the_role_was_reused_with(self) -> None:
        """An operator reading "reused unchanged" needs to know reused as WHAT.

        This test's own reasoning was part of the finding, and the correction is worth stating.
        It used to say that a role of the right name attached to the WRONG policy is accepted,
        "which is correct (it is not this runner's business to re-scope an existing identity)",
        with the named detail as the mitigation. The premise is right and the conclusion did not
        follow: not re-scoping an existing identity is indeed not this runner's business, but
        neither is REUSING it. Between those two there is a third answer — refuse and report —
        and that is the one a shared account-wide identity calls for.

        So the wrong policy is now a CONFLICT (see `TestAnExistingRoleIsVerifiedNotAssumed`), and
        this test keeps only the part that was always right: when a role IS the reviewed one, the
        report says which policy it was reused with, so the reader is not taking it on trust.
        """
        iam = RecordingIam(
            present_roles={*FIXTURE_ROLE_NAMES, "AWSServiceRoleForAutoScaling"},
            trust_documents=bootstrapped_trust_documents(),
            attached_policies=fully_bootstrapped_roles(),
        )
        report, _ = await _run(iam)

        detail = _outcome(report, "controller-role").detail
        assert "AdpWorkspaceControllerPermissions" in detail, detail


FOREIGN_TRUST_DOCUMENT = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": "arn:aws:iam::210987654321:root"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)
"""A well-formed trust policy admitting an account this plan never authorized.

The shape of the real danger: not a malformed document (which would fail on its own), but a
perfectly valid one naming the wrong principal. `210987654321` is not any fixture account.
"""

ADMINISTRATOR_ACCESS = "arn:aws:iam::aws:policy/AdministratorAccess"


class TestAnExistingRoleIsVerifiedNotAssumed:
    """A role of the right NAME is not the right role — Issue #5531 finding F5.

    The reviewed revision read `get_role`, read `list_attached_role_policies`, and reported
    "already exists and is reused unchanged" — a sentence in which *unchanged* had never been
    established, because the only thing compared was the name. Every test below constructs a
    role that passes both of those reads and is not the role the plan describes.

    Why the name is the weak part: it is the one attribute that is published, guessable and
    stable across accounts. A role called `AdpWorkspaceController` can arrive in a child account
    from an older revision of these documents, from a hand-made copy, or from anyone who can
    write IAM there. What decides whether it is safe is who may assume it and what it may do —
    the two things nothing read.

    None of these tests contacts AWS or bootstraps an account.
    """

    def _existing_role(self, role_name: str, **overrides) -> RecordingIam:
        """An account where every role is the reviewed one except `role_name`.

        The others are complete so the pass reaches the role under test rather than stopping
        earlier — execution halts at the first step that blocks, and `bootstrap-role` is first.
        """
        trust = bootstrapped_trust_documents()
        attached = fully_bootstrapped_roles()
        for key, value in overrides.items():
            if key == "trust":
                trust[role_name] = value
            elif key == "policies":
                attached[role_name] = value
        return RecordingIam(
            present_roles={*FIXTURE_ROLE_NAMES, "AWSServiceRoleForAutoScaling"},
            trust_documents=trust,
            attached_policies=attached,
        )

    @pytest.mark.parametrize(("step_name", "role_name"), ROLE_STEPS)
    @pytest.mark.asyncio
    async def test_a_role_trusting_an_unreviewed_principal_is_a_conflict(self, step_name: str, role_name: str) -> None:
        """The tenant-isolation case, for each of the three tiers.

        A role this plan's name refers to, which a party nobody reviewed can assume. Reusing it
        hands that party whatever the tier can do — for `bootstrap-role`, that is `iam:CreateRole`
        and `iam:AttachRolePolicy` inside a customer's account.
        """
        iam = self._existing_role(role_name, trust=FOREIGN_TRUST_DOCUMENT)
        report, executor = await _run(iam)

        outcome = _outcome(report, step_name)
        assert outcome.state is StepState.CONFLICT, outcome.detail
        assert "NOT the reviewed one" in outcome.detail
        assert "who may assume it" in outcome.detail
        # Not reused, and not written over either: both are decisions this module does not take.
        assert iam.writes == [], f"a conflicting role was written to: {iam.writes}"
        assert executor.dispatches == [], "a conflicting role consumed a durable key"
        assert outcome.durable_key is None

    @pytest.mark.asyncio
    async def test_a_conflicting_trust_policy_stops_before_reading_permissions(self) -> None:
        """Nothing the permission read could say would make a wrong-principal role reusable.

        Asserted because the alternative ordering is subtly worse than useless: a pass that read
        the attachments next and found them correct would produce a report mentioning the right
        policy, which reads reassuring next to a role the wrong party can assume.
        """
        iam = self._existing_role("AdpAccountBootstrap", trust=FOREIGN_TRUST_DOCUMENT)
        await _run(iam)

        assert {"RoleName": "AdpAccountBootstrap"} in _calls(iam, "get_role")
        assert {"RoleName": "AdpAccountBootstrap"} not in _calls(iam, "list_attached_role_policies")

    @pytest.mark.parametrize(("step_name", "role_name"), ROLE_STEPS)
    @pytest.mark.asyncio
    async def test_a_role_carrying_a_different_policy_is_a_conflict(self, step_name: str, role_name: str) -> None:
        """Right name, right trust, wrong permissions — and not the half-built role either.

        Distinct from the empty-attachment case on purpose. An empty list is `ABSENT`, which
        licenses the attach that completes an interrupted bootstrap. A list holding someone
        else's policy must NOT license that: attaching would add the reviewed policy alongside
        the unreviewed one and report the role complete.
        """
        iam = self._existing_role(role_name, policies=["arn:aws:iam::123456789012:policy/SomethingNobodyReviewed"])
        report, executor = await _run(iam)

        outcome = _outcome(report, step_name)
        assert outcome.state is StepState.CONFLICT, outcome.detail
        assert "NOT the reviewed policy" in outcome.detail
        assert "SomethingNobodyReviewed" in outcome.detail
        assert iam.writes == [], f"a role with unreviewed permissions was written to: {iam.writes}"
        assert executor.dispatches == []

    @pytest.mark.parametrize(("step_name", "role_name"), ROLE_STEPS)
    @pytest.mark.asyncio
    async def test_the_reviewed_policy_plus_administrator_access_is_a_conflict(self, step_name: str, role_name: str) -> None:
        """The reviewed policy IS attached, and so is `AdministratorAccess`.

        The case the "any policy is enough" reading passed most confidently: the reviewed ARN is
        present, so a membership test succeeds and the detail even names the right policy. The
        tier boundary is nonetheless gone — the whole reason the workload tier cannot write IAM
        is so that it cannot re-bootstrap the account, and `AdministratorAccess` restores that.
        """
        expected = FIXTURE_PERMISSION_ARNS[step_name]
        iam = self._existing_role(role_name, policies=[expected, ADMINISTRATOR_ACCESS])
        report, _ = await _run(iam)

        outcome = _outcome(report, step_name)
        assert outcome.state is StepState.CONFLICT, outcome.detail
        assert ADMINISTRATOR_ACCESS in outcome.detail
        assert "more authority than this tier is meant to hold" in outcome.detail
        assert iam.writes == []

    @pytest.mark.asyncio
    async def test_an_extra_policy_is_reported_and_never_detached(self) -> None:
        """Reported rather than repaired. Detaching is as destructive as replacing.

        This module cannot know what granted the extra policy or what relies on it, and the role
        is account-wide — shared by every workspace in the account. So no `detach_role_policy` is
        attempted, which is also why the port has no such method.
        """
        iam = self._existing_role(
            "AdpWorkspaceWorkload",
            policies=[FIXTURE_PERMISSION_ARNS["workload-role"], ADMINISTRATOR_ACCESS],
        )
        await _run(iam)

        assert iam.attached_policies["AdpWorkspaceWorkload"] == [
            FIXTURE_PERMISSION_ARNS["workload-role"],
            ADMINISTRATOR_ACCESS,
        ], "the runner changed an existing role's policy set"
        assert not hasattr(iam, "detach_role_policy") or "detach_role_policy" not in [name for name, _ in iam.calls]

    @pytest.mark.asyncio
    async def test_a_role_read_that_returns_no_trust_document_is_not_reused(self) -> None:
        """An absent document means the comparison was NOT MADE, which is not a pass.

        A port implementation that omits `AssumeRolePolicyDocument` would otherwise reinstate the
        entire finding silently: every existing role would compare equal to nothing and be
        reused. `NOT_CHECKED` rather than `CONFLICT`, because nothing established a difference
        either — and the two call for different actions (read it properly vs. decide about it).
        """
        trust = bootstrapped_trust_documents()
        del trust["AdpAccountBootstrap"]
        iam = RecordingIam(
            present_roles={*FIXTURE_ROLE_NAMES, "AWSServiceRoleForAutoScaling"},
            trust_documents=trust,
            attached_policies=fully_bootstrapped_roles(),
        )
        report, _ = await _run(iam)

        outcome = _outcome(report, "bootstrap-role")
        assert outcome.state is StepState.NOT_CHECKED, outcome.detail
        assert "no AssumeRolePolicyDocument" in outcome.detail
        assert "not reused on that basis" in outcome.detail
        assert iam.writes == []

    @pytest.mark.asyncio
    async def test_an_unparseable_trust_document_is_not_reused(self) -> None:
        """Not a CONFLICT: a document this cannot read is one whose meaning was not established.

        Fingerprinting the raw bytes instead would report a difference that may not exist, and
        send an operator to reconcile a role that is in fact correct.
        """
        iam = self._existing_role("AdpAccountBootstrap", trust="{not json at all")
        report, _ = await _run(iam)

        outcome = _outcome(report, "bootstrap-role")
        assert outcome.state is StepState.NOT_CHECKED, outcome.detail
        assert "could not be parsed" in outcome.detail
        assert iam.writes == []

    @pytest.mark.asyncio
    async def test_the_same_trust_policy_written_differently_is_not_a_conflict(self) -> None:
        """The false-positive guard, and it is as important as the detection.

        AWS returns `AssumeRolePolicyDocument` in its own normalization, not the string it was
        given: different whitespace, different key order, and `{"AWS": "arn"}` where the input
        said `{"AWS": ["arn"]}`. A raw string comparison would therefore report CONFLICT for a
        correct account on the common path — which blocks legitimate work and, worse, teaches
        whoever reads these reports that CONFLICT means nothing.
        """
        reviewed = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": ["arn:aws:iam::123456789012:root"]},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        )
        # Same meaning: key order reversed, principal de-listed, whitespace added.
        as_aws_returns_it = """{
            "Statement": [ { "Action": "sts:AssumeRole",
                             "Principal": { "AWS": "arn:aws:iam::123456789012:root" },
                             "Effect": "Allow" } ],
            "Version": "2012-10-17"
        }"""
        trust_policies = dict(FIXTURE_TRUST_POLICIES)
        trust_policies["bootstrap-role"] = reviewed
        iam = self._existing_role("AdpAccountBootstrap", trust=as_aws_returns_it)
        report, _ = await _run(iam, trust_policies=trust_policies)

        outcome = _outcome(report, "bootstrap-role")
        assert outcome.state is StepState.ESTABLISHED, outcome.detail
        # The reported detail is the ATTACHMENT read's, because that is the last thing checked on
        # the reuse path. So the evidence that the trust comparison passed is that this step got
        # as far as the attachment read at all: a CONFLICT returns before it.
        assert "exactly the reviewed policy" in outcome.detail
        assert {"RoleName": "AdpAccountBootstrap"} in _calls(iam, "list_attached_role_policies")
        assert iam.writes == []

    @pytest.mark.asyncio
    async def test_a_conflict_stops_the_pass_and_does_not_mask_it_as_a_denial(self) -> None:
        """A conflict on the bootstrap role stops the run, and says so as a conflict.

        Reported as DENIED it would send an operator to grant a permission, which changes
        nothing, and the second reading — that bootstrap lacks access — is actively misleading:
        the read succeeded, and what it found is the problem.
        """
        iam = self._existing_role("AdpAccountBootstrap", trust=FOREIGN_TRUST_DOCUMENT)
        report, _ = await _run(iam)

        assert _outcome(report, "bootstrap-role").state is StepState.CONFLICT
        assert "bootstrap-role" in report.blocked_on
        # The steps after it were not attempted, rather than reported absent.
        assert _outcome(report, "controller-role").state is StepState.NOT_CHECKED
        assert "blocks workspace provisioning" in _outcome(report, "controller-role").detail

    @pytest.mark.asyncio
    async def test_a_conflict_flows_into_the_recovery_report_as_a_decision(self) -> None:
        """End to end into `account_factory.recovery`: the report an operator actually reads.

        The two halves are separately written, so this is the assertion that the runner's state
        and the report's advice agree — a runner that detected the conflict while the report
        still said "re-run bootstrap" would have repaired nothing an operator can see.
        """
        iam = self._existing_role("AdpWorkspaceController", trust=FOREIGN_TRUST_DOCUMENT)
        report, _ = await _run(iam)

        plan = _authorized_plan()
        recovery = recovery_report(
            plan,
            _created_decision(),
            report.observations(),
            report.details(),
        )

        assert recovery.conflicting == ("controller-role",)
        assert recovery.summary.startswith("CONFLICT")
        assert not recovery.bootstrap_retry_is_safe
        assert not recovery.ready_for_workspace_provisioning
        finding = next(f for f in recovery.findings if f.name == "controller-role")
        assert "DECIDE" in finding.next_action


class TestOnlyAVerifiedAbsenceLeadsToACreate:
    """The single condition that permits a write, kept apart from the two that do not."""

    @pytest.mark.parametrize(("step_name", "role_name"), ROLE_STEPS)
    @pytest.mark.asyncio
    async def test_a_denied_existence_read_creates_nothing(self, step_name: str, role_name: str) -> None:
        """ "I am not permitted to look" is not "it is not there".

        Creating on a denied read fails against a role that is already present, and that
        failure arrives mid-bootstrap against a live account. So the step stops, carrying the
        plan's own remediation text — and crucially, no create is attempted for it.
        """
        iam = RecordingIam(denied_reads={role_name})
        report, executor = await _run(iam)

        outcome = _outcome(report, step_name)
        assert outcome.state is StepState.DENIED
        assert "NOT evidence the role is absent" in outcome.detail
        assert outcome.durable_key is None, "a denied read consumed a durable key"
        assert _calls(iam, "create_role") == [] or role_name not in [kwargs["RoleName"] for kwargs in _calls(iam, "create_role")]
        assert [d for d in executor.dispatches if d["target"] == role_name] == []

    @pytest.mark.parametrize(("step_name", "role_name"), ROLE_STEPS)
    @pytest.mark.asyncio
    async def test_an_unanswered_existence_read_creates_nothing(self, step_name: str, role_name: str) -> None:
        """No answer obtained is its own state, and it is not absence either."""
        iam = RecordingIam(unavailable_reads={role_name})
        report, _ = await _run(iam)

        outcome = _outcome(report, step_name)
        assert outcome.state is StepState.NOT_CHECKED
        assert "no answer" in outcome.detail
        assert role_name not in [kwargs["RoleName"] for kwargs in _calls(iam, "create_role")]

    @pytest.mark.asyncio
    async def test_a_denied_create_is_reported_as_denied_and_not_retried(self) -> None:
        """AWS refusing the create is authoritative: nothing was created.

        Reported `DENIED` with remediation, and exactly one dispatch. The absence of a second
        dispatch is the assertion — a runner that retried an authoritative refusal would burn
        the durable key it needs for the recovery pass.
        """
        iam = RecordingIam(create_result=ProviderDenied("AccessDenied: iam:CreateRole"))
        report, executor = await _run(iam)

        outcome = _outcome(report, "bootstrap-role")
        assert outcome.state is StepState.DENIED
        assert "Remediation" in outcome.detail
        assert len(executor.dispatches) == 1, executor.dispatches
        assert _calls(iam, "attach_role_policy") == [], "a policy was attached to a role that was never created"

    @pytest.mark.asyncio
    async def test_a_denied_create_stops_the_steps_that_depend_on_it(self) -> None:
        """The bootstrap role is what writes IAM. Without it the later steps cannot be
        attempted, and they are reported `NOT_CHECKED` rather than absent — nobody looked."""
        iam = RecordingIam(create_result=ProviderDenied("AccessDenied: iam:CreateRole"))
        report, _ = await _run(iam)

        assert _outcome(report, "controller-role").state is StepState.NOT_CHECKED
        assert "not attempted" in _outcome(report, "controller-role").detail
        assert not report.complete
        assert report.creation_must_not_be_retried, "a bootstrap failure must never license a second account"

    @pytest.mark.asyncio
    async def test_an_unanswered_create_is_settled_by_reading_not_by_repeating(self) -> None:
        """A create whose answer was lost may have landed, so the account is asked.

        Here it did land: the double's `create_role` records the role before raising on the
        attach... so this specific case is the half-built one, covered below. What this test
        pins is the narrower property that the runner re-reads at all, and issues no second
        create.
        """
        iam = RecordingIam(create_result=ProviderUnavailable("connection reset"))
        report, executor = await _run(iam)

        outcome = _outcome(report, "bootstrap-role")
        assert outcome.state is not StepState.ESTABLISHED
        assert len(_calls(iam, "create_role")) == 1, "the create was repeated after an unobtained answer"
        assert len(executor.dispatches) == 1
        # The role was NOT created (the double raises before recording it), and the re-read says
        # so — but the report must still not claim the absence as established fact, because the
        # answer to "did the create land" was never obtained.
        assert outcome.durable_key == "op-fixture:bootstrap-role"
        assert outcome.may_have_acted


class TestRecoveringAHalfBuiltRole:
    """The state the review named: present, powerless, and previously reported established."""

    def _half_built(self, role_name: str = "AdpAccountBootstrap") -> RecordingIam:
        """Exactly what an interrupted bootstrap leaves: the role, and no attachment.

        Every other role complete, so the assertions are about this one and the pass does not
        stop before reaching it.
        """
        attached = fully_bootstrapped_roles()
        del attached[role_name]
        return RecordingIam(
            present_roles={*FIXTURE_ROLE_NAMES, "AWSServiceRoleForAutoScaling"},
            trust_documents=bootstrapped_trust_documents(),
            attached_policies=attached,
        )

    @pytest.mark.parametrize(("step_name", "role_name"), ROLE_STEPS)
    @pytest.mark.asyncio
    async def test_a_present_role_with_no_permissions_is_not_established(self, step_name: str, role_name: str) -> None:
        """The headline regression. `get_role` succeeds for this role; it is still not done."""
        iam = self._half_built(role_name)
        report, _ = await _run(iam)

        outcome = _outcome(report, step_name)
        assert outcome.state is StepState.ESTABLISHED, outcome.detail
        # Established because this pass FINISHED it — asserted below. What must never happen is
        # that it was established without anything being written.
        assert _calls(iam, "attach_role_policy") == [{"RoleName": role_name, "PolicyArn": FIXTURE_PERMISSION_ARNS[step_name]}]

    @pytest.mark.parametrize(("step_name", "role_name"), ROLE_STEPS)
    @pytest.mark.asyncio
    async def test_recovery_attaches_and_does_not_create(self, step_name: str, role_name: str) -> None:
        """The sharp edge: the role is already there, so a second create is not the repair.

        `create_role` against an existing role raises `EntityAlreadyExists`, which this runner
        would report as a bootstrap failure — leaving the account permanently stuck in exactly
        the state that needed fixing. So recovery attaches, and the assertion that no create was
        issued is the one that distinguishes the two.
        """
        iam = self._half_built(role_name)
        report, executor = await _run(iam)

        assert _calls(iam, "create_role") == [], f"recovery re-created an existing role: {iam.calls}"
        assert _outcome(report, step_name).created, "a repaired role should be reported as written to by this pass"
        assert "completing the role" in _outcome(report, step_name).detail

    @pytest.mark.parametrize(("step_name", "role_name"), ROLE_STEPS)
    @pytest.mark.asyncio
    async def test_recovery_dispatches_under_the_attach_key_not_the_creates(self, step_name: str, role_name: str) -> None:
        """Two effects, two fences, and this is why the suffix exists.

        The create's key is consumed the moment the first pass commits intent — before its
        `create_role` even runs. If the attach shared that key, the store would refuse it on
        every subsequent pass, and the half-built role could never be finished by anything. So
        the recovery dispatch carries `:attach-permissions`, derived from the step name and
        nothing else, so two workers recovering the same account derive the same key and one
        loses at the unique constraint.
        """
        iam = self._half_built(role_name)
        report, executor = await _run(iam, already_recorded=(f"op-fixture:{step_name}",))

        expected = f"op-fixture:{step_name}:{ATTACH_SUFFIX}"
        assert [d["idempotency_key"] for d in executor.dispatches] == [expected]
        assert executor.refused == [], "the attach was refused, so the create's key was reused"
        assert _outcome(report, step_name).durable_key == expected
        assert _outcome(report, step_name).state is StepState.ESTABLISHED
        assert _calls(iam, "attach_role_policy") == [{"RoleName": role_name, "PolicyArn": FIXTURE_PERMISSION_ARNS[step_name]}]

    @pytest.mark.asyncio
    async def test_the_attach_dispatch_names_the_step_it_is_finishing(self) -> None:
        """One hook serves both, so the dispatch has to say which it is.

        `operation_kind` carries `<step>:attach-permissions`, and the hook strips the suffix to
        find the step. A dispatch that named only the step would make the hook create the role
        again; one that named an unrecognised kind would be `FAILED` without acting.
        """
        iam = self._half_built()
        _, executor = await _run(iam)

        kinds = [d["operation_kind"] for d in executor.dispatches]
        assert kinds == [f"bootstrap-role:{ATTACH_SUFFIX}"]
        assert executor.dispatches[0]["target"] == "AdpAccountBootstrap"

    @pytest.mark.asyncio
    async def test_a_denied_attach_leaves_the_role_reported_unusable(self) -> None:
        """AWS refusing the attach is authoritative, and the honest report is not "established".

        The role remains assumable and powerless, and the detail has to say so — this is the
        one state where a reader who trusts "the role exists" draws exactly the wrong
        conclusion.
        """
        iam = self._half_built()
        iam.attach_result = ProviderDenied("AccessDenied: iam:AttachRolePolicy")
        report, _ = await _run(iam)

        outcome = _outcome(report, "bootstrap-role")
        assert outcome.state is StepState.DENIED
        assert "assumable and powerless" in outcome.detail
        assert "Remediation" in outcome.detail
        assert not report.complete

    @pytest.mark.asyncio
    async def test_an_unanswered_attach_is_settled_by_re_reading_the_attachments(self) -> None:
        """The attach may have landed. The account is asked, and it had.

        The double appends the policy before raising, which is the real sequence when a response
        is lost in transit: the effect happened and the answer did not arrive. A runner that
        reported this `NOT_CHECKED` would leave a finished role looking unfinished forever; one
        that retried would attach twice.
        """
        iam = self._half_built()

        real_attach = iam.attach_role_policy

        def lands_then_loses_the_answer(**kwargs):
            real_attach(**kwargs)
            raise ProviderUnavailable("the attach response was not received")

        iam.attach_role_policy = lands_then_loses_the_answer
        report, executor = await _run(iam)

        outcome = _outcome(report, "bootstrap-role")
        assert outcome.state is StepState.ESTABLISHED, outcome.detail
        assert "no usable answer" in outcome.detail
        assert len(_calls(iam, "attach_role_policy")) == 1, "the attach was repeated after an unobtained answer"

    @pytest.mark.asyncio
    async def test_a_refused_second_attach_dispatch_is_settled_by_reading(self) -> None:
        """A restart whose attach already landed must not be refused into a false failure.

        The store refuses the attach key because a previous pass consumed it. That refusal says
        nothing about whether the attach took effect, so the answer comes from the account.
        """
        iam = RecordingIam(
            present_roles={*FIXTURE_ROLE_NAMES, "AWSServiceRoleForAutoScaling"},
            trust_documents=bootstrapped_trust_documents(),
            attached_policies=fully_bootstrapped_roles(),
        )
        # The role IS complete; the earlier pass just never recorded its own success.
        report, executor = await _run(iam, already_recorded=(f"op-fixture:bootstrap-role:{ATTACH_SUFFIX}",))

        assert _outcome(report, "bootstrap-role").state is StepState.ESTABLISHED
        # It was reused on the read, so no dispatch was even attempted — the completeness check
        # comes first, which is what makes a restart cheap rather than a retry.
        assert executor.dispatches == []
        assert iam.writes == []

    @pytest.mark.asyncio
    async def test_a_denied_attachment_read_does_not_license_a_write(self) -> None:
        """Unable to see whether a role has permissions is not "it has none".

        Attaching on that assumption writes to a role whose current policy set is unknown, using
        an identity that has just been shown to lack visibility into it. So the step stops.
        """
        iam = RecordingIam(
            present_roles={*FIXTURE_ROLE_NAMES, "AWSServiceRoleForAutoScaling"},
            trust_documents=bootstrapped_trust_documents(),
            attached_policies=fully_bootstrapped_roles(),
            denied_attachment_reads={"AdpAccountBootstrap"},
        )
        report, executor = await _run(iam)

        outcome = _outcome(report, "bootstrap-role")
        assert outcome.state is StepState.DENIED
        assert "not yet usable" in outcome.detail
        assert iam.writes == [], f"a denied attachment read still wrote: {iam.writes}"
        assert executor.dispatches == []

    @pytest.mark.asyncio
    async def test_an_unanswered_attachment_read_does_not_license_a_write(self) -> None:
        iam = RecordingIam(
            present_roles={*FIXTURE_ROLE_NAMES, "AWSServiceRoleForAutoScaling"},
            trust_documents=bootstrapped_trust_documents(),
            attached_policies=fully_bootstrapped_roles(),
            unavailable_attachment_reads={"AdpAccountBootstrap"},
        )
        report, _ = await _run(iam)

        assert _outcome(report, "bootstrap-role").state is StepState.NOT_CHECKED
        assert iam.writes == []


class TestAPartialFailureInsideOneDispatch:
    """The create lands and the attach does not — how the half-built role is born."""

    @pytest.mark.asyncio
    async def test_a_create_followed_by_a_denied_attach_is_not_reported_failed(self) -> None:
        """`FAILED` means the provider established nothing was created. The role EXISTS.

        This is the distinction the whole story turns on, applied one level down from account
        creation. A caller reading `FAILED` about this step would retry the create against the
        role that is now sitting in the account.
        """
        iam = RecordingIam(attach_result=ProviderDenied("AccessDenied: iam:AttachRolePolicy"))
        report, executor = await _run(iam)

        outcome = _outcome(report, "bootstrap-role")
        assert outcome.state is not StepState.ESTABLISHED
        assert outcome.state is not StepState.ABSENT, "a created role was reported absent"
        assert "AdpAccountBootstrap" in iam.present_roles, "fixture precondition: the create must have landed"
        assert len(_calls(iam, "create_role")) == 1, "the create was repeated against an existing role"

    @pytest.mark.asyncio
    async def test_the_durable_row_records_that_the_role_exists(self) -> None:
        """The recovery instruction has to survive the process that learned it.

        This pass may be the last thing that ever runs. What the next operator or pass has is
        the durable row, so the sentence "the role exists, re-read it, do not create it again"
        belongs in the row's detail — not only in this pass's report.
        """
        iam = RecordingIam(attach_result=ProviderDenied("AccessDenied: iam:AttachRolePolicy"))
        credentials = RecordingCredentials(iam=iam)
        plan = _authorized_plan()
        hook = bootstrap_hook(
            credentials,
            plan,
            account_id=FIXTURE_CREATED_ACCOUNT,
            trust_policies=FIXTURE_TRUST_POLICIES,
            permission_policy_arns=FIXTURE_PERMISSION_ARNS,
            outcomes=AuthoritativeCallOutcome,
        )
        executor = HookExecutor(hook=hook)

        call, _ = await executor.execute_provider(
            idempotency_key="op-fixture:bootstrap-role",
            provider="aws-iam",
            operation_kind="bootstrap-role",
            target="AdpAccountBootstrap",
        )

        assert call.outcome is AuthoritativeCallOutcome.UNKNOWN, (
            "a create that landed followed by a failed attach must not settle as FAILED: the role exists, and FAILED asserts the opposite"
        )
        assert "WAS created" in call.detail
        assert "do not create it again" in call.detail
        assert call.provider_ref == "AdpAccountBootstrap"

    @pytest.mark.asyncio
    async def test_the_report_does_not_call_the_half_built_role_established(self) -> None:
        """The recovery re-read must ask both questions, or it re-introduces the defect.

        After a lost answer the runner re-reads. A re-read that asked only `get_role` would find
        the role present and report `ESTABLISHED` — the original bug, relocated to the recovery
        path, where it is harder to see.
        """
        iam = RecordingIam(attach_result=ProviderUnavailable("the attach response was not received"))
        report, _ = await _run(iam)

        outcome = _outcome(report, "bootstrap-role")
        assert outcome.state is StepState.NOT_CHECKED, outcome.detail
        assert "IS present on re-read" in outcome.detail
        assert "not usable" in outcome.detail
        assert outcome.created, "the create did land, and the report should say so"
        assert outcome.may_have_acted

    @pytest.mark.asyncio
    async def test_the_next_pass_finishes_what_the_partial_one_started(self) -> None:
        """End to end, across two passes and two keys — the property all of this is for.

        Pass one creates the role and fails the attach. Pass two, with the create's key already
        consumed, attaches under the suffix key and the account ends up correct. Written as one
        test because neither half alone establishes that the account converges.
        """
        iam = RecordingIam(attach_result=ProviderDenied("AccessDenied: iam:AttachRolePolicy"))
        first, first_executor = await _run(iam)
        assert _outcome(first, "bootstrap-role").state is not StepState.ESTABLISHED
        assert "AdpAccountBootstrap" in iam.present_roles

        # The operator granted the missing permission; everything else is as pass one left it.
        iam.attach_result = None
        second, second_executor = await _run(iam, already_recorded=tuple(first_executor.recorded))

        outcome = _outcome(second, "bootstrap-role")
        assert outcome.state is StepState.ESTABLISHED, outcome.detail
        assert outcome.durable_key == f"op-fixture:bootstrap-role:{ATTACH_SUFFIX}"
        # Scoped to THIS role. Pass two legitimately creates the other two, because pass one
        # stopped at the bootstrap role — it is the tier that writes IAM, so the steps after it
        # were never attempted. The property under test is that the half-built role is not
        # created a second time, not that pass two writes nothing.
        bootstrap_creates = [kwargs for kwargs in _calls(iam, "create_role") if kwargs["RoleName"] == "AdpAccountBootstrap"]
        assert len(bootstrap_creates) == 1, "the second pass created the existing role again"
        assert second_executor.refused == [], "the second pass was refused, so it reused a consumed key"
        assert iam.attached_policies["AdpAccountBootstrap"] == [FIXTURE_PERMISSION_ARNS["bootstrap-role"]]
        # And the account converges: after the two passes every role is complete.
        assert second.blocked_on == ("baseline-audit-logging", "baseline-public-access-block"), second.blocked_on


class TestNoRoleIsCreatedWithoutItsPermissions:
    """The gap is refused before a credential is obtained, not discovered mid-run."""

    @pytest.mark.parametrize("step_name", [step for step, _ in ROLE_STEPS])
    @pytest.mark.asyncio
    async def test_a_missing_permission_policy_arn_refuses_the_whole_run(self, step_name: str) -> None:
        """Refused up front, and the refusal names the step.

        Discovering it per-step would mean a run that creates the bootstrap role, then finds it
        has no ARN for the controller — an account with an identity in it and an operator
        deciding whether to re-run something that partially applied. Refusing first means
        nothing was touched at all.
        """
        arns = {key: value for key, value in FIXTURE_PERMISSION_ARNS.items() if key != step_name}
        iam = RecordingIam()

        with pytest.raises(BootstrapRefused) as refusal:
            await _run(iam, permission_policy_arns=arns)

        message = str(refusal.value)
        assert step_name in message
        assert "can be assumed and can do nothing" in message
        assert iam.calls == [], f"a refused bootstrap still contacted IAM: {iam.calls}"

    @pytest.mark.asyncio
    async def test_the_hook_itself_also_refuses_rather_than_creating(self) -> None:
        """Defence in depth, because the two guards catch different things.

        The up-front check protects the composed path. This one protects the hook, which is
        reachable by any caller the executor is wired to — and it is the hook that holds
        `iam:CreateRole`. A hook that created the role and left permissions "for later" is the
        defect; a hook that refuses cannot be.
        """
        iam = RecordingIam()
        credentials = RecordingCredentials(iam=iam)
        hook = bootstrap_hook(
            credentials,
            _authorized_plan(),
            account_id=FIXTURE_CREATED_ACCOUNT,
            trust_policies=FIXTURE_TRUST_POLICIES,
            permission_policy_arns={},
            outcomes=AuthoritativeCallOutcome,
        )
        executor = HookExecutor(hook=hook)

        call, _ = await executor.execute_provider(
            idempotency_key="op-fixture:bootstrap-role",
            provider="aws-iam",
            operation_kind="bootstrap-role",
            target="AdpAccountBootstrap",
        )

        assert call.outcome is AuthoritativeCallOutcome.FAILED
        assert iam.writes == [], f"the hook wrote without a permission policy: {iam.writes}"

    @pytest.mark.asyncio
    async def test_a_missing_trust_policy_refuses_and_synthesizes_nothing(self) -> None:
        """A wrong principal is a role the wrong party can assume, so there is no default.

        `step.scope` is prose for an operator ("child account"), not a principal. Guessing who
        may assume a role that can write IAM in a customer's account is not a guess this module
        is entitled to make.
        """
        iam = RecordingIam()

        with pytest.raises(BootstrapRefused) as refusal:
            await _run(iam, trust_policies={})

        assert "will not synthesize a principal" in str(refusal.value)
        assert iam.writes == []


class TestTheAccountWideRolesAreNeverWorkspaceOwned:
    """These roles outlive every workspace, so no workspace may own or delete them."""

    @pytest.mark.asyncio
    async def test_an_adoptable_account_wide_step_is_refused_at_the_point_of_action(self) -> None:
        """Enforced where the write happens, not only where the plan is built.

        A role adopted into one workspace's state is deleted when that workspace is retired,
        which breaks every other workspace in the same account. The plan refuses to mark these
        steps adoptable; this asserts the runner refuses to act on one even if a plan somehow
        arrives with the flag set — the plan and the runner being two separate places the
        property can be lost.
        """
        import dataclasses

        plan = _authorized_plan()
        # `retained_through_workspace_retirement` has to come off in the same edit: the step's
        # own constructor refuses the pair as contradictory (an adopted resource is deleted with
        # the workspace that adopted it), which is a real guard and not one to route around. So
        # the state constructed here is the one that IS expressible — a step a caller has been
        # persuaded is the workspace's to own — and the assertion is that the runner refuses it
        # anyway, at the point of action, rather than relying on the plan having been built well.
        steps = tuple(
            dataclasses.replace(step, adoptable_by_workspace=True, retained_through_workspace_retirement=False)
            if step.name == "bootstrap-role"
            else step
            for step in plan.steps
        )
        plan = dataclasses.replace(plan, steps=steps)
        iam = RecordingIam()

        with pytest.raises(BootstrapRefused) as refusal:
            await _run(iam, plan=plan)

        assert "account-wide" in str(refusal.value)
        assert "delete it on teardown" in str(refusal.value)
        assert iam.writes == []

    def test_workspace_cleanup_does_not_adopt_any_bootstrap_role(self) -> None:
        """The other half of the same property, asserted from the teardown side.

        The runner refusing to create an adoptable role is only half the guarantee: a cleanup
        plan that named one of these roles as a resource to delete would destroy it for every
        other workspace in the account regardless of how it was created. So the roles bootstrap
        establishes must appear in NO workspace cleanup plan, in any mode.
        """
        from account_factory.modes import OwnershipMode

        bootstrap_roles = set(FIXTURE_ROLE_NAMES) | {"AWSServiceRoleForAutoScaling"}
        for mode in OwnershipMode:
            request = request_for_mode(mode)
            plan = cleanup_plan(request, matching_authorization(request))
            for action in plan.actions:
                assert action.name not in bootstrap_roles, f"{mode.value} cleanup deletes the account-wide {action.name}"
                assert action.kind not in {"Role", "IAMRole", "ServiceLinkedRole"}, (
                    f"{mode.value} cleanup deletes an IAM role: {action.kind}/{action.name}"
                )

    def test_the_plan_keeps_every_bootstrap_step_out_of_workspace_ownership(self) -> None:
        """Stated against the plan's own accessor, which is what a composer would read."""
        plan = _authorized_plan()
        assert plan.account_wide_steps == plan.steps, "some bootstrap step is marked workspace-adoptable"
