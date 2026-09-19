"""Mediated GitHub operations: the gateway acts, the worker never holds the token (#5223).

## The refusal this unblocks, and why it must stay a refusal

`runtime_policy.policy_github_permissions` declines to mint a GitHub App
installation token for a `develop`/`repair` assignment whose accepted policy keeps
`merge` as a human decision. That is correct and it is not a conservatism to be
tuned away: GitHub's `contents: write` permission also authorizes
`PUT /repos/{o}/{r}/pulls/{n}/merge`. A token narrow enough to push a commit is
therefore *already* wide enough to merge it, so handing one to a worker dissolves
the human gate no matter what the worker was told to do with it. The provider has
no permission that expresses "may write a branch, may not merge".

The same token also has a **one-hour floor** — GitHub sets the expiry, not us — so
it cannot be issued at all under a grant shorter than an hour without outliving
the authority it was issued under.

The way out is not a narrower credential, because none exists. It is to stop
issuing a credential: the worker asks for an *operation*, the gateway performs it
with a token that never leaves this process, and the authorization is re-checked
immediately before each provider mutation. Then:

- The human merge gate holds because **merge is a different typed operation** with
  its own required action, and a `develop`/`repair` assignment cannot reach it.
  Nothing the worker does with the commit operation can merge, because the worker
  is not the thing talking to GitHub.
- Short grants work, because there is no token whose lifetime could exceed the
  grant. The operation either happens inside the authorized window or it does not
  happen.

## Why there is no reference token: the operation is synchronous

A worker holds **no handle of any kind** — not a credential, not a reference. It
calls :data:`MEDIATED_GITHUB_OPERATION_PATH` on the existing authenticated worker
transport, and the route derives the whole authorization from protected records on
*that* request before acting. There is nothing to copy, replay or steal, because
nothing is issued between requests.

The five bindings a transferable reference would have had to carry are therefore
checked live, per request, from authenticated sources rather than from a payload:

- **tenant and run** come from the verified run credential (the authenticated
  caller), never from the body;
- **pod workload binding** is the verified workload token's pod UID, so a request
  from another pod authorizes as that pod and cannot inherit this one's assignment;
- **claim generation** is *read* from the protected claim row at request time
  (`current_claim_generation`), so a worker whose work was re-admitted to a newer
  generation fails `require_current_claim` on any mutation;
- **attempt, repository and branch** are derived in `build_assignment` from the
  protected execution row and the accepted plan; matching request fields are
  compared and a mismatch refuses.

This is strictly stronger than verifying a minted reference would be. A reference
attests to authority *as it stood when minted*; these checks observe authority as
it stands at the moment of the effect. The distinction is what makes mid-flight
revocation work.

Expiry needs no separate representation. `authorize_worker_credential` is re-run
before each effect and already resolves the **earliest** of the grant, policy and
flow deadlines, so an operation attempted after that instant is refused by the same
check that authorized the first one — there is no issued lifetime that could
outlive it.

Revocation blocks *subsequent* effects: authorization is re-run before every
provider mutation, so a grant revoked mid-flight stops the next call. It cannot
undo a provider action that already landed, and nothing here claims otherwise —
that honesty is why :class:`OperationOutcome` records what was observed at the
provider rather than what was intended.

## What is derived and what is merely asserted

Tenant, installation, immutable repository id, current node assignment, approved
working branch and accepted plan version are **derived from protected records** —
the dispatch/execution row the worker cannot write and the accepted plan. Request
fields carrying the same values are assertions: they must *match* what was
derived, and a mismatch is a refusal, never an override. This is the same rule
`broker_identity.verify_broker_worker` applies to the repository on the token path,
restated here because this module is reachable independently of it.

## What this module deliberately does NOT do

- **No arbitrary forwarding.** There is no method/URL parameter. The operations are
  a closed enum (:class:`GitHubOperation`); anything not enumerated is unreachable
  rather than refused-by-list, so a future provider capability cannot be smuggled
  in as data.
- **No workflow dispatch, settings/ruleset change, branch deletion, force push, or
  token return.** None of these has a typed operation. There is deliberately no
  operation whose result contains credential material.
- **No merge scheduling.** This supplies mediation only. The contract published
  here is what #5130's merge controller will consume; deciding *when* to merge is
  that story's, and this module never initiates one.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from src.orchestration.execution_policy import Action

__all__ = [
    "MAX_OPERATION_REQUEST_BYTES",
    "MEDIATED_GITHUB_OPERATION_PATH",
    "MUTATING_OPERATIONS",
    "GitHubOperation",
    "OperationAssignment",
    "OperationRefusedError",
    "assigned_branch",
    "escalating_paths",
    "idempotency_key",
    "operation_action",
    "operation_permitted_for_action",
    "request_hash",
]

# REST API Gateway refuses request bodies above 10,000,000 bytes before the
# application runs. Keep a deliberate margin and enforce it here too for callers
# that reach the service without traversing that edge.
MAX_OPERATION_REQUEST_BYTES = 9 * 1024 * 1024


# The internal path the mediated operations endpoint is served on. Named here
# rather than spelled as a literal at each call site because `runtime_policy`,
# `broker_identity` and the route all have to agree on it exactly, and a typo in
# any one of them would silently select a *different* authorization branch — which
# on this path means the difference between "mediated, scoped" and "no scope
# established". Import the constant; never retype the string.
#
# Deliberately under `/internal/v1/agent/`: the worker IAM boundary already
# allows `internal/v1/agent/*` and denies everything else by NotResource
# (`webhook-ingress/infra/agent-authority-boundary.tf`), and that prefix carries
# the two-proof `require_agent_transport` dependency. A top-level
# `/internal/v1/github-operation` would have needed a new IAM statement and a new
# internal-plane allowlist entry to reach the same authenticated place.
MEDIATED_GITHUB_OPERATION_PATH = "/internal/v1/agent/self/github-operation"

# Version tag mixed into the idempotency-key derivation, so a key computed under
# one derivation can never be matched by a later one whose field set differs. This
# is NOT a signing or authentication version: `idempotency_key` derives a
# collision-resistant name for one intended effect, and nothing verifies it as a
# claim. Authority is established per request by the route (see the module
# docstring), not carried in this string.
_OPERATION_KEY_VERSION = "adpgho1"


class OperationRefusedError(Exception):
    """A mediated operation could not be authorized or could not be represented.

    One exception type, and the message never distinguishes "unknown assignment"
    from "not permitted for this action" — the same rule
    :class:`~src.agentauth.run_credential.CredentialError` follows, for the same
    reason: an authorizer that reports *which* check failed tells a caller whether
    its asserted input at least had a plausible shape, and lets it probe from there.
    """


class GitHubOperation(StrEnum):
    """The closed set of provider mutations the gateway will perform for a worker.

    Closed is the security property, not a tidiness one. Every member below maps to
    a specific provider call with a fixed shape; there is no member meaning "make
    this request for me". A capability absent from this enum is not reachable by
    any input, which is a stronger statement than "is on a denylist".

    `MERGE_PULL_REQUEST` is a **separate member on purpose**. It is not a mode of
    `UPSERT_PULL_REQUEST` and not a flag on it, because a flag is a value a caller
    supplies and this must be a distinct authorization question. See
    :func:`operation_action`: merge requires a current `Action.MERGE`, which a
    `develop`/`repair` assignment does not have and cannot acquire here.
    """

    # Read the assigned repository (metadata, refs, existing PR state). The one
    # non-mutating member; it still authorizes, because read access to a private
    # repository is access.
    READ_REPOSITORY = "read_repository"
    # Fetch the assigned repository's CONTENT, so the worker can materialize a work
    # tree without a token to clone with (#5223 startup). Separate from
    # `READ_REPOSITORY` rather than a mode of it: that member answers questions
    # about refs and PR state in a small response, while this one transfers a
    # bounded archive, and the two deserve distinct authorization and size
    # statements. Read-only — it mints no write permission and cannot alter a ref.
    FETCH_REPOSITORY_ARCHIVE = "fetch_repository_archive"
    # Publish a commit to the exact assigned working branch. Never to the default
    # branch and never to another branch — the branch is derived, not requested.
    PUBLISH_COMMIT = "publish_commit"
    # Create or update the pull request for the assigned working branch.
    UPSERT_PULL_REQUEST = "upsert_pull_request"
    # Publish the assigned review or comment.
    PUBLISH_REVIEW = "publish_review"
    # Merge the assigned pull request. Requires `Action.MERGE` to be currently
    # autonomous under the accepted policy; a human-gated merge refuses here.
    MERGE_PULL_REQUEST = "merge_pull_request"


# Operation -> the policy action that must be currently authorized for it.
#
# The mapping is explicit rather than computed from "does it mutate?" because the
# interesting distinction is not mutation, it is *which* delegated authority the
# mutation consumes. A commit and a PR update are the delivery work an owner
# authorized when they permitted `develop`; a merge is the effect that outlives the
# run, which is why `execution_policy.Action` separates them in the first place.
#
# `None` means "the assignment's own action is sufficient" — the operation is part
# of doing the work that was already admitted, so it inherits that action rather
# than naming a second one. Only `MERGE_PULL_REQUEST` names an action, and it names
# the one that no development assignment carries.
_OPERATION_ACTIONS: dict[GitHubOperation, Action | None] = {
    GitHubOperation.READ_REPOSITORY: None,
    GitHubOperation.FETCH_REPOSITORY_ARCHIVE: None,
    GitHubOperation.PUBLISH_COMMIT: None,
    GitHubOperation.UPSERT_PULL_REQUEST: None,
    GitHubOperation.PUBLISH_REVIEW: None,
    GitHubOperation.MERGE_PULL_REQUEST: Action.MERGE,
}

# Operations that change something at the provider. Used to decide where the
# "authorize immediately before the effect" rule applies; a read re-authorizes too,
# but only a mutation is unrecoverable once it lands.
MUTATING_OPERATIONS: frozenset[GitHubOperation] = frozenset(
    {
        GitHubOperation.PUBLISH_COMMIT,
        GitHubOperation.UPSERT_PULL_REQUEST,
        GitHubOperation.PUBLISH_REVIEW,
        GitHubOperation.MERGE_PULL_REQUEST,
    }
)


# Assignment action -> the operations that INHERIT that action (see
# `_OPERATION_ACTIONS`: the ones whose `operation_action` is `None`).
#
# An ALLOWLIST, and that direction is the security property. The alternative —
# refusing the actions we happened to think of — silently permits both a `review`
# assignment publishing commits (which it did, until this table existed) and any
# `Action` member added later by someone who never read this file. An unmapped
# action reaches nothing.
#
# `REVIEW` gets the review operation and reads, not delivery: a reviewer's admitted
# authority is to say something about work, not to change it. `EVALUATE` reads only —
# an evaluation gathers evidence and concludes; it does not deliver.
#
# `MERGE_PULL_REQUEST` is deliberately absent from every row, including `MERGE`'s:
# it names its own action, so the policy gate decides it. See
# `operation_permitted_for_action` for why scoping it here would break it.
_ACTION_OPERATIONS: dict[Action, frozenset[GitHubOperation]] = {
    Action.DEVELOP: frozenset(
        {
            GitHubOperation.READ_REPOSITORY,
            GitHubOperation.FETCH_REPOSITORY_ARCHIVE,
            GitHubOperation.PUBLISH_COMMIT,
            GitHubOperation.UPSERT_PULL_REQUEST,
            GitHubOperation.PUBLISH_REVIEW,
        }
    ),
    Action.REPAIR: frozenset(
        {
            GitHubOperation.READ_REPOSITORY,
            GitHubOperation.FETCH_REPOSITORY_ARCHIVE,
            GitHubOperation.PUBLISH_COMMIT,
            GitHubOperation.UPSERT_PULL_REQUEST,
            GitHubOperation.PUBLISH_REVIEW,
        }
    ),
    # Every action below can already read the repository, and materializing a work
    # tree is how a run reads code it must reason about at all. Withholding the
    # archive from a reviewer would not withhold any authority — it would only send
    # that reviewer back to a clone, which needs the credential this path removes.
    Action.REVIEW: frozenset({GitHubOperation.READ_REPOSITORY, GitHubOperation.FETCH_REPOSITORY_ARCHIVE, GitHubOperation.PUBLISH_REVIEW}),
    Action.EVALUATE: frozenset({GitHubOperation.READ_REPOSITORY, GitHubOperation.FETCH_REPOSITORY_ARCHIVE}),
    Action.MERGE: frozenset({GitHubOperation.READ_REPOSITORY, GitHubOperation.FETCH_REPOSITORY_ARCHIVE}),
    Action.DEPLOY: frozenset({GitHubOperation.READ_REPOSITORY, GitHubOperation.FETCH_REPOSITORY_ARCHIVE}),
}


def operation_permitted_for_action(action: Action | None, operation: GitHubOperation) -> bool:
    """Whether an assignment admitted for `action` may reach `operation`.

    This is the assignment's *scope*, distinct from :func:`operation_action`, which
    asks whether the operation demands an action of its own. The two compose by
    dividing the operations between them rather than both ruling on all of them:

    * An operation with **no** required action inherits the assignment's, so this
      table decides it — "a reviewer may not publish commits".
    * An operation that **names** an action is decided by the policy gate on that
      action, and this returns True. Scoping it here as well would be a second,
      quieter gate on top of the real one: `runtime_action` derives only
      `develop`, `repair`, `review` and `evaluate`, so NO assignment ever carries
      `Action.MERGE`. Requiring it here would make merge unreachable no matter what
      a human accepted — the gate would hold because the path was dead, and
      ungating merge in the policy would stop admitting it. The human merge gate
      must hold because the policy refuses it, which is a decision an owner can
      also reverse.

    `None` action means no action was established. That is a refusal, not a
    wildcard — mediation is a policy-bearing path and an unestablished scope is
    exactly the state in which we cannot say what is permitted.
    """
    if action is None:
        return False
    if operation_action(operation) is not None:
        return True
    return operation in _ACTION_OPERATIONS.get(action, frozenset())


def operation_action(operation: GitHubOperation) -> Action | None:
    """The policy action `operation` additionally requires, or `None`.

    `None` is not "no authorization": the assignment's own action (`develop`,
    `repair`, `review`) is still re-checked by
    :func:`~src.orchestration.runtime_policy.authorize_worker_credential` before
    every call. It means this operation asks for nothing *beyond* that.

    Raises:
        OperationRefusedError: `operation` is not a member. Unreachable through the
            enum, kept because a dict `.get()` returning `None` for an unknown key
            would read as "no extra action required" — the one wrong default here.
    """
    if operation not in _OPERATION_ACTIONS:
        raise OperationRefusedError("unsupported github operation")
    return _OPERATION_ACTIONS[operation]


# ---------------------------------------------------------------------------
# The assignment: every authority field, derived from protected records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationAssignment:
    """What the gateway independently established about the caller's assignment.

    Frozen, for the same reason `ResourceRef` and `EngineGenesis` are: this object
    *is* the authorization input. Code that could reassign `branch` after the
    decision returned would have rebuilt the forgery the derivation exists to
    remove — the authorized branch and the written branch must be the same value.

    Nothing here is read from the request. `repository_id` in particular is the
    provider's immutable numeric id (resolved by
    `work_admission.resolve_repository_id`), not the `owner/name` string, so a
    repository renamed mid-flow still resolves to the same authorization target and
    a *different* repository that took over the old name does not.
    """

    tenant_id: str
    invocation_id: str
    attempt: int
    # The pod this run is executing in, from the verified workload token — not from
    # the request. A request from another pod authorizes as that pod, so it cannot
    # inherit this assignment.
    workload_binding: str
    # The work-claim generation currently held for this assignment, READ from the
    # protected claim row per request. A stale generation means another run owns the
    # work now, and `require_current_claim` refuses the mutation.
    claim_generation: int
    installation_id: int
    repository_id: int
    # `owner/name` as the policy and the grant spell it. Carried alongside the
    # numeric id because the provider API paths need the name form, while every
    # authorization comparison uses the id.
    repository: str
    # The approved working branch, DERIVED (see `assigned_branch`). Never requested.
    branch: str
    default_branch: str
    node_id: str
    accepted_plan_version: int
    # Earliest of the grant, policy and flow deadlines.
    not_after: datetime

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.invocation_id or not self.workload_binding:
            raise OperationRefusedError("assignment is missing a protected identity field")
        if self.attempt < 1 or self.claim_generation < 1:
            raise OperationRefusedError("assignment carries an invalid generation")
        if self.repository_id <= 0 or self.installation_id <= 0:
            raise OperationRefusedError("assignment carries no immutable provider identity")
        if not self.branch or self.branch == self.default_branch:
            # A branch equal to the default branch is refused *structurally*, not by
            # a check at the write site. The assigned branch is derived, so this can
            # only be reached by a repository whose default branch happens to match
            # the derived name — in which case publishing to it would be publishing
            # to the default branch, which no mediated operation may ever do.
            raise OperationRefusedError("assigned branch is unusable or is the default branch")

    @property
    def principal(self) -> str:
        """Stable audit identifier, matching `RunCredential.principal`."""
        return f"{self.invocation_id}#{self.attempt}"


# The working-branch convention. Fixed platform-wide (`entrypoint.py`, the reviewer
# trigger's `head_ref` match, the developer persona's branch contract), so it is
# derived from the assigned issue rather than accepted from the request. A requested
# branch would make "publish to your assigned branch" mean "publish to any branch
# you can name", and no downstream check could tell the two apart.
_BRANCH_TEMPLATE = "agent/issue-{issue}"

# Branch names this module will never treat as assigned, regardless of derivation.
# `..` and a leading `-` are git ref hazards; the rest are the refs whose update is
# an effect no mediated operation is authorized to have.
_PROTECTED_BRANCH_NAMES = frozenset({"main", "master", "develop", "trunk", "HEAD"})


def assigned_branch(*, issue_number: int, default_branch: str) -> str:
    """The one branch this assignment may publish to.

    Derived from the assigned issue number, which comes from the protected dispatch
    record. There is no parameter by which a caller can influence the result — that
    absence is the control, and it is why this returns a value rather than
    validating a supplied one.

    Raises:
        OperationRefusedError: The derivation does not produce a usable,
            non-protected branch name.
    """
    if issue_number <= 0:
        raise OperationRefusedError("assignment has no issue to derive a working branch from")
    branch = _BRANCH_TEMPLATE.format(issue=int(issue_number))
    if branch in _PROTECTED_BRANCH_NAMES or branch == default_branch or ".." in branch or branch.startswith("-"):
        raise OperationRefusedError("derived working branch is not publishable")
    return branch


# ---------------------------------------------------------------------------
# Content that could itself perform a gated action
# ---------------------------------------------------------------------------

# Paths whose *content* is executable by the repository's own automation. A commit
# touching one of these can cause GitHub Actions to act with the repository's own
# permissions — including merging or deploying — so the commit is an indirect way
# to perform an action the policy may have gated to a human.
#
# Matched on the path rather than by parsing YAML for dangerous steps: a parser
# would have to be right about every way a workflow can invoke a gated effect
# (reusable workflows, composite actions, `gh` in a `run:` block, a third-party
# action's internals), and being wrong once admits the escalation. The path is the
# property we can decide correctly.
_AUTOMATION_PATH_PATTERNS = (
    re.compile(r"^\.github/workflows/[^/]+\.ya?ml$"),
    re.compile(r"^\.github/actions/.+$"),
    re.compile(r"^(?:.+/)?action\.ya?ml$"),
)

# The actions a repository's own automation could perform on the worker's behalf.
# A content change under an automation path needs BOTH to be currently autonomous,
# because a workflow the commit installs is not constrained to the operation that
# installed it — it can merge and it can deploy.
_AUTOMATION_IMPLIED_ACTIONS = (Action.MERGE, Action.DEPLOY)


def _normalise_tree_path(path: str) -> str:
    """Reduce one proposed tree path to the spelling the automation patterns match.

    Separators are unified and every leading `./` and `/` is removed, so an
    automation path cannot be hidden behind a spelling difference. This strips
    *prefixes*, not characters: `str.lstrip("./")` would eat the leading dot of
    `.github/...` and turn a workflow file into an ordinary `github/...` path that
    matches nothing — the check would then pass on exactly the content it exists to
    catch. `..` is refused outright rather than resolved: a traversal in a tree path
    is never legitimate, and normalising one would mean deciding what it pointed at.
    """
    normalised = path.replace("\\", "/")
    while normalised.startswith(("./", "/")):
        normalised = normalised[2:] if normalised.startswith("./") else normalised[1:]
    if not normalised or ".." in normalised.split("/"):
        raise OperationRefusedError("proposed change carries a traversing path")
    return normalised


def escalating_paths(paths: object, *, policy) -> tuple[str, ...]:
    """Paths in a proposed change that could perform an action the policy gates.

    Returns the offending paths, sorted, or an empty tuple. A non-empty result is a
    refusal at the call site — the commit does not get published minus the
    offending files, because a partial publication of a change the agent authored
    as a whole is a different change than the one it validated.

    The rule: a path whose content the repository's automation executes requires
    every action that automation could take (`merge`, `deploy`) to be *currently
    autonomous* under the accepted policy. If either is gated to a human or simply
    not permitted, the change is refused.

    **Branch naming is deliberately not consulted.** "It is only on the agent's
    branch" is not a bound on a workflow file: `pull_request_target`,
    `workflow_run`, `issue_comment` and a scheduled trigger all run definitions
    from a non-default ref or with elevated tokens, and a reviewer or a bot merging
    the PR later runs it on the default branch. A control that depends on the
    branch would be one provider trigger away from being no control at all.

    Args:
        paths: The paths the change touches. Any iterable of strings; a non-iterable
            or a non-string member is refused rather than skipped.
        policy: The accepted :class:`~src.orchestration.execution_policy.ExecutionPolicy`
            in force. Only `permits` is used, so a caller cannot pass a summary and
            have it read as permissive.

    Raises:
        OperationRefusedError: `paths` is not a collection of strings.
    """
    if isinstance(paths, str) or not hasattr(paths, "__iter__"):
        raise OperationRefusedError("proposed change paths are not enumerable")
    candidates = []
    for path in paths:
        if not isinstance(path, str) or not path:
            raise OperationRefusedError("proposed change carries an unusable path")
        candidates.append(_normalise_tree_path(path))

    automation = sorted({path for path in candidates if any(pattern.match(path) for pattern in _AUTOMATION_PATH_PATTERNS)})
    if not automation:
        return ()
    if all(policy.permits(action) for action in _AUTOMATION_IMPLIED_ACTIONS):
        # The owner autonomously authorized both effects the automation could have,
        # so installing a definition that could take them is within the grant.
        return ()
    return tuple(automation)


def idempotency_key(*, assignment: OperationAssignment, operation: GitHubOperation, request_hash: str) -> str:
    """The retry key for one exact request under one exact assignment.

    Bound to the request hash **and** the assignment, which is what makes it safe to
    retry and unsafe to reuse. A provider timeout after a commit or a PR creation is
    indistinguishable at the client from a failure, so the retry must be able to
    prove it is the same request — and a key bound only to a caller-chosen string
    would let a *different* request claim a completed operation's result, which is
    how one authorized effect becomes two different ones.

    The assignment fields included are the ones whose change means "this is no
    longer the same piece of work": tenant, run, attempt, repository, branch and
    node. Claim generation is included too, so work that changed hands cannot
    inherit the previous owner's keys.

    What this key is NOT, stated because an earlier version of this docstring
    claimed otherwise: there is no durable operation/result ledger. Nothing
    persists the key and nothing consults it, so it does not by itself deduplicate
    a redelivered request. Duplicate-safety on the mutating paths comes from
    **reconciliation** — after a provider timeout the route looks up whether the
    intended commit or PR actually landed, identifying a commit by the tree and
    parent it had already built, and reports `unknown` rather than guessing (see
    `reconcile_commit` / `reconcile_pull_request` and the Idempotency section of
    `docs/security/mediated-github-operations.md`).

    So the key's present role is a collision-resistant NAME for one intended effect
    under one assignment: stable across a retry of the same request, and necessarily
    different for a different request or a changed assignment, which is what makes a
    future ledger able to key on it safely. Adding that durable record is a separate
    accepted-design decision, not something this function implies is already in
    place. Callers must not treat the key's presence as evidence that a duplicate
    request was refused.
    """
    if not request_hash or not isinstance(request_hash, str):
        raise OperationRefusedError("operation request hash is required for idempotent retry")
    material = _canonical(
        {
            "v": _OPERATION_KEY_VERSION,
            "tenant_id": assignment.tenant_id,
            "invocation_id": assignment.invocation_id,
            "attempt": int(assignment.attempt),
            "claim_generation": int(assignment.claim_generation),
            "repository_id": int(assignment.repository_id),
            "branch": assignment.branch,
            "node_id": assignment.node_id,
            "operation": operation.value,
            "request_hash": request_hash,
        }
    )
    return f"ghop_{hashlib.sha256(material).hexdigest()[:48]}"


def request_hash(request: object) -> str:
    """A stable digest of the operation request the caller actually asked for.

    Canonical JSON, so field order cannot make the same request hash two ways and
    defeat the idempotency binding above.
    """
    try:
        return hashlib.sha256(_canonical(request)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise OperationRefusedError("operation request is not representable") from exc


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
