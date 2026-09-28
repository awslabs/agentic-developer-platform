"""Carrying out child-account bootstrap against a real account — Issue #5531 (w6-08).

`account_factory.bootstrap` describes what a fresh account needs and in what order. This
executes that description. The reviewed revision had only the description, which is the
second finding it was returned for.

## The prerequisite that dictates the order

A workspace KMS key policy names the `AWSServiceRoleForAutoScaling` ARN, and KMS validates
every principal in a key policy **when the key is created**. In a brand-new account that
role does not exist, so key creation fails — and it fails with an error about a malformed
policy principal, which reads like a policy bug rather than a missing account-wide role.
Waiting for the later EKS node group to create the role is too late: the key is created
before the node group exists.

So the role is established here, by the account-bootstrap identity, before any workspace
touches the account. `check_order` is asked to confirm the plan's ordering rather than
trusting the tuple to be in the sequence someone happened to write it in.

## Read before write, and a denied read is not an absence

The role already exists in most accounts — anything that has ever used an Auto Scaling
group has it. So bootstrap reads first and reuses the exact existing role. Creating
unconditionally would make the common case an error.

The distinction carrying the risk is between **absent** and **not permitted to look**.
`iam:GetRole` failing with an authorization error does not mean the role is missing, and
creating on that assumption fails against a role that is already there. So a denied or
unreadable check stops that step with the plan's own remediation text, and only a positive
"no such role" permits creation. This is `PresenceRule.CREATE_IF_ABSENT`'s stated meaning:
"not create-and-ignore-the-already-exists-error", because an error-swallowing create cannot
tell "it was already there" from "the create was denied".

## Why reads happen outside the durable executor and creates inside it

A read establishes nothing in the account, so it needs no durable intent — and recording
one would be actively harmful. `OperationExecutor.execute_provider` records intent with
`fresh=True`, which **refuses a key that already has a row**: "Provider intent already
exists; reconcile instead of repeating the call". A module that recorded intent for its own
reads would make its own subsequent create undispatchable forever.

Creates go through the executor, one durable key per step, so a crash between intent and
answer leaves evidence that the create may have happened. On a later pass the read comes
first: if the role is now present the step is satisfied and nothing is dispatched, so the
common restart path never collides with the existing key at all.

## Why this module will not invent a trust policy

`BootstrapStep.scope` is prose for an operator ("child account (account-wide, once per
account)"), not a principal ARN, and the plan's `command` points at a trust policy *file*
it does not contain. So the trust policy is an input, supplied per step by the composer,
and a step with no supplied document is **refused rather than created**. Deriving a
principal from prose would mean guessing who may assume a role that can write IAM in a
customer's account, and the failure mode of guessing wrong is a role the wrong party can
assume. There is no safe default here, so there is no default.

## Bootstrap failure never means the account should be recreated

Every failure here happens in an account that **already exists and is already billable**.
`BootstrapReport.creation_must_not_be_retried` is named so a caller has to read it: the
account is real, and re-running creation opens a second one rather than repairing this one.
That is also why the report carries observations in `account_factory.recovery`'s vocabulary
rather than a bare success flag — a partly-bootstrapped account is the state that most
needs describing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from account_factory.bootstrap import (
    AUTOSCALING_SERVICE_LINKED_ROLE,
    AUTOSCALING_SERVICE_PRINCIPAL,
    AUTOSCALING_STEP,
    BootstrapPlan,
    BootstrapStep,
    PresenceRule,
    RoleTier,
    check_order,
)
from account_factory.recovery import StepState

from .execution import (
    CallOutcome,
    DurableExecutor,
    OutcomeVocabulary,
    ProviderCallRecord,
    as_outcome,
    outcome_vocabulary,
)
from .placement import AccountPlacement
from .ports import (
    AccountCredentials,
    CredentialSource,
    IamClient,
    ProviderDenied,
    ProviderUnavailable,
    RoleAbsent,
)

__all__ = [
    "ATTACH_SUFFIX",
    "BootstrapOutcome",
    "BootstrapRefused",
    "BootstrapReport",
    # Re-exported from `ports` rather than defined here: raising it is the port
    # implementation's obligation, and callers that already import it from this module keep
    # working. See `ports.RoleAbsent` for why it subclasses `ProviderDenied`.
    "RoleAbsent",
    "bootstrap_account",
    "bootstrap_hook",
    "establish_service_linked_role",
    "role_name_for",
    "step_key",
]

_PROVIDER = "aws-iam"


class BootstrapRefused(Exception):
    """Bootstrap declined to act, before touching the account.

    A refusal, not a failure: nothing was attempted, so there is nothing to reconcile.
    Raised for a plan whose ordering `check_order` rejects, and for a step whose trust
    policy was not supplied.
    """


@dataclass(frozen=True)
class BootstrapOutcome:
    """What one bootstrap step established.

    `state` is `account_factory.recovery.StepState` rather than a local enum, so the report
    feeds that module's recovery arithmetic without translation — including its
    `NOT_CHECKED`, which keeps an unobserved step from reading as either present or absent.
    """

    step: str
    state: StepState
    detail: str = ""
    created: bool = False
    durable_key: str | None = None
    """The idempotency key a create was dispatched under, when one was.

    `None` for a step satisfied by a read: no key was consumed because no effect was
    attempted. Carried so an operator can find the row for a step that may have acted.
    """

    @property
    def blocks_progress(self) -> bool:
        """This step is not established, so work depending on it cannot proceed."""
        return self.state is not StepState.ESTABLISHED

    @property
    def may_have_acted(self) -> bool:
        """A create may have taken effect without being confirmed.

        True exactly when a dispatch happened and the result was not established. The
        question a later pass asks before deciding whether a re-read is needed.
        """
        return self.durable_key is not None and self.state is not StepState.ESTABLISHED


@dataclass
class BootstrapReport:
    """What bootstrap established in an account that already exists.

    Deliberately not a boolean. The thing a caller most needs to be stopped from concluding
    is that a bootstrap failure means the account should be created again.
    """

    account_id: str
    workspace_id: str
    outcomes: list[BootstrapOutcome] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Every planned step is established."""
        return bool(self.outcomes) and all(outcome.state is StepState.ESTABLISHED for outcome in self.outcomes)

    @property
    def blocked_on(self) -> tuple[str, ...]:
        """The steps that are not established, in plan order."""
        return tuple(outcome.step for outcome in self.outcomes if outcome.blocks_progress)

    @property
    def partially_bootstrapped(self) -> bool:
        """Some steps established and some did not — the state needing the most care.

        A caller that treated this as "failed" might tear down; one that treated it as
        "succeeded" would hand out an account missing its baseline. It is neither.
        """
        states = [outcome.state for outcome in self.outcomes]
        return any(s is StepState.ESTABLISHED for s in states) and any(s is not StepState.ESTABLISHED for s in states)

    @property
    def creation_must_not_be_retried(self) -> bool:
        """Always true, and named so a caller has to read it.

        The account exists. Whatever failed here, re-running creation opens a second
        billable account rather than repairing this one.
        """
        return True

    def observations(self) -> dict[str, StepState]:
        """Per-step states, in the shape `account_factory.recovery.recovery_report` takes."""
        return {outcome.step: outcome.state for outcome in self.outcomes}

    def details(self) -> dict[str, str]:
        """Per-step detail text, in the shape `recovery_report` takes."""
        return {o.step: o.detail for o in self.outcomes if o.detail}


def step_key(operation_id: str, step_name: str) -> str:
    """The stable durable key for one bootstrap step of one operation.

    `operation_id` plus the step name, and nothing else. Nothing per-attempt may enter it:
    a key containing a timestamp, a uuid or an attempt number is a fresh key on every
    retry, and a fresh key defeats the duplicate-refusal the store provides.
    """
    return f"{operation_id}:{step_name}"


def role_name_for(step: BootstrapStep) -> str:
    """The IAM role name this step establishes, read out of the plan's own command.

    Taken from the `--role-name` argument in `step.command` rather than derived from the
    step name. The plan's command is what an operator would run, so it carries the
    canonical spelling (`AdpAccountBootstrap`, not a name computed from `bootstrap-role`).
    Computing one here would be a second source of truth that drifts from the command an
    operator is told to use — and a bootstrap that reads a role nobody creates concludes
    "absent" forever.
    """
    command = list(step.command)
    if "--role-name" in command:
        index = command.index("--role-name")
        if index + 1 < len(command):
            return command[index + 1]
    raise BootstrapRefused(
        f"the {step.name!r} step's command does not name a role, so the role this module "
        f"would read or create cannot be determined from the plan: {' '.join(command)}"
    )


def _require_account_wide_ownership(step: BootstrapStep) -> None:
    """Refuse to act on an account-wide step a plan marked workspace-adoptable.

    Enforced where the action happens, not only in the plan: an account-wide role adopted
    into one workspace's state is deleted when that workspace is retired, which breaks
    every other workspace in the same account.
    """
    if step.adoptable_by_workspace:
        raise BootstrapRefused(
            f"refusing to establish {step.name!r} as a workspace-adoptable resource: it is "
            f"account-wide and shared by every workspace in the account, and a workspace "
            f"that owned it would delete it on teardown"
        )


def _require_authorized_plan(executor: DurableExecutor, plan: BootstrapPlan) -> None:
    """Fail closed unless this plan was fully authorized and belongs to this operation.

    The symmetric guard to `creation_runner._require_complete_authorization`, and it exists
    for the same reason: `account_factory.bootstrap.bootstrap_plan` accepts
    `authorization=None` so that the offline planner can run before any authorization
    exists, and it records the comparisons nobody made on `plan.unchecked_authorization`
    rather than failing. That field was carried and then never read by anything that acts.

    An unread `unchecked_authorization` is worse here than on the creation path. The steps
    this runner writes are **account-wide**: three cross-account roles and a service-linked
    role, shared by every workspace in the account and outliving all of them. A plan whose
    `workspace_id` was never compared against anything still produces a perfectly valid
    list of roles to create — in whichever account the caller's credential reaches.

    Two obligations:

    1. **Nothing may be left unchecked.** A non-empty list refuses, naming the fields.
    2. **The plan must belong to THIS operation.** Workspace and organization are compared
       against the executor's lease, which is server-resolved and cannot be influenced by a
       request body. Without it, a self-consistent plan for another tenant's workspace
       passes every field-level comparison and writes roles into this operation's account.

    Raises `BootstrapRefused` before a credential is obtained, so no read and no write
    reaches the account.
    """
    if plan.unchecked_authorization:
        unchecked = sorted(plan.unchecked_authorization)
        raise BootstrapRefused(
            f"refusing to bootstrap from a plan with {len(unchecked)} unverified "
            f"authorization comparison(s): {', '.join(unchecked)}. These steps write "
            f"account-wide roles shared by every workspace in the account, and an unchecked "
            f"comparison is not a passed one. Build the plan with the authorization from the "
            f"admitted operation"
        )

    mismatches = []
    if plan.workspace_id != executor.workspace_id:
        mismatches.append(f"the plan's workspace {plan.workspace_id!r} is not the operation's workspace {executor.workspace_id!r}")
    operation_org = plan.operation_org_id or plan.organization_id
    if operation_org != executor.org_id:
        mismatches.append(f"the plan's operation organization {operation_org!r} is not the operation's organization {executor.org_id!r}")
    if mismatches:
        raise BootstrapRefused(
            "the supplied bootstrap plan does not belong to this operation: "
            + "; ".join(mismatches)
            + ". Refusing rather than writing account-wide roles on behalf of a tenant this "
            "operation was not admitted for"
        )


def _require_permission_policies(plan: BootstrapPlan, permission_policy_arns: dict[str, str]) -> None:
    """Refuse a bootstrap that could not give every role it creates its permissions.

    Checked up front, before a credential is obtained, rather than discovered per step. A run
    that creates the bootstrap role and then finds it has no ARN for the controller role has
    already written an identity into the account, and the operator is left deciding whether to
    re-run something that partially applied. Refusing first means nothing was touched.

    Only role steps need one; the service-linked role is AWS's own identity and the baseline
    controls are not roles.
    """
    missing = sorted(step.name for step in plan.steps if isinstance(step.tier, RoleTier) and not permission_policy_arns.get(step.name))
    if missing:
        raise BootstrapRefused(
            f"no permission policy ARN was supplied for {len(missing)} role step(s): "
            f"{', '.join(missing)}. A role created without permissions can be assumed and can "
            f"do nothing, and a later pass reads it as established — so the gap would never be "
            f"closed. Refusing before any credential is obtained, so nothing is written"
        )


def _require_verified_placement(account_id: str, placement: AccountPlacement | None) -> None:
    """Refuse to bootstrap an account that is not verifiably in its approved unit.

    Every step below writes something **account-wide** — three cross-account roles and a
    service-linked role, shared by every workspace in the account and outliving all of them. An
    account at the organization root is outside every service control policy the approved OU
    exists to impose, so doing that work there means creating privileged identities in an
    account none of the approved guardrails apply to, and then handing a workspace the result.

    Refused here rather than reported as a finding, and before the credential is obtained, for
    the same reason `_require_authorized_plan` is: a report saying "the roles were created but
    the account is ungoverned" describes a state that should not have been reachable. The
    account exists either way, which is precisely why it is worth not compounding.

    `None` is refused rather than treated as permission. An absent placement means nobody
    checked, and a default that let an unchecked placement through would make the guard
    unenforceable in exactly the composition that forgot to wire it — see
    `ValidationAuthorization` on why absent and verified must not look alike.

    The placement is also checked to be ABOUT this account. A verified placement for a
    different account otherwise satisfies this guard completely, which is the same
    one-account's-evidence-for-another confusion `recovery._same_account_subject` refuses.
    """
    if placement is None:
        raise BootstrapRefused(
            f"refusing to bootstrap account {account_id} with no placement evidence: nothing "
            f"established that it is inside its approved organizational unit. Every step here "
            f"writes account-wide identities, and an account still at the organization root is "
            f"outside the controls the approval was granted against. Verify placement with "
            f"placement.verify_placement and pass the result"
        )
    if placement.account_id != account_id:
        raise BootstrapRefused(
            f"the supplied placement is about account {placement.account_id}, not the "
            f"{account_id} being bootstrapped. One account's placement is not evidence about "
            f"another's, and accepting it would write account-wide roles into an account whose "
            f"position nobody verified"
        )
    if not placement.verified:
        raise BootstrapRefused(
            f"refusing to bootstrap account {account_id}: its placement in organizational unit "
            f"{placement.organizational_unit_id!r} is not established ({placement.state.value}) "
            f"— {placement.detail}. Creating account-wide roles in an account outside its "
            f"approved unit puts privileged identities where the approved controls do not reach"
        )


def _trust_fingerprint(document: object) -> str | None:
    """A comparable form of one trust policy, or `None` when it cannot be parsed.

    Two documents describing the same permission can differ in whitespace, key order and
    list order, and AWS returns `AssumeRolePolicyDocument` in its own normalization rather
    than the string it was given. So a raw string comparison would report a CONFLICT for
    every role this bootstrap itself created — a false conflict on the common path, which is
    worse than the missing check, because it blocks a correct account and trains whoever
    reads it to ignore the state.

    Sorting is recursive and order-insensitive at every level, because a policy's `Statement`,
    `Action` and `Principal` lists are sets in meaning: two orderings are the same grant.

    A one-element list collapses to the element itself, which is the normalization IAM's own
    grammar requires rather than a convenience. `"Action": "sts:AssumeRole"` and
    `"Action": ["sts:AssumeRole"]` are the same policy, as are `{"AWS": "arn"}` and
    `{"AWS": ["arn"]}`, and AWS returns whichever form it prefers regardless of which was sent.
    Without the collapse this reports CONFLICT for a role this bootstrap itself created — a
    false conflict on the common path, which is worse than the missing check: it blocks a correct
    account and teaches whoever reads these reports to ignore the state. A list of two or more
    stays a list, so a document granting MORE than the reviewed one still differs.

    `None`, deliberately, rather than a fingerprint of the unparseable text: a document this
    cannot read is one whose meaning was NOT established, and the caller must treat that as
    unverified rather than as a mismatch. Fingerprinting the bytes would make an unreadable
    document a CONFLICT — sending an operator to reconcile a difference that may not exist.
    """
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except (ValueError, TypeError):
            return None
    if not isinstance(document, dict):
        return None

    def canonical(value: object) -> str:
        if isinstance(value, dict):
            return "{" + ",".join(f"{key}={canonical(item)}" for key, item in sorted((str(k), v) for k, v in value.items())) + "}"
        if isinstance(value, list | tuple):
            parts = sorted(canonical(item) for item in value)
            # One element renders as the element: see the docstring on IAM's scalar/list
            # equivalence. Two or more keep the brackets, so a longer list cannot collapse
            # into a shorter one and compare equal to it.
            return parts[0] if len(parts) == 1 else "[" + ",".join(parts) + "]"
        return str(value)

    try:
        return canonical(document)
    except (TypeError, ValueError):
        return None


def _read_role(iam: IamClient, role_name: str, expected_trust_policy: str | None = None) -> tuple[StepState, str]:
    """Read one role, keeping absent, denied, unreadable and *different* strictly apart.

    The channels are the entire point: only `RoleAbsent` may lead to a create.

    The ORDER of the handlers below is load-bearing, because `RoleAbsent` is a subclass of
    `ProviderDenied` — see `ports.RoleAbsent` for why it has to be. Matching the base class
    first would swallow every absence into `DENIED`, and on a fresh account that means
    bootstrap reports a permissions problem for roles that simply are not there yet and
    creates nothing, ever.

    ## Why existence is not enough, even with the attachment read alongside it

    The reviewed revision returned `ESTABLISHED` with the message "already exists and is
    reused unchanged" for a role it had not looked inside. That sentence was untrue in the
    one way that matters: *unchanged* was never established, because the only thing read was
    the name.

    A role's trust policy is who may assume it. A role named `AdpWorkspaceController` that
    trusts an account nobody reviewed is a cross-account door into this tenant's account,
    and reusing it hands that door a workspace. The name is the part an attacker or an
    accident controls most easily; the trust document is the part that decides who gets in.
    So when the reviewed document is available, reuse requires the role's ACTUAL
    `AssumeRolePolicyDocument` to match it, and a difference is `CONFLICT` — not a reuse, and
    not something this module re-scopes on its own.

    `expected_trust_policy=None` keeps this read usable where no reviewed document is on hand
    — the service-linked role, whose trust policy is AWS's and not this plan's, and the
    re-read paths that only need to know whether a create landed. In that case the detail
    says the comparison was not made, so "verified unchanged" and "never checked" do not read
    alike.
    """
    try:
        response = iam.get_role(RoleName=role_name)
    except RoleAbsent:
        return StepState.ABSENT, f"{role_name} does not exist in this account"
    except ProviderDenied as exc:
        return (
            StepState.DENIED,
            (
                f"whether {role_name} exists could not be established: the read was denied "
                f"({exc}). This is NOT evidence the role is absent, so nothing was created"
            ),
        )
    except ProviderUnavailable as exc:
        return (
            StepState.NOT_CHECKED,
            (f"the read of {role_name} returned no answer ({exc}). Nothing was established and nothing was created"),
        )

    if expected_trust_policy is None:
        return (
            StepState.ESTABLISHED,
            f"{role_name} already exists; its trust policy was not compared against a reviewed document by this read",
        )

    role = (response or {}).get("Role") or {}
    actual = role.get("AssumeRolePolicyDocument")
    if actual is None:
        # The read succeeded and did not carry the document. Nothing about the trust was
        # established, so this is NOT_CHECKED rather than a pass: an absent field is the
        # "verified and never checked look alike" failure, and treating it as a match would
        # reinstate the whole defect for any port that omits the field.
        return (
            StepState.NOT_CHECKED,
            (
                f"{role_name} exists, but the read returned no AssumeRolePolicyDocument, so "
                f"who may assume it was not established. It is not reused on that basis"
            ),
        )

    expected = _trust_fingerprint(expected_trust_policy)
    observed = _trust_fingerprint(actual)
    if expected is None or observed is None:
        which = "the reviewed document" if expected is None else f"{role_name}'s own trust policy"
        return (
            StepState.NOT_CHECKED,
            (
                f"{role_name} exists, but {which} could not be parsed as a policy document, so "
                f"who may assume it was not established. It is not reused on that basis"
            ),
        )
    if expected != observed:
        return (
            StepState.CONFLICT,
            (
                f"{role_name} exists but its trust policy is NOT the reviewed one: who may "
                f"assume it differs from what this plan authorizes. Reusing it would hand a "
                f"workspace an identity a party nobody reviewed can assume, and re-scoping or "
                f"replacing it affects whatever uses it today — so bootstrap does neither"
            ),
        )
    return (
        StepState.ESTABLISHED,
        f"{role_name} already exists and its trust policy matches the reviewed document, so it is reused unchanged",
    )


def _read_attached_permissions(iam: IamClient, role_name: str, expected_policy_arn: str | None = None) -> tuple[StepState, str]:
    """Read whether a role that EXISTS carries the REVIEWED permission policy, and only it.

    Existence is not usability. A bootstrap that created the role and then failed — denied on
    the attach, or interrupted between the two calls — leaves a role that can be assumed and
    can do nothing. Without this read, the next pass's `get_role` succeeds, the step reports
    `ESTABLISHED`, and nothing ever finishes the role: the failure resurfaces much later as a
    workspace build that cannot reach the account it was given, with no trace of the partial
    bootstrap that caused it.

    So a role with no attached policy is reported `ABSENT`, not `ESTABLISHED`. `ABSENT` is the
    one state that permits a write, which is what lets the next pass complete the attach. It
    does NOT cause a second `create_role`: `_establish_role_step` reads the role first and only
    dispatches the attach for a role already present.

    ## Why ANY policy was not enough

    The reviewed revision returned `ESTABLISHED` as soon as the list was non-empty, naming
    whatever it found. Two different roles pass that test: the one this plan describes, and one
    carrying `AdministratorAccess`. The second is the reason this is a tenant-isolation finding
    rather than a tidiness one — the tiers exist precisely so that the workload role cannot
    write IAM and therefore cannot re-bootstrap the account, and a workload role that arrives
    already attached to `AdministratorAccess` is reported bootstrapped and handed a workspace
    with the tier boundary silently gone.

    Naming it in the detail was the reviewed revision's mitigation, and it is not one. The
    detail is prose in a report nobody is required to read, whereas `ESTABLISHED` is the value
    the code acts on.

    Two things are therefore required for reuse, and neither implies the other:

    * the reviewed policy IS attached — otherwise the role does not have the permissions this
      plan says it needs, whatever else it has;
    * nothing ELSE is attached — an extra policy is extra authority in an account-wide role
      shared by every workspace, and this module cannot know what granted it or who relies on
      it. Detaching is as destructive as replacing, so it is a decision, not a repair.

    Both failures are `CONFLICT`: something of this name exists and is not what the plan
    describes. `ABSENT` would be wrong for either, because `ABSENT` licenses the attach — which
    would leave the extra policy in place and report the result as complete.

    `expected_policy_arn=None` preserves the pre-existing "carries something" reading for
    callers with no reviewed ARN to compare against, and says so in the detail rather than
    claiming it was verified.
    """
    try:
        response = iam.list_attached_role_policies(RoleName=role_name)
    except ProviderDenied as exc:
        return (
            StepState.DENIED,
            (
                f"{role_name} exists, but whether it carries any permission policy could not "
                f"be established: the read was denied ({exc}). Treat the role as not yet "
                f"usable rather than assuming it is complete"
            ),
        )
    except ProviderUnavailable as exc:
        return (
            StepState.NOT_CHECKED,
            (f"{role_name} exists, but the read of its attached policies returned no answer ({exc}). Its usability is unknown"),
        )

    attached = list(response.get("AttachedPolicies") or ())
    if not attached:
        return (
            StepState.ABSENT,
            (
                f"{role_name} exists but carries NO permission policy, so it can be assumed "
                f"and can do nothing. This is a half-built role from an earlier interrupted "
                f"or denied bootstrap, not a finished one"
            ),
        )

    arns = sorted(str(policy.get("PolicyArn") or "") for policy in attached)
    names = ", ".join(sorted(str(policy.get("PolicyName") or policy.get("PolicyArn")) for policy in attached))
    if expected_policy_arn is None:
        return (
            StepState.ESTABLISHED,
            f"{role_name} already exists and carries {names}, which was not compared against a reviewed policy by this read",
        )

    unexpected = [arn for arn in arns if arn != expected_policy_arn]
    if expected_policy_arn not in arns:
        return (
            StepState.CONFLICT,
            (
                f"{role_name} exists and carries {names}, but NOT the reviewed policy "
                f"{expected_policy_arn} this plan attaches. It is a role of the right name with "
                f"permissions nobody reviewed, so it is neither reused nor completed by "
                f"attaching to it: what it can already do has to be decided first"
            ),
        )
    if unexpected:
        return (
            StepState.CONFLICT,
            (
                f"{role_name} carries the reviewed policy {expected_policy_arn} AND "
                f"{len(unexpected)} further policy(ies) this plan did not attach: "
                f"{', '.join(unexpected)}. That is more authority than this tier is meant to "
                f"hold in an account every workspace shares, and detaching is as destructive as "
                f"replacing — so it is reported rather than repaired"
            ),
        )
    return (
        StepState.ESTABLISHED,
        f"{role_name} already exists and carries exactly the reviewed policy {expected_policy_arn}; reused unchanged",
    )


def _reread_after_lost_answer(
    iam: IamClient,
    role_name: str,
    step: BootstrapStep,
    key: str,
    why: str,
    *,
    expected_trust_policy: str | None = None,
    expected_policy_arn: str | None = None,
) -> BootstrapOutcome:
    """Settle a create whose answer was not obtained, by reading the account again.

    The create may have taken effect. Re-reading is the only way to find out, and it is
    safe because it changes nothing. Reporting `ABSENT` here instead would invite a second
    create that then fails against the role the first one made.

    For a role step the re-read is TWO reads, not one. A dispatch that creates the role and
    then loses its answer on the attach leaves the role present and powerless, and a re-read
    that asked only "does it exist" would answer `ESTABLISHED` — writing the half-built role
    into the report as a finished one, which is the whole defect this pass repairs, merely
    relocated to the recovery path.

    A half-built role is therefore reported `NOT_CHECKED`: this pass has already consumed the
    create's durable key, so it does not also finish the role here. The attach lives under its
    own key (`ATTACH_SUFFIX`), which no pass has consumed, so the next pass through
    `_establish_role_step` reads the role as present-but-unattached and dispatches it. The
    report names the key either way, so nothing is lost between the two passes.

    The reviewed documents are compared here too, and this path is where they matter most. It
    is reached when the store refused a second dispatch — meaning something else got this far
    — so the role now present was not necessarily put there by this plan. Asking only "is a
    role of that name there now" would let another party's role settle this pass as
    established, under cover of a lost answer.
    """
    state, detail = _read_role(iam, role_name, expected_trust_policy)
    if state is StepState.CONFLICT:
        # Named as a conflict rather than folded into the generic unsettled branch below. The
        # difference is the whole remedy: "the answer was not obtained, re-read" invites another
        # pass, which will read the same differing role and stall the same way forever.
        return BootstrapOutcome(
            step=step.name,
            state=StepState.CONFLICT,
            detail=(
                f"{why}, and the re-read found a role that is not the one this plan describes: "
                f"{detail}. Durable key {key} holds the record of the dispatch"
            ),
            durable_key=key,
        )
    if state is StepState.ESTABLISHED and isinstance(step.tier, RoleTier):
        attached_state, attached_detail = _read_attached_permissions(iam, role_name, expected_policy_arn)
        if attached_state is StepState.CONFLICT:
            return BootstrapOutcome(
                step=step.name,
                state=StepState.CONFLICT,
                detail=(
                    f"{why}. {role_name} IS present on re-read, but its permissions are not the "
                    f"ones this plan describes: {attached_detail}. Durable key {key} holds the "
                    f"record of the dispatch"
                ),
                created=True,
                durable_key=key,
            )
        if attached_state is not StepState.ESTABLISHED:
            return BootstrapOutcome(
                step=step.name,
                state=StepState.NOT_CHECKED,
                detail=(
                    f"{why}. {role_name} IS present on re-read, so the create landed, but the "
                    f"role is not usable: {attached_detail}. Not reported established, and not "
                    f"finished here — the attach has its own durable key, so the next pass "
                    f"completes it. Durable key {key} holds the record of the create"
                ),
                created=True,
                durable_key=key,
            )
        return BootstrapOutcome(
            step=step.name,
            state=StepState.ESTABLISHED,
            detail=f"{why}, but {attached_detail} on re-read, so it was established",
            created=True,
            durable_key=key,
        )
    if state is StepState.ESTABLISHED:
        return BootstrapOutcome(
            step=step.name,
            state=StepState.ESTABLISHED,
            detail=f"{why}, but {role_name} is present on re-read, so it was established",
            created=True,
            durable_key=key,
        )
    return BootstrapOutcome(
        step=step.name,
        state=StepState.NOT_CHECKED,
        detail=(
            f"{why}, and the re-read did not establish its state ({detail}). The role may "
            f"exist; do not assume otherwise. Durable key {key} holds the record"
        ),
        durable_key=key,
    )


async def _dispatch_create(
    executor: DurableExecutor,
    iam: IamClient,
    step: BootstrapStep,
    role_name: str,
    refusal_types: tuple[type[BaseException], ...],
    *,
    expected_trust_policy: str | None = None,
    expected_policy_arn: str | None = None,
) -> BootstrapOutcome:
    """Run one create through the durable executor, under this step's stable key.

    The executor commits intent before the hook runs, so a crash mid-call leaves evidence.
    It also refuses a key that already has a row, which is how a second concurrent worker
    — or a restarted one whose earlier create may have landed — is stopped from repeating
    the effect. That refusal is answered with a re-read, never with a retry.

    The create itself is performed by the hook the composer installed on the executor
    (`bootstrap_hook`), not passed in here: `OperationExecutor` takes its `provider_call`
    at construction and `execute_provider` accepts no per-call hook. Routing the write
    through that one composed hook is also what keeps every IAM write in a single place.

    `operation_kind` carries the step name, which is what lets that one hook tell which
    step it is being asked to perform.
    """
    key = step_key(executor.operation_id, step.name)

    try:
        call, _ = await executor.execute_provider(
            idempotency_key=key,
            provider=_PROVIDER,
            operation_kind=step.name,
            target=role_name,
        )
    except refusal_types as exc:
        # The store already holds a row for this key: an earlier or concurrent attempt got
        # this far. Its effect may have landed, so the answer comes from the account, never
        # from repeating the call.
        return _reread_after_lost_answer(
            iam,
            role_name,
            step,
            key,
            f"the durable store refused a second dispatch ({exc})",
            expected_trust_policy=expected_trust_policy,
            expected_policy_arn=expected_policy_arn,
        )

    # Normalized, never compared with `is`: the row carries the harness enum member. An
    # identity comparison here reads a role this bootstrap DID create as not-established,
    # so the next pass tries to create it again. See `execution.as_outcome`.
    outcome = as_outcome(getattr(call, "outcome", None))
    if outcome is CallOutcome.SUCCEEDED:
        return BootstrapOutcome(
            step=step.name,
            state=StepState.ESTABLISHED,
            detail=f"{role_name} was absent and has been created for this account",
            created=True,
            durable_key=key,
        )
    if outcome is CallOutcome.FAILED:
        return BootstrapOutcome(
            step=step.name,
            state=StepState.DENIED,
            detail=(f"creating {role_name} was refused by AWS. Remediation: {step.denial_remediation}"),
            durable_key=key,
        )
    # UNKNOWN, ABSENT, or an unobserved row: the answer was not obtained.
    return _reread_after_lost_answer(
        iam,
        role_name,
        step,
        key,
        f"the create for {role_name} did not return a usable answer",
        expected_trust_policy=expected_trust_policy,
        expected_policy_arn=expected_policy_arn,
    )


async def establish_service_linked_role(
    executor: DurableExecutor,
    iam: IamClient,
    step: BootstrapStep,
    *,
    role_name: str = AUTOSCALING_SERVICE_LINKED_ROLE,
    refusal_types: tuple[type[BaseException], ...] = (),
) -> BootstrapOutcome:
    """Ensure the Auto Scaling service-linked role exists, reusing it when it does.

    The ordering-critical step. Read first; reuse the exact existing role; create only
    after absence has been positively established; fail closed on a denied read with the
    plan's own remediation text.

    The service principal is pinned to Auto Scaling here rather than taken from a caller.
    This function's entire justification is one specific account-wide prerequisite, and a
    parameterised principal would turn it into a general-purpose service-linked-role
    creator with `iam:CreateServiceLinkedRole` behind it.

    No reviewed trust document is compared for this one, and that is correct rather than an
    omission. A service-linked role's trust policy is AWS's, not this plan's: it is created by
    `iam:CreateServiceLinkedRole` from the service name, this module never supplies a document
    for it, and there is nothing in the plan to compare against. Inventing an expected document
    would produce a false CONFLICT on every account that already has the role — which is most
    of them, since anything that has ever used an Auto Scaling group has it.
    """
    _require_account_wide_ownership(step)
    if step.presence is not PresenceRule.CREATE_IF_ABSENT:
        raise BootstrapRefused(
            f"the {step.name!r} step is marked {step.presence.value!r}; only "
            f"{PresenceRule.CREATE_IF_ABSENT.value!r} authorizes creating it after a "
            f"verified absence"
        )

    state, detail = _read_role(iam, role_name)
    if state is StepState.ESTABLISHED:
        # The common case. No write is attempted at all, and no durable key is consumed.
        return BootstrapOutcome(step=step.name, state=state, detail=detail)
    if state is StepState.DENIED:
        return BootstrapOutcome(
            step=step.name,
            state=state,
            detail=f"{detail}. Remediation: {step.denial_remediation}",
        )
    if state is StepState.NOT_CHECKED:
        return BootstrapOutcome(step=step.name, state=state, detail=detail)

    return await _dispatch_create(executor, iam, step, role_name, refusal_types)


def _expected_documents(
    step: BootstrapStep,
    trust_policies: dict[str, str],
    permission_policy_arns: dict[str, str],
) -> tuple[str | None, str | None]:
    """The reviewed trust document and permission-policy ARN for one role step, if supplied.

    Both come from the composer, for the reason the module docstring gives: a principal is not
    derivable from `step.scope`'s prose and an ARN contains an account id. This pairs them so
    every read of an existing role compares against the SAME documents the create would have
    used — the alternative is a create path and a reuse path that disagree about what the role
    is supposed to be, which is how a role passes verification and then fails in use.
    """
    return trust_policies.get(step.name), permission_policy_arns.get(step.name)


ATTACH_SUFFIX = "attach-permissions"
"""Distinguishes the attach's durable key from the create's for the same step.

They are two provider calls with two separate effects, so they need two fences. Sharing one
key would mean the attach could never be dispatched after a create had used it — the store
refuses a key that already has a row — which is exactly how the half-built role became
permanent. The suffix is derived from the step name and nothing else, so two workers finishing
the same interrupted bootstrap derive the same key and one loses at the unique constraint.
"""


async def _dispatch_attach(
    executor: DurableExecutor,
    iam: IamClient,
    step: BootstrapStep,
    role_name: str,
    refusal_types: tuple[type[BaseException], ...],
    *,
    reason: str,
    expected_policy_arn: str | None = None,
) -> BootstrapOutcome:
    """Attach the reviewed permission policy to a role that exists without one.

    The recovery path for a bootstrap interrupted between `create_role` and
    `attach_role_policy`. It is a separate dispatch under its own key rather than a re-run of
    the create, because the role is already there: re-creating would fail against it and report
    an `EntityAlreadyExists` as a bootstrap failure, stranding the account.

    Verified by re-reading the attachments, not by trusting the call's own answer, for the same
    reason `_reread_after_lost_answer` exists: when the answer is not obtained the effect may
    still have landed. The re-read compares against the reviewed ARN, so an attach whose answer
    was lost cannot be settled as established by finding some OTHER policy attached — which is
    the state a concurrent pass, or a hand edit racing this one, would leave.
    """
    key = f"{step_key(executor.operation_id, step.name)}:{ATTACH_SUFFIX}"

    try:
        call, _ = await executor.execute_provider(
            idempotency_key=key,
            provider=_PROVIDER,
            operation_kind=f"{step.name}:{ATTACH_SUFFIX}",
            target=role_name,
        )
    except refusal_types as exc:
        state, detail = _read_attached_permissions(iam, role_name, expected_policy_arn)
        if state is StepState.ESTABLISHED:
            return BootstrapOutcome(step=step.name, state=state, detail=detail, created=True, durable_key=key)
        if state is StepState.CONFLICT:
            return BootstrapOutcome(
                step=step.name,
                state=StepState.CONFLICT,
                detail=(
                    f"the durable store refused a second attach dispatch ({exc}) and the re-read "
                    f"found permissions that are not the ones this plan describes: {detail}. "
                    f"Durable key {key} holds the record"
                ),
                durable_key=key,
            )
        return BootstrapOutcome(
            step=step.name,
            state=StepState.NOT_CHECKED,
            detail=(
                f"the durable store refused a second attach dispatch ({exc}) and the re-read "
                f"did not establish the role's permissions ({detail}). Durable key {key} holds "
                f"the record"
            ),
            durable_key=key,
        )

    outcome = as_outcome(getattr(call, "outcome", None))
    if outcome is CallOutcome.SUCCEEDED:
        return BootstrapOutcome(
            step=step.name,
            state=StepState.ESTABLISHED,
            detail=f"{reason}. Its permission policy has now been attached, completing the role",
            created=True,
            durable_key=key,
        )
    if outcome is CallOutcome.FAILED:
        return BootstrapOutcome(
            step=step.name,
            state=StepState.DENIED,
            detail=(
                f"{reason}, and attaching its permission policy was refused by AWS. The role "
                f"remains assumable and powerless. Remediation: {step.denial_remediation}"
            ),
            durable_key=key,
        )
    # UNKNOWN: the attach may have landed. Ask the account rather than repeating the call.
    state, detail = _read_attached_permissions(iam, role_name, expected_policy_arn)
    if state is StepState.ESTABLISHED:
        return BootstrapOutcome(
            step=step.name,
            state=state,
            detail=f"the attach returned no usable answer, but {detail}",
            created=True,
            durable_key=key,
        )
    if state is StepState.CONFLICT:
        return BootstrapOutcome(
            step=step.name,
            state=StepState.CONFLICT,
            detail=(
                f"the attach for {role_name} returned no usable answer, and the re-read found "
                f"permissions that are not the ones this plan describes: {detail}. Durable key "
                f"{key} holds the record"
            ),
            durable_key=key,
        )
    return BootstrapOutcome(
        step=step.name,
        state=StepState.NOT_CHECKED,
        detail=(
            f"the attach for {role_name} returned no usable answer and the re-read did not settle it ({detail}). Durable key {key} holds the record"
        ),
        durable_key=key,
    )


async def _establish_role_step(
    executor: DurableExecutor,
    iam: IamClient,
    step: BootstrapStep,
    trust_policies: dict[str, str],
    refusal_types: tuple[type[BaseException], ...],
    permission_policy_arns: dict[str, str] | None = None,
) -> BootstrapOutcome:
    """Establish one of the three scoped cross-account roles, reusing an existing one.

    The tiers exist so that the workload role cannot write IAM and therefore cannot
    re-bootstrap the account. This module does not widen a tier: it creates the role the
    plan names, with the trust policy the composer supplied, and attaches nothing the plan
    did not ask for.

    "Established" means the role exists, its trust policy is the reviewed one, and it carries
    exactly the reviewed permission policy. Each of those three was separately missing:

    * Existence alone was the first reviewed gap: a role created but not attached is assumable
      and powerless, and a pass that called it established would leave it that way permanently.
      A present-but-unattached role takes the completion path below rather than the reuse path.
    * Whether the existing role is the RIGHT role was the second, and it is the tenant-isolation
      one. A role of the correct name whose trust policy admits an unreviewed party, or which
      carries `AdministratorAccess`, satisfied both of the earlier reads and was reported
      "reused unchanged" — a sentence about a role nothing had looked inside.

    A role that exists and differs is `CONFLICT`: bootstrap neither reuses it nor writes over
    it. Both of those are decisions with consequences for whatever is using the role today, so
    they need a person and an explicit authorization, and the report says exactly what differed.
    """
    _require_account_wide_ownership(step)
    role_name = role_name_for(step)
    expected_trust, expected_arn = _expected_documents(step, trust_policies, permission_policy_arns or {})

    state, detail = _read_role(iam, role_name, expected_trust)
    if state is StepState.CONFLICT:
        # Returned before the attachment read, and before any dispatch. There is nothing to
        # learn from the second read here: whoever may assume this role is already wrong, and
        # the permissions it carries do not change that or make it reusable.
        return BootstrapOutcome(step=step.name, state=state, detail=detail)
    if state is StepState.ESTABLISHED:
        # The role is there and is the one this plan describes. Whether it is USABLE is a
        # second question, and the answer decides between reusing it unchanged and finishing an
        # interrupted earlier bootstrap.
        attached_state, attached_detail = _read_attached_permissions(iam, role_name, expected_arn)
        if attached_state is StepState.ESTABLISHED:
            return BootstrapOutcome(step=step.name, state=attached_state, detail=attached_detail)
        if attached_state is StepState.CONFLICT:
            # NOT the attach path. `_dispatch_attach` would add the reviewed policy alongside
            # whatever is already there and then report the role complete — leaving the extra
            # authority in place with a success next to it.
            return BootstrapOutcome(step=step.name, state=attached_state, detail=attached_detail)
        if attached_state in (StepState.DENIED, StepState.NOT_CHECKED):
            remediation = f" Remediation: {step.denial_remediation}" if step.denial_remediation else ""
            return BootstrapOutcome(step=step.name, state=attached_state, detail=f"{attached_detail}.{remediation}")
        # ABSENT: the role exists with no permissions. Complete it by attaching, and
        # specifically NOT by creating the role again — which would fail against the role the
        # earlier pass already made and report that failure as a bootstrap error.
        return await _dispatch_attach(
            executor,
            iam,
            step,
            role_name,
            refusal_types,
            reason=attached_detail,
            expected_policy_arn=expected_arn,
        )
    if state in (StepState.DENIED, StepState.NOT_CHECKED):
        remediation = f" Remediation: {step.denial_remediation}" if step.denial_remediation else ""
        return BootstrapOutcome(step=step.name, state=state, detail=f"{detail}.{remediation}")

    if step.presence is PresenceRule.MUST_ALREADY_EXIST:
        return BootstrapOutcome(
            step=step.name,
            state=StepState.ABSENT,
            detail=(
                f"{role_name} is absent and the plan marks this step "
                f"{PresenceRule.MUST_ALREADY_EXIST.value!r}, so bootstrap does not create "
                f"it. {step.denial_remediation}"
            ),
        )

    trust_policy = trust_policies.get(step.name)
    if not trust_policy:
        # No safe default exists. See the module docstring: `step.scope` is prose for an
        # operator, not a principal, and guessing who may assume a role that can write IAM
        # in a customer account is not a guess this module is entitled to make.
        raise BootstrapRefused(
            f"no trust policy was supplied for the {step.name!r} step, so {role_name} "
            f"cannot be created. The plan names the document it needs "
            f"({' '.join(step.command)}); this module will not synthesize a principal, "
            f"because a wrong principal is a role the wrong party can assume"
        )

    return await _dispatch_create(
        executor,
        iam,
        step,
        role_name,
        refusal_types,
        expected_trust_policy=expected_trust,
        expected_policy_arn=expected_arn,
    )


def bootstrap_hook(
    credentials: CredentialSource,
    plan: BootstrapPlan,
    *,
    account_id: str,
    trust_policies: dict[str, str] | None = None,
    permission_policy_arns: dict[str, str] | None = None,
    permission_policy_documents: dict[str, object] | None = None,
    policy_update_arns: frozenset[str] = frozenset(),
    audit_trail_arn: str | None = None,
    outcomes: OutcomeVocabulary | None = None,
):
    """Build the provider hook the durable executor invokes for bootstrap writes.

    Installed as `OperationExecutor(..., provider_call=bootstrap_hook(...))`, the same way
    `creation_runner.creation_hook` is: the executor takes its hook at construction, so the
    composer wires this once and `bootstrap_account` drives it through `execute_provider`.

    This is the **only** place in this package that writes to IAM. Everything else reads.
    That concentration is deliberate — there is exactly one function to audit for "what can
    this module change in a customer's account", and it can perform only the writes the
    plan named.

    The hook's contract:

    * It performs **one** write, chosen by `call.operation_kind` (the step name).
    * `ProviderDenied` becomes `FAILED`: an authoritative refusal, nothing was created.
    * `ProviderUnavailable` becomes `UNKNOWN`, never `FAILED`. The role may now exist, and
      `bootstrap_account` settles that by re-reading rather than by retrying.
    * An unrecognised step is `FAILED` without acting, so a plan step this module does not
      implement cannot silently pass as established.

    `permission_policy_arns` maps a step name to the ARN of its reviewed permission policy.
    Supplied by the composer, like the trust policies and for the same reason: an ARN contains
    an account id, so synthesizing one here would be this module guessing which account's
    policy to attach. A role step with no ARN supplied is `FAILED` rather than created, because
    a role created with permissions still pending is the half-built role the review found —
    assumable, powerless, and reported as established by the next pass.

    `outcomes` is the `CallOutcome` class the executor will `isinstance`-check this hook's answer
    against — the real `harness_jobs` one, passed by the composer. It is not optional
    decoration: see `execution.outcome_vocabulary` for why answering in this package's own copy
    makes the executor discard every outcome and record `UNKNOWN` instead, silently.
    """
    policies = dict(trust_policies or {})
    permission_arns = dict(permission_policy_arns or {})
    # Resolved once, at composition time, not per call.
    outcomes = outcome_vocabulary(outcomes)

    async def hook(call: ProviderCallRecord):
        from .baseline import control_hook

        controlled = await control_hook(
            call,
            credentials,
            account_id=account_id,
            policy_arns=permission_arns,
            policy_documents=permission_policy_documents or {},
            policy_update_arns=policy_update_arns,
            outcomes=outcomes,
        )
        if controlled is not None:
            return controlled
        step_name = getattr(call, "operation_kind", "")
        # An attach-only dispatch carries the step name with a suffix, so the same hook can
        # tell "create this role" from "finish the role an earlier pass left half-built".
        attach_only = step_name.endswith(f":{ATTACH_SUFFIX}")
        if attach_only:
            step_name = step_name[: -len(f":{ATTACH_SUFFIX}")]
        try:
            step = plan.step(step_name)
        except Exception as exc:  # BootstrapError: not a step this plan has
            return outcomes.FAILED, f"no step named {step_name!r} in this plan: {exc}", None

        try:
            child = await credentials.child_account(operation_id=getattr(call, "operation_id", ""), account_id=account_id)
        except ProviderDenied as exc:
            return outcomes.FAILED, f"credential refused for {account_id}: {exc}", None
        except ProviderUnavailable as exc:
            return outcomes.UNKNOWN, f"no credential obtained for {account_id}: {exc}", None

        try:
            if step.name == AUTOSCALING_STEP:
                # Pinned principal: see `establish_service_linked_role`.
                child.iam.create_service_linked_role(
                    AWSServiceName=AUTOSCALING_SERVICE_PRINCIPAL,
                    Description=(
                        "Required by ADP workspace KMS key policies for encrypted node volumes. Account-wide; retained through workspace retirement."
                    ),
                )
                return outcomes.SUCCEEDED, "service-linked role created", AUTOSCALING_SERVICE_LINKED_ROLE

            if not isinstance(step.tier, RoleTier):
                return (
                    outcomes.FAILED,
                    f"{step.name!r} is not a role step; this hook performs no write for it",
                    None,
                )

            role_name = role_name_for(step)
            policy_arn = permission_arns.get(step.name)
            if not policy_arn:
                # Refused rather than created-without-permissions. A role with no policy is
                # assumable and powerless, and the next pass's `get_role` would report it
                # established — so the gap would never be closed by anything.
                return (
                    outcomes.FAILED,
                    (
                        f"no permission policy ARN supplied for {step.name!r}; {role_name} not "
                        f"created, because a role with no permissions is assumable and powerless"
                    ),
                    None,
                )

            if attach_only:
                # The role already exists; only the attach is missing. Creating it again would
                # fail against the role the earlier pass made.
                child.iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
                return outcomes.SUCCEEDED, f"{policy_arn} attached to the existing {role_name}", role_name

            trust_policy = policies.get(step.name)
            if not trust_policy:
                return (
                    outcomes.FAILED,
                    f"no trust policy supplied for {step.name!r}; {role_name} not created",
                    None,
                )
            child.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=trust_policy,
                Description=step.reason[:1000],
            )
            # Immediately, in the same dispatch: the window between these two calls is the
            # window in which a crash leaves a half-built role, so it is kept as small as it
            # can be. `_read_attached_permissions` is what recovers the case where a crash
            # lands in it anyway.
            #
            # Once `create_role` has returned, the role EXISTS, and from here on no failure may
            # be reported as `FAILED`. `FAILED` means "the provider established nothing was
            # created", and a caller reading it about a step whose role is now sitting in the
            # account would be reading the opposite of the truth — then retrying the create
            # against it. So the attach's own failure is caught here and answered `UNKNOWN`,
            # which keeps the intent recoverable and sends the next pass to re-read.
            try:
                child.iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
            except (ProviderDenied, ProviderUnavailable) as exc:
                return (
                    outcomes.UNKNOWN,
                    (
                        f"{role_name} WAS created, and attaching {policy_arn} then failed "
                        f"({exc}). Not reported failed: the role exists and is assumable with "
                        f"no permissions. Re-read it; do not create it again"
                    ),
                    role_name,
                )
            return outcomes.SUCCEEDED, f"{role_name} created with {policy_arn}", role_name
        except ProviderDenied as exc:
            return outcomes.FAILED, f"create denied: {exc}", None
        except ProviderUnavailable as exc:
            # The write may have landed. UNKNOWN keeps the intent recoverable; a re-read
            # settles it. Reporting FAILED here would license a retry against a role that
            # already exists.
            return outcomes.UNKNOWN, f"create returned no answer: {exc}", None

    return hook


async def bootstrap_account(
    executor: DurableExecutor,
    credentials: CredentialSource,
    plan: BootstrapPlan,
    *,
    account_id: str,
    placement: AccountPlacement | None = None,
    trust_policies: dict[str, str] | None = None,
    permission_policy_arns: dict[str, str] | None = None,
    permission_policy_documents: dict[str, object] | None = None,
    policy_update_arns: frozenset[str] = frozenset(),
    audit_trail_arn: str | None = None,
    observed: dict[str, StepState] | None = None,
    refusal_types: tuple[type[BaseException], ...] = (),
) -> BootstrapReport:
    """Bootstrap one existing child account, in the plan's verified order.

    `check_order` runs before anything else, so a plan whose steps were assembled in the
    wrong sequence is refused rather than executed — the service-linked role must precede
    any workspace KMS key, and that cannot be left to the order someone wrote the tuple in.

    Execution stops at the first step that does not establish. Continuing past a missing
    bootstrap role would mean attempting later steps with an identity that does not exist,
    and every subsequent failure would be a consequence of the first rather than
    information. Steps after the stop are `NOT_CHECKED`, which keeps them distinct from
    steps that were checked and found absent.

    `placement` is the authoritative evidence that the account is inside its approved
    organizational unit, as `placement.verify_placement` established it. REQUIRED in effect:
    omitting it refuses, because these steps write account-wide identities and an account still
    at the organization root is outside every control the approval was granted against. See
    `_require_verified_placement`.

    Managed policy documents and baseline controls are verified through the bound
    child-account clients. `observed` is retained only to refuse legacy caller-asserted
    evidence. The selected audit trail is read, not created or reconfigured.

    `refusal_types` names the executor's own dispatch-refusal exception, so this module can
    recognise "that key already has a row" without importing the package that defines it —
    see `execution.py` for why the dependency is structural. A composer that passes nothing
    gets no special handling and the refusal propagates, which is safe but less informative.

    Obtaining credentials inside the child account can itself fail. That is a bootstrap
    failure, never a creation failure: the account exists either way.
    """
    check_order(plan)
    # Before the credential, so an unauthorized plan cannot even read the account. See
    # `_require_authorized_plan` for why an unchecked comparison is refused rather than
    # reported: every step below writes an identity the whole account shares.
    _require_authorized_plan(executor, plan)
    # Also before the credential. Placement is a property of the account this whole report is
    # about, so it is checked once here rather than per step: a step-level check would let the
    # first role land in an ungoverned account before the second one objected.
    _require_verified_placement(account_id, placement)
    policies = dict(trust_policies or {})
    permission_arns = dict(permission_policy_arns or {})
    _require_permission_policies(plan, permission_arns)

    # An observation naming a step this plan does not have is refused rather than dropped,
    # on the same reasoning `recovery_report` gives for the same check: a misspelled step
    # name would otherwise silently discard the only evidence a reader cannot reconstruct,
    # and an observation about a step the plan lacks is an observation about something else.
    if observed:
        raise BootstrapRefused("caller-supplied baseline observations are not authoritative; baseline controls are read by the runner")
    if not policy_update_arns.issubset(set(permission_arns.values())):
        raise BootstrapRefused("policy update approval names a policy outside this bootstrap plan")
    documents = dict(permission_policy_documents or {})

    report = BootstrapReport(account_id=account_id, workspace_id=plan.workspace_id)

    try:
        child: AccountCredentials = await credentials.child_account(operation_id=executor.operation_id, account_id=account_id)
    except (ProviderDenied, ProviderUnavailable) as exc:
        # Nothing was read, so every step is NOT_CHECKED rather than absent.
        for step in plan.steps:
            report.outcomes.append(
                BootstrapOutcome(
                    step=step.name,
                    state=StepState.NOT_CHECKED,
                    detail=(
                        f"no credential was obtained for account {account_id} ({exc}), so "
                        f"nothing was read or created. The account exists; this is a "
                        f"bootstrap failure, not a creation one"
                    ),
                )
            )
        return report

    stopped_by = ""
    for step in plan.steps:
        if stopped_by:
            report.outcomes.append(
                BootstrapOutcome(
                    step=step.name,
                    state=StepState.NOT_CHECKED,
                    detail=(f"not attempted: the earlier {stopped_by!r} step blocks workspace provisioning and did not establish"),
                )
            )
            continue

        if step.name == AUTOSCALING_STEP:
            outcome = await establish_service_linked_role(executor, child.iam, step, refusal_types=refusal_types)
        elif isinstance(step.tier, RoleTier):
            from .baseline import ControlResult, ensure_policy

            if step.name not in documents:
                policy = ControlResult(StepState.NOT_CHECKED, "reviewed managed-policy document is required")
            else:
                policy = await ensure_policy(
                    executor,
                    child.iam,
                    step=step.name,
                    arn=permission_arns[step.name],
                    document=documents[step.name],
                    allow_update=permission_arns[step.name] in policy_update_arns,
                    refusal_types=refusal_types,
                )
            if policy.state is StepState.ESTABLISHED:
                outcome = await _establish_role_step(executor, child.iam, step, policies, refusal_types, permission_arns)
            else:
                outcome = BootstrapOutcome(step=step.name, state=policy.state, detail=policy.detail, durable_key=policy.durable_key)
        else:
            from .baseline import ensure_public_access_block, read_audit

            if step.name == "baseline-public-access-block":
                control = await ensure_public_access_block(executor, child, account_id, refusal_types=refusal_types)
            else:
                control = read_audit(child, account_id, audit_trail_arn)
            outcome = BootstrapOutcome(step=step.name, state=control.state, detail=control.detail, durable_key=control.durable_key)

        report.outcomes.append(outcome)
        # Stop only on a step the PLAN says blocks provisioning. Not every unestablished
        # step is a reason to abandon the rest: the baseline controls this runner does not
        # verify are reported `NOT_CHECKED`, and halting on them would relabel every
        # following step "not attempted" — hiding the roles bootstrap could still have
        # established behind a step that was never this runner's to establish.
        # `blocks_workspace_provisioning` is exactly the plan's distinction between "a gap
        # to close later" and "the next workspace build fails outright", and the plan
        # refuses to set it without naming what would fail.
        if outcome.blocks_progress and step.blocks_workspace_provisioning:
            stopped_by = step.name

    return report
