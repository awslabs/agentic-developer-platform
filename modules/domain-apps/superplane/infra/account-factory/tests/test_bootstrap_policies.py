"""The bootstrap roles' reviewed IAM documents exist and are least-privilege — #5531 (w6-08).

## The defect these tests exist to prevent

The three role steps named `bootstrap-trust-policy.json`, `controller-trust-policy.json` and
`workload-trust-policy.json` as bare `file://` arguments. None of the three files existed
anywhere in the repository, and no step attached a permission policy to the role it created.
So the plan was not runnable, and the failure mode was the expensive one: `create-role` fails
at the moment an operator runs it against a real account, part-way through bootstrap, with
earlier steps already applied — an account that is neither un-bootstrapped nor bootstrapped.
Even had the files appeared, all three roles would have been created with no permissions: an
identity that can be assumed and can then do nothing, failing later and somewhere else.

## Why the documents are asserted by parsing rather than by reading them in review

A reviewer reads them once. These tests read them on every change, and they assert the two
rules `infra/workspaces/iam.tf` states for the same class of document:

* no `Principal: "*"` and no bare-account principal on an assumable role — a trust policy
  naming only an account id admits *every* principal in that account, including every role
  created there later; and
* no `Resource: "*"` in a permission policy, except for actions that take no resource, which
  must say so in their `Sid`.

Parsing rather than grepping for `"*"`, deliberately: a wildcard inside a prose comment is not
a grant, and a check that cannot tell the difference is one that gets disabled the first time
it fires wrongly.

## What these tests deliberately do NOT establish

That the policies are sufficient for a real workspace build, that AWS would accept them, or
that any role exists. They are offline assertions about reviewed documents. Applying them to a
live child account is a Wave 6 operations-gate activity requiring its own named authorization;
nothing here creates a role, attaches a policy, or contacts AWS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from account_factory.bootstrap import (
    POLICY_DIR,
    BootstrapError,
    BootstrapStep,
    PresenceRule,
    RoleTier,
    bootstrap_plan,
    policy_path,
)

from .conftest import new_account_request

TRUST_DOCUMENTS = (
    "bootstrap-trust-policy.json",
    "controller-trust-policy.json",
    "workload-trust-policy.json",
)

PERMISSION_DOCUMENTS = (
    "bootstrap-permissions-policy.json",
    "controller-permissions-policy.json",
    "workload-permissions-policy.json",
)

# Actions that genuinely take no resource, so `"Resource": "*"` is the only expressible form
# rather than a wildcard grant. Each use must say which action it is covering in its `Sid`.
_RESOURCELESS_ACTIONS = frozenset(
    {
        "sts:GetCallerIdentity",
        "s3:GetAccountPublicAccessBlock",
        "s3:PutAccountPublicAccessBlock",
        "cloudtrail:DescribeTrails",
        "cloudtrail:GetTrailStatus",
        "ec2:DescribeVpcs",
        "ec2:DescribeSubnets",
        "ec2:DescribeSecurityGroups",
        "ec2:DescribeAvailabilityZones",
        "ec2:DescribeRouteTables",
    }
)


def _document(filename: str) -> dict:
    return json.loads((POLICY_DIR / filename).read_text(encoding="utf-8"))


def _statements(document: dict) -> list[dict]:
    statements = document["Statement"]
    return statements if isinstance(statements, list) else [statements]


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


def _resources(statement: dict) -> list[str]:
    resource = statement.get("Resource", [])
    return [resource] if isinstance(resource, str) else list(resource)


class TestTheDocumentsTheCommandsNameActuallyExist:
    """The headline finding: the plan referenced six documents, and none of them existed."""

    @pytest.mark.parametrize("filename", TRUST_DOCUMENTS + PERMISSION_DOCUMENTS)
    def test_every_named_document_is_present_and_is_valid_json(
        self, filename: str
    ) -> None:
        """Present AND parseable, because an unparseable policy fails the same way a missing
        one does — at `create-role` time, against a live account, mid-bootstrap."""
        document = _document(filename)
        assert document["Version"] == "2012-10-17", filename
        assert _statements(document), f"{filename} grants nothing"

    def test_a_step_cannot_name_a_document_that_is_not_there(self) -> None:
        """`policy_path` refuses at plan-build time, which is what moves the failure offline.

        This is the actual repair: it is not enough that the six files exist today. A future
        rename must fail here, on a runner with no AWS identity, rather than in the middle of
        an operator's bootstrap run.
        """
        with pytest.raises(BootstrapError) as refusal:
            policy_path("no-such-trust-policy.json")

        message = str(refusal.value)
        assert "no-such-trust-policy.json" in message
        assert "half-built" in message or "already applied" in message, (
            f"the refusal should say why a missing document is worse than a missing file: {message}"
        )

    def test_every_role_step_names_a_resolvable_absolute_document(self) -> None:
        """A bare relative `file://` resolves against wherever the operator is standing.

        The original commands were `file://bootstrap-trust-policy.json`, which even with the
        file present would have worked only from one directory and failed silently-wrongly from
        another, had a same-named file been there.
        """
        for step in bootstrap_plan(new_account_request()).steps:
            if step.tier is None:
                continue
            document = next(part for part in step.command if part.startswith("file://"))
            path = Path(document.removeprefix("file://"))
            assert path.is_absolute(), (
                f"{step.name} names a relative document: {document}"
            )
            assert path.is_file(), (
                f"{step.name} names a document that is not there: {path}"
            )


class TestNoRoleIsCreatedWithoutPermissions:
    """A role with a trust policy and no permissions can be assumed and can do nothing."""

    def test_every_role_tier_attaches_a_reviewed_permission_policy(self) -> None:
        for step in bootstrap_plan(new_account_request()).steps:
            if step.tier is None:
                continue
            assert step.permission_policy, (
                f"{step.name} creates a role with no permissions"
            )
            assert Path(step.permission_policy).is_file(), step.permission_policy

    def test_a_role_step_with_no_permission_policy_is_refused(self) -> None:
        """Enforced in the step's own constructor, so the gap cannot be reintroduced quietly.

        Without this, a tier added later would default to an empty permission policy and the
        test above would be the only thing standing between that and a half-built account.
        """
        with pytest.raises(BootstrapError) as refusal:
            BootstrapStep(
                name="a-new-tier",
                tier=RoleTier.WORKLOAD,
                reason="a tier added without permissions",
                command=("aws", "iam", "create-role", "--role-name", "AdpSomething"),
                read_command=("aws", "iam", "get-role", "--role-name", "AdpSomething"),
                presence=PresenceRule.REUSE_IF_PRESENT,
                scope="child account",
                denial_remediation="stated so the constructor's other checks pass",
            )

        assert "unusable" in str(refusal.value)


class TestNoTrustPolicyAdmitsMoreThanOnePrincipal:
    """The rule from `infra/workspaces/iam.tf`: a bare-account principal is not a scope."""

    @pytest.mark.parametrize("filename", TRUST_DOCUMENTS)
    def test_no_wildcard_or_bare_account_principal(self, filename: str) -> None:
        """A trust policy naming only `arn:aws:iam::<id>:root` — or an account id — lets every
        principal in that account assume the role, including roles created there later."""
        for statement in _statements(_document(filename)):
            principal = statement["Principal"]
            assert principal != "*", filename
            values: list[str] = []
            for entry in principal.values():
                values.extend([entry] if isinstance(entry, str) else list(entry))
            assert values, f"{filename} states a principal block with no principal"
            for value in values:
                assert value != "*", f"{filename} admits any principal"
                assert not value.endswith(":root"), (
                    f"{filename} admits a whole account: {value}"
                )
                assert "/" in value, (
                    f"{filename} names {value!r}, which is not a specific role or provider — "
                    f"an account-level principal is every identity in that account"
                )

    @pytest.mark.parametrize("filename", TRUST_DOCUMENTS)
    def test_every_assume_role_statement_is_further_conditioned(
        self, filename: str
    ) -> None:
        """A named role ARN alone is not enough for a cross-account trust.

        The `sts:ExternalId` condition is what stops a confused-deputy: without it, anything
        that can persuade the named management-account role to assume on its behalf reaches
        this account. The workload document conditions on the OIDC subject instead, which is
        the same property for a federated principal — it pins one service account.
        """
        for statement in _statements(_document(filename)):
            condition = statement.get("Condition")
            assert condition, f"{filename} trusts a principal with no further condition"
            keys = {key.lower() for block in condition.values() for key in block}
            pinned = any("externalid" in key or key.endswith(":sub") for key in keys)
            assert pinned, (
                f"{filename} states conditions but pins no external id or subject: {sorted(keys)}"
            )


class TestNoPermissionPolicyGrantsMoreThanItNeeds:
    """The second rule: no `Resource: "*"` this module wrote, unless the action takes none."""

    @pytest.mark.parametrize("filename", PERMISSION_DOCUMENTS)
    def test_every_statement_is_identified_and_scoped(self, filename: str) -> None:
        for statement in _statements(_document(filename)):
            assert statement.get("Sid"), f"{filename} has an unidentified statement"
            assert statement["Effect"] == "Allow", f"{filename}: {statement['Sid']}"
            if "*" not in _resources(statement):
                continue
            # A wildcard resource is permitted only where every action in the statement takes
            # no resource. Mixing one resourceless action into a statement is how a genuine
            # wildcard grant gets in wearing an accepted exception's clothes.
            unscoped = [
                action
                for action in _actions(statement)
                if action not in _RESOURCELESS_ACTIONS
            ]
            assert not unscoped, (
                f'{filename}: statement {statement["Sid"]!r} uses Resource "*" for '
                f"{unscoped}, which do take a resource"
            )

    @pytest.mark.parametrize("filename", PERMISSION_DOCUMENTS)
    def test_no_action_wildcard_and_no_service_wide_grant(self, filename: str) -> None:
        """`iam:*` is the grant that lets a role re-scope the account's own guardrails."""
        for statement in _statements(_document(filename)):
            for action in _actions(statement):
                assert action != "*", (
                    f"{filename}: {statement['Sid']} grants every action"
                )
                assert not action.endswith(":*"), (
                    f"{filename}: {statement['Sid']} grants {action}"
                )


class TestOnlyTheBootstrapTierCanWriteIam:
    """The tiering is the reason there are three roles rather than one union role."""

    @pytest.mark.parametrize(
        "filename",
        ["controller-permissions-policy.json", "workload-permissions-policy.json"],
    )
    def test_no_lower_tier_can_create_or_modify_an_identity(
        self, filename: str
    ) -> None:
        """If the controller or the workload could write IAM, the tiering would be decorative.

        `iam:PassRole` is deliberately allowed for the controller and is not an identity write:
        it hands an existing role to a service. It is asserted separately below to be scoped, so
        it cannot pass an arbitrary role.
        """
        forbidden = {
            "iam:createrole",
            "iam:putrolepolicy",
            "iam:attachrolepolicy",
            "iam:updateassumerolepolicy",
            "iam:createuser",
            "iam:createaccesskey",
            "iam:createservicelinkedrole",
        }
        for statement in _statements(_document(filename)):
            for action in _actions(statement):
                assert action.lower() not in forbidden, (
                    f"{filename}: {statement['Sid']} grants {action}"
                )

    def test_the_controller_can_pass_only_workspace_roles_and_only_to_services(
        self,
    ) -> None:
        """An unscoped `iam:PassRole` is privilege escalation: the controller could hand the
        bootstrap role — the one tier that writes IAM — to a service it controls."""
        statements = [
            s
            for s in _statements(_document("controller-permissions-policy.json"))
            if "iam:PassRole" in _actions(s)
        ]
        assert len(statements) == 1, (
            "iam:PassRole should appear once, so its scope is reviewable in one place"
        )
        statement = statements[0]
        assert "*" not in _resources(statement)
        for resource in _resources(statement):
            assert "AdpAccountBootstrap" not in resource, (
                "the controller can pass the IAM-writing role"
            )
        assert "iam:PassedToService" in str(statement.get("Condition", {})), (
            "PassRole is not restricted to a service"
        )

    def test_the_workload_cannot_read_the_accounts_baseline_controls(self) -> None:
        """The stated reason the workload is a separate tier. Tenant work that can enumerate
        the account's audit configuration knows what is and is not being recorded."""
        actions = [
            action.lower()
            for statement in _statements(_document("workload-permissions-policy.json"))
            for action in _actions(statement)
        ]
        for action in actions:
            assert not action.startswith("cloudtrail:"), action
            assert not action.startswith("organizations:"), action
            assert "accountpublicaccessblock" not in action, action

    def test_the_bootstrap_tier_can_create_only_the_roles_the_plan_names(self) -> None:
        """It writes IAM, so its resource list is the boundary on what identities can exist.

        `iam:CreateRole` scoped to `*` in this document would make the bootstrap role able to
        mint an administrator in the child account.
        """
        document = _document("bootstrap-permissions-policy.json")
        creators = [s for s in _statements(document) if "iam:CreateRole" in _actions(s)]
        assert creators, (
            "the bootstrap tier must be the thing that creates the other two"
        )
        for statement in creators:
            resources = _resources(statement)
            assert "*" not in resources, "the bootstrap role could create any identity"
            for resource in resources:
                assert resource.endswith(
                    ("AdpWorkspaceController", "AdpWorkspaceWorkload")
                ), resource

    def test_no_allow_statement_grants_attachrolepolicy_without_a_condition(
        self,
    ) -> None:
        """Every `iam:AttachRolePolicy` Allow must name which policies may be attached.

        Scoping the RESOURCE is not enough here, and that is the whole finding. The resource of
        an attach is the ROLE receiving the policy; the policy being attached is named by the
        `iam:PolicyARN` condition key and by nothing else. So an attach grant scoped to the two
        workspace roles but carrying no condition lets the bootstrap identity attach *any*
        managed policy to them — `AdministratorAccess` included — which hands the controller and
        workload tiers exactly the privileges the tiering exists to withhold.

        IAM statements are independent grants, so a second, carefully conditioned attach
        statement does not narrow an unconditioned one: the union is what applies, and the
        union of "any policy" with "these two policies" is "any policy". That is why this walks
        every Allow rather than checking that a correct statement exists somewhere.
        """
        for filename in PERMISSION_DOCUMENTS:
            document = _document(filename)
            for statement in _statements(document):
                if statement.get("Effect") != "Allow":
                    continue
                if "iam:AttachRolePolicy" not in _actions(statement):
                    continue
                condition = statement.get("Condition") or {}
                assert "iam:PolicyARN" in str(condition), (
                    f"{filename}: statement {statement.get('Sid')!r} allows "
                    f"iam:AttachRolePolicy without an iam:PolicyARN condition, so it can "
                    f"attach any managed policy — including AdministratorAccess — to "
                    f"{_resources(statement)}. A separate conditioned statement cannot "
                    f"restrict this one; IAM takes the union of Allow statements"
                )

    def test_the_service_linked_role_grant_is_pinned_to_auto_scaling(self) -> None:
        """Unscoped, `iam:CreateServiceLinkedRole` mints a role for ANY AWS service in the
        account — far larger than the one prerequisite it exists for. `bootstrap.py`'s
        `denial_remediation` explicitly tells an operator not to grant it unscoped."""
        statements = [
            s
            for s in _statements(_document("bootstrap-permissions-policy.json"))
            if "iam:CreateServiceLinkedRole" in _actions(s)
        ]
        assert len(statements) == 1
        condition = str(statements[0].get("Condition", {}))
        assert "autoscaling.amazonaws.com" in condition, (
            "the service-linked-role grant is not pinned to a service"
        )


class TestTheDocumentsCarryNoEnvironmentAndNoSecret:
    """Committed artifacts are reviewed, logged and attached to CI output."""

    @pytest.mark.parametrize("filename", TRUST_DOCUMENTS + PERMISSION_DOCUMENTS)
    def test_no_real_account_id_is_committed(self, filename: str) -> None:
        """A checked-in document carrying a real account id is silently wrong for every other
        environment, and the wrongness is a working role in the wrong organization's account.

        So account ids stay as placeholders the composer substitutes. This test is what stops a
        convenient hardcoded id from being committed the first time someone runs the plan.
        """
        import re

        text = (POLICY_DIR / filename).read_text(encoding="utf-8")
        assert "${" in text, (
            f"{filename} substitutes nothing, so it is pinned to one environment"
        )
        for match in re.findall(r"\b\d{12}\b", text):
            pytest.fail(f"{filename} carries a literal 12-digit account id: {match}")

    @pytest.mark.parametrize("filename", TRUST_DOCUMENTS + PERMISSION_DOCUMENTS)
    def test_no_credential_shaped_value(self, filename: str) -> None:
        import re

        text = (POLICY_DIR / filename).read_text(encoding="utf-8")
        assert not re.search(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", text), filename
        lowered = text.lower()
        for token in ("secret_access_key", "session_token", "private key", "password"):
            assert token not in lowered, f"{filename} names {token}"


def test_bootstrap_cannot_cross_attach_role_tiers_or_administrator_access():
    """Evaluate every Allow: a narrower statement cannot cancel a broader one."""
    import fnmatch
    import json
    from pathlib import Path

    document = json.loads(
        (
            Path(__file__).parents[1] / "policies/bootstrap-permissions-policy.json"
        ).read_text()
    )

    def values(value):
        return [value] if isinstance(value, str) else value

    def allowed(role, policy):
        for statement in document["Statement"]:
            if statement["Effect"] != "Allow":
                continue
            if not any(
                fnmatch.fnmatchcase("iam:AttachRolePolicy", pattern)
                for pattern in values(statement["Action"])
            ):
                continue
            if not any(
                fnmatch.fnmatchcase(role, pattern)
                for pattern in values(statement["Resource"])
            ):
                continue
            condition = (
                statement.get("Condition", {}).get("ArnEquals", {}).get("iam:PolicyARN")
            )
            if condition is None or policy in values(condition):
                return True
        return False

    prefix = "arn:aws:iam::${child_account_id}:"
    for tier, other in (("Controller", "Workload"), ("Workload", "Controller")):
        role = prefix + "role/AdpWorkspace" + tier
        assert allowed(role, prefix + "policy/AdpWorkspace" + tier + "Permissions")
        assert not allowed(role, prefix + "policy/AdpWorkspace" + other + "Permissions")
        assert not allowed(role, "arn:aws:iam::aws:policy/AdministratorAccess")
