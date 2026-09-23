"""The copied contract has not drifted from the authoritative one — Issue #5531 (w6-08).

This package carries its own `CallOutcome` rather than importing `harness_jobs`', for the
reason `execution.py`'s module docstring gives: two tests assert that package is not
importable from the API app and the provisioning adapter, and composing the real facade is
#5535's (w6-12) decision rather than this story's.

A copied vocabulary is only safe with a drift test, and the copy on this branch had a
defect that no amount of comparing the copy to itself would have found.

## The defect these tests exist to prevent

`creation_runner._settled_outcome` compared the durable record's outcome against this
package's copy with `is`. At runtime the record carries a member of the *harness* enum, and
two distinct enum classes never share members, so the comparison could not match. A real
`SUCCEEDED` fell through to the unresolved branch: a created AWS account was reported as
`UNKNOWN` and its account id discarded.

That is the exact failure this story exists to prevent — an account that exists, is
billing, and that no record names — arrived at through a comparison operator rather than
through any provider behaviour. And it is invisible to a suite that uses only the local
copy, which is why every test below drives the real entry points with the REAL harness
class.

`test_the_local_and_authoritative_vocabularies_agree` is the guard against the values
drifting apart later; the `TestOutcomesReadCorrectly` cases are the guard against the
comparison style regressing, which is the half that actually bit.
"""

from __future__ import annotations

import pytest
from harness_jobs.execution import CallOutcome as AuthoritativeCallOutcome

from account_provisioning.creation_runner import (
    _settled_outcome,
    encode_reference,
)
from account_provisioning.execution import CallOutcome as LocalCallOutcome
from account_provisioning.execution import as_outcome, outcome_vocabulary

from .conftest import FIXTURE_CREATED_ACCOUNT, FIXTURE_REQUEST_ID


class TestVocabularyAgreement:
    """The copy and the original still name the same things."""

    def test_the_local_and_authoritative_vocabularies_agree(self) -> None:
        """Member names and wire values match exactly, in both directions.

        Both directions on purpose. A missing member here means this package cannot read an
        outcome the store can write; an EXTRA member means it can produce one the store
        cannot store. The values are what land in a database column, so a changed spelling
        is a silent read failure rather than an error.
        """
        local = {member.name: member.value for member in LocalCallOutcome}
        authoritative = {member.name: member.value for member in AuthoritativeCallOutcome}
        assert local == authoritative, (
            "this package's `CallOutcome` has drifted from `harness_jobs.execution."
            "CallOutcome`. The values are written to that package's database column, so a "
            "difference here is a silent misread of a provider outcome, not a type error."
        )

    def test_the_two_classes_are_genuinely_distinct(self) -> None:
        """The premise of every test below: `is` cannot work across the boundary.

        Stated as an assertion rather than left implicit, so that if the packages are ever
        genuinely unified this test fails and tells the next reader that the `as_outcome`
        normalization is no longer load-bearing — instead of leaving them to wonder why it
        exists.
        """
        assert LocalCallOutcome is not AuthoritativeCallOutcome
        assert LocalCallOutcome.SUCCEEDED is not AuthoritativeCallOutcome.SUCCEEDED
        # Equality DOES hold, because both are `(str, Enum)` over identical values. That is
        # precisely why normalizing by value is a complete fix.
        assert LocalCallOutcome.SUCCEEDED == AuthoritativeCallOutcome.SUCCEEDED


class TestAsOutcome:
    """`as_outcome` is the single reader every comparison goes through."""

    @pytest.mark.parametrize("member", list(AuthoritativeCallOutcome))
    def test_every_authoritative_member_normalizes(self, member) -> None:
        normalized = as_outcome(member)
        assert isinstance(normalized, LocalCallOutcome)
        assert normalized.value == member.value

    @pytest.mark.parametrize("member", list(LocalCallOutcome))
    def test_every_local_member_passes_through(self, member) -> None:
        assert as_outcome(member) is member

    @pytest.mark.parametrize("raw", ["succeeded", "failed", "absent", "unknown"])
    def test_the_bare_database_spelling_normalizes(self, raw: str) -> None:
        """The column holds a plain string, so a record read back may carry one."""
        assert as_outcome(raw) == LocalCallOutcome(raw)

    def test_an_unobserved_intent_stays_none(self) -> None:
        """`None` means "intent committed, nothing observed" and must not become UNKNOWN.

        The distinction is load-bearing in the other direction from the rest of this file:
        `None` is what a caller reads to learn that a row exists but no call has been
        settled, and collapsing it into `UNKNOWN` would lose that.
        """
        assert as_outcome(None) is None

    def test_an_unreadable_value_becomes_unknown_not_none(self) -> None:
        """An unrecognised outcome is conservative, never permissive.

        `UNKNOWN` rather than `None` because `None` means "no call observed", which invites
        a fresh attempt. A value this code cannot read is precisely a state in which it has
        NOT established that no account exists, so it must not be able to authorize a second
        billable account.
        """
        assert as_outcome("a-state-this-package-has-never-heard-of") is LocalCallOutcome.UNKNOWN
        assert as_outcome(object()) is LocalCallOutcome.UNKNOWN


class _Record:
    operation_id = "op-fixture"

    """A durable record carrying whatever outcome spelling a test wants to supply."""

    def __init__(self, outcome: object, provider_ref: str | None) -> None:
        self.idempotency_key = "op-fixture:organizations-create-account"
        self.provider_ref = provider_ref
        self.outcome = outcome


class TestOutcomesReadCorrectly:
    """The real entry point reads the REAL harness enum. This is the regression.

    Each case runs twice — once with the local copy as a positive control, once with the
    authoritative class as the runtime reality. Before the fix the two disagreed, and the
    authoritative run was the wrong one. Parametrizing rather than writing the harness case
    alone is deliberate: if these ever diverge again, the failure names which side changed.
    """

    @pytest.mark.parametrize(
        "outcome_class",
        [LocalCallOutcome, AuthoritativeCallOutcome],
        ids=["local-copy-positive-control", "authoritative-harness-enum"],
    )
    def test_a_created_account_is_read_as_created_with_its_id(self, outcome_class) -> None:
        """The defect, pinned: a real success must not read as unresolved.

        This is the assertion the previous revision failed. With `is` against the copy, the
        authoritative-enum run returned `unknown` and `account_id=None` for an account that
        AWS had confirmed and named.
        """
        record = _Record(
            outcome_class.SUCCEEDED,
            encode_reference(request_id=FIXTURE_REQUEST_ID, account_id=FIXTURE_CREATED_ACCOUNT),
        )

        outcome = _settled_outcome(record)

        assert outcome.succeeded, (
            "a confirmed account read back as not-succeeded. An account that exists and is "
            "billing would be reported as possibly-absent, which is the untracked-account "
            "failure this story exists to prevent."
        )
        assert outcome.account_id == FIXTURE_CREATED_ACCOUNT
        assert outcome.create_account_request_id == FIXTURE_REQUEST_ID
        # A settled success is not awaiting anything.
        assert not outcome.may_exist
        assert not outcome.needs_reconciliation

    @pytest.mark.parametrize(
        "outcome_class",
        [LocalCallOutcome, AuthoritativeCallOutcome],
        ids=["local-copy-positive-control", "authoritative-harness-enum"],
    )
    def test_an_established_failure_is_read_as_failed_with_no_account(self, outcome_class) -> None:
        """A provider refusal establishes that nothing was created."""
        from account_factory.creation import CreateAccountFailure, CreateAccountStatus

        record = _Record(
            outcome_class.FAILED,
            encode_reference(
                request_id=FIXTURE_REQUEST_ID,
                failure=CreateAccountFailure.EMAIL_ALREADY_EXISTS,
            ),
        )

        outcome = _settled_outcome(record)

        assert outcome.status is CreateAccountStatus.FAILED
        assert outcome.account_id is None
        assert outcome.failure is CreateAccountFailure.EMAIL_ALREADY_EXISTS
        # Nothing was created, so nothing is outstanding to reconcile.
        assert not outcome.may_exist

    @pytest.mark.parametrize(
        "outcome_class",
        [LocalCallOutcome, AuthoritativeCallOutcome],
        ids=["local-copy-positive-control", "authoritative-harness-enum"],
    )
    def test_a_lost_answer_keeps_the_request_id_and_stays_unresolved(self, outcome_class) -> None:
        """An unobtained answer must stay unresolved AND keep its reconciliation handle.

        Both halves matter. Unresolved is what stops a blind retry; the retained request id
        is the only thing that lets a later pass ask about *that* request rather than start
        a new one.
        """
        from account_factory.creation import CreateAccountStatus

        record = _Record(outcome_class.UNKNOWN, encode_reference(request_id=FIXTURE_REQUEST_ID))

        outcome = _settled_outcome(record)

        assert outcome.status is CreateAccountStatus.UNKNOWN
        assert outcome.account_id is None
        assert outcome.may_exist
        assert outcome.needs_reconciliation
        assert outcome.create_account_request_id == FIXTURE_REQUEST_ID

    @pytest.mark.parametrize(
        "outcome_class",
        [LocalCallOutcome, AuthoritativeCallOutcome],
        ids=["local-copy-positive-control", "authoritative-harness-enum"],
    )
    def test_a_lost_answer_with_no_request_id_is_the_worst_case_and_says_so(self, outcome_class) -> None:
        """No answer and no handle: unresolved, and NOT reconcilable.

        The state that needs an operator, because an account may be opening and there is
        nothing to ask about. It must not report itself as reconcilable, or a recovery loop
        would spin on a request id it does not have.
        """
        from account_factory.creation import CreateAccountStatus

        outcome = _settled_outcome(_Record(outcome_class.UNKNOWN, None))

        assert outcome.status is CreateAccountStatus.UNKNOWN
        assert outcome.may_exist
        assert not outcome.needs_reconciliation
        assert outcome.create_account_request_id is None

    @pytest.mark.parametrize(
        "outcome_class",
        [LocalCallOutcome, AuthoritativeCallOutcome],
        ids=["local-copy-positive-control", "authoritative-harness-enum"],
    )
    def test_an_unobserved_intent_row_is_unresolved(self, outcome_class) -> None:
        """Intent committed, nothing observed — the crash window. Unresolved, not absent.

        `outcome_class` is unused in the body by design: the point is that a row with NO
        outcome behaves identically whichever vocabulary the store uses, because the
        conservative branch is reached by absence rather than by a comparison.
        """
        from account_factory.creation import CreateAccountStatus

        outcome = _settled_outcome(_Record(None, encode_reference(request_id=FIXTURE_REQUEST_ID)))

        assert outcome.status is CreateAccountStatus.UNKNOWN
        assert outcome.may_exist
        assert outcome.needs_reconciliation


class TestTheOutboundHalfOfTheSameBoundary:
    """A hook's ANSWER crosses the same boundary, and was crossing it wrongly.

    Everything above is the inbound direction: an outcome read off a durable record, where the
    defect was `is`. This class is the mirror, and it was a live defect on this branch found
    while writing the bootstrap-role tests.

    The real executor validates a hook's answer:

        if not isinstance(outcome, CallOutcome):       # ITS OWN class
            raise ContractViolation("Provider hook must return CallOutcome")
        except Exception:                              # ...and then swallows everything
            outcome = CallOutcome.UNKNOWN

    Two distinct enum classes never satisfy `isinstance` across the boundary, however equal
    their values. So a hook answering in this package's copy had EVERY answer discarded and
    replaced with `UNKNOWN` — no log line, no error reaching the caller, and the executor's
    `UNKNOWN` branch keeping the intent recoverable so that nothing looked broken.

    The consequences are the ones this whole story is about: a created account reported as "an
    account may exist" with its id discarded, and an authoritative AWS refusal reported as
    possibly-happened so it never becomes a retry. `equality` is not enough here and neither is
    `as_outcome` — the check is on TYPE, so the fix is to answer in the class the executor holds.
    """

    def test_the_local_copy_does_not_satisfy_the_executors_type_check(self) -> None:
        """The premise, asserted rather than assumed — and it is not obvious.

        `LocalCallOutcome.SUCCEEDED == AuthoritativeCallOutcome.SUCCEEDED` is True, which is
        what makes the inbound normalization a complete fix and what makes this outbound trap so
        easy to walk into: every equality check a developer tries by hand passes.
        """
        assert not isinstance(LocalCallOutcome.SUCCEEDED, AuthoritativeCallOutcome), (
            "the two classes now satisfy isinstance, so `outcome_vocabulary` is no longer "
            "load-bearing — check whether the packages were unified before deleting it"
        )
        assert LocalCallOutcome.SUCCEEDED == AuthoritativeCallOutcome.SUCCEEDED, (
            "equality across the boundary is what makes the type check the only thing that fails, and the failure silent"
        )

    @pytest.mark.parametrize("member", ["SUCCEEDED", "FAILED", "UNKNOWN"])
    def test_the_resolved_vocabulary_satisfies_the_type_check(self, member: str) -> None:
        """What a hook composed with the real class answers in passes the real check."""
        resolved = outcome_vocabulary(AuthoritativeCallOutcome)
        assert isinstance(getattr(resolved, member), AuthoritativeCallOutcome)

    def test_an_uncomposed_vocabulary_falls_back_to_the_local_copy(self) -> None:
        """`None` is for the offline tests that drive a hook directly, and must stay usable."""
        assert outcome_vocabulary(None) is LocalCallOutcome

    @pytest.mark.asyncio
    async def test_the_creation_hook_answers_in_the_class_it_was_given(self) -> None:
        """Driven through the real `creation_hook`, on the success path.

        The success path specifically, because that is the one where the silent `UNKNOWN`
        substitution costs an account: AWS confirmed it, named it, and the record would have
        said "may exist" with no id.
        """
        from account_provisioning.creation_runner import creation_hook

        from .conftest import (
            RecordingCredentials,
            RecordingOrganizations,
            new_account_request,
            succeeded_response,
        )

        organizations = RecordingOrganizations(create_result=succeeded_response())
        credentials = RecordingCredentials(organizations=organizations)
        hook = creation_hook(credentials, new_account_request(), outcomes=AuthoritativeCallOutcome)

        outcome, _, reference = await hook(_Record(None, None))

        assert isinstance(outcome, AuthoritativeCallOutcome), (
            "the hook answered in a class the executor will reject, so the executor would record UNKNOWN for a confirmed account and discard its id"
        )
        assert outcome is AuthoritativeCallOutcome.SUCCEEDED
        assert FIXTURE_CREATED_ACCOUNT in (reference or "")

    @pytest.mark.asyncio
    async def test_the_bootstrap_hook_answers_in_the_class_it_was_given(self) -> None:
        """The same, for the other hook. Both are composed the same way and both were wrong."""
        from account_factory.bootstrap import bootstrap_plan
        from account_provisioning.bootstrap_runner import bootstrap_hook

        from .conftest import (
            FIXTURE_PERMISSION_ARNS,
            FIXTURE_TRUST_POLICIES,
            RecordingCredentials,
            RecordingIam,
            matching_authorization,
            new_account_request,
        )

        request = new_account_request()
        iam = RecordingIam()
        hook = bootstrap_hook(
            RecordingCredentials(iam=iam),
            bootstrap_plan(request, matching_authorization(request)),
            account_id=FIXTURE_CREATED_ACCOUNT,
            trust_policies=FIXTURE_TRUST_POLICIES,
            permission_policy_arns=FIXTURE_PERMISSION_ARNS,
            outcomes=AuthoritativeCallOutcome,
        )

        record = _Record(None, None)
        record.operation_id = "op-fixture"
        record.operation_kind = "bootstrap-role"
        outcome, _, provider_ref = await hook(record)

        assert isinstance(outcome, AuthoritativeCallOutcome)
        assert outcome is AuthoritativeCallOutcome.SUCCEEDED
        assert provider_ref == "AdpAccountBootstrap"

    @pytest.mark.parametrize("hook_name", ["creation_hook", "bootstrap_hook"])
    def test_both_hooks_accept_the_vocabulary_as_a_keyword(self, hook_name: str) -> None:
        """Structural, so a hook added or re-signed later cannot quietly drop it.

        Keyword-only and defaulted: a composer that forgets it gets the local copy and the
        silent-UNKNOWN behaviour back, which is why `outcome_vocabulary`'s docstring is where the
        reason lives rather than only here.
        """
        import inspect

        from account_provisioning import bootstrap_runner, creation_runner

        module = creation_runner if hook_name == "creation_hook" else bootstrap_runner
        parameter = inspect.signature(getattr(module, hook_name)).parameters["outcomes"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
