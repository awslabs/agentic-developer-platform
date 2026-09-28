"""A created account is not a placed account — Issue #5531 (w6-08).

## The defect these tests exist to prevent

`CreateAccount` has no organizational-unit parameter. AWS opens the account at the organization
**root** and leaves it there. The root is the least restricted position in the tree: it is
outside every service control policy attached to the OU the approval was granted against.

The reviewed revision carried an `organizational_unit_id` on the request, validated that the OU
existed before spending money, and then never put the account in it. `ports.OrganizationsClient`
had no `move_account` at all, so nothing could have. That gap has the worst shape a gap can
have: the configuration claimed placement, a real validation confirmed the destination, and the
account was ungoverned. Every later step compounded it — bootstrap writes three account-wide
cross-account roles, so the privileged identities went into an account none of the approved
controls reached.

## What the repair has to establish, and why each needs its own test

1. **The move happens**, under its own durable fence — not the creation fence, which is already
   consumed by the time an account exists to move.
2. **Placement is established by reading the account's ACTUAL parent**, never by the move
   returning. A move that reports success and leaves the account elsewhere is the case a
   dispatch-trusting implementation gets wrong, and it is the case that matters: it produces a
   confident false claim rather than a visible failure.
3. **An account under an unexpected OU is a conflict, not a correction.** Something placed it
   there deliberately; moving it out could remove it from controls another team relies on.
4. **Nothing downstream proceeds without verified placement.** `bootstrap_account` refuses,
   because an unchecked placement and a verified one must not look alike.

## Why these drive the real hook rather than a scripted outcome

`HookExecutor` actually invokes `placement_hook`, so the assertions are about which Organizations
call this code makes, with which arguments, and what it concludes from each answer — including
the answer it never gets. A scripted outcome would make every test here assert its own premise.
The read sequences are written out per test because the hook reads `list_parents` again before
moving (it needs a source parent it can defend), so a moving pass performs three reads: the
caller's, the hook's, and the authoritative re-read.

## What these tests do NOT establish

That AWS accepts a move, that any SCP applies, or that any account was created or placed. No
AWS is contacted anywhere in this file and no live account vending or organization change is
exercised or authorized by it. Applying any of this to a real organization is a Wave 6
operations-gate activity with its own named authorization.
"""

from __future__ import annotations

import pytest
from harness_jobs.execution import CallOutcome as AuthoritativeCallOutcome

from account_factory.bootstrap import bootstrap_plan
from account_factory.modes import OwnershipMode
from account_factory.recovery import StepState
from account_provisioning.bootstrap_runner import BootstrapRefused, bootstrap_account
from account_provisioning.placement import (
    PLACEMENT_STEP,
    AccountPlacement,
    PlacementRefused,
    placement_hook,
    placement_key,
    read_placement,
    verify_placement,
)
from account_provisioning.ports import ProviderDenied, ProviderUnavailable

from .conftest import (
    FIXTURE_CREATED_ACCOUNT,
    FIXTURE_ORGANIZATIONAL_UNIT,
    FIXTURE_PERMISSION_ARNS,
    FIXTURE_ROOT_ID,
    FIXTURE_TRUST_POLICIES,
    HookExecutor,
    RecordingCredentials,
    RecordingExecutor,
    RecordingIam,
    RecordingOrganizations,
    bootstrapped_trust_documents,
    fully_bootstrapped_roles,
    matching_authorization,
    new_account_request,
    request_for_mode,
)

# The OU some other team placed the account in. Well-formed, and deliberately not the approved
# one: the interesting case is not a malformed id but a legitimate placement that disagrees.
FOREIGN_UNIT = "ou-test-elsewhere9"

AT_ROOT = {"Parents": [{"Id": FIXTURE_ROOT_ID, "Type": "ROOT"}]}
IN_APPROVED_UNIT = {"Parents": [{"Id": FIXTURE_ORGANIZATIONAL_UNIT, "Type": "ORGANIZATIONAL_UNIT"}]}
IN_FOREIGN_UNIT = {"Parents": [{"Id": FOREIGN_UNIT, "Type": "ORGANIZATIONAL_UNIT"}]}

PLACEMENT_KEY = f"op-fixture:{PLACEMENT_STEP}"


async def _verify(organizations: RecordingOrganizations, *, already_recorded: tuple[str, ...] = ()):
    """Drive the real `verify_placement` against `organizations`. Returns `(placement, executor)`.

    One `RecordingCredentials` shared between the hook and the caller, as the composer wires one
    source in production — so an assertion about which operation a credential was obtained for
    covers both sides.
    """
    request = new_account_request()
    credentials = RecordingCredentials(organizations=organizations)
    hook = placement_hook(
        credentials,
        request,
        account_id=FIXTURE_CREATED_ACCOUNT,
        # The REAL harness enum, as the composer passes in production. The executor
        # `isinstance`-checks the hook's answer against this class and silently substitutes
        # `UNKNOWN` for anything else, so a test letting the hook answer in this package's own
        # copy would read `UNKNOWN` on every path and call it a pass.
        outcomes=AuthoritativeCallOutcome,
    )
    executor = HookExecutor(hook=hook, already_recorded=already_recorded)
    placement = await verify_placement(
        executor,
        credentials,
        request,
        account_id=FIXTURE_CREATED_ACCOUNT,
        # The store's refusal of an already-recorded key, supplied by the composer in production
        # for the reason `execution.py` gives — this package does not import `harness_jobs`.
        refusal_types=(Exception,),
    )
    return placement, executor


class TestWhereTheAccountActuallyIs:
    """`read_placement` classifies Organizations' answer. Reads only, never moves."""

    def test_an_account_at_the_root_is_not_placed(self) -> None:
        """The state every newly created account is in, and the finding F1 is about.

        `ABSENT` rather than `ESTABLISHED` is the whole point: the account is real, billable,
        and outside the controls the approval was granted against.
        """
        organizations = RecordingOrganizations(parents=AT_ROOT)

        placement = read_placement(
            organizations,
            account_id=FIXTURE_CREATED_ACCOUNT,
            organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
        )

        assert placement.state is StepState.ABSENT
        assert not placement.verified
        assert placement.actual_parent_id == FIXTURE_ROOT_ID
        # The detail has to say why the root is not merely a different location. An operator
        # reading "at r-test" without that has no reason to treat it as urgent.
        assert "outside the controls" in placement.detail
        assert organizations.move_calls == [], "a read established placement by moving the account"

    def test_an_account_in_the_approved_unit_is_placed(self) -> None:
        organizations = RecordingOrganizations(parents=IN_APPROVED_UNIT)

        placement = read_placement(
            organizations,
            account_id=FIXTURE_CREATED_ACCOUNT,
            organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
        )

        assert placement.state is StepState.ESTABLISHED
        assert placement.verified
        assert placement.actual_parent_id == FIXTURE_ORGANIZATIONAL_UNIT

    def test_an_account_in_another_unit_is_a_conflict_not_a_correction(self) -> None:
        """Somebody placed this account somewhere on purpose.

        Not `ABSENT`, which would move it, and not `ESTABLISHED`, which would use it. The unit
        it is in may carry controls another team depends on, and taking it out of those is a
        decision with context this code does not have.
        """
        organizations = RecordingOrganizations(parents=IN_FOREIGN_UNIT)

        placement = read_placement(
            organizations,
            account_id=FIXTURE_CREATED_ACCOUNT,
            organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
        )

        assert placement.state is StepState.CONFLICT
        assert not placement.verified
        assert placement.blocks_progress
        # BOTH units named. Whoever decides needs to see what it is in and what was approved.
        assert FOREIGN_UNIT in placement.detail
        assert FIXTURE_ORGANIZATIONAL_UNIT in placement.detail

    def test_a_denied_parent_read_is_a_permission_not_a_placement(self) -> None:
        """`DENIED`, so the remediation is a grant rather than a move.

        Reporting this as `ABSENT` would send the next pass to move an account whose position
        nobody could read — which is how an account already correctly placed gets moved.
        """
        organizations = RecordingOrganizations(parents=ProviderDenied("not authorized to perform organizations:ListParents"))

        placement = read_placement(
            organizations,
            account_id=FIXTURE_CREATED_ACCOUNT,
            organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
        )

        assert placement.state is StepState.DENIED
        assert not placement.verified
        assert placement.blocks_progress

    def test_an_unobtained_parent_read_establishes_nothing(self) -> None:
        """The distinction the whole wave turns on, applied to placement.

        "I could not read the parent" must never settle as "the account is at the root",
        because that reading licenses a move.
        """
        organizations = RecordingOrganizations(parents=ProviderUnavailable("connection reset"))

        placement = read_placement(
            organizations,
            account_id=FIXTURE_CREATED_ACCOUNT,
            organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
        )

        assert placement.state is StepState.NOT_CHECKED
        assert not placement.verified
        assert placement.actual_parent_id is None

    @pytest.mark.parametrize(
        ("response", "why"),
        [
            ({"Parents": []}, "no parent at all"),
            (
                {
                    "Parents": [
                        {"Id": FIXTURE_ROOT_ID, "Type": "ROOT"},
                        {"Id": FOREIGN_UNIT, "Type": "ORGANIZATIONAL_UNIT"},
                    ]
                },
                "two parents",
            ),
            ({"Parents": [{"Type": "ROOT"}]}, "a parent with no id"),
            ({}, "no Parents key"),
        ],
    )
    def test_an_answer_this_code_cannot_read_is_not_a_placement(self, response: dict, why: str) -> None:
        """An account has exactly one parent. Anything else is unread, not unplaced.

        Each of these could plausibly be coerced into "probably at the root", and each coercion
        licenses a move against an account whose position was never established. `NOT_CHECKED`
        keeps every one of them out of the branch that acts.
        """
        organizations = RecordingOrganizations(parents=response)

        placement = read_placement(
            organizations,
            account_id=FIXTURE_CREATED_ACCOUNT,
            organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
        )

        assert placement.state is StepState.NOT_CHECKED, f"{why} was read as a placement"
        assert not placement.verified
        assert placement.actual_parent_id is None

    def test_the_read_asks_about_the_account_it_was_given(self) -> None:
        """Cheap, and it catches the mistake that makes every other test here vacuous.

        A read that asked about the wrong child would classify some other account's parent and
        report it under this account's id.
        """
        organizations = RecordingOrganizations(parents=AT_ROOT)

        read_placement(
            organizations,
            account_id=FIXTURE_CREATED_ACCOUNT,
            organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
        )

        assert organizations.calls == [("list_parents", {"ChildId": FIXTURE_CREATED_ACCOUNT})]


class TestTheAccountIsActuallyMoved:
    """`verify_placement` performs the move F1 found missing, once, under its own fence."""

    @pytest.mark.asyncio
    async def test_an_account_at_the_root_is_moved_into_the_approved_unit(self) -> None:
        """The repair, in one test: the move is dispatched, from the root, to the approved OU.

        Three reads: the caller sees the root and dispatches, the hook sees the root and moves,
        and the re-read sees the approved unit. The returned placement is verified on the
        strength of that last read rather than on the move's own report.
        """
        organizations = RecordingOrganizations(parents=[AT_ROOT, AT_ROOT, IN_APPROVED_UNIT])

        placement, _ = await _verify(organizations)

        assert organizations.move_calls == [
            {
                "AccountId": FIXTURE_CREATED_ACCOUNT,
                "SourceParentId": FIXTURE_ROOT_ID,
                "DestinationParentId": FIXTURE_ORGANIZATIONAL_UNIT,
            }
        ]
        assert placement.verified
        assert placement.moved
        assert placement.actual_parent_id == FIXTURE_ORGANIZATIONAL_UNIT

    @pytest.mark.asyncio
    async def test_the_move_is_fenced_under_its_own_key_not_the_creation_one(self) -> None:
        """A separate fence, and this is not a stylistic separation.

        The creation key is already consumed by the time an account exists to move: the store
        refuses a key that has a row, so sharing it would make every successfully created
        account permanently unmovable — the exact half-finished state this module closes.
        """
        organizations = RecordingOrganizations(parents=[AT_ROOT, AT_ROOT, IN_APPROVED_UNIT])

        placement, executor = await _verify(organizations)

        assert placement.durable_key == PLACEMENT_KEY
        assert [dispatch["idempotency_key"] for dispatch in executor.dispatches] == [PLACEMENT_KEY]
        assert PLACEMENT_STEP != "organizations-create-account", "the move must not share the creation step name"

    def test_the_placement_key_is_stable_across_restarts(self) -> None:
        """Nothing per-attempt may enter the key, or a retry is a second move.

        Derived twice from two separately constructed executors for the same operation, because
        the property that matters is that a RESTARTED process computes the same key — not that
        one object returns the same string twice.
        """
        first = placement_key(RecordingExecutor(operation_id="op-restart"))
        second = placement_key(RecordingExecutor(operation_id="op-restart"))

        assert first == second
        assert placement_key(RecordingExecutor(operation_id="op-other")) != first

    @pytest.mark.asyncio
    async def test_an_already_placed_account_is_not_moved_again(self) -> None:
        """The ordinary second-pass case. A no-op, and it must consume no fence.

        AWS errors on a move whose source and destination match, so an implementation that
        moved unconditionally would fail on every re-run of a correct account — and an operator
        would read that failure as placement being broken.
        """
        organizations = RecordingOrganizations(parents=IN_APPROVED_UNIT)

        placement, executor = await _verify(organizations)

        assert placement.verified
        assert not placement.moved
        assert organizations.move_calls == []
        assert executor.dispatches == [], "an already-placed account consumed a durable key"
        assert placement.durable_key is None

    @pytest.mark.asyncio
    async def test_an_account_in_a_foreign_unit_is_never_moved_out_of_it(self) -> None:
        """The conflict stops before the dispatch, not after it.

        Reported rather than corrected, and reported without having touched anything: whatever
        put the account under that unit may be relying on its controls.
        """
        organizations = RecordingOrganizations(parents=IN_FOREIGN_UNIT)

        placement, executor = await _verify(organizations)

        assert placement.state is StepState.CONFLICT
        assert not placement.verified
        assert organizations.move_calls == []
        assert executor.dispatches == [], "a conflicting placement still dispatched a move"

    @pytest.mark.asyncio
    async def test_a_denied_read_does_not_license_a_move(self) -> None:
        organizations = RecordingOrganizations(parents=ProviderDenied("not authorized to perform organizations:ListParents"))

        placement, executor = await _verify(organizations)

        assert placement.state is StepState.DENIED
        assert organizations.move_calls == []
        assert executor.dispatches == []

    @pytest.mark.asyncio
    async def test_an_unobtained_read_does_not_license_a_move(self) -> None:
        """The account may already be correctly placed. Moving it on a failed read is how a
        correct account is moved out of the unit it belonged in."""
        organizations = RecordingOrganizations(parents=ProviderUnavailable("timed out"))

        placement, executor = await _verify(organizations)

        assert placement.state is StepState.NOT_CHECKED
        assert organizations.move_calls == []
        assert executor.dispatches == []


class TestPlacementIsVerifiedByReadingNotByDispatching:
    """The property F1's recommended fix names: assert the account's ACTUAL parent."""

    @pytest.mark.asyncio
    async def test_a_move_that_reports_success_but_did_not_land_is_not_verified(self) -> None:
        """The case a dispatch-trusting implementation gets wrong, and the reason for the re-read.

        The hook runs for real and `move_account` returns, so the durable row settles
        `SUCCEEDED`. The account is still at the root. An implementation reading the dispatch's
        outcome would report placement established — a confident false claim, which is worse
        than a visible failure because nothing downstream would question it.
        """
        organizations = RecordingOrganizations(parents=[AT_ROOT, AT_ROOT, AT_ROOT])

        placement, _ = await _verify(organizations)

        assert organizations.move_calls, "fixture precondition: the move must have been dispatched"
        assert not placement.verified
        assert placement.state is StepState.ABSENT
        assert placement.actual_parent_id == FIXTURE_ROOT_ID
        # BOTH halves in the detail. "Reported success" alone is the false claim; "still at the
        # root" alone loses the fact that something claimed otherwise, which is what an operator
        # needs in order to distrust the dispatch rather than the read.
        assert "reported success" in placement.detail
        assert "authoritative read" in placement.detail

    @pytest.mark.asyncio
    async def test_a_move_whose_answer_was_lost_but_landed_is_verified(self) -> None:
        """The mirror case, and the reason the re-read is unconditional rather than on failure.

        The move call never answers, so the hook returns `UNKNOWN` and the row never settles as
        success. The account is nonetheless in the approved unit. Reporting that as unplaced
        would send the next pass to move an account already where it belongs.
        """
        organizations = RecordingOrganizations(
            parents=[AT_ROOT, AT_ROOT, IN_APPROVED_UNIT],
            move_result=ProviderUnavailable("no answer from Organizations"),
        )

        placement, _ = await _verify(organizations)

        assert placement.verified
        assert placement.state is StepState.ESTABLISHED
        assert "did not return a usable answer" in placement.detail

    @pytest.mark.asyncio
    async def test_a_refused_second_dispatch_is_settled_by_reading_the_parent(self) -> None:
        """A restarted or concurrent worker finds the key already recorded.

        Answered by reading where the account is, never by repeating the move: the other pass's
        move may well have landed, and AWS is the only thing that knows. Two reads only here,
        because the hook never runs.
        """
        organizations = RecordingOrganizations(parents=[AT_ROOT, IN_APPROVED_UNIT])

        placement, executor = await _verify(organizations, already_recorded=(PLACEMENT_KEY,))

        assert executor.refused == [PLACEMENT_KEY]
        assert organizations.move_calls == [], "a refused dispatch repeated the move anyway"
        assert placement.verified
        assert "refused a second move dispatch" in placement.detail

    @pytest.mark.asyncio
    async def test_a_denied_move_leaves_the_account_reported_unplaced(self) -> None:
        """An authoritative refusal. The account stays at the root and the report says so."""
        organizations = RecordingOrganizations(
            parents=[AT_ROOT, AT_ROOT, AT_ROOT],
            move_result=ProviderDenied("not authorized to perform organizations:MoveAccount"),
        )

        placement, _ = await _verify(organizations)

        assert not placement.verified
        assert placement.state is StepState.ABSENT
        assert "AWS refused it" in placement.detail

    @pytest.mark.asyncio
    async def test_the_hook_reads_the_source_parent_rather_than_assuming_the_root(self) -> None:
        """`move_account` needs a source, and the only honest one is where the account is NOW.

        Between the caller's read and the move, the fence may have admitted a different worker
        or an operator may have moved the account by hand. So the hook reads again and refuses
        what it does not expect — here, an account another pass already placed correctly, which
        must not be "moved" out of a unit it has just been put in.
        """
        organizations = RecordingOrganizations(parents=[AT_ROOT, IN_APPROVED_UNIT, IN_APPROVED_UNIT])

        placement, executor = await _verify(organizations)

        # The caller saw the root and dispatched; the hook then saw the approved unit and did
        # NOT move. A hook that had assumed the root would have asked AWS to move an account
        # out of the unit it had just been placed in.
        assert executor.dispatches, "fixture precondition: the caller must have dispatched"
        assert organizations.move_calls == []
        assert placement.verified


class TestPlacementRefusesWhatItHasNoApprovalToDo:
    """Guards that run before any credential is obtained."""

    @pytest.mark.parametrize(
        "mode",
        [OwnershipMode.EXISTING_ACCOUNT_MANAGED, OwnershipMode.BRING_EXISTING_CLUSTER],
    )
    @pytest.mark.asyncio
    async def test_only_the_named_new_account_mode_may_place_an_account(self, mode: OwnershipMode) -> None:
        """ADP did not open an adopted account and has no approval to re-place it.

        `modes.validate` already requires `organizational_unit_id` to be absent in both existing
        modes, so a placement here could only act on a unit nobody authorized.
        """
        organizations = RecordingOrganizations(parents=AT_ROOT)
        credentials = RecordingCredentials(organizations=organizations)
        executor = RecordingExecutor()

        with pytest.raises(PlacementRefused) as refusal:
            await verify_placement(
                executor,
                credentials,
                request_for_mode(mode),
                account_id=FIXTURE_CREATED_ACCOUNT,
            )

        assert "requires mode" in str(refusal.value)
        assert organizations.calls == [], f"a refused placement still called Organizations: {organizations.calls}"
        assert credentials.management_calls == [], "a refused placement still obtained a credential"
        assert executor.dispatches == []

    @pytest.mark.asyncio
    async def test_an_unstated_unit_is_refused_rather_than_defaulted(self) -> None:
        """The default is the root, and the root is the position that means "no controls apply".

        Defaulting here would make the module's own reason for existing unreachable: it would
        "place" the account exactly where the finding says it must not be left.
        """
        organizations = RecordingOrganizations(parents=AT_ROOT)
        credentials = RecordingCredentials(organizations=organizations)
        executor = RecordingExecutor()

        with pytest.raises(PlacementRefused) as refusal:
            await verify_placement(
                executor,
                credentials,
                new_account_request(organizational_unit_id=None),
                account_id=FIXTURE_CREATED_ACCOUNT,
            )

        assert "requires an organizational unit" in str(refusal.value)
        assert organizations.calls == []
        assert executor.dispatches == []


class TestBootstrapRequiresAVerifiedPlacement:
    """Nothing writes account-wide roles into an account outside its approved unit."""

    def _plan(self):
        request = new_account_request()
        return bootstrap_plan(request, matching_authorization(request))

    async def _refused(self, placement: AccountPlacement | None) -> str:
        """Bootstrap with `placement`, expecting a refusal, and assert nothing was touched."""
        iam = RecordingIam(present_roles=set(), attached_policies=fully_bootstrapped_roles())
        credentials = RecordingCredentials(iam=iam)
        executor = RecordingExecutor()

        with pytest.raises(BootstrapRefused) as refusal:
            await bootstrap_account(
                executor,
                credentials,
                self._plan(),
                account_id=FIXTURE_CREATED_ACCOUNT,
                placement=placement,
                permission_policy_arns=FIXTURE_PERMISSION_ARNS,
            )

        # The credential is obtained AFTER this guard, so a refused bootstrap never reaches the
        # account at all — which is stronger than "an exception was raised", since an exception
        # thrown after the first `create_role` would still have left an identity behind.
        assert credentials.child_calls == [], "a refused bootstrap still obtained a credential"
        assert iam.calls == [], f"a refused bootstrap still contacted IAM: {iam.calls}"
        assert executor.dispatches == []
        return str(refusal.value)

    @pytest.mark.asyncio
    async def test_an_account_still_at_the_root_is_not_bootstrapped(self) -> None:
        """The compounding failure F1 describes, refused.

        Bootstrap writes three account-wide cross-account roles. Doing that at the root puts
        privileged identities into an account none of the approved controls reach — and then
        hands a workspace the result.
        """
        message = await self._refused(
            AccountPlacement(
                account_id=FIXTURE_CREATED_ACCOUNT,
                organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
                state=StepState.ABSENT,
                detail="at the organization root",
            )
        )

        assert "placement" in message
        assert StepState.ABSENT.value in message

    @pytest.mark.asyncio
    async def test_an_omitted_placement_is_a_refusal_not_permission(self) -> None:
        """Absent and verified must not look alike.

        A default that let an unchecked placement through would make the guard unenforceable in
        exactly the composition that forgot to wire it — which is the composition it exists for.
        """
        message = await self._refused(None)

        assert "no placement evidence" in message

    @pytest.mark.parametrize("state", [StepState.CONFLICT, StepState.DENIED, StepState.NOT_CHECKED])
    @pytest.mark.asyncio
    async def test_no_unverified_state_is_treated_as_placed(self, state: StepState) -> None:
        """Each non-established state on its own, because each arrives by a different route.

        A guard written as "not ABSENT" would let all three of these through, and each one is a
        real answer `read_placement` returns: an account somebody else placed, a parent read
        nobody was permitted to make, and a read that produced nothing usable.
        """
        message = await self._refused(
            AccountPlacement(
                account_id=FIXTURE_CREATED_ACCOUNT,
                organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
                state=state,
                detail=f"placement is {state.value}",
            )
        )

        assert "is not established" in message
        assert state.value in message

    @pytest.mark.asyncio
    async def test_another_accounts_verified_placement_does_not_satisfy_this_one(self) -> None:
        """One account's evidence is not proof about another's.

        The same cross-subject confusion `recovery._same_account_subject` refuses, arriving by a
        different route: without this check a verified placement for any account at all would
        clear the guard, and the roles would go into an account whose position nobody read.
        """
        message = await self._refused(
            AccountPlacement(
                account_id="000000000999",
                organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
                state=StepState.ESTABLISHED,
                detail="a different account is correctly placed",
                actual_parent_id=FIXTURE_ORGANIZATIONAL_UNIT,
            )
        )

        assert "000000000999" in message
        assert FIXTURE_CREATED_ACCOUNT in message

    @pytest.mark.asyncio
    async def test_a_verified_placement_does_let_bootstrap_proceed(self) -> None:
        """The positive control. Without it every refusal above would also hold for a guard
        that refused unconditionally — which would break the feature instead of governing it.

        The account is already fully bootstrapped with the reviewed trust documents, so the
        expected behaviour is reads only: the guard admits the pass, and the pass writes nothing.
        """
        iam = RecordingIam(
            present_roles={
                "AdpAccountBootstrap",
                "AdpWorkspaceController",
                "AdpWorkspaceWorkload",
                "AWSServiceRoleForAutoScaling",
            },
            attached_policies=fully_bootstrapped_roles(),
            trust_documents=bootstrapped_trust_documents(),
        )
        credentials = RecordingCredentials(iam=iam)
        executor = RecordingExecutor()

        await bootstrap_account(
            executor,
            credentials,
            self._plan(),
            account_id=FIXTURE_CREATED_ACCOUNT,
            placement=AccountPlacement(
                account_id=FIXTURE_CREATED_ACCOUNT,
                organizational_unit_id=FIXTURE_ORGANIZATIONAL_UNIT,
                state=StepState.ESTABLISHED,
                detail="in the approved unit",
                actual_parent_id=FIXTURE_ORGANIZATIONAL_UNIT,
            ),
            trust_policies=FIXTURE_TRUST_POLICIES,
            permission_policy_arns=FIXTURE_PERMISSION_ARNS,
        )

        assert credentials.child_calls == [("op-fixture", FIXTURE_CREATED_ACCOUNT)]
        assert iam.writes == [], f"a reuse-only pass wrote to IAM: {iam.writes}"
        assert executor.dispatches == [], "a reuse-only pass must consume no durable key"
