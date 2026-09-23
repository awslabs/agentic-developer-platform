"""Child-account bootstrap description — Issue #5531 (w6-08), EPIC #4910.

These tests pin the four cases the #5532 handoff named for the Auto Scaling service-linked
role — fresh-account creation, existing-role reuse, denied creation or read, and
workspace-cleanup non-adoption — plus the ordering property the whole prerequisite turns on:
the role must be established BEFORE any workspace KMS key is created, because the key policy
names the role ARN and KMS validates principals at key-creation time.

## What these tests deliberately do NOT establish

Nothing here calls AWS, creates a role, reads a role, or bootstraps an account. Every
assertion is about the DESCRIPTION `bootstrap.py` produces: which steps it names, in which
order, with which presence rule and which remediation text. That a real account ends up with
`AWSServiceRoleForAutoScaling`, that `iam:CreateServiceLinkedRole` is in fact scoped to
`autoscaling.amazonaws.com` on a live identity, and that a live KMS key creation then
succeeds are LIVE criteria. They require separately named authorization (AC-02) and cannot be
closed by this file. Mocked or offline evidence never closes a live criterion.
"""

from __future__ import annotations

import pytest
from account_factory import bootstrap
from account_factory.bootstrap import (
    AUTOSCALING_SERVICE_LINKED_ROLE,
    AUTOSCALING_SERVICE_PRINCIPAL,
    AUTOSCALING_STEP,
    BOOTSTRAP_ROLE_STEP,
    BootstrapError,
    BootstrapPlan,
    BootstrapStep,
    PresenceRule,
    RoleTier,
    bootstrap_plan,
    check_order,
)
from account_factory.modes import OwnershipMode

from .conftest import (
    BUILDERS,
    bring_existing_cluster_request,
    existing_account_request,
    matching_authorization,
    new_account_request,
)

# The two modes that own an AWS account, and therefore get a bootstrap description.
ACCOUNT_MODES = (
    OwnershipMode.NEW_ACCOUNT_MANAGED,
    OwnershipMode.EXISTING_ACCOUNT_MANAGED,
)


def _step_names(plan: BootstrapPlan) -> list[str]:
    return [step.name for step in plan.steps]


def _valid_step(**overrides) -> BootstrapStep:
    """A minimally valid step, so invariant tests vary one field at a time."""
    fields = {
        "name": "fixture-step",
        "tier": None,
        "reason": "a fixture step, so the invariant under test is the only thing varying",
        "command": ("aws", "sts", "get-caller-identity"),
        "presence": PresenceRule.REUSE_IF_PRESENT,
        "scope": "child account (fixture)",
        "denial_remediation": "a fixture remediation, so the missing-remediation check is not "
        "the thing failing",
    }
    fields.update(overrides)
    return BootstrapStep(**fields)


# ─────────────────────────────────────────────────────────────────────────────────────────
# Case 1: fresh-account creation — the role is established, and established FIRST
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_a_fresh_account_plan_establishes_the_autoscaling_service_linked_role():
    """The gap #5532's review found: a vended account has no Auto Scaling role at all."""
    plan = bootstrap_plan(new_account_request())
    step = plan.step(AUTOSCALING_STEP)
    assert AUTOSCALING_SERVICE_PRINCIPAL in step.command
    assert step.command[:3] == ("aws", "iam", "create-service-linked-role")


def test_the_role_precedes_workspace_kms_key_creation():
    """The ordering the whole prerequisite exists for.

    Stated on the step rather than left implicit, because the failure of getting it wrong is
    a KMS policy error naming a principal, which reads as a malformed key policy rather than
    as a missing account-wide role.
    """
    step = bootstrap_plan(new_account_request()).step(AUTOSCALING_STEP)
    assert "KMS" in step.precedes
    assert "key-creation time" in step.reason


def test_the_service_linked_role_is_created_with_the_bootstrap_identity_not_before_it():
    """ "Using the account-bootstrap identity" means the identity has to exist first.

    A plan that created the service-linked role before the bootstrap role would describe a
    call made by an identity that does not exist yet.
    """
    names = _step_names(bootstrap_plan(new_account_request()))
    assert names.index(BOOTSTRAP_ROLE_STEP) < names.index(AUTOSCALING_STEP)


def test_the_role_name_and_principal_match_the_workspace_side_exactly():
    """A near-miss name reads to the workspace check as an ABSENT role, not a typo.

    `workspaces/scripts/workspace_kms.py` compares against these exact strings, so they are
    asserted literally here rather than through the constants alone — a test that only
    compared a constant to itself would pass after either side was renamed.
    """
    assert AUTOSCALING_SERVICE_LINKED_ROLE == "AWSServiceRoleForAutoScaling"
    assert AUTOSCALING_SERVICE_PRINCIPAL == "autoscaling.amazonaws.com"
    step = bootstrap_plan(new_account_request()).step(AUTOSCALING_STEP)
    assert AUTOSCALING_SERVICE_LINKED_ROLE in step.reason


def test_the_command_is_the_one_the_workspaces_readme_names():
    """The two halves of the contract must not drift into describing different commands."""
    step = bootstrap_plan(new_account_request()).step(AUTOSCALING_STEP)
    assert step.command == (
        "aws",
        "iam",
        "create-service-linked-role",
        "--aws-service-name",
        "autoscaling.amazonaws.com",
    )


# ─────────────────────────────────────────────────────────────────────────────────────────
# Case 2: existing-role reuse — idempotent, and idempotent by READING first
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_an_existing_role_is_reused_rather_than_recreated():
    """ "Reuse the exact existing role" — create only after VERIFIED absence.

    `CREATE_IF_ABSENT` is the read-then-create rule. It is not "create and ignore the
    already-exists error": an error-swallowing create cannot tell "it was already there" from
    "the create was denied", and those need opposite responses.
    """
    step = bootstrap_plan(new_account_request()).step(AUTOSCALING_STEP)
    assert step.presence is PresenceRule.CREATE_IF_ABSENT
    assert "VERIFIED absence" in PresenceRule.CREATE_IF_ABSENT.meaning


def test_describing_bootstrap_twice_describes_the_same_steps():
    """Re-running over an already-bootstrapped account is a no-op, not a replacement.

    Asserted on the description because that is what this module produces: two calls that
    disagreed would mean the plan depended on hidden state, and an operator following the
    second one would act on an account the first one already changed.
    """
    first = bootstrap_plan(new_account_request())
    second = bootstrap_plan(new_account_request())
    assert first == second


def test_an_adopted_account_still_gets_the_prerequisite_verified():
    """The reuse case in its realistic form: an existing account ADP manages.

    Skipping the plan for an account that already exists is exactly what leaves the
    service-linked role unchecked until a KMS key creation fails with a confusing policy
    error. The role is checked; `CREATE_IF_ABSENT` keeps the check a no-op when it is there.
    """
    plan = bootstrap_plan(existing_account_request())
    assert plan.step(AUTOSCALING_STEP).presence is PresenceRule.CREATE_IF_ABSENT


@pytest.mark.parametrize("mode", ACCOUNT_MODES)
def test_every_account_owning_mode_names_the_prerequisite(mode):
    """Asserted for every such mode, not the one mode a test author happened to pick."""
    plan = bootstrap_plan(BUILDERS[mode]())
    assert AUTOSCALING_STEP in _step_names(plan)


def test_no_role_step_recreates_something_that_already_exists():
    """Every account-wide role is reused if present.

    A `create-role` run against a role that exists either errors — which a naive runner reads
    as a bootstrap failure — or, worse, replaces a trust policy an operator had tightened.
    """
    plan = bootstrap_plan(new_account_request())
    for step in plan.steps:
        if step.tier is not None:
            assert step.presence is PresenceRule.REUSE_IF_PRESENT, step.name


# ─────────────────────────────────────────────────────────────────────────────────────────
# Case 3: denied creation or read — fail closed, and say what to do about it
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_a_denied_step_has_a_stated_remedy():
    """Fail-closed is only useful if it says what to do next.

    A permission error with no remediation leaves the operator holding an AccessDenied and no
    next move, and the tempting wrong move — having workspace provisioning create the role
    instead — is the defect this whole prerequisite exists to prevent.
    """
    step = bootstrap_plan(new_account_request()).step(AUTOSCALING_STEP)
    assert "iam:CreateServiceLinkedRole" in step.denial_remediation
    assert "iam:GetRole" in step.denial_remediation


def test_the_remedy_for_a_denial_is_not_to_let_the_workspace_do_it():
    """The wrong fix, named explicitly so nobody reaches for it under pressure."""
    remediation = (
        bootstrap_plan(new_account_request()).step(AUTOSCALING_STEP).denial_remediation
    )
    assert "do not have workspace provisioning create the role" in remediation


def test_the_permission_is_scoped_to_one_service():
    """Unscoped `iam:CreateServiceLinkedRole` would let bootstrap mint a service-linked role
    for ANY AWS service in the account — a far larger grant than the one thing it needs."""
    remediation = (
        bootstrap_plan(new_account_request()).step(AUTOSCALING_STEP).denial_remediation
    )
    assert f"scoped to {AUTOSCALING_SERVICE_PRINCIPAL}" in remediation
    assert "unscoped" in remediation


def test_a_step_with_no_denial_remediation_is_refused():
    """The invariant behind the assertions above, so a later step cannot omit it."""
    with pytest.raises(BootstrapError, match="strands the operator"):
        _valid_step(denial_remediation="")


def test_a_step_with_no_stated_reason_is_refused():
    """A step nobody can justify is a step a later reader deletes or reorders."""
    with pytest.raises(BootstrapError, match="must state why it exists"):
        _valid_step(reason="   ")


def test_every_step_in_a_real_plan_states_a_remedy_and_a_reason():
    """Not just the service-linked role: a denial anywhere needs a next move."""
    for step in bootstrap_plan(new_account_request()).steps:
        assert step.reason.strip(), step.name
        assert step.denial_remediation.strip(), step.name


def test_a_missing_baseline_control_is_a_refusal_rather_than_a_silent_skip():
    """`MUST_ALREADY_EXIST` is the "bootstrap does not create this" rule.

    An organization trail normally already covers a new account; creating a second one
    duplicates cost and evidence. But an account with NO trail must not be quietly accepted.
    """
    step = bootstrap_plan(new_account_request()).step("baseline-audit-logging")
    assert step.presence is PresenceRule.MUST_ALREADY_EXIST
    assert "never a silent skip" in PresenceRule.MUST_ALREADY_EXIST.meaning
    assert (
        "do not create a per-account trail as a substitute" in step.denial_remediation
    )


# ─────────────────────────────────────────────────────────────────────────────────────────
# Case 4: workspace cleanup must not adopt the account-wide role
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_the_service_linked_role_is_not_adoptable_by_a_workspace():
    """The property that keeps one workspace's teardown from breaking its neighbours.

    A workspace that took the role into its own Terraform state would delete it on teardown,
    and every OTHER workspace in the account would then fail its next encrypted-node
    operation.
    """
    step = bootstrap_plan(new_account_request()).step(AUTOSCALING_STEP)
    assert step.adoptable_by_workspace is False
    assert step.retained_through_workspace_retirement is True


def test_retiring_a_workspace_does_not_release_any_account_wide_step():
    """Account-wide means account-wide: nothing in the plan is per-workspace state."""
    plan = bootstrap_plan(new_account_request())
    assert plan.account_wide_steps == plan.steps
    for step in plan.account_wide_steps:
        assert step.retained_through_workspace_retirement is True, step.name


def test_a_step_cannot_be_both_adoptable_and_retained():
    """The two flags are not independent, so the contradiction is refused at construction.

    An adopted resource IS deleted with the workspace that adopted it. A step claiming both
    would read as "safe to put in workspace state and safe to keep", which is the exact
    combination that deletes a neighbour's prerequisite.
    """
    with pytest.raises(BootstrapError, match="cannot both hold"):
        _valid_step(
            adoptable_by_workspace=True, retained_through_workspace_retirement=True
        )


def test_an_adoptable_step_is_permitted_when_it_is_not_claimed_to_be_retained():
    """The refusal above is about the contradiction, not about adoption itself."""
    step = _valid_step(
        adoptable_by_workspace=True, retained_through_workspace_retirement=False
    )
    assert step.adoptable_by_workspace is True


# ─────────────────────────────────────────────────────────────────────────────────────────
# The ordering is checked, not merely arranged
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_a_plan_missing_the_service_linked_role_is_refused():
    """Otherwise the prerequisite is maintained only by the order someone wrote a tuple in."""
    plan = bootstrap_plan(new_account_request())
    without = BootstrapPlan(
        workspace_id=plan.workspace_id,
        organization_id=plan.organization_id,
        steps=tuple(s for s in plan.steps if s.name != AUTOSCALING_STEP),
    )
    with pytest.raises(BootstrapError, match=AUTOSCALING_SERVICE_LINKED_ROLE):
        check_order(without)


def test_a_plan_that_creates_the_role_before_the_bootstrap_identity_is_refused():
    plan = bootstrap_plan(new_account_request())
    autoscaling = plan.step(AUTOSCALING_STEP)
    reordered = BootstrapPlan(
        workspace_id=plan.workspace_id,
        organization_id=plan.organization_id,
        steps=(autoscaling, *(s for s in plan.steps if s.name != AUTOSCALING_STEP)),
    )
    with pytest.raises(BootstrapError, match="does not exist yet"):
        check_order(reordered)


def test_a_plan_missing_the_bootstrap_identity_is_refused():
    """Every later step is described as being carried out by an identity the plan
    would then never establish."""
    plan = bootstrap_plan(new_account_request())
    without = BootstrapPlan(
        workspace_id=plan.workspace_id,
        organization_id=plan.organization_id,
        steps=tuple(s for s in plan.steps if s.name != BOOTSTRAP_ROLE_STEP),
    )
    with pytest.raises(BootstrapError, match="every later step is carried out with"):
        check_order(without)


def test_a_plan_that_creates_a_lesser_role_before_the_bootstrap_role_is_refused():
    plan = bootstrap_plan(new_account_request())
    workload = plan.step("workload-role")
    reordered = BootstrapPlan(
        workspace_id=plan.workspace_id,
        organization_id=plan.organization_id,
        steps=(workload, *(s for s in plan.steps if s.name != "workload-role")),
    )
    with pytest.raises(BootstrapError, match="ordered before"):
        check_order(reordered)


def test_a_plan_that_repeats_a_step_is_refused():
    """Which of the two applies decides what the account ends up with."""
    plan = bootstrap_plan(new_account_request())
    doubled = BootstrapPlan(
        workspace_id=plan.workspace_id,
        organization_id=plan.organization_id,
        steps=(*plan.steps, plan.step(AUTOSCALING_STEP)),
    )
    with pytest.raises(BootstrapError, match="repeats step"):
        check_order(doubled)


def test_a_real_plan_passes_its_own_order_check():
    """The positive control: the refusals above would be vacuous if nothing ever passed."""
    check_order(bootstrap_plan(new_account_request()))


def test_asking_for_a_step_that_is_not_in_the_plan_is_an_error():
    """A missing step must not read as an absent-but-fine one."""
    with pytest.raises(BootstrapError, match="no bootstrap step"):
        bootstrap_plan(new_account_request()).step("not-a-step")


# ─────────────────────────────────────────────────────────────────────────────────────────
# Role tiers
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_only_the_bootstrap_tier_may_write_iam():
    """The property that makes "the workload cannot re-bootstrap the account" a property of
    the credential rather than of a review."""
    assert RoleTier.BOOTSTRAP.may_write_iam is True
    assert RoleTier.CONTROLLER.may_write_iam is False
    assert RoleTier.WORKLOAD.may_write_iam is False


def test_the_plan_establishes_three_separate_roles_rather_than_one():
    """One role would hold the union of all three tiers' permissions, so tenant work would
    inherit the ability to create IAM roles."""
    plan = bootstrap_plan(new_account_request())
    tiers = [step.tier for step in plan.steps if step.tier is not None]
    assert tiers == [RoleTier.BOOTSTRAP, RoleTier.CONTROLLER, RoleTier.WORKLOAD]


def test_the_roles_are_created_most_privileged_first():
    """Each tier is created BY the tier above it, so no role creates itself."""
    names = _step_names(bootstrap_plan(new_account_request()))
    assert names.index(BOOTSTRAP_ROLE_STEP) < names.index("controller-role")
    assert names.index("controller-role") < names.index("workload-role")


def test_the_service_linked_role_is_not_one_of_adps_tiers():
    """It is AWS's own identity for the Auto Scaling service. Recording it as a tier would
    imply ADP assumes it."""
    assert bootstrap_plan(new_account_request()).step(AUTOSCALING_STEP).tier is None


# ─────────────────────────────────────────────────────────────────────────────────────────
# Bootstrap is refused where it would act on an account ADP does not own
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_bring_existing_cluster_gets_no_bootstrap_description():
    """That mode creates no AWS infrastructure and adopts a cluster someone else runs, so
    "bootstrapping" it would rewrite roles and baseline controls in an account ADP does not
    own."""
    with pytest.raises(BootstrapError, match="does not own"):
        bootstrap_plan(bring_existing_cluster_request())


def test_a_request_that_does_not_validate_gets_no_plan():
    """Validated first, so a plan naming an account this run may not act on is never produced.

    A plan is a document an operator acts from; producing one for an unauthorized target and
    refusing only later would put the target in a reviewed artifact.
    """
    request = new_account_request()
    other_workspace = matching_authorization(request, workspace_id="ws-somebody-else")
    with pytest.raises(BootstrapError, match="does not validate"):
        bootstrap_plan(request, other_workspace)


def test_a_matching_authorization_still_produces_a_plan():
    """The positive control for the refusal above."""
    request = new_account_request()
    plan = bootstrap_plan(request, matching_authorization(request))
    assert plan.workspace_id == request.workspace_id
    assert plan.organization_id == request.organization_id


def test_the_plan_is_bound_to_the_workspace_and_organization_it_was_asked_for():
    """Registration binds to the authenticated organization/workspace operation, so the plan
    carries which one it describes rather than being an anonymous list of steps."""
    request = new_account_request(workspace_id="ws-other-fixture")
    plan = bootstrap_plan(request)
    assert plan.workspace_id == "ws-other-fixture"


# ─────────────────────────────────────────────────────────────────────────────────────────
# The module describes; it does not act
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_the_plan_is_a_description_and_nothing_in_it_is_callable():
    """`command` is text an operator would run, not a callable this module could invoke.

    The offline-by-construction property is enforced module-wide by
    `test_no_legacy_targets.py`'s identifier scan; this asserts the shape locally so a future
    edit that made a step hold a callable would fail here too.
    """
    for step in bootstrap_plan(new_account_request()).steps:
        assert isinstance(step.command, tuple)
        assert all(isinstance(part, str) for part in step.command)
        assert not callable(step.command)


def test_no_step_names_a_credential():
    """Plans are reviewed, logged and committed to CI artifacts, so a credential in one would
    be disclosed by the review process itself."""
    forbidden = (
        "aws_access_key_id",
        "aws_secret_access_key",
        "session_token",
        "--password",
    )
    for step in bootstrap_plan(new_account_request()).steps:
        rendered = " ".join(step.command).lower()
        for token in forbidden:
            assert token not in rendered, step.name


def test_every_presence_rule_can_be_explained_to_an_operator():
    """A report showing `presence: create-if-absent` and nothing else would not have told the
    operator that the read must come first.

    Asserted over every member so a rule added later cannot be described only by its slug.
    """
    for rule in PresenceRule:
        assert rule.meaning.strip()
        assert rule.meaning != rule.value


def test_bootstrap_exports_what_the_cli_needs():
    """`check_order` is part of the surface: a caller assembling a plan needs to be able to
    check it, not merely to trust that `bootstrap_plan` did."""
    for name in ("bootstrap_plan", "check_order", "BootstrapError", "PresenceRule"):
        assert name in bootstrap.__all__


# ─────────────────────────────────────────────────────────────────────────────────────────
# Unchecked authorization is carried, not dropped
# ─────────────────────────────────────────────────────────────────────────────────────────


def test_a_plan_states_which_authorization_comparisons_were_not_made():
    """A plan built with no authorization must not look like one built with a verified match.

    `bootstrap_plan` validates and then had no use for the unchecked list, so it discarded it.
    That made `bootstrap-plan` and `recovery-report` the only subcommands able to describe
    writing account-wide roles — the least reversible thing this module describes — without
    saying which ownership questions nobody answered.
    """
    plan = bootstrap_plan(new_account_request())
    assert plan.unchecked_authorization
    assert "organization_id" in plan.unchecked_authorization


def test_a_fully_authorized_plan_reports_nothing_unchecked():
    """The positive control: the field reports unmade comparisons, not every comparison."""
    request = new_account_request()
    plan = bootstrap_plan(request, matching_authorization(request))
    assert plan.unchecked_authorization == ()
