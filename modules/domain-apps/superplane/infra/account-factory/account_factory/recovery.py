"""Recovery and retention for a half-built account — Issue #5531 (w6-08).

The state this module exists for is **create-succeeded, bootstrap-failed**: the AWS account is
real and billable, and the roles, service-linked role or baseline controls that make it usable
are partly or wholly absent. It is the worst state to be in and the easiest to mishandle,
because the two obvious moves are both wrong:

* **Retry from the top.** If the creation outcome was never read, retrying can open a SECOND
  account. Nothing in this module lets a report say a retry is safe while the creation decision
  is unresolved.
* **Tear it down and start over.** Closure is an irreversible 90-day suspension of an account
  whose id cannot be reused in that window, so "start over" is not a reset — it is a permanent
  cost plus a second account. `cleanup.closure_request` already refuses every implicit route to
  one; this module never produces a closure request at all.

What is actually needed is a report that separates four questions a single "failed" status
collapses: what EXISTS, what is INCOMPLETE, what a retry would and would not REPEAT, and what
is RETAINED regardless. A status line cannot carry that, which is why this is a report and not
a flag.

## Unchecked is not absent

A step nobody looked at must not be reported as missing, and must not be reported as present.
This is the same distinction `creation.py` draws between FAILED and UNKNOWN, applied to
bootstrap: "the role is not there" invites creating it, while "I did not look" invites looking.
Conflating them means either creating something that exists or assuming something that does
not. `StepState.NOT_CHECKED` is therefore a first-class state, it is what a step defaults to,
and it keeps `every_step_accounted_for` false — so a report with gaps cannot read as a clean
bill of health.

Nothing here executes, reads or repairs anything. Outcomes are OBSERVATIONS supplied by
whoever actually looked; this module only decides what they add up to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .bootstrap import BootstrapPlan, BootstrapStep, PresenceRule
from .creation import AttemptDecision, AttemptDisposition, _derive_key

__all__ = [
    "RecoveryError",
    "RecoveryReport",
    "StepFinding",
    "StepState",
    "blocking_prerequisites",
    "recovery_report",
]


class RecoveryError(Exception):
    """A recovery report was refused. No report is returned."""


class StepState(str, Enum):
    """What is known about one bootstrap step in a live account.

    Five states rather than a boolean, because the four non-established ones call for
    different actions and the differences are exactly what a "failed" status loses.
    """

    ESTABLISHED = "established"
    """Verified present by a read. A retry does not repeat this step."""

    ABSENT = "absent"
    """Verified NOT present by a read that succeeded. This is what a retry would establish,
    and the only state in which creating the thing is the right move."""

    DENIED = "denied"
    """The read or the create was refused by permissions. Distinct from `ABSENT` because
    retrying changes nothing until the permission is granted — a retry loop on a denial is an
    infinite loop that looks like progress."""

    NOT_CHECKED = "not-checked"
    """Nobody looked. NOT absent and NOT established: it neither invites creating the thing nor
    permits concluding the account is ready. The default, so a step omitted from the
    observations cannot silently read as fine."""

    CONFLICT = "conflict"
    """Something of this name EXISTS but is not what the plan describes.

    Distinct from every other state, and the distinction is the point. `ESTABLISHED` would
    reuse it; `ABSENT` would try to create it, which fails against the thing already there;
    `DENIED` would tell an operator to fix a permission that is not the problem.

    The case this exists for is a role carrying the right name and the wrong contents — a
    hand-made one, one from an older revision of these documents, or one trusting a principal
    nobody reviewed. Reusing it silently is a tenant-isolation failure: a role named
    `AdpWorkspaceController` that trusts the wrong account, or carries `AdministratorAccess`,
    passes as "bootstrapped" and is then handed a workspace.

    Resolving it requires a person to decide whether to re-scope or replace the existing
    identity, because both are destructive to whatever is using it today. So this never
    auto-remediates: it blocks, and it says what differed.
    """

    @property
    def is_known(self) -> bool:
        """True when a read actually established this state."""
        return self is not StepState.NOT_CHECKED

    @property
    def retry_would_act(self) -> bool:
        """True only for `ABSENT`.

        A retry re-establishes what is verified missing. It does not repeat an established
        step (`REUSE_IF_PRESENT`/`CREATE_IF_ABSENT` make that a no-op), it cannot get past a
        `DENIED` one, and it must not act on a step nobody read — acting on an unread step is
        how a create lands on something that already exists.
        """
        return self is StepState.ABSENT


@dataclass(frozen=True)
class StepFinding:
    """What was observed about one step, and what follows from it.

    A finding carries the step it is about rather than only a name, so a report can state the
    retention and remediation the step already declares instead of restating them — two copies
    of a remediation is one that can disagree with itself.
    """

    step: BootstrapStep
    state: StepState
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state is StepState.DENIED and not self.detail.strip():
            # A denial with no detail is indistinguishable from a guess, and the remedy
            # depends on WHICH permission was refused.
            raise RecoveryError(
                f"a denied finding for {self.step.name!r} must say what was refused: a "
                f"denial with no detail cannot be told from an assumption"
            )
        if self.state is StepState.CONFLICT and not self.detail.strip():
            # Required for the same reason as a denial's, and more urgently. A conflict asks a
            # person to decide whether to re-scope or replace an existing identity, and that
            # decision is impossible without knowing WHAT differed — a wrong trusted principal
            # and an extra attached policy call for opposite remedies. A bare "conflict" also
            # reads as a transient failure and invites exactly the re-run that cannot work.
            raise RecoveryError(
                f"a conflicting finding for {self.step.name!r} must say what differed: a "
                f"conflict with no detail cannot be acted on, because re-scoping and "
                f"replacing the existing identity are different decisions"
            )

    @property
    def name(self) -> str:
        return self.step.name

    @property
    def retry_repeats(self) -> bool:
        return self.state.retry_would_act

    @property
    def blocks_progress(self) -> bool:
        """True when a retry cannot advance this step as things stand.

        `DENIED` blocks on a permission; `NOT_CHECKED` blocks because the safe action is
        unknown; `CONFLICT` blocks because something else is already occupying the name and
        replacing it is a decision a person has to take. All three need someone to do
        something other than run the retry again.
        """
        return self.state in {
            StepState.DENIED,
            StepState.NOT_CHECKED,
            StepState.CONFLICT,
        }

    @property
    def next_action(self) -> str:
        """What to do about this step, in words, for the operator reading the report."""
        if self.state is StepState.ESTABLISHED:
            if self.step.presence is PresenceRule.MUST_ALREADY_EXIST:
                return "nothing: verified present, and bootstrap does not create it"
            return "nothing: verified present, and re-running bootstrap reuses it unchanged"
        if self.state is StepState.ABSENT:
            return (
                f"establish it ({' '.join(self.step.command)}); verified absent, so this "
                f"creates rather than replaces"
            )
        if self.state is StepState.DENIED:
            return f"resolve the denial, then re-read: {self.step.denial_remediation}"
        if self.state is StepState.CONFLICT:
            return (
                "DECIDE, do not re-run. Something of this name already exists and differs "
                f"from what this plan describes: {self.detail}. Bootstrap will not re-scope "
                "or replace an existing identity on its own, because whatever is using it "
                "today would be affected. Re-scope or replace it under explicit "
                "authorization, then re-read"
            )
        return (
            "READ IT FIRST. Nobody has looked, so it is neither known present nor known "
            "absent, and creating it blindly could act on something that already exists"
        )


@dataclass(frozen=True)
class RecoveryReport:
    """The explicit recovery and retention report for a half-built account.

    Reports rather than decides-and-hides: every property below is derived from findings a
    reader can also see, because an operator asked to act on an irreversible account needs the
    evidence and not only the verdict.
    """

    workspace_id: str
    organization_id: str
    account_id: str | None
    creation_disposition: AttemptDisposition
    findings: tuple[StepFinding, ...] = field(default_factory=tuple)

    # ── What exists ─────────────────────────────────────────────────────────────────────

    @property
    def account_exists(self) -> bool:
        """True only when a recorded, conclusive success says so.

        `UNRESOLVED` is deliberately false here and true in `account_may_exist_untracked`
        below. Reporting a possible account as existing would invite adopting an id nothing
        confirmed; reporting it as absent would abandon a real one.
        """
        return self.creation_disposition is AttemptDisposition.ALREADY_CREATED

    @property
    def account_may_exist_untracked(self) -> bool:
        """True while an account may exist that nothing is tracking — the costly unknown."""
        return self.creation_disposition is AttemptDisposition.UNRESOLVED

    @property
    def established(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.findings if f.state is StepState.ESTABLISHED)

    # ── What is incomplete ──────────────────────────────────────────────────────────────

    @property
    def incomplete(self) -> tuple[str, ...]:
        """Steps not verified established — including the ones nobody checked.

        A step nobody read belongs here rather than in `established`: the account is not known
        to be ready, and that is what "incomplete" means.
        """
        return tuple(
            f.name for f in self.findings if f.state is not StepState.ESTABLISHED
        )

    @property
    def unchecked(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.findings if f.state is StepState.NOT_CHECKED)

    @property
    def denied(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.findings if f.state is StepState.DENIED)

    @property
    def conflicting(self) -> tuple[str, ...]:
        """Steps whose name is already taken by something that is not what the plan describes.

        Listed separately from `denied` and `unchecked` because the remedy is a decision rather
        than a permission grant or another read. Lumping these in with denials would send an
        operator to fix an IAM policy that is working exactly as intended.
        """
        return tuple(f.name for f in self.findings if f.state is StepState.CONFLICT)

    @property
    def every_step_accounted_for(self) -> bool:
        """False while any step is unread, so a report with gaps cannot read as complete."""
        return all(f.state.is_known for f in self.findings)

    @property
    def account_is_usable(self) -> bool:
        """True only when every step was READ and every one is established.

        The conjunction is the point: "no failures were observed" is not "everything was
        observed to be fine", and an account missing the service-linked role is one whose next
        workspace KMS key creation fails on a principal that does not exist.
        """
        return bool(self.findings) and all(
            f.state is StepState.ESTABLISHED for f in self.findings
        )

    @property
    def ready_for_workspace_provisioning(self) -> bool:
        """Every bootstrap step established AND a recorded account to have established them in.

        Separate from `account_is_usable`, which is only about the steps. Observations can say
        every step is present while no recorded attempt says this workspace has an account at
        all — in which case the observations are about some other account, or the attempt was
        never recorded. Neither is a state to build a workspace in, so the two conditions are
        conjoined here rather than letting a clean step list stand in for a real account.
        """
        return self.account_exists and self.account_is_usable

    # ── What a retry would and would not repeat ─────────────────────────────────────────

    @property
    def retry_would_repeat(self) -> tuple[str, ...]:
        """Steps a bootstrap retry would actually act on — the verified-absent ones only."""
        return tuple(f.name for f in self.findings if f.retry_repeats)

    @property
    def retry_would_skip(self) -> tuple[str, ...]:
        """Steps a retry would not act on, for any reason. Named so "the retry did nothing
        about X" is visible before the retry rather than after it."""
        return tuple(f.name for f in self.findings if not f.retry_repeats)

    @property
    def retry_cannot_advance(self) -> tuple[str, ...]:
        """Steps a retry cannot get past as things stand: denied, or never read."""
        return tuple(f.name for f in self.findings if f.blocks_progress)

    @property
    def bootstrap_retry_is_safe(self) -> bool:
        """Whether re-running BOOTSTRAP (never creation) is safe right now.

        Safe requires all three:

        * the creation outcome is settled — an unresolved creation means the account this
          would bootstrap may not be the only one, or may not be the one recorded;
        * an account id is known, so the retry has a target rather than a search;
        * nothing is blocked. A retry over a denial or an unread step is a retry whose
          outcome is already known not to be completion.

        Deliberately never true while `account_may_exist_untracked` is true. A retry presented
        as safe under an unknown outcome is the failure mode this whole module exists for.
        """
        if self.account_may_exist_untracked:
            return False
        if self.creation_disposition is not AttemptDisposition.ALREADY_CREATED:
            return False
        if not self.account_id:
            return False
        return not self.retry_cannot_advance

    @property
    def creation_retry_is_safe(self) -> bool:
        """Always False. Bootstrap recovery never re-runs account CREATION.

        A constant rather than an omission, because the question gets asked. The account
        already exists in this state; calling `CreateAccount` again is the duplicate-account
        bug, and whether creating is permitted at all is `creation.assess_attempt`'s to answer
        from the recorded ledger — not something a bootstrap report may imply.
        """
        return False

    # ── What is retained ────────────────────────────────────────────────────────────────

    @property
    def retained(self) -> tuple[str, ...]:
        """What survives regardless of what recovery does, and is not cleaned up by it.

        Reported explicitly rather than left as an absence: "this was not removed" has to be
        visible in the report, because an account-wide role that a reader assumes was cleaned
        up is one they will look for and not find.
        """
        retained = [
            (
                f"the AWS account itself ({self.account_id or 'id not recorded'}), which "
                f"recovery never closes: closure is an irreversible 90-day suspension and "
                f"must be requested by name through cleanup.closure_request"
            )
        ]
        for finding in self.findings:
            if finding.state is StepState.ESTABLISHED:
                retained.append(
                    f"{finding.name}: account-wide and retained through workspace "
                    f"retirement; recovery neither deletes nor re-creates it"
                )
            elif finding.state is StepState.CONFLICT:
                # Belongs in this list precisely because it is NOT established. Something is
                # occupying the name, recovery does not remove it, and a reader who sees the
                # step reported as incomplete would otherwise reasonably assume nothing is
                # there — then be surprised when their own create fails against it.
                retained.append(
                    f"{finding.name}: NOT established, but something of this name exists and "
                    f"recovery does not remove or re-scope it ({finding.detail})"
                )
        return tuple(retained)

    @property
    def needs_operator(self) -> bool:
        """True when no automated move is both available and safe."""
        return not self.bootstrap_retry_is_safe

    @property
    def summary(self) -> str:
        """One line that does not overstate what is known."""
        if self.account_may_exist_untracked:
            return (
                "UNRESOLVED: an account may exist that nothing is tracking. Neither a retry "
                "nor releasing this workspace is authorized; resolve the recorded attempt "
                "against AWS first"
            )
        if self.conflicting:
            # Ahead of BOTH the unread line and the denial line, and the order was arrived at by
            # a test rather than by taste.
            #
            # Against the denial line: a conflict is the one state a permission grant cannot
            # fix, and an operator who reads BLOCKED first resolves the grant, re-runs, and lands
            # back here having learned nothing about the role actually in the way.
            #
            # Against the unread line: a conflict on a step that blocks provisioning is normally
            # the REASON the later steps are unread — bootstrap stopped there. Leading with
            # "4 step(s) were never checked, read them before acting" describes the consequence
            # and buries the cause, and the reading it invites ("go look at the rest") cannot
            # succeed while the conflict stands. Both counts appear below, so nothing is lost.
            #
            # And ahead of the plain INCOMPLETE line it would otherwise fall through to entirely:
            # a conflicting step is `is_known` and not `DENIED`, so the report used to end on
            # "verified absent and establishable by re-running bootstrap" — advice that cannot
            # work, because a create against an existing name fails, and that failure then reads
            # as a permissions problem.
            unread = (
                f" {len(self.unchecked)} further step(s) were never checked."
                if self.unchecked
                else ""
            )
            return (
                f"CONFLICT: {len(self.conflicting)} step(s) already exist under this plan's "
                f"name but differ from what the plan describes ({', '.join(self.conflicting)}). "
                f"Re-running bootstrap cannot resolve this and must not: reusing them would "
                f"hand a workspace an identity nobody reviewed, and replacing them affects "
                f"whatever uses them today. A person decides, under explicit "
                f"authorization.{unread}"
            )
        if not self.every_step_accounted_for:
            return (
                f"INCOMPLETE AND PARTLY UNREAD: {len(self.unchecked)} step(s) were never "
                f"checked, so the account is not known to be ready and not known to be "
                f"broken. Read them before acting"
            )
        if self.denied:
            return (
                f"BLOCKED: {len(self.denied)} step(s) denied by permissions. Retrying "
                f"changes nothing until the denial is resolved"
            )
        if self.account_is_usable:
            if not self.account_exists:
                # Observations can say every step is established while no recorded attempt
                # says an account exists. That is not completion, it is a report about an
                # account nothing has confirmed — and calling it COMPLETE would let a reader
                # conclude a workspace's account is ready when none was recorded as opened.
                return (
                    "NO RECORDED ACCOUNT: every step was observed established, but no "
                    "recorded creation attempt says this workspace has an account. Either "
                    "the observations are about a different account or the attempt was never "
                    "recorded; resolve which before acting"
                )
            return "COMPLETE: every bootstrap step was read and is established"
        return (
            f"INCOMPLETE: {len(self.retry_would_repeat)} step(s) verified absent and "
            f"establishable by re-running bootstrap; the account is retained meanwhile"
        )


def _same_account_subject(plan: BootstrapPlan, decision: AttemptDecision) -> None:
    """Refuse a report that pairs a plan with an attempt about a DIFFERENT account.

    The report's two halves arrive from different places and were never compared. The step
    findings describe the account the PLAN is about; the account id, and every disposition
    derived from it, come from the DECISION's recorded attempt. Nothing checked that the two
    were about the same thing.

    That gap is what makes the module's own warnings unenforceable. `summary` and
    `ready_for_workspace_provisioning` both worry, in prose, that "the observations are about
    a different account" — and then the only case they can actually detect is the one where no
    attempt exists at all. A report built from workspace A's plan and workspace B's succeeded
    attempt reported `COMPLETE`, `account_exists=True`, `bootstrap_retry_is_safe=True`, and
    B's account id, with A's step observations as the evidence. Every safety property in this
    file evaluates to the reassuring answer, because each one is individually correct about
    the half it can see.

    The consequences run in both directions, which is why this is refused rather than merely
    noted:

    * **A real account reads as ready.** A's workspace is cleared for provisioning on the
      strength of B's account existing. The roles A's build assumes are absent.
    * **A recovery action targets the wrong account.** `bootstrap_retry_is_safe` becomes true
      with `account_id` pointing at B, so the remediation writes IAM roles into an account
      that was never in this plan — account-wide, retained through workspace retirement, and
      in a tenant that did not ask for them.

    ## What is compared, and why these fields

    The same four fields that decide WHICH account this is, mirroring
    `creation.account_identity_key`: the organization, the placement (OU), the workspace and
    the contact address.

    The workspace and the organization are compared directly, because the plan carries both and
    a mismatch in either is the case an operator is most likely to hit and most likely to
    misread — so the refusal names the two values rather than only reporting a digest
    disagreement.

    The OU and the contact address are reached through the identity KEY, which derives from all
    four. That covers the two fields the plan cannot state in the clear (see
    `BootstrapPlan.account_identity_key`) and keeps the address out of the refusal message. The
    key is re-derived from the ATTEMPT's own recorded fields rather than trusting the stored
    one, so a record edited away from its own identity is caught too: a stored key that no
    longer matches what the record says about itself cannot establish which account it is
    about.

    Only the identity fields. Not the cluster inputs, for the reason `_idempotency_key` gives:
    a corrected CIDR is the same account, and refusing on one would block a legitimate recovery
    over a cosmetic edit.

    ## What deliberately passes

    * **A decision with NO attempt.** The genuine first-attempt and never-recorded cases, which
      the report already describes correctly through `account_exists` being false. Refusing it
      would make the module unable to report the state it most needs to — an account nothing
      recorded.
    * **A plan with no `account_identity_key`.** Absent means the comparison was NOT MADE, on
      the same footing as `unchecked_authorization`; the workspace and organization are still
      compared. Treating absent as a mismatch would break the direct-construction plans, and
      treating it as a pass on all four fields would be the "verified and never checked look
      alike" failure `ValidationAuthorization` warns about.
    """
    attempt = decision.attempt
    if attempt is None:
        return

    mismatches = []
    if attempt.workspace_id != plan.workspace_id:
        mismatches.append(
            f"workspace: the plan describes {plan.workspace_id!r} but the recorded attempt "
            f"is for {attempt.workspace_id!r}"
        )
    if attempt.organization_id != plan.organization_id:
        mismatches.append(
            f"organization: the plan describes {plan.organization_id!r} but the recorded "
            f"attempt is for {attempt.organization_id!r}"
        )

    # Re-derived from the attempt's OWN recorded fields rather than read off it, so a record
    # edited away from its own identity fails this too.
    attempt_key = _derive_key(
        attempt.organization_id,
        attempt.organizational_unit_id,
        attempt.workspace_id,
        attempt.account_email,
    )
    if (
        plan.account_identity_key is not None
        and plan.account_identity_key != attempt_key
    ):
        mismatches.append(
            "identity: the plan's account identity and the attempt's disagree once the "
            "organizational unit and contact address are included, so the two are about "
            "different accounts even where the workspace and organization match"
        )
    if attempt.idempotency_key != attempt_key:
        # Checked against the attempt itself, not against the plan: this is an integrity
        # failure inside the record rather than a disagreement between two records. The stored
        # key is what `AttemptLedger.find` looks a prior attempt up under, so a record whose key
        # no longer matches its own fields is one that a lookup for this request will MISS —
        # reporting no prior attempt, and permitting a create while this attempt's account may
        # already exist. It cannot be allowed to stand in as evidence about any account.
        mismatches.append(
            "integrity: the attempt's recorded idempotency key does not re-derive from the "
            "organization, organizational unit, workspace and address the attempt itself "
            "records, so the record has been edited away from the identity it was written "
            "for and cannot establish which account it is about"
        )

    if mismatches:
        raise RecoveryError(
            f"refusing to report on a plan and a creation attempt that are about different "
            f"accounts — {'; '.join(mismatches)}. The step observations describe the plan's "
            f"account while the account id and every disposition come from the attempt, so "
            f"this report would present one account's evidence as proof about another: it "
            f"could clear a workspace for provisioning because a DIFFERENT account exists, "
            f"and it could aim a bootstrap retry at an account that was never in this plan"
        )


def recovery_report(
    plan: BootstrapPlan,
    decision: AttemptDecision,
    observed: dict[str, StepState] | None = None,
    details: dict[str, str] | None = None,
) -> RecoveryReport:
    """Build the recovery/retention report for a possibly half-built account.

    `observed` maps step name to what a read established. Steps absent from it become
    `NOT_CHECKED` rather than being dropped, so the report covers the whole plan and a caller
    cannot shrink the report by supplying less.

    The account id comes from the creation DECISION's recorded attempt, never from a caller
    argument. A caller-supplied id would make this report able to describe an account the
    ledger never recorded ADP opening, which is precisely the id a recovery action would then
    act on.

    The plan and the decision must be about the SAME account; see `_same_account_subject` for
    why a mismatch is refused outright rather than reported as a finding.
    """
    _same_account_subject(plan, decision)

    observed = dict(observed or {})
    details = dict(details or {})

    # Details are checked against the plan on the same footing as observations. A detail keyed
    # to a name the plan does not have would otherwise be silently dropped — losing the
    # operator's own account of what they saw, which is the part of the report a reader cannot
    # reconstruct. A misspelled step name is the likely cause, and it must be said out loud.
    planned = {step.name for step in plan.steps}
    unknown_steps = sorted((set(observed) | set(details)) - planned)
    if unknown_steps:
        raise RecoveryError(
            f"observations name step(s) that are not in this plan: "
            f"{', '.join(unknown_steps)}. An observation about a step the plan does not have "
            f"is about something else, and reporting it would describe a different account "
            f"than the one being recovered"
        )

    findings = tuple(
        StepFinding(
            step=step,
            state=observed.get(step.name, StepState.NOT_CHECKED),
            detail=details.get(step.name, ""),
        )
        for step in plan.steps
    )

    account_id = decision.attempt.account_id if decision.attempt else None
    return RecoveryReport(
        workspace_id=plan.workspace_id,
        organization_id=plan.organization_id,
        account_id=account_id,
        creation_disposition=decision.disposition,
        findings=findings,
    )


def blocking_prerequisites(report: RecoveryReport) -> tuple[str, ...]:
    """Steps whose incompleteness blocks workspace provisioning outright.

    Separated from the general incomplete list because the consequences differ in kind. A
    missing baseline control is a gap to close; a missing Auto Scaling service-linked role
    means the next workspace KMS key creation FAILS, with an error about a policy principal
    that does not name the real cause. Whoever reads this report needs to know which of the
    two they are looking at before they schedule the workspace build.

    Keyed on the step's own `blocks_workspace_provisioning` rather than on whether `precedes`
    is non-empty: every step precedes something, so that test would call the whole plan
    blocking and tell the reader nothing.
    """
    return tuple(
        finding.name
        for finding in report.findings
        if finding.state is not StepState.ESTABLISHED
        and finding.step.blocks_workspace_provisioning
    )
