"""Nothing in a rendered manifest can open an AWS account — Issue #5531 (w6-08).

## The defect these tests exist to prevent

`adp-account-ownership.yaml` declared an ACK `organizations.services.k8s.aws/Account`, and
`render.py` instantiated it as stage 1 of new-account-managed. So the thing that actually
opened the AWS account was a Kubernetes custom resource, reconciled by the Organizations
controller on its own schedule — which means every protection #5531 builds was on a path the
account never travelled:

* **No committed intent.** The durable store's guarantee is that a row exists before the
  provider is called, so a crash between the call and the answer leaves evidence. A controller
  calls `CreateAccount` when it reconciles; nothing wrote an intent first.
* **No request id tied to the operation.** The `CreateAccountRequestId` lived in the custom
  resource's status. Reconciling an unresolved outcome needs that id bound to the operation
  that caused the call, and status on a cluster object is not that.
* **No authenticated operation envelope.** A reconciliation loop has no principal, no
  approval and no lease. "Who authorized this account?" had no answer at all.
* **A controller retry is a second CreateAccount.** The failure mode the whole story exists to
  prevent, performed automatically by design, with no fence able to see it.

Deleting the `Account` from the graph is not a cleanup — it is the fix, and it is structural.
A graph with no `Account` resource **cannot** create an account however it is applied, so the
bypass cannot be reintroduced by applying the manifest differently, by a controller upgrade,
or by an operator who does not know the rule. Creation moved to
`account_provisioning.creation_runner.create_account`, where the fence is.

## What these tests check that reading the manifest would not

The manifest and the renderer have to agree, and there are two independent ways for them to
drift: the renderer could emit an `Account` the graph no longer declares (kro would reject
it, but the rendered plan would read as if an account were being created), or the graph could
regain an `Account` that nothing rendered (dormant until someone instantiated it by hand). So
the graph document and the rendered output are both asserted against, separately.

The positive half matters as much: rendering must still bind a workspace to its account, or
"no account creation" would be satisfied by a renderer that produced nothing usable.

Nothing here applies anything, contacts AWS, or vends an account — and no live account
creation is authorized by this file or by the code it covers. See `test_render.py`'s note.
"""

from __future__ import annotations

import json

import pytest
import yaml
from account_factory.render import RenderError, render

from . import MODULE_DIR
from .conftest import (
    BUILDERS,
    FIXTURE_CREATED_ACCOUNT,
    FIXTURE_ORGANIZATIONAL_UNIT,
    FIXTURE_TARGET_ACCOUNT,
    FIXTURE_WORKSPACE,
    bring_existing_cluster_request,
    existing_account_request,
    governed_account_id,
    new_account_request,
)

OWNERSHIP_GRAPH = MODULE_DIR / "manifests" / "adp-account-ownership.yaml"

# Every kind that asks a controller to call `CreateAccount`. `Account` is ACK's; the vendored
# combined root declares one internally, so instantiating it would create an account too.
_ACCOUNT_CREATING_KINDS = frozenset({"Account", "FullAccountInfrastructure"})


def _graph() -> dict:
    return yaml.safe_load(OWNERSHIP_GRAPH.read_text())


def _resources(graph: dict) -> list[dict]:
    return list(graph["spec"]["resources"])


class TestTheGraphCannotCreateAnAccount:
    """Asserted against the manifest document, because that is what a cluster reconciles."""

    def test_the_ownership_graph_declares_no_account_resource(self) -> None:
        """The headline fix. An `Account` here is a `CreateAccount` outside the fence.

        Checked by resource KIND rather than by searching the file text, because the header
        comment necessarily discusses the `Account` it removed — a substring test would either
        fail on the explanation or force the explanation to be deleted, and the explanation is
        the reason the next person does not add the resource back.
        """
        kinds = {resource["template"]["kind"] for resource in _resources(_graph())}
        assert not kinds & _ACCOUNT_CREATING_KINDS, (
            f"the ownership graph declares {sorted(kinds & _ACCOUNT_CREATING_KINDS)}, which lets a controller "
            f"call CreateAccount outside the durable fence"
        )

    def test_the_graph_requires_the_account_id_as_an_input(self) -> None:
        """An account it cannot create, it must be told — and told mandatorily.

        `default=` is kro's optional marker. An optional id would reconcile against an empty
        account number and the object would fail in-cluster as a permissions error, which
        reads like an IAM problem rather than the missing-input mistake it is. Required means
        `render.py` refuses first, offline, where a reviewer sees it.
        """
        declared = _graph()["spec"]["schema"]["spec"]
        assert "accountId" in declared, (
            "the graph cannot bind to an account it is never told about"
        )
        assert "default=" not in str(declared["accountId"]), (
            "accountId must not be optional: an empty account id reconciles against nothing"
        )

    def test_nothing_in_the_graph_references_a_removed_account_resource(self) -> None:
        """A dangling `${account...}` would leave kro unable to resolve the graph at all.

        This is the specific way the fix could have broken the feature instead of securing it:
        the `IAMRoleSelector`'s ARN was built from `${account.status.accountID}`. Left in
        place, the workspace would have no resolvable cross-account access path — an
        account-creation bypass traded for a workspace that cannot reach its own account.
        """
        serialized = json.dumps(_resources(_graph()))
        assert "${account." not in serialized, (
            "a resource still references the removed `account` resource's status"
        )

    def test_the_role_selector_reaches_the_bound_account(self) -> None:
        """The positive control: access is still expressed, and as an identity reference.

        A role ARN, never a credential — the property `test_render.py` checks for rendered
        output, asserted here on the graph that produces it.
        """
        selectors = [
            resource
            for resource in _resources(_graph())
            if resource["template"]["kind"] == "IAMRoleSelector"
        ]
        assert len(selectors) == 1
        arn = selectors[0]["template"]["spec"]["arn"]
        assert "${schema.spec.accountId}" in arn, (
            "the role selector does not name the account this graph was told to bind to"
        )
        assert arn.startswith("arn:aws:iam::")


class TestRenderingCannotProduceAnAccountCreatingObject:
    """Asserted against rendered output, since the renderer can drift from the graph."""

    def test_no_mode_renders_an_account_creating_object(self) -> None:
        """Every mode, because the rule is about the platform and not about one path.

        The adopting modes never declared an `Account` — a resource naming an account that
        already exists asks for one that is already there — so this is a regression guard for
        them and the fix itself for new-account-managed.
        """
        for mode, build in BUILDERS.items():
            request = build()
            result = render(request, creation_record=governed_account_id(request))
            rendered = {obj["kind"] for obj in result.objects}
            assert not rendered & _ACCOUNT_CREATING_KINDS, (
                f"{mode.value} renders {sorted(rendered & _ACCOUNT_CREATING_KINDS)}"
            )

    def test_new_account_managed_binds_to_the_account_it_was_given(self) -> None:
        """The positive control, without which "creates no account" is satisfied vacuously.

        A renderer that emitted nothing would pass every test above and provision nothing. The
        rendered object must carry the id it was handed, so applying it binds this workspace to
        the account the governed path actually opened.
        """
        result = render(
            new_account_request(),
            creation_record=governed_account_id(new_account_request()),
        )
        ownership = next(
            obj for obj in result.objects if obj["kind"] == "AccountOwnership"
        )

        assert ownership["spec"]["accountId"] == FIXTURE_CREATED_ACCOUNT
        # The approval-bound placement travels with it, so the applied object evidences WHERE
        # the governed call placed the account rather than asking for a placement now.
        assert ownership["spec"]["organizationalUnitId"] == FIXTURE_ORGANIZATIONAL_UNIT

    def test_the_bound_account_is_stated_as_a_stage_precondition(self) -> None:
        """An operator applying stage 1 must be told what has to be true first.

        Before the fix there was nothing to state: applying the stage was the beginning of the
        causal chain. Now the account must already exist and be recorded, and a precondition
        naming the specific account is what makes that checkable rather than folkloric.
        """
        result = render(
            new_account_request(),
            creation_record=governed_account_id(new_account_request()),
        )
        ownership = next(
            stage for stage in result.stages if stage.name == "account ownership"
        )

        assert ownership.precondition is not None
        assert FIXTURE_CREATED_ACCOUNT in ownership.precondition
        assert "CreateAccount" in ownership.precondition


class TestRenderingRefusesWhatItCannotBind:
    """The refusals, so a missing or wrong id cannot become a runtime surprise."""

    def test_new_account_managed_without_an_account_id_is_refused(self) -> None:
        """The load-bearing refusal: no id means the old behaviour would have created one.

        Rendering must not degrade to "emit it anyway and let the cluster sort it out". The
        refusal has to explain itself too — an unexplained refusal invites someone to restore
        the `Account` resource to make rendering work again, which is precisely the bypass.
        """
        with pytest.raises(RenderError) as refusal:
            render(new_account_request())

        message = str(refusal.value)
        assert "CreateAccount" in message
        assert "creation_runner" in message, (
            f"the refusal did not point at the governed path that opens the account: {message}"
        )

    @pytest.mark.parametrize(
        "build",
        [existing_account_request, bring_existing_cluster_request],
        ids=["existing-account-managed", "bring-existing-cluster"],
    )
    def test_an_adopting_mode_refuses_a_creation_record_id(self, build) -> None:
        """Two sources for one fact is how the wrong account gets used.

        The adopting modes take their account from `target_account_id`, which `modes.py`
        requires there and forbids in new-account-managed. Accepting a creation-record id here
        as well would mean a mismatch between the two silently picks one — and an account ADP
        opened is not interchangeable with an account a tenant asked ADP to adopt.
        """
        with pytest.raises(RenderError) as refusal:
            render(build(), account_id=FIXTURE_CREATED_ACCOUNT)

        message = str(refusal.value)
        assert "target_account_id" in message
        assert "adopt" in message

    @pytest.mark.parametrize(
        "malformed",
        ["", "   ", "12345", "not-an-account", "0000000007777", FIXTURE_WORKSPACE],
        ids=[
            "empty",
            "whitespace",
            "too-short",
            "words",
            "too-long",
            "a-workspace-name",
        ],
    )
    def test_a_malformed_account_id_is_refused_offline(self, malformed: str) -> None:
        """Caught here rather than in a cluster, because of how it would fail there.

        A malformed id renders an `IAMRoleSelector` whose ARN names no account. ACK's failure
        for that is an access error, which reads as a trust-policy or permissions problem —
        someone would go widen a policy to fix a typo. Refusing offline names the actual cause.

        The empty and whitespace cases are also the boundary with the refusal above: they must
        not slip through as "an id was supplied".
        """
        with pytest.raises(RenderError):
            render(new_account_request(), account_id=malformed)

    def test_the_adopted_account_is_still_reached_without_a_creation_id(self) -> None:
        """The positive control for the refusal above: adopting modes render normally.

        The asymmetry has to be a rule about where an account id comes from, not a new
        requirement that breaks the modes that never created an account.
        """
        result = render(existing_account_request())
        selector = next(
            obj for obj in result.objects if obj["kind"] == "IAMRoleSelector"
        )

        assert FIXTURE_TARGET_ACCOUNT in selector["spec"]["arn"]
        assert FIXTURE_CREATED_ACCOUNT not in json.dumps(result.objects)
