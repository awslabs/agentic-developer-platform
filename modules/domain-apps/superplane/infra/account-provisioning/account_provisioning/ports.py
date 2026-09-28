"""The provider and credential surfaces this package calls — Issue #5531 (w6-08).

`account-factory/` decides whether an account may be created and describes what bootstrap
must establish. Nothing in it can act, and that is enforced: its
`tests/test_no_legacy_targets.py` fails if any `.py` file under that directory so much as
names `boto3`. This package is the half that acts, which is why it is a sibling directory
rather than a module inside that one — keeping the guard there at full strength.

## Why ports rather than clients

Every entry point here is handed the surfaces below. This package constructs no boto3
client, reads no profile or region, and holds no credential, for the reason
`harness_jobs.execution` gives for the same choice: a module that could build its own
client is the module a credential ends up in. Credential authorization and trusted
delivery are #5528's (w6-05), and composition is the deployment's.

The practical payoff is that the offline tests drive the *real* entry points. A test
supplies a recording stub, so the assertions are about what this package would do to AWS
— which call, with which arguments, in which order, and what it does with each answer —
rather than about a re-implementation of it.

## Why these are Protocols and deliberately narrow

Structural typing, as `superplane_contracts.delivery.TrustedDeliveryChannel` uses: the
real implementation is composed elsewhere, and a test double provides exactly this surface
and none of the conveniences a full SDK client would carry.

The surface is the smallest that the accepted design needs. `OrganizationsClient` can
create an account, ask about a creation request, list roots/OUs, move an account between
parents and read the parent an account is actually under — it cannot close an account or
delete an OU. That is not an oversight to be filled in later: an absent method cannot be
called by mistake, and account closure in particular is an irreversible 90-day suspension
that `account_factory.cleanup` refuses every implicit route to. Widening any of these is a
reviewable change to this file.

## Why `move_account` and `list_parents` were added, having been deliberately absent

This file used to say `OrganizationsClient` "cannot close an account, move one, or delete an
OU", and the absence of the move was load-bearing in the same way the absence of closure is.
It was also wrong, and the review that caught it is worth recording here because the shape of
the mistake is the interesting part.

`CreateAccount` has **no placement parameter**. A new account always lands at the organization
root, and the root is the least restricted position in the tree — outside every service control
policy the approved OU exists to impose. So while the request carried an
`organizational_unit_id`, and `_confirm_placement` verified that the OU existed, nothing ever
put the account in it. The configuration claimed placement, the validation confirmed the
target, and the account was born outside all of it. A narrow port did not prevent an unsafe
effect there; it prevented the *safe* one, and left the unsafe state as the default.

The two methods are therefore the minimum that closes it, and they are split read from write on
purpose:

* `move_account` is the only write, it is dispatched under its own durable fence, and it can
  only move an account BETWEEN two parents that the caller must name — there is no "detach".
* `list_parents` is the authoritative read. Placement is established by AWS saying the account
  is under the approved OU, never by a move call having returned, for the same reason
  `IamClient.get_role` exists: an effect that was requested is not an effect that landed.

What is still deliberately absent: `delete_organizational_unit`, `close_account`,
`detach_policy`, and anything that could alter an SCP. Moving an account between two named
parents cannot remove a control from the organization; it can only change which account is
subject to which. That is the narrowest widening that lets placement be both performed and
verified.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = [
    "AccountCredentials",
    "CredentialSource",
    "IamClient",
    "OrganizationsClient",
    "ProviderDenied",
    "ProviderUnavailable",
    "RoleAbsent",
]


class ProviderDenied(Exception):
    """The provider refused, and the refusal is authoritative.

    Raised for a definite "no": a validation error, or an authorization failure on a read
    this package needs. A caller may treat this as established fact.

    Deliberately NOT raised when the answer was not obtained — see `ProviderUnavailable`.
    Collapsing the two is the defect this wave exists to prevent, because "AWS told me no"
    and "I could not reach AWS" license opposite next moves.
    """


class RoleAbsent(ProviderDenied):
    """The provider positively established that a role does not exist.

    Defined here, on the port, because it is the port implementation's job to raise it: only
    the adapter holding the real client can see AWS's `NoSuchEntity` error code, and only that
    code means "absent". `bootstrap_runner` re-exports it for the callers that already import
    it from there.

    A subclass of `ProviderDenied` deliberately, and this is the repair for a real gap. The
    two are raised by the same call for reasons that license opposite next moves, and an
    adapter written to this file's documented contract — "raises `ProviderDenied` when absent
    or unreadable" — would have raised the base class for an absent role. `_read_role` matches
    `RoleAbsent` first, so on a genuinely fresh account every role read would have come back
    `DENIED`, every step would have stopped with a permissions remediation, and bootstrap could
    never have created the roles it exists to create. Subclassing means the specific exception
    also satisfies every `except ProviderDenied` a port implementation or caller already has,
    so narrowing the contract cannot silently drop an absence on the floor.

    The inheritance direction matters and is the safe one: an adapter that raises the BASE class
    for an absent role is read as "not permitted to look", which refuses to create. An adapter
    that raised this for an authorization failure would turn "I cannot see it" into "it is not
    there" — which is how bootstrap comes to create a role that already exists — so that
    direction is the one no port may take, and `IamClient.get_role` says so below.
    """


class PolicyAbsent(ProviderDenied):
    """Only authoritative NoSuchEntity/NoSuchPublicAccessBlockConfiguration."""


class ProviderUnavailable(Exception):
    """The provider's answer was not obtained, so nothing was established.

    A timeout, a dropped connection, a throttle that exhausted its retries. After this,
    the effect may or may not have happened, and the only safe reading is that it might
    have. `runner.py` maps this onto `harness_jobs`' `CallOutcome.UNKNOWN`, which retains
    budget and refuses an automatic retry, rather than onto `FAILED`.
    """


class AccountCredentials(Protocol):
    """Credentials for one account, as an already-constructed set of clients.

    A Protocol over *clients* rather than over keys or a session: this package never holds
    credential material, so there is nothing here that could be logged, echoed or written
    to a file. Whoever composes this resolves an operation-bound credential through
    #5528's (w6-05) trusted delivery and hands back the clients it built.
    """

    @property
    def organizations(self) -> OrganizationsClient:
        """Organizations in the management account. Used only for creation and its status."""

    @property
    def iam(self) -> IamClient:
        """IAM in whichever account this credential is for."""

    @property
    def s3control(self) -> S3ControlClient:
        """Account-level public-access protections in the bound child account."""

    @property
    def cloudtrail(self) -> CloudTrailClient:
        """Authoritative audit-coverage observations for the bound child account."""


class CredentialSource(Protocol):
    """Where per-account, operation-bound credentials come from.

    Both methods take the operation the work is bound to, so a credential cannot be
    obtained outside one. `superplane_contracts.delivery.RunBinding` is the contract for
    that binding on the delivery side; the composer is what connects the two.
    """

    async def management(self, *, operation_id: str) -> AccountCredentials:
        """Credentials for the organization's management account.

        Raises `ProviderDenied` when this operation may not act on the management account,
        and `ProviderUnavailable` when authority could not be established at all.
        """

    async def child_account(self, *, operation_id: str, account_id: str) -> AccountCredentials:
        """Credentials INSIDE the child account, for bootstrap.

        A fresh account is reachable only through the organization's account-access role,
        so obtaining these is itself a step that can fail — and a failure here is
        `bootstrap failed`, never `creation failed`. The account exists either way.
        """


@runtime_checkable
class OrganizationsClient(Protocol):
    """The Organizations calls the accepted design needs, and no others.

    Method names and the shapes of returned data follow botocore's, so a real boto3
    client satisfies this Protocol with no adapter. The returned dicts are botocore
    response shapes; `runner.py` reads only the documented keys it needs from them.
    """

    def create_account(
        self,
        *,
        Email: str,
        AccountName: str,
        IamUserAccessToBilling: str = ...,
        Tags: list[dict[str, str]] = ...,
    ) -> dict:
        """Ask for one new account. Returns `{"CreateAccountStatus": {...}}`.

        Asynchronous: the reply carries an `Id` (a `car-...` request id) and a `State` of
        `IN_PROGRESS`, not an account. The account id arrives later, from
        `describe_create_account_status`. That gap is the whole reason intent is committed
        to the database before this is called.
        """

    def describe_create_account_status(self, *, CreateAccountRequestId: str) -> dict:
        """The current state of one creation request, by its `car-...` id.

        This is the reconciliation call: after a lost reply, the stored request id is what
        lets a later pass ask about *that* request rather than start a new one.
        """

    def list_roots(self, *, NextToken: str = ...) -> dict:
        """The organization's roots, used to validate the requested OU placement.

        Present because an unvalidated placement silently lands the account at the
        organization root — the least restricted position in the tree.
        """

    def list_organizational_units_for_parent(self, *, ParentId: str, NextToken: str = ...) -> dict:
        """The OUs under one parent, used to confirm the requested OU actually exists."""

    def list_parents(self, *, ChildId: str) -> dict:
        """Where the account ACTUALLY is. Returns `{"Parents": [{"Id": ..., "Type": ...}]}`.

        The authoritative placement read, and the reason placement is not considered done when
        `move_account` returns. An account has exactly one parent in Organizations, so a
        response with anything other than one entry is not a placement this code will vouch
        for — `Type` is `"ROOT"` or `"ORGANIZATIONAL_UNIT"`, and the root is precisely the
        position that means "no OU controls apply".

        Raises `ProviderDenied` when the read is refused and `ProviderUnavailable` when no
        answer was obtained. The distinction matters here as much as anywhere: "I could not
        read the parent" must never settle as "the parent is the approved one".
        """

    def move_account(self, *, AccountId: str, SourceParentId: str, DestinationParentId: str) -> dict:
        """Move one account from one parent to another. The only write besides creation.

        Both parents are required by AWS and are required here. `SourceParentId` is read from
        `list_parents` immediately before, not assumed to be the root: a move that names the
        wrong source fails rather than silently acting on an account that something else has
        already placed.

        Not idempotent in the way a create-if-absent is: calling it when the account is
        already at the destination is an error from AWS, so the caller reads first and moves
        only from a parent that is not the destination.
        """


@runtime_checkable
class IamClient(Protocol):
    """The IAM calls bootstrap needs, split so that reads cannot be mistaken for writes."""

    def get_role(self, *, RoleName: str) -> dict:
        """Read one role. Raises `RoleAbsent` when absent, `ProviderDenied` when unreadable.

        The read that makes bootstrap idempotent, and the one place where the port's
        obligation is more than forwarding an error. An implementation MUST raise `RoleAbsent`
        — and only that — for AWS's `NoSuchEntity`, because that is the single condition under
        which anything here creates a role.

        Raising the base `ProviderDenied` for an absent role is a safe mistake: it reads as
        "not permitted to look", so nothing is created and the step reports a remediation an
        operator can act on. Raising `RoleAbsent` for an authorization failure is the unsafe
        one — it turns "I cannot see it" into "it is not there", which is how bootstrap comes
        to attempt a create against a role that already exists, exactly the fresh-account
        failure #5532's review describes. So the rule is: `NoSuchEntity` and nothing else.
        """

    def create_service_linked_role(self, *, AWSServiceName: str, Description: str = ...) -> dict:
        """Create a service-linked role for one AWS service principal.

        Called only after `get_role` established absence, and only ever for
        `autoscaling.amazonaws.com` — `runner.py` refuses any other principal rather than
        relying on the caller to pass the right one.
        """

    def create_role(self, *, RoleName: str, AssumeRolePolicyDocument: str, Description: str = ...) -> dict:
        """Create one of the three scoped cross-account roles."""

    def list_attached_role_policies(self, *, RoleName: str) -> dict:
        """Which managed policies are attached to one role. Returns
        `{"AttachedPolicies": [{"PolicyName": ..., "PolicyArn": ...}, ...]}`.

        Present because "the role exists" is not "the role is usable". A role created with a
        trust policy and no permissions can be assumed and can then do nothing, and without
        this read a bootstrap that crashed between `create_role` and `attach_role_policy`
        reports the half-built role as established on its next pass — so nothing ever
        finishes it, and the failure surfaces much later as a workspace build that cannot
        reach the account it was given.
        """

    def get_policy(self, *, PolicyArn: str) -> dict: ...
    def get_policy_version(self, *, PolicyArn: str, VersionId: str) -> dict: ...
    def create_policy(self, *, PolicyName: str, Path: str, PolicyDocument: str) -> dict: ...
    def create_policy_version(self, *, PolicyArn: str, PolicyDocument: str, SetAsDefault: bool) -> dict: ...

    def attach_role_policy(self, *, RoleName: str, PolicyArn: str) -> dict:
        """Attach a managed policy to a role this bootstrap created."""


class S3ControlClient(Protocol):
    def get_public_access_block(self, *, AccountId: str) -> dict: ...
    def put_public_access_block(self, *, AccountId: str, PublicAccessBlockConfiguration: dict) -> dict: ...


class CloudTrailClient(Protocol):
    def describe_trails(self, *, trailNameList: list[str], includeShadowTrails: bool) -> dict: ...
    def get_trail_status(self, *, Name: str) -> dict: ...
