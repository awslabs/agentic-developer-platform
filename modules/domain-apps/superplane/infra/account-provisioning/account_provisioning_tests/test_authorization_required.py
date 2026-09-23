"""Nothing acts on an unauthorized request — Issue #5531 (w6-08).

## The defect these tests exist to prevent

Both entry points took authorization as something a caller could simply omit.

`create_account` declared `authorization: ValidationAuthorization | None = None` and passed
it straight to `assess_attempt`, which calls `ensure_valid` and **discards the result**.
`ensure_valid` returns the comparisons that were NOT made, and it returns them rather than
raising because the same validator has to run offline before any authorization exists. So a
caller that omitted authorization got `may_create_account == True` with the organization,
the management account, the management cluster, the mode, the workspace and the permitted
placement compared against nothing at all — and then opened a billable AWS account.

`bootstrap_account` had the same hole one level along. `bootstrap_plan` records the unmade
comparisons on `plan.unchecked_authorization`, and no code that *acted* on a plan ever read
that field. The consequence is worse there than on the creation path, because those steps
write **account-wide** identities — three cross-account roles and a service-linked role,
shared by every workspace in the account and outliving all of them.

## The three things each guard must establish, and why each needs its own test

1. **Present.** An omitted authorization must be impossible, not defaulted.
2. **Complete.** An authorization that simply left `permitted_organizational_units` unset
   would place an account anywhere in the organization tree and report itself verified. An
   unchecked comparison is not a pass.
3. **Bound to this operation.** This is the one a field-level validator cannot catch by
   construction: a caller can supply a request and an authorization that agree with each
   other perfectly and describe **another tenant's workspace**. Every comparison inside
   `validate` passes, because they are all request-against-authorization. So the guards
   compare both against the executor's *lease* — server-resolved identity that nothing in a
   request body can influence.

Every refusal below asserts on the recording doubles as well as the exception: no provider
call dispatched, no credential obtained, no IAM write. An exception raised *after*
`CreateAccount` would still have left an account behind, so "it raised" is not the claim.

No AWS is contacted anywhere in this file, and no live account vending is exercised or
authorized by it — see `conftest.py`.
"""

from __future__ import annotations

import pytest
from harness_jobs.execution import CallOutcome as AuthoritativeCallOutcome

from account_factory.bootstrap import bootstrap_plan
from account_provisioning.bootstrap_runner import BootstrapRefused, bootstrap_account
from account_provisioning.creation_runner import CreationRefused, create_account

from .conftest import (
    FIXTURE_CREATED_ACCOUNT,
    FIXTURE_ORG_ID,
    FIXTURE_PERMISSION_ARNS,
    FIXTURE_PERMISSION_DOCUMENTS,
    FIXTURE_WORKSPACE,
    RecordingCredentials,
    RecordingExecutor,
    RecordingIam,
    RecordingOrganizations,
    fully_bootstrapped_roles,
    matching_authorization,
    new_account_request,
    verified_placement,
)


def _nothing_happened(organizations: RecordingOrganizations, credentials: RecordingCredentials) -> None:
    """The assertion every refusal shares: no call of any kind reached a provider."""
    assert organizations.calls == [], f"a refused creation still called Organizations: {organizations.calls}"
    assert credentials.management_calls == [], "a refused creation still obtained a management credential"
    assert credentials.child_calls == [], "a refused creation still obtained a child-account credential"


class TestCreationRequiresAuthorization:
    """`create_account` refuses before any credential is obtained."""

    @pytest.mark.asyncio
    async def test_an_omitted_authorization_is_a_refusal_not_a_default(self) -> None:
        """The headline defect: `authorization=None` used to authorize a real account.

        Passed explicitly here because the signature no longer has a default — which is the
        point. A caller that omits the argument now cannot reach this function at all, and
        one that passes `None` explicitly is refused.
        """
        request = new_account_request()
        organizations = RecordingOrganizations()
        credentials = RecordingCredentials(organizations=organizations)
        executor = RecordingExecutor()

        with pytest.raises(CreationRefused) as refusal:
            await create_account(executor, credentials, request, authorization=None)

        assert "requires an operation authorization" in str(refusal.value)
        assert executor.dispatches == [], "a creation with no authorization reached the durable executor"
        _nothing_happened(organizations, credentials)

    def test_the_signature_has_no_authorization_default(self) -> None:
        """Structural, so the defect cannot return by editing the guard's callers away.

        A default of `None` is what made the omission expressible in the first place. This
        asserts on the signature rather than on behaviour because the two failure modes are
        different: the test above catches a guard that stops checking, and this one catches a
        default being reintroduced so that omitting the argument silently means "unverified".
        """
        import inspect

        parameter = inspect.signature(create_account).parameters["authorization"]
        assert parameter.default is inspect.Parameter.empty, "`authorization` has a default again; omitting it must not be expressible"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
            "`authorization` must stay keyword-only so it cannot be supplied positionally by accident"
        )

    @pytest.mark.parametrize(
        "omitted",
        [
            "organization_id",
            "management_account_id",
            "management_cluster",
            "permitted_modes",
            "workspace_id",
            "permitted_organizational_units",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_single_unmade_comparison_refuses_and_is_named(self, omitted: str) -> None:
        """Each field on its own, because each one alone is sufficient to refuse.

        Parametrized rather than tested as a single all-fields-absent case: an
        implementation that only checked whether *any* comparison was made would pass that
        case and still let `permitted_organizational_units` through, which is the one that
        decides where in the organization tree a real account lands.

        The refusal must NAME the field. An operator who cannot tell which comparison was
        missing cannot fix the authorization, and the likeliest fix for an unexplained
        refusal is to stop passing authorization at all.
        """
        request = new_account_request()
        authorization = matching_authorization(request, **{omitted: None})
        organizations = RecordingOrganizations()
        credentials = RecordingCredentials(organizations=organizations)
        executor = RecordingExecutor()

        with pytest.raises(CreationRefused) as refusal:
            await create_account(executor, credentials, request, authorization=authorization)

        message = str(refusal.value)
        assert "every authorization comparison" in message
        # `permitted_modes` is reported as `mode`, and the permitted-set fields as the
        # request field they gate; assert on the request-side name the operator would look
        # for rather than on the authorization attribute.
        expected = {
            "permitted_modes": "mode",
            "permitted_organizational_units": "organizational_unit_id",
        }.get(omitted, omitted)
        assert expected in message, f"the refusal did not name the unchecked field {expected!r}: {message}"
        assert executor.dispatches == []
        _nothing_happened(organizations, credentials)

    @pytest.mark.asyncio
    async def test_an_authorization_for_another_workspace_is_refused(self) -> None:
        """The cross-tenant case a field-level validator cannot catch.

        The request and the authorization agree with each other completely — so
        `validate` returns no problems and nothing unchecked. They just describe a workspace
        that is not the one this operation was admitted for. Only the comparison against the
        lease catches it, which is why the guard does not stop at `ensure_valid`.
        """
        request = new_account_request(workspace_id="ws-someone-else")
        authorization = matching_authorization(request)
        organizations = RecordingOrganizations()
        credentials = RecordingCredentials(organizations=organizations)
        # The lease says this operation belongs to `ws-fixture`.
        executor = RecordingExecutor(workspace_id=FIXTURE_WORKSPACE)

        with pytest.raises(CreationRefused) as refusal:
            await create_account(executor, credentials, request, authorization=authorization)

        message = str(refusal.value)
        assert "does not belong to this operation" in message
        assert "ws-someone-else" in message and FIXTURE_WORKSPACE in message
        assert executor.dispatches == []
        _nothing_happened(organizations, credentials)

    @pytest.mark.asyncio
    async def test_an_authorization_for_another_organization_is_refused(self) -> None:
        """The same hole one level up: a self-consistent pair naming another organization."""
        request = new_account_request(organization_id="o-otherorg999")
        authorization = matching_authorization(request)
        organizations = RecordingOrganizations()
        credentials = RecordingCredentials(organizations=organizations)
        executor = RecordingExecutor(org_id=FIXTURE_ORG_ID)

        with pytest.raises(CreationRefused) as refusal:
            await create_account(executor, credentials, request, authorization=authorization)

        assert "does not belong to this operation" in str(refusal.value)
        assert executor.dispatches == []
        _nothing_happened(organizations, credentials)

    @pytest.mark.asyncio
    async def test_an_authorization_naming_the_lease_but_a_request_naming_another_is_refused(self) -> None:
        """A valid authorization must not be reusable to act on a different workspace.

        The authorization is genuinely this operation's. The *request* is not. `ensure_valid`
        would catch this pair as a problem, but the guard also compares the request against
        the lease directly, so the refusal does not depend on the authorization having
        carried a workspace at all. The triangle has to close on all three sides.
        """
        request = new_account_request(workspace_id="ws-elsewhere")
        authorization = matching_authorization(new_account_request())
        organizations = RecordingOrganizations()
        credentials = RecordingCredentials(organizations=organizations)
        executor = RecordingExecutor(workspace_id=FIXTURE_WORKSPACE)

        with pytest.raises(CreationRefused):
            await create_account(executor, credentials, request, authorization=authorization)

        assert executor.dispatches == []
        _nothing_happened(organizations, credentials)

    @pytest.mark.asyncio
    async def test_a_fully_authorized_request_does_reach_the_executor(self) -> None:
        """The positive control, without which every test above could pass vacuously.

        A guard that refused everything would satisfy all the refusals and break the
        feature. This asserts the authorized path gets through to exactly one dispatch,
        under the stable key, after the placement read.
        """
        request = new_account_request()
        authorization = matching_authorization(request)
        organizations = RecordingOrganizations()
        credentials = RecordingCredentials(organizations=organizations)
        executor = RecordingExecutor(
            outcome=AuthoritativeCallOutcome.SUCCEEDED,
            provider_ref=f"request=car-x account={FIXTURE_CREATED_ACCOUNT}",
        )

        outcome = await create_account(executor, credentials, request, authorization=authorization)

        assert outcome.succeeded
        assert outcome.account_id == FIXTURE_CREATED_ACCOUNT
        assert len(executor.dispatches) == 1, "an authorized creation must dispatch exactly once"
        assert executor.dispatches[0]["idempotency_key"] == "op-fixture:organizations-create-account"
        # The placement was confirmed by a read before the dispatch, and the credential was
        # obtained for this operation rather than ambiently.
        assert credentials.management_calls == ["op-fixture"]
        assert [name for name, _ in organizations.calls] == [
            "list_roots",
            "list_organizational_units_for_parent",
        ]


class TestBootstrapRequiresAnAuthorizedPlan:
    """`bootstrap_account` refuses a plan whose authorization nobody checked."""

    @pytest.mark.asyncio
    async def test_a_plan_built_without_authorization_is_refused(self) -> None:
        """`unchecked_authorization` was carried and never read. Now it refuses.

        `bootstrap_plan(request)` with no authorization is a legitimate offline call — it is
        how an operator sees what bootstrapping would require. What must not happen is that
        same plan being handed to something that writes account-wide roles.
        """
        request = new_account_request()
        plan = bootstrap_plan(request)
        assert plan.unchecked_authorization, "fixture precondition: this plan must have unmade comparisons"

        iam = RecordingIam()
        credentials = RecordingCredentials(iam=iam)
        executor = RecordingExecutor()

        with pytest.raises(BootstrapRefused) as refusal:
            await bootstrap_account(executor, credentials, plan, account_id=FIXTURE_CREATED_ACCOUNT)

        message = str(refusal.value)
        assert "unverified authorization comparison" in message
        assert "workspace_id" in message
        # Not even a read. The credential is obtained after this guard, so a refused
        # bootstrap never reaches the account at all.
        assert credentials.child_calls == [], "a refused bootstrap still obtained a credential in the child account"
        assert iam.calls == [], f"a refused bootstrap still contacted IAM: {iam.calls}"
        assert executor.dispatches == []

    @pytest.mark.asyncio
    async def test_a_plan_for_another_workspace_is_refused(self) -> None:
        """A fully authorized plan for someone else's workspace must not write here.

        The plan is internally impeccable: built with a matching authorization, nothing
        unchecked. It belongs to another tenant. The roles it describes are account-wide, so
        creating them under this operation's credential is the least reversible mistake in
        this package.
        """
        request = new_account_request(workspace_id="ws-someone-else")
        plan = bootstrap_plan(request, matching_authorization(request))
        assert not plan.unchecked_authorization

        iam = RecordingIam()
        credentials = RecordingCredentials(iam=iam)
        executor = RecordingExecutor(workspace_id=FIXTURE_WORKSPACE)

        with pytest.raises(BootstrapRefused) as refusal:
            await bootstrap_account(executor, credentials, plan, account_id=FIXTURE_CREATED_ACCOUNT)

        assert "does not belong to this operation" in str(refusal.value)
        assert credentials.child_calls == []
        assert iam.calls == []

    @pytest.mark.asyncio
    async def test_a_plan_for_another_organization_is_refused(self) -> None:
        request = new_account_request(organization_id="o-otherorg999")
        plan = bootstrap_plan(request, matching_authorization(request))

        iam = RecordingIam()
        credentials = RecordingCredentials(iam=iam)
        executor = RecordingExecutor(org_id=FIXTURE_ORG_ID)

        with pytest.raises(BootstrapRefused) as refusal:
            await bootstrap_account(executor, credentials, plan, account_id=FIXTURE_CREATED_ACCOUNT)

        assert "does not belong to this operation" in str(refusal.value)
        assert credentials.child_calls == []
        assert iam.calls == []

    @pytest.mark.asyncio
    async def test_an_authorized_plan_does_reach_the_account(self) -> None:
        """The positive control: an authorized plan reads the account and reuses what exists.

        Every role is already present, so the expected behaviour is reads only and no write
        at all — which also demonstrates that the guard added here did not turn the
        reuse-if-present path into a refusal.
        """
        request = new_account_request()
        plan = bootstrap_plan(request, matching_authorization(request))
        iam = RecordingIam(
            present_roles={
                "AdpAccountBootstrap",
                "AdpWorkspaceController",
                "AdpWorkspaceWorkload",
                "AWSServiceRoleForAutoScaling",
            },
            # Present AND carrying their permission policies. Without the attachments these
            # would be half-built roles, and the pass would correctly try to finish them
            # instead of reusing them — so an already-bootstrapped account has to be
            # expressed as both.
            attached_policies=fully_bootstrapped_roles(),
        )
        credentials = RecordingCredentials(iam=iam)
        executor = RecordingExecutor()

        report = await bootstrap_account(
            executor,
            credentials,
            plan,
            account_id=FIXTURE_CREATED_ACCOUNT,
            placement=verified_placement(),
            permission_policy_arns=FIXTURE_PERMISSION_ARNS,
            permission_policy_documents=FIXTURE_PERMISSION_DOCUMENTS,
        )

        assert credentials.child_calls == [("op-fixture", FIXTURE_CREATED_ACCOUNT)]
        assert iam.writes == [], f"an already-bootstrapped account was written to: {iam.writes}"
        assert executor.dispatches == [], "a reuse-only pass must consume no durable key"
        # The two baseline controls stay unverified, which is `_baseline_outcome`'s
        # deliberate answer rather than a gap in this test: this package holds no client for
        # them, so the account correctly never reads as complete.
        assert not report.complete
        assert report.blocked_on == ("baseline-audit-logging", "baseline-public-access-block")
