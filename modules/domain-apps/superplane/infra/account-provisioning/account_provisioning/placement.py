"""Putting a created account into the approved organizational unit — Issue #5531 (w6-08).

`CreateAccount` has no placement parameter. Whatever `organizational_unit_id` a request
carries, AWS opens the account at the **organization root** and leaves it there until
something explicitly moves it. The root is the least restricted position in the tree: it is
outside every service control policy attached to the OU the approval was granted against.

So the earlier revision of this package had a gap with a particularly bad shape. The request
named an OU, `creation_runner._confirm_placement` verified that the OU existed, the
configuration read as "placed in the approved unit" — and nothing ever placed it there. The
validation was real, the claim was false, and the account was born outside all of the controls.
Confirming the destination exists and putting the account in it are two different acts, and
only the first was implemented.

This module is the second one. It is a separate file rather than more of `creation_runner`
because `bootstrap_runner` has to depend on it too — bootstrap writes account-wide IAM roles,
and doing that in an account sitting at the root means writing privileged identities into an
ungoverned account. Both runners importing this avoids a cycle between them.

## Placement is established by a read, never by a move returning

`move_account` returning is a statement that the request was accepted. `list_parents` saying
the account is under the approved OU is a statement about where the account IS. Those come
apart exactly where it matters — a lost answer, a superseded worker, a concurrent operator —
so `verify_placement` re-reads after every dispatch and `AccountPlacement.verified` is true
only on the strength of that read. This is the same rule `bootstrap_runner` follows for a role
it creates, and the same rule `IamClient.get_role` exists to serve.

## Why the move is its own durable fence

A crash between creating the account and moving it leaves a real, billable account at the
root. That is a recoverable state only if the move can be resumed without either repeating an
effect or losing the record of having tried, which is what a separate idempotency key buys:
`PLACEMENT_STEP` is not the creation step, so consuming the creation fence does not consume
this one, and a restart re-derives the same placement key rather than inventing a fresh one.

## What this module will not do

It will not move an account out of an organizational unit that something else put it in. An
account found under an unexpected OU is a `CONFLICT` and stops here: another team's SCPs may
be the reason it is there, and "move it where this request wanted" is a decision a person
takes with that context, not a remediation a runner applies. The root is the one source this
module moves from on its own, because the root is where AWS puts every new account and is
therefore the one parent that carries no intent.
"""

from __future__ import annotations

from dataclasses import dataclass

from account_factory.modes import AccountFactoryRequest, OwnershipMode
from account_factory.recovery import StepState

from .execution import (
    CallOutcome,
    DurableExecutor,
    OutcomeVocabulary,
    ProviderCallRecord,
    as_outcome,
    outcome_vocabulary,
)
from .ports import (
    AccountCredentials,
    CredentialSource,
    OrganizationsClient,
    ProviderDenied,
    ProviderUnavailable,
)

__all__ = [
    "PLACEMENT_STEP",
    "ROOT_PARENT_TYPE",
    "AccountPlacement",
    "PlacementRefused",
    "placement_hook",
    "placement_key",
    "read_placement",
    "verify_placement",
]


PLACEMENT_STEP = "organizations-move-account"
"""The step name that, with the operation id, forms the placement fence.

A constant, and distinct from `creation_runner.CREATION_STEP`, for two reasons that pull in
the same direction. It must be IDENTICAL across a restart, so nothing per-attempt may enter it
— `creation_key` says why at greater length. And it must DIFFER from the creation step, or the
move could never be dispatched at all: the store refuses a key that already has a row, so
sharing the creation key would make every successfully created account permanently unmovable,
which is precisely the half-finished state this module exists to close.
"""

_PROVIDER = "aws-organizations"
_OPERATION_KIND = "move-account"

ROOT_PARENT_TYPE = "ROOT"
"""The `Type` `list_parents` reports for the organization root.

Named because the root is not just another parent here: it is where `CreateAccount` leaves
every new account, it is the only parent this module will move an account away from without an
operator deciding, and it is the position that means "no OU controls apply".
"""


class PlacementRefused(Exception):
    """Placement must not proceed, and no Organizations call was made.

    Raised before any provider effect. A caller seeing this knows the account was not moved by
    this call — which is not the same as knowing where it is; that is `read_placement`'s answer.
    """


@dataclass(frozen=True)
class AccountPlacement:
    """Where one account actually is, as an authoritative read established it.

    Handed to `bootstrap_runner.bootstrap_account`, which refuses to write account-wide roles
    without one. It is a value object rather than a boolean on purpose: a caller can pass
    `placement_verified=True` without having checked anything, whereas the only way to obtain
    one of these with `verified` true is for `read_placement` to have asked AWS and been told
    the account is under the approved unit.

    `account_id` and `organizational_unit_id` are carried so the consumer can check the
    placement is about the account and unit it is acting on. Without them a verified placement
    for one account would satisfy a bootstrap of another — the same cross-subject confusion
    `recovery._same_account_subject` refuses, arriving by a different route.
    """

    account_id: str
    organizational_unit_id: str
    state: StepState
    detail: str
    actual_parent_id: str | None = None
    """The parent AWS reported, when exactly one was reported. `None` when the read did not
    establish a parent at all, which is not the same as the account having none."""

    durable_key: str | None = None
    """The fence a move was dispatched under, when one was. `None` when no dispatch happened —
    including the common case where the account was already correctly placed."""

    moved: bool = False
    """True when this call dispatched a move. Distinct from `verified`: a dispatched move whose
    answer was lost is `moved` and not `verified`, and that pair is the state an operator needs
    to see rather than either half alone."""

    @property
    def verified(self) -> bool:
        """The account IS under the approved unit, and a read said so.

        The single question every consumer asks, and the only property that may gate an effect.
        """
        return self.state is StepState.ESTABLISHED

    @property
    def blocks_progress(self) -> bool:
        """True when nothing downstream may proceed and a retry will not fix it by itself.

        `CONFLICT` needs a decision, `DENIED` needs a permission, `NOT_CHECKED` needs a read
        that worked. `ABSENT` is excluded deliberately: it is the ordinary post-creation state
        and the one a retry does resolve.
        """
        return self.state in {
            StepState.CONFLICT,
            StepState.DENIED,
            StepState.NOT_CHECKED,
        }


def placement_key(executor: DurableExecutor) -> str:
    """The idempotency key for this operation's account move.

    Derived from the operation id and a constant step, exactly like `creation_key`'s
    generation 0 and for the same reasons: every restart of the same operation derives the
    same key, so the store's duplicate refusal engages rather than a second move being
    dispatched against an account another worker may already have moved.

    No generation. A retryable-failure generation exists on the creation path because a failed
    create may be legitimately re-attempted for a transient reason; a move that failed for a
    transient reason is re-attempted by re-reading where the account is and moving from
    whatever parent it is actually under, which needs no new fence because the re-read is what
    establishes whether anything is still to be done.
    """
    return f"{executor.operation_id}:{PLACEMENT_STEP}"


def _require_new_account_mode(request: AccountFactoryRequest) -> None:
    """Refuse to move an account for any mode but the named new-account one.

    Moving an account between organizational units is a governance change to an account that
    may already be running things. In the two existing-account modes ADP did not open the
    account and has no approval to re-place it, and `modes.validate` already requires
    `organizational_unit_id` to be ABSENT in both — so a placement here could only act on an
    OU nobody authorized.
    """
    if request.mode is not OwnershipMode.NEW_ACCOUNT_MANAGED:
        raise PlacementRefused(
            f"placing an account into an organizational unit requires mode "
            f"{OwnershipMode.NEW_ACCOUNT_MANAGED.value!r}, not {request.mode.value!r}. ADP "
            f"did not open an adopted account and has no approval to re-place it"
        )


def _approved_unit(request: AccountFactoryRequest) -> str:
    """The OU this request was approved for, as a non-empty id.

    An unstated unit is refused rather than defaulted, on the same reasoning
    `_confirm_placement` gives: the default is the root, and the root is the position whose
    whole meaning is that none of the approved controls apply.
    """
    unit = (request.organizational_unit_id or "").strip()
    if not unit:
        raise PlacementRefused(
            "new-account-managed requires an organizational unit to place the account in; "
            "an unstated placement leaves the account at the organization root, which is "
            "outside every control the approved unit imposes"
        )
    return unit


def read_placement(
    organizations: OrganizationsClient,
    *,
    account_id: str,
    organizational_unit_id: str,
) -> AccountPlacement:
    """Ask AWS where the account actually is, and classify the answer. Reads only.

    The five answers and why each is its own state:

    * the approved unit → `ESTABLISHED`. The only state that permits anything downstream.
    * the organization root → `ABSENT`. Where `CreateAccount` leaves every new account, so
      this is the expected state immediately after creation and the one a move resolves.
    * some other organizational unit → `CONFLICT`, and it stops here. Something placed this
      account deliberately; moving it out could remove it from controls another team relies
      on, and that trade is a decision rather than a remediation. Reported with both unit ids
      so whoever decides can see them.
    * the read was refused → `DENIED`. A permission problem, not a placement problem.
    * the read produced no usable answer, or more than one parent → `NOT_CHECKED`. Nothing was
      established. An account has exactly one parent in Organizations, so a response carrying
      several is one this code does not understand well enough to act on, and guessing which
      entry is authoritative is how an account gets moved out of the wrong place.

    `NOT_CHECKED` rather than `ABSENT` for an unobtained answer is the distinction the whole
    wave turns on: "I could not read the parent" must never settle as "the account is at the
    root", because that reading licenses a move.
    """
    try:
        response = organizations.list_parents(ChildId=account_id)
    except ProviderDenied as exc:
        return AccountPlacement(
            account_id=account_id,
            organizational_unit_id=organizational_unit_id,
            state=StepState.DENIED,
            detail=(
                f"reading the parent of account {account_id} was refused: {exc}. Placement is "
                f"NOT established, and this is a permission to grant rather than a move to "
                f"make: without the read there is no way to know whether a move is needed"
            ),
        )
    except ProviderUnavailable as exc:
        return AccountPlacement(
            account_id=account_id,
            organizational_unit_id=organizational_unit_id,
            state=StepState.NOT_CHECKED,
            detail=(
                f"the parent of account {account_id} could not be read: {exc}. Nothing is "
                f"established about where the account is; do not move it on the assumption "
                f"that it is still at the root"
            ),
        )

    parents = list((response or {}).get("Parents") or [])
    if len(parents) != 1:
        return AccountPlacement(
            account_id=account_id,
            organizational_unit_id=organizational_unit_id,
            state=StepState.NOT_CHECKED,
            detail=(
                f"Organizations reported {len(parents)} parents for account {account_id}, and "
                f"an account has exactly one. Placement is not established, because choosing "
                f"which of several answers to believe is how an account is moved out of the "
                f"parent it was meant to be in"
            ),
        )

    parent = parents[0] or {}
    parent_id = (parent.get("Id") or "").strip()
    parent_type = (parent.get("Type") or "").strip()
    if not parent_id:
        return AccountPlacement(
            account_id=account_id,
            organizational_unit_id=organizational_unit_id,
            state=StepState.NOT_CHECKED,
            detail=(
                f"Organizations reported a parent for account {account_id} with no id, so "
                f"where the account sits was not established and no source parent exists to "
                f"move it from"
            ),
        )

    if parent_id == organizational_unit_id:
        return AccountPlacement(
            account_id=account_id,
            organizational_unit_id=organizational_unit_id,
            state=StepState.ESTABLISHED,
            detail=(
                f"account {account_id} is under the approved organizational unit "
                f"{organizational_unit_id}, as Organizations reports it, so the controls "
                f"attached to that unit apply to it"
            ),
            actual_parent_id=parent_id,
        )

    if parent_type == ROOT_PARENT_TYPE:
        return AccountPlacement(
            account_id=account_id,
            organizational_unit_id=organizational_unit_id,
            state=StepState.ABSENT,
            detail=(
                f"account {account_id} is at the organization root {parent_id}, which is "
                f"where CreateAccount leaves every new account and is outside the controls "
                f"attached to {organizational_unit_id}. It has not been placed yet"
            ),
            actual_parent_id=parent_id,
        )

    return AccountPlacement(
        account_id=account_id,
        organizational_unit_id=organizational_unit_id,
        state=StepState.CONFLICT,
        detail=(
            f"account {account_id} is already under {parent_id} "
            f"({parent_type or 'an unreported type'}), not the approved organizational unit "
            f"{organizational_unit_id}. Something placed it there deliberately, and moving it "
            f"out may remove it from controls that placement exists to impose — so this is "
            f"reported for a decision rather than corrected"
        ),
        actual_parent_id=parent_id,
    )


def placement_hook(
    credentials: CredentialSource,
    request: AccountFactoryRequest,
    *,
    account_id: str,
    outcomes: OutcomeVocabulary | None = None,
):
    """Build the provider hook the durable executor invokes to move the account.

    Composed like `creation_runner.creation_hook` and `bootstrap_runner.bootstrap_hook`: the
    executor takes its hook at construction, so the composer wires this once.

    The hook reads `list_parents` again before moving, and that second read is not redundant
    with the caller's. `move_account` requires the source parent, and the only honest source is
    the one the account is under at the moment of the move — not the one it was under when the
    caller decided a move was needed. Between those two moments the store's fence may have
    admitted a different worker, or an operator may have moved the account by hand. So the hook
    derives the source itself and refuses anything it does not expect:

    * already at the destination → `SUCCEEDED` without a move. Nothing to do is not a failure,
      and AWS errors on a move whose source and destination match.
    * under some other organizational unit → `FAILED` without a move, for the reason
      `read_placement` gives: moving an account out of a unit something else chose is a
      decision, and a hook is not where decisions are taken.
    * at the root → the move, from that root.

    `ProviderDenied` becomes `FAILED`; `ProviderUnavailable` becomes `UNKNOWN`, never `FAILED`,
    because a move whose answer was not obtained may well have landed and the account's
    position is then settled by re-reading rather than by moving again.

    `outcomes` is the `CallOutcome` class the executor `isinstance`-checks this hook's answer
    against — see `execution.outcome_vocabulary` for why answering in this package's own copy
    makes the executor discard the answer and silently record `UNKNOWN`.
    """
    _require_new_account_mode(request)
    unit = _approved_unit(request)
    outcomes = outcome_vocabulary(outcomes)

    async def hook(call: ProviderCallRecord):
        management: AccountCredentials = await credentials.management(operation_id=getattr(call, "operation_id", ""))
        organizations = management.organizations

        current = read_placement(
            organizations,
            account_id=account_id,
            organizational_unit_id=unit,
        )
        if current.state is StepState.ESTABLISHED:
            return (
                outcomes.SUCCEEDED,
                f"no move was needed: {current.detail}",
                current.actual_parent_id,
            )
        if current.state is StepState.CONFLICT:
            return outcomes.FAILED, current.detail, current.actual_parent_id
        if current.state is StepState.DENIED:
            return outcomes.FAILED, current.detail, None
        if current.state is StepState.NOT_CHECKED:
            # No source parent was established, so there is nothing to move FROM. Reported as
            # unobtained rather than failed: the account's position is unknown, and a `FAILED`
            # here would read as "the move cannot succeed", which is not what was learned.
            return outcomes.UNKNOWN, current.detail, None

        source = current.actual_parent_id or ""
        try:
            organizations.move_account(
                AccountId=account_id,
                SourceParentId=source,
                DestinationParentId=unit,
            )
        except ProviderDenied as exc:
            return (
                outcomes.FAILED,
                f"moving account {account_id} from {source} to {unit} was refused: {exc}",
                source,
            )
        except ProviderUnavailable as exc:
            return (
                outcomes.UNKNOWN,
                (
                    f"moving account {account_id} from {source} to {unit} did not return an "
                    f"answer: {exc}. The account may now be in {unit}; its position must be "
                    f"read rather than the move repeated"
                ),
                source,
            )
        return (
            outcomes.SUCCEEDED,
            f"account {account_id} was moved from {source} into {unit}",
            unit,
        )

    return hook


async def verify_placement(
    executor: DurableExecutor,
    credentials: CredentialSource,
    request: AccountFactoryRequest,
    *,
    account_id: str,
    refusal_types: tuple[type[BaseException], ...] = (),
) -> AccountPlacement:
    """Establish that the account is in the approved unit, moving it once if it is not.

    The sequence, and why it is this sequence:

    1. Refuse any mode but the named new-account one, and refuse an unstated unit. Before a
       credential is obtained, so an unauthorized placement reaches no provider at all.
    2. **Read** where the account is. An account already correctly placed consumes no durable
       key and dispatches nothing — the ordinary second-pass case, which must be a no-op rather
       than a move.
    3. Stop on anything that is not "at the root": a foreign unit, a denial and an unobtained
       answer each need something other than a move, and each says which.
    4. Dispatch the move under the placement fence, through the executor, so intent is
       committed before the call and a second worker is refused rather than queued.
    5. **Read again, always.** The returned placement's `verified` comes from this read and
       never from the dispatch's own outcome. A move that reported success but left the account
       elsewhere, and a move whose answer was lost but landed, are both settled correctly here
       and only here.

    `refusal_types` names the executor's dispatch-refusal exception so this module can
    recognise "that key already has a row" without importing the package that defines it — see
    `execution.py` for why the dependency is structural. The refusal is answered with the same
    re-read as everything else, because another worker having dispatched the move is exactly
    the case where AWS, not this process, knows the outcome.

    Raises `PlacementRefused` when nothing was attempted. Never raises for a placement that
    merely did not succeed: that is the returned value's job to describe.
    """
    _require_new_account_mode(request)
    unit = _approved_unit(request)

    management = await credentials.management(operation_id=executor.operation_id)
    before = read_placement(
        management.organizations,
        account_id=account_id,
        organizational_unit_id=unit,
    )
    if before.state is not StepState.ABSENT:
        # Established, conflicting, denied or unread. None of the four is a move, and three of
        # them are things a move would make worse.
        return before

    key = placement_key(executor)
    dispatched_detail = ""
    try:
        call, _ = await executor.execute_provider(
            idempotency_key=key,
            provider=_PROVIDER,
            operation_kind=_OPERATION_KIND,
            target=account_id,
        )
    except refusal_types as exc:
        # The store already holds a row for this key, so an earlier or concurrent pass got this
        # far. Its move may have landed; the answer comes from the account's actual parent,
        # never from repeating the call.
        dispatched_detail = f"the durable store refused a second move dispatch ({exc})"
    else:
        # Normalized, never compared with `is`: the row carries the harness enum member, and an
        # identity comparison here reads a move that DID land as not having landed. See
        # `execution.as_outcome`.
        outcome = as_outcome(getattr(call, "outcome", None))
        if outcome is CallOutcome.SUCCEEDED:
            dispatched_detail = "the move was dispatched and reported success"
        elif outcome is CallOutcome.FAILED:
            dispatched_detail = "the move was dispatched and AWS refused it"
        else:
            dispatched_detail = "the move was dispatched and did not return a usable answer"

    after = read_placement(
        management.organizations,
        account_id=account_id,
        organizational_unit_id=unit,
    )
    return AccountPlacement(
        account_id=after.account_id,
        organizational_unit_id=after.organizational_unit_id,
        state=after.state,
        # The dispatch's own report is kept beside the read's, never in place of it. An operator
        # reading "reported success" next to "the account is at the root" is being told the one
        # thing that matters about this class of failure, and a message carrying only the
        # dispatch's answer would assert the opposite.
        detail=f"{dispatched_detail}; the authoritative read then found: {after.detail}",
        actual_parent_id=after.actual_parent_id,
        durable_key=key,
        moved=True,
    )
