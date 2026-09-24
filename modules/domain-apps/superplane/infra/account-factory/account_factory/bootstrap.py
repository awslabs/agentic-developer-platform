"""Child-account bootstrap, described rather than performed — Issue #5531 (w6-08).

A freshly vended AWS account is not usable. It has no role ADP can assume, no baseline
controls, and — the specific gap this module was asked to close — no
`AWSServiceRoleForAutoScaling`. Everything here is a DESCRIPTION of what bootstrap must
establish, in the order it must be established, for an operator or a separately-authorized
lane to carry out. Nothing in this module executes anything; see `cli.py`'s module docstring.

## Why the service-linked role is bootstrap's and not the workspace's

`modules/domain-apps/superplane/infra/workspaces/` creates a KMS key whose key policy NAMES
the Auto Scaling service-linked-role ARN. KMS validates every principal in a key policy at
key-CREATION time, so if that role does not exist yet the key cannot be created at all. The
failure surfaces as a policy error about a principal, which reads like a malformed policy
rather than a missing account-wide role — so the ordering has to be stated, not discovered.

`workspaces/scripts/workspace_kms.py::verify_account_prerequisites` does a read-only
`iam get-role` and fails closed, with remediation text naming account bootstrap as the owner:
"workspace provisioning never creates or adopts this account-wide role." This module is the
other half of that contract.

The role is account-wide and outlives any one workspace, which is why it must not enter
per-workspace Terraform state. A workspace that adopted it would delete it on teardown, and
every OTHER workspace in that account would then fail its next encrypted-node operation —
one workspace's cleanup breaking its neighbours. `BootstrapStep.adoptable_by_workspace` is
`False` for exactly this reason, and `retained_through_workspace_retirement` records that
retiring a workspace does not release it.

## Three role tiers, because one role would be the union of all three

Bootstrap establishes separate bootstrap/controller/workload roles rather than one role that
can do everything. A single role would hold the union of every permission any of the three
ever needs, so the workload — the least trusted of the three, running arbitrary tenant
work — would inherit the ability to create IAM roles and read the account's baseline
controls. The tiers are what make "the workload cannot re-bootstrap the account" a property
of the credential rather than of a review.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# One-directional: `creation` imports `modes`, never `bootstrap`, so this does not cycle. The
# derivation is imported rather than reimplemented because a second spelling of "which account
# is this?" is the exact drift `recovery_report`'s comparison exists to detect.
from .creation import account_identity_key
from .modes import (
    AccountFactoryRequest,
    ModeError,
    OwnershipMode,
    ValidationAuthorization,
    ensure_valid,
)

__all__ = [
    "AUTOSCALING_SERVICE_LINKED_ROLE",
    "AUTOSCALING_SERVICE_PRINCIPAL",
    "BOOTSTRAP_ROLE_STEP",
    "POLICY_DIR",
    "BootstrapError",
    "BootstrapPlan",
    "BootstrapStep",
    "PresenceRule",
    "RoleTier",
    "bootstrap_plan",
    "check_order",
    "policy_path",
]


POLICY_DIR = Path(__file__).resolve().parent.parent / "policies"
if Path(__file__).with_name("_data").is_dir():
    POLICY_DIR = Path(__file__).with_name("_data") / "policies"
"""Where the reviewed trust and permission documents live.

Resolved from this file rather than left as a bare relative `file://` argument. The commands
these steps carry used to say `file://bootstrap-trust-policy.json`, which resolves against
whatever directory the operator happened to be standing in — and the three files did not
exist anywhere in the repository, so every role command was unrunnable and any account
bootstrapped from this plan would have been left half-built.

An absolute path makes the command runnable from anywhere and makes the document that applies
the document that was reviewed. Note that `account_provisioning.bootstrap_runner` still takes
trust policies as injected STRINGS and refuses a step with none supplied — it deliberately
does not read this directory, so "which document applied" stays an argument at the call site.
"""


AUTOSCALING_SERVICE_LINKED_ROLE = "AWSServiceRoleForAutoScaling"
"""The account-wide role a workspace KMS key policy names. Spelled once, because the
workspace side matches this name exactly and a near-miss would read as an absent role."""

AUTOSCALING_SERVICE_PRINCIPAL = "autoscaling.amazonaws.com"
"""The one service `iam:CreateServiceLinkedRole` may be scoped to. Granting the permission
unscoped would let bootstrap mint a service-linked role for ANY AWS service in the account,
which is a far larger grant than the one thing it needs."""

BOOTSTRAP_ROLE_STEP = "bootstrap-role"
"""The step that establishes the identity every LATER step is carried out with.

Named rather than spelled inline at each use, because `check_order` compares step names to
decide whether the plan's ordering is safe, and a comparison against a typo'd literal is a
check that silently passes."""

AUTOSCALING_STEP = "autoscaling-service-linked-role"
"""The step `check_order` requires to be present and to follow the bootstrap role."""

_CREATED_BY_BOOTSTRAP_ROLE = ("controller-role", "workload-role")
"""Steps the bootstrap role itself performs, so they cannot precede it."""


class BootstrapError(Exception):
    """A bootstrap plan was refused. No plan is returned — the caller gets nothing."""


def policy_path(filename: str) -> Path:
    """Resolve one reviewed policy document, refusing to name a file that is not there.

    The refusal is the point. A plan that cheerfully carries `file://missing-policy.json` is
    one whose commands fail at the moment an operator runs them against a real account, half
    way through bootstrap, with roles already created — which is precisely the "a created
    account remains half-built" state this module was reviewed for. Checking at plan-build time
    moves that failure offline, before anything has been established.
    """
    candidate = POLICY_DIR / filename
    if not candidate.is_file():
        raise BootstrapError(
            f"the reviewed policy document {filename!r} does not exist at {candidate}. A "
            f"bootstrap step cannot name a document an operator's command could not resolve: "
            f"the command would fail against a live account with earlier steps already applied"
        )
    return candidate


class RoleTier(str, Enum):
    """The three separately-scoped identities bootstrap establishes.

    Separate rather than one union role: see the module docstring. Ordered here from most to
    least privileged, which is also the order they are created in — each tier is created BY
    the tier above it, so the workload role cannot be the thing that creates itself.
    """

    BOOTSTRAP = "bootstrap"
    """Establishes the account: creates the other two roles, the service-linked role and the
    baseline controls. The only tier holding IAM write permissions, and the only one that may
    be assumed from the management account by the bootstrap operation itself. It is NOT the
    role ongoing provisioning uses — leaving IAM-write reachable from routine reconciliation
    would make every later operation able to re-scope the account's own guardrails."""

    CONTROLLER = "controller"
    """What the management cluster's controllers assume to build and reconcile workspace
    infrastructure. Holds no IAM write permission, so a compromised or malfunctioning
    controller cannot widen its own scope or that of the workload."""

    WORKLOAD = "workload"
    """What tenant work runs as. Least trusted of the three: it reaches only the workspace
    resources it was given, and can neither read the account's baseline controls nor create
    an identity. This is the tier that would inherit IAM-write if the three were one role."""

    @property
    def may_write_iam(self) -> bool:
        """Only the bootstrap tier may create identities."""
        return self is RoleTier.BOOTSTRAP


class PresenceRule(str, Enum):
    """What to do when the thing a step establishes already exists.

    The distinction matters because the safe answer differs, and "create it" is wrong for
    every account-wide resource: a second `create-service-linked-role` returns an error that
    a naive runner reads as a bootstrap failure, and a re-created baseline control silently
    discards whatever an operator had tightened.
    """

    REUSE_IF_PRESENT = "reuse-if-present"
    """Read first; if it exists, use it unchanged and create nothing. Re-running bootstrap
    over an account that already has this is a no-op, not an error and not a replacement."""

    CREATE_IF_ABSENT = "create-if-absent"
    """Create only after a read has VERIFIED absence. Not "create and ignore the already-exists
    error": an error-swallowing create cannot distinguish "it was already there" from "the
    create was denied", and those need opposite responses."""

    MUST_ALREADY_EXIST = "must-already-exist"
    """Bootstrap does not create this. Its absence is a refusal with remediation, never a
    silent skip."""

    @property
    def meaning(self) -> str:
        """The rule in words, for a report an operator reads.

        A real attribute rather than only the member docstrings above, because Python does
        not attach a per-member `__doc__` to enum members — each one reports the CLASS
        docstring instead. So the docstrings are for whoever reads this file, and this is
        what a report can actually print; a report that showed an operator
        `presence: create-if-absent` and nothing else would not have told them that the read
        must come first.
        """
        return _PRESENCE_MEANINGS[self]


_PRESENCE_MEANINGS = {
    PresenceRule.REUSE_IF_PRESENT: (
        "Read first; if it exists, use it unchanged and create nothing. Re-running bootstrap "
        "over an account that already has this is a no-op, not an error and not a replacement."
    ),
    PresenceRule.CREATE_IF_ABSENT: (
        "Create only after a read has VERIFIED absence. Not create-and-ignore-the-"
        "already-exists-error: an error-swallowing create cannot distinguish "
        '"it was already there" from "the create was denied", and those need opposite '
        "responses."
    ),
    PresenceRule.MUST_ALREADY_EXIST: (
        "Bootstrap does not create this. Its absence is a refusal with remediation, never a "
        "silent skip."
    ),
}
"""Every rule is spelled out; `PresenceRule.meaning` raises on a member added without one,
which is deliberate — a rule with no stated meaning is one a report describes by its slug."""


@dataclass(frozen=True)
class BootstrapStep:
    """One thing child-account bootstrap must establish, described for deliberate execution.

    A description, never an action — like `render.PrerequisiteOperation`, which this
    deliberately resembles. `command` is what an operator would run; nothing here runs it.

    `precedes` names what would FAIL if this step had not happened, rather than an abstract
    ordering number. An ordering constraint whose consequence is not stated is one a future
    reader reorders, because nothing on the constraint says what it was protecting.

    `blocks_workspace_provisioning` is separate from `precedes` and narrower. Every step
    precedes SOMETHING, so `precedes` cannot distinguish a gap to close later from a gap that
    makes the next workspace build fail outright — and a recovery report that called everything
    blocking would be telling an operator nothing.
    """

    name: str
    tier: RoleTier | None
    reason: str
    command: tuple[str, ...]
    presence: PresenceRule
    scope: str
    precedes: str = ""
    adoptable_by_workspace: bool = False
    retained_through_workspace_retirement: bool = True
    denial_remediation: str = ""
    blocks_workspace_provisioning: bool = False
    read_command: tuple[str, ...] = ()
    """The read that decides whether `command` runs at all.

    Separate from `command` because the two are different acts with different permissions, and
    conflating them is how `baseline-public-access-block` came to be marked `CREATE_IF_ABSENT`
    while its only command was `get-public-access-block` — a step that reported on a control it
    never established. A rule that says "read first, then create" needs somewhere to put each
    half; with one field, one of them is missing and nobody can see which.
    """
    permission_policy: str = ""
    """The reviewed permission document attached to the role this step creates.

    Empty for steps that create no role. A role created with a trust policy and no permissions
    is an identity that can be assumed and can then do nothing, which fails later and
    elsewhere — as a workspace build that cannot reach the account it was given.
    """

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise BootstrapError(
                f"bootstrap step {self.name!r} must state why it exists"
            )
        if self.presence is PresenceRule.CREATE_IF_ABSENT and not self.read_command:
            # The rule's whole content is "a read established absence first". Without a read
            # command the step cannot carry out the rule it claims to follow, and a runner
            # would either skip the read or invent one.
            raise BootstrapError(
                f"bootstrap step {self.name!r} is {PresenceRule.CREATE_IF_ABSENT.value!r} but "
                f"states no read_command, so nothing establishes the absence it must verify "
                f"before creating"
            )
        if self.read_command and self.read_command == self.command:
            # This is the exact shape of the reviewed defect: one command doing duty as both
            # the read and the write means the write does not exist.
            raise BootstrapError(
                f"bootstrap step {self.name!r} uses the same command to read and to write, so "
                f"it cannot establish anything: {' '.join(self.command)}"
            )
        if self.tier is not None and not self.permission_policy.strip():
            raise BootstrapError(
                f"bootstrap step {self.name!r} creates a {self.tier.value} role but attaches no "
                f"permission policy, so the role would be created unusable"
            )
        if self.blocks_workspace_provisioning and not self.precedes.strip():
            # What it blocks IS the justification for blocking. A step that halts a workspace
            # build without naming what would fail is one an operator overrides.
            raise BootstrapError(
                f"bootstrap step {self.name!r} claims to block workspace provisioning but "
                f"does not state what would fail without it"
            )
        if not self.denial_remediation.strip():
            # A step whose denial has no stated remedy leaves the operator with a permission
            # error and no next move. Fail-closed is only useful if it says what to do.
            raise BootstrapError(
                f"bootstrap step {self.name!r} must state what to do when it is denied: a "
                f"refusal with no remediation strands the operator"
            )
        if self.adoptable_by_workspace and self.retained_through_workspace_retirement:
            raise BootstrapError(
                f"bootstrap step {self.name!r} claims to be adoptable by a workspace AND "
                f"retained through workspace retirement, which cannot both hold: an adopted "
                f"resource is deleted with the workspace that adopted it"
            )


@dataclass(frozen=True)
class BootstrapPlan:
    """The ordered bootstrap description for one child account.

    `steps` is ordered, and the order is a requirement rather than a presentation choice —
    `check_order` enforces the one ordering whose violation is silent and expensive.
    """

    workspace_id: str
    organization_id: str
    steps: tuple[BootstrapStep, ...]
    unchecked_authorization: tuple[str, ...] = ()
    """Authorization comparisons that were NOT performed, because no value was supplied.

    Carried on the plan rather than dropped, for the same reason `render` carries it: a plan
    that describes writing account-wide roles, and a recovery report that can call an account
    ready for a workspace, must be able to say which ownership questions nobody answered. An
    absent comparison is not a passed one, and the account-wide steps here are the least
    reversible thing this module describes.
    """

    account_identity_key: str | None = None
    """Which account this plan is about, as the derived identity key (#5531, w6-08).

    `workspace_id` and `organization_id` above are two of the four fields that decide WHICH
    account a request is for; the other two — the organizational unit it is placed into and the
    contact address it is opened with — were not represented on the plan at all. That made them
    uncheckable by `recovery.recovery_report`, which pairs a plan's step observations with a
    creation attempt's account id and must establish that the two are about the same account.

    Carried as the derived key rather than as the raw OU and address, so the comparison covers
    all four fields in one equality against the same derivation `creation._idempotency_key`
    uses — two spellings of the same rule are two that can drift apart. It also keeps the
    contact address off the plan, and therefore out of every rendering and log line a plan
    appears in.

    `None` when the plan was built without a request to derive from, which is the direct
    `BootstrapPlan(...)` construction the ordering tests use. Absent means the comparison is
    NOT MADE rather than passed, on the same footing as `unchecked_authorization`.
    """
    operation_org_id: str | None = None

    @property
    def account_wide_steps(self) -> tuple[BootstrapStep, ...]:
        """Steps that outlive any one workspace and must never be adopted into its state."""
        return tuple(step for step in self.steps if not step.adoptable_by_workspace)

    def step(self, name: str) -> BootstrapStep:
        for candidate in self.steps:
            if candidate.name == name:
                return candidate
        raise BootstrapError(f"no bootstrap step named {name!r}")


def _autoscaling_step() -> BootstrapStep:
    """The service-linked role, established before any workspace KMS key exists.

    Uses the exact role name and service principal the workspace side checks for, and the
    exact command its README names, so the two halves of the contract cannot drift into
    describing near-miss identities.
    """
    return BootstrapStep(
        name=AUTOSCALING_STEP,
        # Not a tier: a service-linked role is AWS's own identity for the Auto Scaling
        # service, not one of ADP's three. Recording it as a tier would imply ADP assumes it.
        tier=None,
        reason=(
            f"A workspace KMS key policy names the {AUTOSCALING_SERVICE_LINKED_ROLE} ARN, and "
            f"KMS validates every principal in a key policy at key-creation time. Without "
            f"this role the workspace key cannot be created, and the failure reads as a "
            f"malformed policy rather than a missing account-wide role"
        ),
        command=(
            "aws",
            "iam",
            "create-service-linked-role",
            "--aws-service-name",
            AUTOSCALING_SERVICE_PRINCIPAL,
        ),
        # The read that makes the create conditional. Most accounts already have this role —
        # anything that has ever used an Auto Scaling group does — so an unconditional create
        # makes the COMMON case an error, and a runner that swallows that error can no longer
        # tell "it was already there" from "the create was denied".
        read_command=(
            "aws",
            "iam",
            "get-role",
            "--role-name",
            AUTOSCALING_SERVICE_LINKED_ROLE,
        ),
        # Read first. The role exists already in most accounts — anything that has ever used
        # an Auto Scaling group has it — so creating unconditionally makes the common case an
        # error, and the account-bootstrap identity needs `iam:GetRole` either way.
        presence=PresenceRule.CREATE_IF_ABSENT,
        scope=(
            "child account (account-wide, once per account; shared by every workspace in it)"
        ),
        precedes=(
            "workspace KMS key creation, and therefore all encrypted managed-node "
            "provisioning in this account"
        ),
        # The property that keeps one workspace's teardown from breaking its neighbours.
        adoptable_by_workspace=False,
        retained_through_workspace_retirement=True,
        # Not "a gap to close later": without this the workspace KMS key cannot be created at
        # all, so the workspace build fails rather than completing with a weaker account.
        blocks_workspace_provisioning=True,
        denial_remediation=(
            f"Grant the account-bootstrap identity iam:GetRole and iam:CreateServiceLinkedRole "
            f"scoped to {AUTOSCALING_SERVICE_PRINCIPAL}, then re-run bootstrap. Do NOT grant "
            f"the permission unscoped, and do not have workspace provisioning create the role "
            f"instead: an account-wide role owned by one workspace is deleted when that "
            f"workspace is retired, breaking every other workspace in the account"
        ),
    )


def _role_steps() -> tuple[BootstrapStep, ...]:
    """The three scoped roles, in creation order — each created by the tier above it."""
    return (
        BootstrapStep(
            name=BOOTSTRAP_ROLE_STEP,
            tier=RoleTier.BOOTSTRAP,
            reason=(
                "A newly vended account has no identity ADP can assume, so the first step is "
                "the one role the management account may assume to establish the rest. Held "
                "separately from the controller role because it carries IAM write "
                "permissions, which routine reconciliation must not be able to reach"
            ),
            command=(
                "aws",
                "iam",
                "create-role",
                "--role-name",
                "AdpAccountBootstrap",
                "--assume-role-policy-document",
                f"file://{policy_path('bootstrap-trust-policy.json')}",
            ),
            read_command=(
                "aws",
                "iam",
                "get-role",
                "--role-name",
                "AdpAccountBootstrap",
            ),
            permission_policy=str(policy_path("bootstrap-permissions-policy.json")),
            presence=PresenceRule.REUSE_IF_PRESENT,
            scope="child account (account-wide, once per account)",
            precedes="every other bootstrap step, and all workspace provisioning",
            # Without an identity to assume, nothing can act in the account at all.
            blocks_workspace_provisioning=True,
            denial_remediation=(
                "The management account's bootstrap operation lacks permission to create a "
                "role in the child account. For a newly created account this is normally the "
                "organization's account-creation role; confirm which identity the account was "
                "created with rather than widening an existing role"
            ),
        ),
        BootstrapStep(
            name="controller-role",
            tier=RoleTier.CONTROLLER,
            reason=(
                "What the management cluster's controllers assume to build and reconcile "
                "workspace infrastructure. It holds no IAM write permission, so a controller "
                "that is compromised or malfunctioning cannot widen its own scope"
            ),
            command=(
                "aws",
                "iam",
                "create-role",
                "--role-name",
                "AdpWorkspaceController",
                "--assume-role-policy-document",
                f"file://{policy_path('controller-trust-policy.json')}",
            ),
            read_command=(
                "aws",
                "iam",
                "get-role",
                "--role-name",
                "AdpWorkspaceController",
            ),
            permission_policy=str(policy_path("controller-permissions-policy.json")),
            presence=PresenceRule.REUSE_IF_PRESENT,
            scope="child account (account-wide, once per account)",
            precedes="workspace infrastructure reconciliation",
            # The controllers have nothing to assume, so reconciliation cannot start.
            blocks_workspace_provisioning=True,
            denial_remediation=(
                "Bootstrap could not create the controller role. Verify the bootstrap role "
                "was established first and carries IAM write permission for this account"
            ),
        ),
        BootstrapStep(
            name="workload-role",
            tier=RoleTier.WORKLOAD,
            reason=(
                "What tenant work runs as. Separate from the controller role so that tenant "
                "work cannot reconcile infrastructure, and separate from the bootstrap role "
                "so it can neither create an identity nor read the account's baseline "
                "controls"
            ),
            command=(
                "aws",
                "iam",
                "create-role",
                "--role-name",
                "AdpWorkspaceWorkload",
                "--assume-role-policy-document",
                f"file://{policy_path('workload-trust-policy.json')}",
            ),
            read_command=(
                "aws",
                "iam",
                "get-role",
                "--role-name",
                "AdpWorkspaceWorkload",
            ),
            permission_policy=str(policy_path("workload-permissions-policy.json")),
            presence=PresenceRule.REUSE_IF_PRESENT,
            scope="child account (account-wide, once per account)",
            precedes="tenant workload execution",
            denial_remediation=(
                "Bootstrap could not create the workload role. Verify the bootstrap role was "
                "established first; do not run tenant work under the controller role as a "
                "workaround, which would give tenant work infrastructure permissions"
            ),
        ),
    )


def _baseline_control_steps() -> tuple[BootstrapStep, ...]:
    """The baseline controls, which bootstrap verifies rather than silently re-applies."""
    return (
        BootstrapStep(
            name="baseline-audit-logging",
            tier=None,
            reason=(
                "An account with no audit trail cannot be investigated after the fact, and "
                "the record has to exist BEFORE the account is used rather than being turned "
                "on once something is already wrong"
            ),
            command=("aws", "cloudtrail", "describe-trails"),
            # Verified, not created: an organization trail normally already covers the
            # account, and creating a second one duplicates cost and evidence.
            presence=PresenceRule.MUST_ALREADY_EXIST,
            scope="child account (account-wide, normally via an organization trail)",
            precedes="any workload running in this account",
            denial_remediation=(
                "No audit trail covers this account. Confirm the organization trail includes "
                "newly created accounts; do not create a per-account trail as a substitute "
                "without deciding who owns it"
            ),
        ),
        BootstrapStep(
            name="baseline-public-access-block",
            tier=None,
            reason=(
                "Account-level S3 public access block, applied before any workload can create "
                "a bucket. Applied after the fact it is a remediation of data that may "
                "already have been exposed"
            ),
            # The WRITE. Before this it was `get-public-access-block` — a read — while the step
            # was marked `create-if-absent`, so the control this step exists to establish was
            # never established by it. The account read as bootstrapped with public access
            # unblocked, which is the state the step's own reason calls unacceptable.
            #
            # All four sub-settings, because a partial block is the one that reads as protection
            # and is not: blocking new public ACLs while honouring existing ones leaves any
            # bucket that already carries one public.
            command=(
                "aws",
                "s3control",
                "put-public-access-block",
                "--public-access-block-configuration",
                (
                    "BlockPublicAcls=true,IgnorePublicAcls=true,"
                    "BlockPublicPolicy=true,RestrictPublicBuckets=true"
                ),
                "--account-id",
                "${child_account_id}",
            ),
            read_command=(
                "aws",
                "s3control",
                "get-public-access-block",
                "--account-id",
                "${child_account_id}",
            ),
            presence=PresenceRule.CREATE_IF_ABSENT,
            scope="child account (account-wide)",
            precedes="any workload that can create a bucket",
            denial_remediation=(
                "Bootstrap could not read or set the account public access block. Treat the "
                "account as not yet bootstrapped rather than proceeding with it unset"
            ),
        ),
    )


def bootstrap_plan(
    request: AccountFactoryRequest,
    authorization: ValidationAuthorization | None = None,
) -> BootstrapPlan:
    """Describe what bootstrapping this request's child account requires.

    Validates first, so a plan naming an account this run may not act on cannot be produced.

    Refuses `bring-existing-cluster`: that mode creates no AWS infrastructure and adopts a
    cluster someone else runs, so "bootstrapping" it would mean rewriting roles and baseline
    controls in an account ADP does not own.

    Both account-managed modes DO get a plan. A newly vended account needs every step, and an
    adopted account needs the same prerequisites verified — `PresenceRule.REUSE_IF_PRESENT`
    is what makes running it against an already-bootstrapped account a no-op rather than a
    replacement. Skipping the plan for adopted accounts is what leaves the service-linked
    role unchecked until a KMS key creation fails with a confusing policy error.
    """
    if request.mode is OwnershipMode.BRING_EXISTING_CLUSTER:
        raise BootstrapError(
            f"refusing to describe child-account bootstrap for mode "
            f"{request.mode.value!r}: it adopts a cluster and creates no AWS infrastructure, "
            f"so bootstrapping would rewrite roles and controls in an account ADP does not own"
        )
    try:
        unchecked = ensure_valid(request, authorization)
    except ModeError as exc:
        raise BootstrapError(
            f"refusing to describe bootstrap for a request that does not validate: {exc}"
        ) from exc

    steps = (
        *_role_steps(),
        # BEFORE the workspace infrastructure steps, and before anything that creates a KMS
        # key. `check_order` enforces this; see its docstring for why it is checked and not
        # merely arranged.
        _autoscaling_step(),
        *_baseline_control_steps(),
    )
    plan = BootstrapPlan(
        workspace_id=request.workspace_id,
        organization_id=request.organization_id,
        steps=steps,
        unchecked_authorization=tuple(unchecked),
        # Derived here, from the request, because this is the only place that HAS the request.
        # It is what lets `recovery_report` establish that a creation attempt's account id is
        # about this plan's account and not another workspace's.
        account_identity_key=account_identity_key(request),
        operation_org_id=authorization.operation_org_id if authorization else None,
    )
    check_order(plan)
    return plan


def check_order(plan: BootstrapPlan) -> None:
    """Refuse a plan whose ordering would fail late and confusingly.

    Checked rather than merely arranged, because the consequence of getting it wrong is not
    a visibly broken plan: the steps all look reasonable in any order, and the failure
    surfaces much later as a KMS policy error about a principal. A property that is only
    maintained by the order someone happened to write the tuple in is one a later edit
    silently breaks.
    """
    names = [step.name for step in plan.steps]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise BootstrapError(
            f"bootstrap plan repeats step(s): {', '.join(duplicates)}. Which one applies "
            f"decides what the account ends up with"
        )

    if AUTOSCALING_STEP not in names:
        raise BootstrapError(
            f"bootstrap plan omits {AUTOSCALING_SERVICE_LINKED_ROLE}. A workspace KMS key "
            f"policy names it, and KMS validates principals at key-creation time, so the "
            f"workspace key could not be created"
        )
    if BOOTSTRAP_ROLE_STEP not in names:
        # Checked before indexing: without it, every later step is described as being carried
        # out by an identity the plan never establishes.
        raise BootstrapError(
            f"bootstrap plan omits {BOOTSTRAP_ROLE_STEP!r}, the identity every later step is "
            f"carried out with"
        )

    bootstrap_role = names.index(BOOTSTRAP_ROLE_STEP)
    if bootstrap_role > names.index(AUTOSCALING_STEP):
        raise BootstrapError(
            "the bootstrap role must be established before the service-linked role: the "
            "service-linked role is created USING the account-bootstrap identity, which does "
            "not exist yet"
        )

    for name in _CREATED_BY_BOOTSTRAP_ROLE:
        if name in names and names.index(name) < bootstrap_role:
            raise BootstrapError(
                f"{name!r} is ordered before the {BOOTSTRAP_ROLE_STEP!r} step that creates it"
            )
