"""Recording provider doubles and real-database fixtures — Issue #5531 (w6-08).

`import account_provisioning`, `import account_factory` and `import harness_jobs` all work
here because this package's `__init__.py` put their roots on `sys.path` first — see the
reasoning there, including why the harness package is available to the tests but not to the
production code.

## What the doubles are for

Every AWS client below RECORDS what it was asked to do and returns a scripted answer. That
shape is chosen so the assertions can be about the thing that actually matters on a
governed path: *which* call this code makes, with *which* arguments, in *which* order, and
what it does with each answer — including the answer it never gets. A test that asserted on
a re-implementation of the runner would establish nothing about the runner.

None of these contacts AWS. There is no network client anywhere in this file, and the
`calls` list on each double is the evidence: a test that expects no write asserts the list
is empty, which is a stronger statement than "no exception was raised".

## Synthetic identities only

Every id here is documentation-range and obviously a fixture, following
`account-factory/tests/conftest.py`: the legacy flow shipped a real account id and a real
person's email address as working defaults, and `test_no_legacy_targets.py` exists to keep
that shut. A fixture leaking into a real invocation must not be able to act on anything.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from harness_jobs.execution import CallOutcome as AuthoritativeCallOutcome
from harness_jobs.execution import ProviderCallRefused

from account_factory.modes import (
    AccountFactoryRequest,
    OwnershipMode,
    ValidationAuthorization,
)
from account_factory.recovery import StepState
from account_provisioning.placement import AccountPlacement
from account_provisioning.ports import ProviderDenied, ProviderUnavailable, RoleAbsent

# --------------------------------------------------------------------------------------
# Synthetic identities
# --------------------------------------------------------------------------------------

FIXTURE_ORG_ID = "o-testorg1234"
FIXTURE_MANAGEMENT_ACCOUNT = "000000000001"
FIXTURE_MANAGEMENT_CLUSTER = "fixture-management-cluster"
FIXTURE_WORKSPACE = "ws-fixture"
FIXTURE_REGION = "us-west-2"
FIXTURE_ORGANIZATIONAL_UNIT = "ou-test-fixture01"
FIXTURE_ROOT_ID = "r-test"
# The account id AWS "returns" for a created account, and a `car-...` request id of the
# shape Organizations actually produces.
FIXTURE_CREATED_ACCOUNT = "000000000777"
FIXTURE_REQUEST_ID = "car-fixture0000000000000000000001"

# The three scoped roles, and the ARN of each one's reviewed permission policy. A composer
# supplies these ARNs; the runner refuses to create a role without one, because a role with no
# permissions is assumable and powerless.
FIXTURE_ROLE_NAMES = ("AdpAccountBootstrap", "AdpWorkspaceController", "AdpWorkspaceWorkload")
FIXTURE_PERMISSION_ARNS = {
    "bootstrap-role": f"arn:aws:iam::{FIXTURE_CREATED_ACCOUNT}:policy/AdpAccountBootstrapPermissions",
    "controller-role": f"arn:aws:iam::{FIXTURE_CREATED_ACCOUNT}:policy/AdpWorkspaceControllerPermissions",
    "workload-role": f"arn:aws:iam::{FIXTURE_CREATED_ACCOUNT}:policy/AdpWorkspaceWorkloadPermissions",
}
FIXTURE_PERMISSION_DOCUMENTS = {step: {"Version": "2012-10-17", "Statement": []} for step in FIXTURE_PERMISSION_ARNS}
FIXTURE_TRUST_POLICIES = {step: '{"Version":"2012-10-17","Statement":[]}' for step in FIXTURE_PERMISSION_ARNS}


FIXTURE_ROLE_FOR_STEP = {
    "bootstrap-role": "AdpAccountBootstrap",
    "controller-role": "AdpWorkspaceController",
    "workload-role": "AdpWorkspaceWorkload",
}
"""Which role each role step establishes, for fixtures that are keyed by role name.

The runner reads this pairing out of the plan's own `--role-name` argument; this is only how a
fixture keyed by role name is built from documents keyed by step name.
"""


def bootstrapped_trust_documents() -> dict[str, str]:
    """Trust documents for an account whose three roles carry the REVIEWED trust policy.

    Spelled explicitly, and never defaulted inside the double, for the reason
    `fully_bootstrapped_roles` gives about attachments: the state that matters to the
    tenant-isolation finding is a role of the right name trusting the wrong party, and a fixture
    that quietly supplied the right document could not express it.
    """
    return {FIXTURE_ROLE_FOR_STEP[step]: document for step, document in FIXTURE_TRUST_POLICIES.items()}


def fully_bootstrapped_roles() -> dict[str, list[str]]:
    """Attachments for an account whose three roles are complete, not merely present.

    Spelled once because "present" and "usable" are different states and most tests mean the
    latter. A role present with no attachment is the half-built role the review found, and it
    should be constructed deliberately by a test that is about that case — never reached by
    accident because a fixture forgot the attachments.
    """
    return {
        "AdpAccountBootstrap": [FIXTURE_PERMISSION_ARNS["bootstrap-role"]],
        "AdpWorkspaceController": [FIXTURE_PERMISSION_ARNS["controller-role"]],
        "AdpWorkspaceWorkload": [FIXTURE_PERMISSION_ARNS["workload-role"]],
    }


def verified_placement(account_id: str = FIXTURE_CREATED_ACCOUNT, **overrides) -> AccountPlacement:
    """An account whose placement in the approved unit was authoritatively read.

    What `bootstrap_runner.bootstrap_account` requires before it will write account-wide roles.
    Built by hand here rather than by running `verify_placement`, so the bootstrap tests are not
    coupled to the placement path — but built through the real dataclass, so a test cannot
    accidentally satisfy the guard with a state the production read could never produce.

    The placement tests themselves never use this. They assert on what `read_placement` and
    `verify_placement` DERIVE from Organizations' answers, and a hand-made value there would be
    the test asserting its own premise.
    """
    fields = {
        "account_id": account_id,
        "organizational_unit_id": FIXTURE_ORGANIZATIONAL_UNIT,
        "state": StepState.ESTABLISHED,
        "detail": f"account {account_id} is under the approved organizational unit {FIXTURE_ORGANIZATIONAL_UNIT}",
        "actual_parent_id": FIXTURE_ORGANIZATIONAL_UNIT,
    }
    fields.update(overrides)
    return AccountPlacement(**fields)


def new_account_request(**overrides) -> AccountFactoryRequest:
    """A valid `new-account-managed` request — the only mode that creates an account."""
    fields = {
        "mode": OwnershipMode.NEW_ACCOUNT_MANAGED,
        "organization_id": FIXTURE_ORG_ID,
        "management_account_id": FIXTURE_MANAGEMENT_ACCOUNT,
        "management_cluster": FIXTURE_MANAGEMENT_CLUSTER,
        "region": FIXTURE_REGION,
        "workspace_id": FIXTURE_WORKSPACE,
        "account_email": "fixture-workspace@example.invalid",
        "organizational_unit_id": FIXTURE_ORGANIZATIONAL_UNIT,
        "vpc_cidr": "10.64.0.0/16",
        "availability_zones": (f"{FIXTURE_REGION}a", f"{FIXTURE_REGION}b"),
        "cluster_version": "1.31",
        "node_instance_type": "m6i.large",
    }
    fields.update(overrides)
    return AccountFactoryRequest(**fields)


def request_for_mode(mode: OwnershipMode, **overrides) -> AccountFactoryRequest:
    """A valid request in ANY mode, so a property can be asserted across all three.

    Each mode admits a different field set and refuses the others — `existing-account-managed`
    refuses `account_email` and `organizational_unit_id` (acting on them would re-parent an
    account that already sits somewhere in the tree), and `bring-existing-cluster` refuses the
    cluster inputs it did not create. So "take the new-account request and change the mode" does
    not produce a valid request, and a test that tried would fail on validation rather than on
    the property it was written to check.

    Mirrors `account-factory/tests/conftest.py`'s `BUILDERS`, which exists for the same reason.
    """
    common = {
        "organization_id": FIXTURE_ORG_ID,
        "management_account_id": FIXTURE_MANAGEMENT_ACCOUNT,
        "management_cluster": FIXTURE_MANAGEMENT_CLUSTER,
        "region": FIXTURE_REGION,
        "workspace_id": FIXTURE_WORKSPACE,
    }
    if mode is OwnershipMode.NEW_ACCOUNT_MANAGED:
        return new_account_request(**overrides)
    if mode is OwnershipMode.EXISTING_ACCOUNT_MANAGED:
        fields = {
            **common,
            "mode": mode,
            "target_account_id": FIXTURE_CREATED_ACCOUNT,
            "vpc_cidr": "10.65.0.0/16",
            "availability_zones": (f"{FIXTURE_REGION}a", f"{FIXTURE_REGION}b"),
            "cluster_version": "1.31",
            "node_instance_type": "m6i.large",
        }
    else:
        fields = {
            **common,
            "mode": mode,
            "target_account_id": FIXTURE_CREATED_ACCOUNT,
            "existing_cluster_name": "fixture-adopted-cluster",
        }
    fields.update(overrides)
    return AccountFactoryRequest(**fields)


def matching_authorization(request: AccountFactoryRequest, **overrides) -> ValidationAuthorization:
    """A complete authorization permitting exactly this request — the positive control.

    Derived from the request because it is a known-good control to compare refusals
    against, not the production path. Production authorization comes from
    `ValidationAuthorization.from_operation_binding`, which reads a server-resolved
    principal; a caller that could build its own from its own request would only ever
    confirm that the request agrees with itself.
    """
    fields = {
        "organization_id": request.organization_id,
        "management_account_id": request.management_account_id,
        "management_cluster": request.management_cluster,
        "permitted_modes": frozenset(OwnershipMode),
        "workspace_id": request.workspace_id,
        "permitted_target_accounts": (frozenset({request.target_account_id}) if request.target_account_id else frozenset()),
        "permitted_organizational_units": (frozenset({request.organizational_unit_id}) if request.organizational_unit_id else frozenset()),
    }
    fields.update(overrides)
    return ValidationAuthorization(**fields)


# --------------------------------------------------------------------------------------
# Recording AWS doubles
# --------------------------------------------------------------------------------------


class RecordingOrganizations:
    """An Organizations double that records calls and returns scripted answers.

    `create_account` behaviour is driven by `create_result`, which is either a response
    dict or an exception INSTANCE to raise. An exception instance rather than a flag, so a
    test can distinguish the two failure modes the whole story turns on — `ProviderDenied`
    ("AWS established nothing was created") from `ProviderUnavailable` ("no answer was
    obtained, an account may exist").
    """

    def __init__(
        self,
        *,
        create_result: object = None,
        describe_results: list[object] | None = None,
        roots: list[dict] | None = None,
        organizational_units: list[dict] | None = None,
        parents: object = None,
        move_result: object = None,
    ) -> None:
        self.create_result = create_result
        self.describe_results = list(describe_results or [])
        self._roots = roots if roots is not None else [{"Id": FIXTURE_ROOT_ID}]
        self._organizational_units = organizational_units if organizational_units is not None else [{"Id": FIXTURE_ORGANIZATIONAL_UNIT}]
        # Where the account sits, per account id. A NEW account is at the root, because that is
        # where `CreateAccount` leaves every one of them and the placement code exists for
        # exactly that fact — so the default here is the real default, not the desired one. A
        # test that wants a correctly placed account says so.
        self._parents = parents
        self._move_result = move_result
        # Every call, in order. The evidence a test asserts on.
        self.calls: list[tuple[str, dict]] = []

    @property
    def create_calls(self) -> list[dict]:
        """Just the `CreateAccount` calls. More than one is a duplicate account."""
        return [kwargs for name, kwargs in self.calls if name == "create_account"]

    @property
    def move_calls(self) -> list[dict]:
        """Just the `MoveAccount` calls. The evidence for what was actually placed where."""
        return [kwargs for name, kwargs in self.calls if name == "move_account"]

    def create_account(self, **kwargs) -> dict:
        self.calls.append(("create_account", kwargs))
        result = self.create_result
        if isinstance(result, Exception):
            raise result
        if result is None:
            return in_progress_response()
        return result

    def describe_create_account_status(self, **kwargs) -> dict:
        self.calls.append(("describe_create_account_status", kwargs))
        if not self.describe_results:
            raise AssertionError("describe_create_account_status was called with no scripted answer left")
        result = self.describe_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def list_roots(self) -> dict:
        self.calls.append(("list_roots", {}))
        return {"Roots": list(self._roots)}

    def list_organizational_units_for_parent(self, **kwargs) -> dict:
        self.calls.append(("list_organizational_units_for_parent", kwargs))
        return {"OrganizationalUnits": list(self._organizational_units)}

    def list_parents(self, **kwargs) -> dict:
        """Where an account is, as botocore shapes it: `{"Parents": [{"Id", "Type"}]}`.

        `parents` may be an exception instance to raise (so a test can drive the denied and
        unobtained reads separately), a list of responses consumed in order (so a test can make
        the read before a move differ from the read after it — which is the only way to express
        a move that landed, or one that reported success and did not), or a single response
        returned every time.
        """
        self.calls.append(("list_parents", kwargs))
        answer = self._parents
        if answer is None:
            # The honest default for a freshly created account.
            return {"Parents": [{"Id": FIXTURE_ROOT_ID, "Type": "ROOT"}]}
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, list):
            if not answer:
                raise AssertionError("list_parents was called with no scripted answer left")
            # Popped, so a sequence describes successive reads rather than being re-served.
            answer = answer.pop(0)
            if isinstance(answer, Exception):
                raise answer
        return answer

    def move_account(self, **kwargs) -> dict:
        self.calls.append(("move_account", kwargs))
        result = self._move_result
        if isinstance(result, Exception):
            raise result
        return result if result is not None else {}


class RecordingIam:
    """An IAM double recording reads and writes separately.

    `get_role` answers from `present_roles`, raising `ProviderDenied` for an absent one —
    which is what the real client does, and the reason the runner has to distinguish
    "absent" from "not permitted to look". `denied_reads` forces the second case for a role
    whose presence is genuinely unknown.

    Attachments are tracked separately from presence, which is what makes the half-built role
    expressible: `present_roles={"AdpWorkspaceController"}` with no entry in `attached_policies`
    is exactly the state a bootstrap interrupted between `create_role` and `attach_role_policy`
    leaves behind. A double that could not represent it could not test recovery from it.

    Trust documents are tracked separately for the same reason, and they are what makes the
    wrong-role case expressible at all. `get_role` returns `AssumeRolePolicyDocument` — the real
    client always does, and the runner now compares it against the reviewed document, so a
    double that omitted it would put every existing role permanently in "the trust was not
    established" and no test could tell a matching role from a differing one.

    `trust_documents` is NOT defaulted to the reviewed document for a pre-existing role, for the
    reason `fully_bootstrapped_roles` gives about attachments: a fixture that silently supplies
    the right answer is a fixture that cannot express the wrong one, and the wrong one is the
    finding. A role in `present_roles` with no entry here models a `get_role` that returned no
    document, which the runner treats as unverified. `bootstrapped_trust_documents()` is the
    explicit way to say "this account's roles are the reviewed ones".
    """

    def __init__(
        self,
        *,
        present_roles: set[str] | None = None,
        denied_reads: set[str] | None = None,
        unavailable_reads: set[str] | None = None,
        create_result: object = None,
        attached_policies: dict[str, list[str]] | None = None,
        trust_documents: dict[str, str] | None = None,
        denied_attachment_reads: set[str] | None = None,
        unavailable_attachment_reads: set[str] | None = None,
        attach_result: object = None,
    ) -> None:
        self.permission_documents = {arn: FIXTURE_PERMISSION_DOCUMENTS[step] for step, arn in FIXTURE_PERMISSION_ARNS.items()}
        self.present_roles = set(present_roles or ())
        self.denied_reads = set(denied_reads or ())
        self.unavailable_reads = set(unavailable_reads or ())
        self.create_result = create_result
        self.attached_policies = {role: list(arns) for role, arns in (attached_policies or {}).items()}
        self.trust_documents = dict(trust_documents or {})
        self.denied_attachment_reads = set(denied_attachment_reads or ())
        self.unavailable_attachment_reads = set(unavailable_attachment_reads or ())
        # Separate from `create_result` so a test can make the ATTACH fail while the create
        # succeeds — the sequence that produces a half-built role in the first place.
        self.attach_result = attach_result
        self.calls: list[tuple[str, dict]] = []

    # Named rather than inlined below, so a read added to the port cannot be silently counted
    # as a write — which would make every "no write happened" assertion fail confusingly.
    READ_CALLS = frozenset({"get_role", "list_attached_role_policies", "get_policy", "get_policy_version"})

    @property
    def writes(self) -> list[tuple[str, dict]]:
        """Only the calls that change the account. A read-only pass leaves this empty."""
        return [(name, kwargs) for name, kwargs in self.calls if name not in self.READ_CALLS]

    def get_policy(self, *, PolicyArn):
        self.calls.append(("get_policy", {"PolicyArn": PolicyArn}))
        if PolicyArn not in self.permission_documents:
            from account_provisioning.ports import PolicyAbsent

            raise PolicyAbsent("policy absent")
        return {"Policy": {"Arn": PolicyArn, "DefaultVersionId": "v1"}}

    def get_policy_version(self, *, PolicyArn, VersionId):
        self.calls.append(("get_policy_version", {"PolicyArn": PolicyArn, "VersionId": VersionId}))
        return {"PolicyVersion": {"Document": self.permission_documents[PolicyArn]}}

    def create_policy(self, *, PolicyName, Path, PolicyDocument):
        import json

        arn = f"arn:aws:iam::{FIXTURE_CREATED_ACCOUNT}:policy{Path}{PolicyName}"
        self.calls.append(("create_policy", {"PolicyName": PolicyName, "Path": Path, "PolicyDocument": PolicyDocument}))
        self.permission_documents[arn] = json.loads(PolicyDocument)
        return {"Policy": {"Arn": arn}}

    def create_policy_version(self, *, PolicyArn, PolicyDocument, SetAsDefault):
        import json

        self.calls.append(("create_policy_version", {"PolicyArn": PolicyArn, "SetAsDefault": SetAsDefault}))
        self.permission_documents[PolicyArn] = json.loads(PolicyDocument)
        return {"PolicyVersion": {"VersionId": "v2"}}

    def get_role(self, *, RoleName: str) -> dict:  # noqa: N803
        self.calls.append(("get_role", {"RoleName": RoleName}))
        if RoleName in self.unavailable_reads:
            raise ProviderUnavailable(f"the read for {RoleName} did not return an answer")
        if RoleName in self.denied_reads:
            raise ProviderDenied(f"not permitted to read {RoleName}")
        if RoleName in self.present_roles:
            role = {"RoleName": RoleName, "Arn": f"arn:aws:iam::{FIXTURE_CREATED_ACCOUNT}:role/{RoleName}"}
            document = self.trust_documents.get(RoleName)
            if document is not None:
                # Only when one is recorded. Botocore always returns this field for a real role;
                # omitting it models the port implementation that does not, which the runner
                # must treat as "not established" rather than as a match.
                role["AssumeRolePolicyDocument"] = document
            return {"Role": role}
        # `RoleAbsent`, not the base `ProviderDenied`. This double raised the base class, which
        # is what a port written to the old contract would have done — and it is the one answer
        # under which nothing is ever created, so every role step on a genuinely empty account
        # reported DENIED and bootstrap could not bootstrap. The double raising what the port
        # now requires is what makes the create paths below reachable at all.
        raise RoleAbsent(f"NoSuchEntity: {RoleName}")

    def create_service_linked_role(self, **kwargs) -> dict:
        self.calls.append(("create_service_linked_role", kwargs))
        if isinstance(self.create_result, Exception):
            raise self.create_result
        # A created role becomes present, so a re-read in the same test sees it. This is
        # what makes the restart/idempotence tests meaningful.
        self.present_roles.add("AWSServiceRoleForAutoScaling")
        return {"Role": {"RoleName": "AWSServiceRoleForAutoScaling"}}

    def create_role(self, **kwargs) -> dict:
        self.calls.append(("create_role", kwargs))
        if isinstance(self.create_result, Exception):
            raise self.create_result
        self.present_roles.add(kwargs["RoleName"])
        # The document this create was given becomes the role's own, so a re-read in the same
        # test sees what was actually created. Without this a role this bootstrap just made
        # would read back as having no trust document — and the re-read paths, which exist
        # precisely to settle a create whose answer was lost, could never establish anything.
        self.trust_documents[kwargs["RoleName"]] = kwargs["AssumeRolePolicyDocument"]
        return {"Role": {"RoleName": kwargs["RoleName"]}}

    def list_attached_role_policies(self, *, RoleName: str) -> dict:  # noqa: N803
        self.calls.append(("list_attached_role_policies", {"RoleName": RoleName}))
        if RoleName in self.unavailable_attachment_reads:
            raise ProviderUnavailable(f"the attachment read for {RoleName} did not return an answer")
        if RoleName in self.denied_attachment_reads:
            raise ProviderDenied(f"not permitted to list policies attached to {RoleName}")
        arns = self.attached_policies.get(RoleName, [])
        return {"AttachedPolicies": [{"PolicyArn": arn, "PolicyName": arn.rsplit("/", 1)[-1]} for arn in arns]}

    def attach_role_policy(self, **kwargs) -> dict:
        self.calls.append(("attach_role_policy", kwargs))
        if isinstance(self.attach_result, Exception):
            raise self.attach_result
        # An attached policy becomes visible to a re-read, which is what makes the
        # partial-failure recovery tests meaningful rather than circular.
        self.attached_policies.setdefault(kwargs["RoleName"], []).append(kwargs["PolicyArn"])
        return {}


class RecordingCredentials:
    """A credential source recording which operation each credential was obtained for.

    The binding is the point: both methods take an operation id, so a test can assert that
    a credential was never obtained outside one, and that it was obtained for the SAME
    operation the executor holds. `deny`/`unavailable` make the two failure modes testable
    separately — for bootstrap in particular, failing to reach the child account is
    `bootstrap failed`, never `creation failed`, because the account exists either way.
    """

    def __init__(
        self,
        *,
        organizations: RecordingOrganizations | None = None,
        iam: RecordingIam | None = None,
        deny: bool = False,
        unavailable: bool = False,
    ) -> None:
        self._organizations = organizations or RecordingOrganizations()
        self._iam = iam or RecordingIam()
        self.deny = deny
        self.unavailable = unavailable
        self.management_calls: list[str] = []
        self.child_calls: list[tuple[str, str]] = []

    def _clients(self) -> object:
        organizations, iam = self._organizations, self._iam

        class _Credentials:
            @property
            def organizations(self):
                return organizations

            @property
            def iam(self):
                return iam

        return _Credentials()

    def _guard(self, what: str) -> None:
        if self.deny:
            raise ProviderDenied(f"this operation may not act on {what}")
        if self.unavailable:
            raise ProviderUnavailable(f"authority for {what} could not be established")

    async def management(self, *, operation_id: str):
        self.management_calls.append(operation_id)
        self._guard("the management account")
        return self._clients()

    async def child_account(self, *, operation_id: str, account_id: str):
        self.child_calls.append((operation_id, account_id))
        self._guard(f"account {account_id}")
        return self._clients()


class RecordingExecutor:
    """A durable-executor double that records dispatches and never performs one.

    Structurally the `execution.DurableExecutor` Protocol. The lease identity
    (`operation_id`, `workspace_id`, `org_id`) is constructor-supplied because that is
    exactly what the authorization guards compare against: in production those three come
    from the admitted operation's lease and nothing in a request body can influence them, so
    a test that could not set them independently of the request could not distinguish
    "checked against the lease" from "checked against itself".

    `dispatches` is the evidence. A guard that refuses must leave it EMPTY — an assertion
    that no provider call was made, which is stronger than "an exception was raised", since
    an exception raised *after* `CreateAccount` would still have opened an account.
    """

    def __init__(
        self,
        *,
        operation_id: str = "op-fixture",
        workspace_id: str = FIXTURE_WORKSPACE,
        org_id: str = FIXTURE_ORG_ID,
        outcome: object = None,
        provider_ref: str | None = None,
        refuse_with: BaseException | None = None,
    ) -> None:
        self.operation_id = operation_id
        self.workspace_id = workspace_id
        self.org_id = org_id
        self._outcome = outcome
        self._provider_ref = provider_ref
        self._refuse_with = refuse_with
        self.dispatches: list[dict] = []
        self.intents: list[dict] = []

    async def provider_calls(self, *, provider, operation_kind):
        # These unit doubles model no interrupted baseline history. Real recovery
        # is exercised against PostgreSQL in test_baseline_controls.py.
        return ()

    async def execute_provider(self, **kwargs):
        self.dispatches.append(kwargs)
        if self._refuse_with is not None:
            raise self._refuse_with
        return SettledCall(kwargs["idempotency_key"], self._outcome, self._provider_ref, kwargs["target"]), None

    async def record_intent(self, **kwargs):
        self.intents.append(kwargs)
        return SettledCall(kwargs["idempotency_key"], self._outcome, self._provider_ref, kwargs["target"])


class HookExecutor:
    """A durable-executor double that actually INVOKES the composed provider hook.

    `RecordingExecutor` scripts an outcome and never calls anything, which is right for the
    guard tests: they assert that nothing reached a provider. It is the wrong shape for the
    bootstrap-write tests, because the behaviour under test is the hook's — which IAM call it
    makes for which dispatch, and what outcome it derives from what AWS answered. A scripted
    outcome would be the test asserting its own premise.

    So this reproduces the three things `harness_jobs.execution.OperationExecutor` does that
    this package's correctness depends on, and nothing else:

    1. **Intent is committed under the key before the hook runs** (`recorded`, in order).
    2. **A key that already has a row is refused** — `_record(..., fresh=True)` raises
       `ProviderCallRefused`. This is what stops a restarted or concurrent worker from
       repeating an effect, and it is why the attach needs a key of its own: sharing the
       create's key would make the attach permanently undispatchable.
    3. **A hook that raises becomes `UNKNOWN`, never `FAILED`** — the executor swallows the
       exception precisely so an exploded hook cannot be read as "nothing happened".

    It deliberately does NOT reproduce the advisory lock, the fence token or the UNIQUE
    constraint. Those are properties of PostgreSQL and are asserted against a real server in
    the real-database suite; a Python dict imitating them would prove nothing about either.
    """

    def __init__(
        self,
        *,
        hook,
        operation_id: str = "op-fixture",
        workspace_id: str = FIXTURE_WORKSPACE,
        org_id: str = FIXTURE_ORG_ID,
        already_recorded: tuple[str, ...] = (),
    ) -> None:
        self.operation_id = operation_id
        self.workspace_id = workspace_id
        self.org_id = org_id
        self._hook = hook
        # Keys a PREVIOUS pass committed intent under. The state a restart actually finds, and
        # the only way to test the refusal path without running two executors.
        self.recorded: list[str] = list(already_recorded)
        self.dispatches: list[dict] = []
        self.refused: list[str] = []

    async def provider_calls(self, *, provider, operation_kind):
        # These unit doubles model no interrupted baseline history. Real recovery
        # is exercised against PostgreSQL in test_baseline_controls.py.
        return ()

    async def execute_provider(self, **kwargs):
        key = kwargs["idempotency_key"]
        if key in self.recorded:
            self.refused.append(key)
            raise ProviderCallRefused(f"Provider intent already exists for {key}; reconcile instead of repeating the call")
        # Committed BEFORE the hook, so a hook that raises still leaves the key consumed —
        # which is what makes the crash recoverable rather than invisible.
        self.recorded.append(key)
        self.dispatches.append(kwargs)
        # The record the hook is handed carries `operation_kind` — which is how one composed
        # hook tells "create this role" from "finish the role an earlier pass left half-built"
        # — and `operation_id`, which is what it obtains the child credential for. Both come
        # from the dispatch, never from the hook's own closure.
        call = SettledCall(
            key,
            None,
            None,
            kwargs["target"],
            operation_id=self.operation_id,
            operation_kind=kwargs["operation_kind"],
        )
        try:
            outcome, detail, provider_ref = await self._hook(call)
        except Exception as exc:  # noqa: BLE001 - mirrors the executor's own blanket catch
            return SettledCall(key, AuthoritativeCallOutcome.UNKNOWN, None, kwargs["target"], detail=f"hook raised: {exc}"), None
        return SettledCall(key, outcome, provider_ref, kwargs["target"], detail=detail), None

    async def record_intent(self, **kwargs):
        key = kwargs["idempotency_key"]
        if key not in self.recorded:
            self.recorded.append(key)
        return SettledCall(key, None, None, kwargs["target"])


class RecordingReconciliationStore:
    """A `execution.ReconciliationStore` double that records settlements and performs none.

    The offline half of the recovery tests. It answers the question "what would have been
    WRITTEN, under which key, with which outcome and which reference" — which is exactly the
    question the unpersisted version of `reconcile_creation` could not be asked, because it
    wrote nothing at all.

    `refuse_with` is an exception INSTANCE, so a test can drive the already-settled race the
    real store produces (`ProviderCallRefused`) without a database. That refusal is a normal
    outcome on this path, not an error, and the distinction is what these doubles exist for:
    the row's existing state standing is correct, and silently reporting it as settled by this
    pass would not be.

    The real store's behaviour under concurrency, the `stage='intended'` predicate that makes
    the refusal atomic, and the durability of what it wrote are properties of PostgreSQL and
    are asserted in `test_durability_postgres.py`. This double imitates none of them.
    """

    def __init__(self, *, refuse_with: BaseException | None = None) -> None:
        self._refuse_with = refuse_with
        self.settlements: list[dict] = []

    async def reconcile(self, **kwargs):
        self.settlements.append(kwargs)
        if self._refuse_with is not None:
            raise self._refuse_with
        return SettledCall(
            kwargs["idempotency_key"],
            kwargs["outcome"],
            kwargs.get("provider_ref"),
            detail=kwargs.get("detail"),
        )


class SettledCall:
    """A durable row as this package reads it — `execution.ProviderCallRecord`.

    `target` carries the store's immutable binding. The real store refuses to re-record a key
    whose target differs from the stored row, which is what `creation_runner.payload_digest`
    relies on; a double that omitted the field would let the payload-fence tests pass while
    the production check had nothing to compare against.
    """

    def __init__(
        self,
        idempotency_key: str,
        outcome: object,
        provider_ref: str | None,
        target: str = "",
        *,
        detail: str | None = None,
        operation_id: str = "op-fixture",
        operation_kind: str = "",
    ) -> None:
        self.idempotency_key = idempotency_key
        self.outcome = outcome
        self.provider_ref = provider_ref
        self.target = target
        # `detail` is what the hook said. Not part of the `ProviderCallRecord` Protocol this
        # package reads, but carried so a test can assert on the SENTENCE an operator will see
        # in the durable row — which for the half-built-role case is the only place the "the
        # role exists, do not create it again" instruction is recorded.
        self.detail = detail
        # Read by `bootstrap_hook` off the record it is handed, to obtain the child credential
        # for the operation the call belongs to.
        self.operation_id = operation_id
        self.operation_kind = operation_kind


def in_progress_response(request_id: str = FIXTURE_REQUEST_ID) -> dict:
    """The normal first reply to `CreateAccount`: a request id, and no account yet."""
    return {"CreateAccountStatus": {"Id": request_id, "State": "IN_PROGRESS"}}


def succeeded_response(
    request_id: str = FIXTURE_REQUEST_ID,
    account_id: str = FIXTURE_CREATED_ACCOUNT,
) -> dict:
    return {"CreateAccountStatus": {"Id": request_id, "State": "SUCCEEDED", "AccountId": account_id}}


def failed_response(reason: str = "EMAIL_ALREADY_EXISTS", request_id: str = FIXTURE_REQUEST_ID) -> dict:
    """A reply establishing that nothing was created."""
    return {"CreateAccountStatus": {"Id": request_id, "State": "FAILED", "FailureReason": reason}}


# --------------------------------------------------------------------------------------
# Real-database fixtures
# --------------------------------------------------------------------------------------
#
# The durability and duplicate-fencing claims are properties of PostgreSQL, not of Python:
# "no second account under concurrency" is a UNIQUE constraint firing, and "the record
# survived the crash" is a claim about what is on disk. A fake or SQLite would pass a suite
# proving none of that. So: a real server, or a skip that says why.
#
# Sources, in order, matching `modules/harness/jobs/tests/conftest.py`:
#   1. ACCOUNT_PROVISIONING_TEST_POSTGRES_URL / HARNESS_JOBS_TEST_POSTGRES_URL
#   2. `pgserver` (wheels for Python <= 3.12)
#   3. `embedded_postgres` (Python 3.13+)
#
# The third is why this suite runs on this container at all: pgserver has no 3.13 wheel,
# and an earlier run on this branch concluded from that alone that a real database was
# unobtainable here and deferred the evidence. It was obtainable.

ENV_VAR = "ACCOUNT_PROVISIONING_TEST_POSTGRES_URL"
SHARED_ENV_VAR = "HARNESS_JOBS_TEST_POSTGRES_URL"
REQUIRE_VAR = "ACCOUNT_PROVISIONING_REQUIRE_POSTGRES"

_UNAVAILABLE = (
    f"requires a disposable PostgreSQL database: set {ENV_VAR} or {SHARED_ENV_VAR}, or "
    "install `pgserver` (Python <= 3.12) or `embedded-postgres` (Python 3.13+). These "
    "tests assert constraint, transaction and lock behaviour that only a real database "
    "has, so there is nothing to fall back to."
)


def _external_url() -> str | None:
    return os.environ.get(ENV_VAR) or os.environ.get(SHARED_ENV_VAR)


def _database_is_obtainable() -> bool:
    if _external_url():
        return True
    if importlib.util.find_spec("pgserver") is not None:
        return True
    return importlib.util.find_spec("embedded_postgres") is not None


# A skip is the honest answer for a developer with no database. It is the WRONG answer for
# CI: "12 skipped" is green, and a lane reporting green while asserting none of the
# durability guarantees is worse than no lane. Setting the require-var turns the skip into
# a failure, and the CI lane sets it.
requires_postgres = pytest.mark.skipif(
    not _database_is_obtainable() and not os.environ.get(REQUIRE_VAR),
    reason=_UNAVAILABLE,
)

_RESOLVED_URL: str | None = None


@pytest.fixture(scope="session")
def postgres_server(tmp_path_factory) -> AsyncIterator[str]:
    """A base DSN for a real server. Session-scoped; isolation is a fresh schema per test."""
    global _RESOLVED_URL
    external = _external_url()
    if external:
        _RESOLVED_URL = external.replace("postgresql+asyncpg://", "postgresql://")
        yield _RESOLVED_URL
        return

    if importlib.util.find_spec("pgserver") is not None:
        import pgserver

        server = pgserver.get_server(str(tmp_path_factory.mktemp("account-provisioning-pgdata")))
        try:
            _RESOLVED_URL = server.get_uri()
            yield _RESOLVED_URL
        finally:
            _RESOLVED_URL = None
            server.cleanup()
        return

    if importlib.util.find_spec("embedded_postgres") is not None:
        from pathlib import Path

        from embedded_postgres.postgres_server import PostgresServer

        server = PostgresServer(Path(tmp_path_factory.mktemp("account-provisioning-pgdata-ep")))
        server.ensure_pgdata_inited()
        server.ensure_postgres_running()
        try:
            _RESOLVED_URL = server.get_uri()
            yield _RESOLVED_URL
        finally:
            _RESOLVED_URL = None
            server.cleanup()
        return

    if os.environ.get(REQUIRE_VAR):
        raise AssertionError(f"{REQUIRE_VAR} is set, so this suite must not skip. {_UNAVAILABLE}")
    pytest.skip(_UNAVAILABLE)


def postgres_url() -> str:
    """The resolved DSN. Also called from test bodies that open a second connection."""
    if _RESOLVED_URL:
        return _RESOLVED_URL
    url = _external_url()
    if not url:
        pytest.skip(_UNAVAILABLE)
    return url.replace("postgresql+asyncpg://", "postgresql://")


@pytest_asyncio.fixture
async def pool(postgres_server: str) -> AsyncIterator[object]:
    """An asyncpg pool on a fresh schema with the shared store's DDL applied.

    A pool rather than one connection because the concurrency tests need genuinely separate
    connections: two coroutines sharing one connection are serialized by the driver, so a
    "concurrent duplicate create" test on a single connection would prove nothing about the
    constraint that actually does the fencing.
    """
    asyncpg = pytest.importorskip("asyncpg", reason="asyncpg is required for the real-database suite")
    from harness_jobs import apply

    schema = "account_provisioning_test_" + uuid.uuid4().hex
    url = postgres_url()

    admin = await asyncpg.connect(url)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await admin.close()

    created = await asyncpg.create_pool(
        url,
        min_size=1,
        max_size=10,
        server_settings={"search_path": schema, "statement_timeout": "15000"},
    )
    assert created is not None
    async with created.acquire() as connection:
        await apply(connection)
    try:
        yield created
    finally:
        await created.close()
        admin = await asyncpg.connect(url)
        try:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await admin.close()


@asynccontextmanager
async def _noop():  # pragma: no cover - replaced per test
    yield None
