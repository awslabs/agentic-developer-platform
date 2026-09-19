"""The mediated GitHub operation contract: bindings, gates and escalation (#5223).

These are the adversarial cases the module exists for, exercised as literals rather
than through a simulated store — the same split `execution_policy` uses. The route
and the worker helper are exercised separately; what is proven here is that the
contract itself cannot be talked into a wider effect.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.agentauth.github_operations import (
    MUTATING_OPERATIONS,
    GitHubOperation,
    OperationAssignment,
    OperationRefusedError,
    assigned_branch,
    escalating_paths,
    idempotency_key,
    operation_action,
    operation_permitted_for_action,
    request_hash,
)
from src.orchestration.execution_policy import Action, ExecutionPolicy, PolicyLimits

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _assignment(**overrides) -> OperationAssignment:
    fields = {
        "tenant_id": "org-a",
        "invocation_id": "run-1",
        "attempt": 1,
        "workload_binding": "pod-uid-1",
        "claim_generation": 3,
        "installation_id": 4242,
        "repository_id": 987654,
        "repository": "acme/widgets",
        "branch": "agent/issue-5223",
        "default_branch": "main",
        "node_id": "node-1",
        "accepted_plan_version": 2,
        "not_after": NOW + timedelta(minutes=10),
    }
    fields.update(overrides)
    return OperationAssignment(**fields)


def _policy(*, allowed=(Action.DEVELOP, Action.MERGE), gated=(Action.MERGE,)) -> ExecutionPolicy:
    return ExecutionPolicy(
        org_id="org-a",
        repository_ids=["acme/widgets"],
        allowed_actions=list(allowed),
        human_gates=list(gated),
        expires_at=NOW + timedelta(hours=2),
        limits=PolicyLimits(
            max_wall_clock_seconds=7200,
            max_spend_usd=Decimal("10"),
            max_attempts_per_node=3,
            max_concurrent_actions=2,
        ),
    )


# --- The merge gate is a separate authorization question --------------------


def test_merge_is_the_only_operation_that_names_an_action():
    """A develop/repair assignment cannot reach merge, because merge asks for more.

    The delivery operations inherit the assignment's already-admitted action; merge
    names `Action.MERGE` explicitly. That asymmetry IS the human gate surviving
    mediation.
    """
    assert operation_action(GitHubOperation.MERGE_PULL_REQUEST) is Action.MERGE
    for operation in (
        GitHubOperation.READ_REPOSITORY,
        GitHubOperation.PUBLISH_COMMIT,
        GitHubOperation.UPSERT_PULL_REQUEST,
        GitHubOperation.PUBLISH_REVIEW,
    ):
        assert operation_action(operation) is None


def test_human_gated_merge_policy_does_not_permit_the_merge_action():
    """The policy a develop assignment runs under refuses merge while permitting develop."""
    policy = _policy()
    assert policy.permits(Action.DEVELOP)
    assert not policy.permits(Action.MERGE)


def test_a_review_only_assignment_cannot_reach_the_delivery_operations():
    """Scope is an allowlist per admitted action, not a denylist of one action.

    A `review` assignment saying something about work is its whole authority;
    publishing a commit or opening a pull request is delivery it was never admitted
    for. `evaluate` gathers evidence and reaches neither.
    """
    for permitted in (GitHubOperation.READ_REPOSITORY, GitHubOperation.PUBLISH_REVIEW):
        assert operation_permitted_for_action(Action.REVIEW, permitted)
    for refused in (GitHubOperation.PUBLISH_COMMIT, GitHubOperation.UPSERT_PULL_REQUEST):
        assert not operation_permitted_for_action(Action.REVIEW, refused)
        assert not operation_permitted_for_action(Action.EVALUATE, refused)
    assert not operation_permitted_for_action(Action.EVALUATE, GitHubOperation.PUBLISH_REVIEW)


def test_an_unestablished_action_reaches_nothing():
    """An unestablished action is a refusal, not a wildcard."""
    for operation in GitHubOperation:
        assert not operation_permitted_for_action(None, operation)


def test_scope_does_not_become_a_second_quieter_merge_gate():
    """Merge is gated by the policy, so scope must not also rule on it.

    `runtime_action` derives only develop/repair/review/evaluate — no assignment
    ever carries `Action.MERGE`. If scope required it, merge would be unreachable
    because the path was dead rather than because a human gated it, and ungating
    merge in the policy would no longer admit it. Regression guard for exactly
    that: the gate an owner controls must stay the thing that decides.
    """
    for action in (Action.DEVELOP, Action.REPAIR, Action.REVIEW, Action.EVALUATE):
        assert operation_permitted_for_action(action, GitHubOperation.MERGE_PULL_REQUEST)


def test_no_operation_forwards_an_arbitrary_request():
    """The closed enum is the control: there is no member meaning "make this call".

    This assertion is deliberately exhaustive rather than a subset check, so adding
    a member is a decision someone makes here too. `FETCH_REPOSITORY_ARCHIVE` is
    the one added since: startup materializes the work tree through it, because a
    mediated run has no token to clone with.
    """
    assert set(GitHubOperation) == {
        GitHubOperation.READ_REPOSITORY,
        GitHubOperation.FETCH_REPOSITORY_ARCHIVE,
        GitHubOperation.PUBLISH_COMMIT,
        GitHubOperation.UPSERT_PULL_REQUEST,
        GitHubOperation.PUBLISH_REVIEW,
        GitHubOperation.MERGE_PULL_REQUEST,
    }
    # Both reads, so neither may take the mutation path: a read that counted as a
    # mutation would consume claim-generation checks meant for writes, and — worse
    # in the other direction — a *write* mistakenly listed as a read would skip
    # `require_current_claim` entirely.
    assert GitHubOperation.READ_REPOSITORY not in MUTATING_OPERATIONS
    assert GitHubOperation.FETCH_REPOSITORY_ARCHIVE not in MUTATING_OPERATIONS
    with pytest.raises(OperationRefusedError):
        operation_action("dispatch_workflow")  # type: ignore[arg-type]


# --- The assigned branch is derived, never requested -----------------------


def test_working_branch_is_derived_from_the_assigned_issue():
    assert assigned_branch(issue_number=5223, default_branch="main") == "agent/issue-5223"


@pytest.mark.parametrize("issue", [0, -1])
def test_branch_derivation_refuses_an_unassigned_issue(issue):
    with pytest.raises(OperationRefusedError):
        assigned_branch(issue_number=issue, default_branch="main")


def test_branch_equal_to_the_default_branch_is_refused():
    """Publishing to the default branch is unreachable, not merely checked for."""
    with pytest.raises(OperationRefusedError):
        assigned_branch(issue_number=7, default_branch="agent/issue-7")
    with pytest.raises(OperationRefusedError):
        _assignment(branch="main", default_branch="main")


def test_assignment_requires_immutable_provider_identity():
    """A repository name is not an authorization target; the numeric id is."""
    with pytest.raises(OperationRefusedError):
        _assignment(repository_id=0)
    with pytest.raises(OperationRefusedError):
        _assignment(installation_id=0)


# --- Idempotency is bound to the request AND the assignment ----------------


def test_same_request_under_same_assignment_reuses_its_key():
    """A provider timeout can retry: the identical request computes the identical key."""
    assignment = _assignment()
    digest = request_hash({"message": "fix", "paths": ["a.py"]})
    first = idempotency_key(assignment=assignment, operation=GitHubOperation.PUBLISH_COMMIT, request_hash=digest)
    second = idempotency_key(assignment=assignment, operation=GitHubOperation.PUBLISH_COMMIT, request_hash=digest)
    assert first == second


def test_changed_content_cannot_reuse_a_completed_operation_key():
    """Different content under the same operation gets a different key, so it has no
    prior outcome to claim — one authorized effect cannot become two."""
    assignment = _assignment()
    original = idempotency_key(
        assignment=assignment,
        operation=GitHubOperation.PUBLISH_COMMIT,
        request_hash=request_hash({"message": "fix", "paths": ["a.py"]}),
    )
    altered = idempotency_key(
        assignment=assignment,
        operation=GitHubOperation.PUBLISH_COMMIT,
        request_hash=request_hash({"message": "fix", "paths": ["b.py"]}),
    )
    assert original != altered


def test_field_order_does_not_change_a_request_digest():
    assert request_hash({"a": 1, "b": 2}) == request_hash({"b": 2, "a": 1})


@pytest.mark.parametrize("field,value", [("invocation_id", "run-2"), ("claim_generation", 4), ("branch", "agent/issue-1"), ("node_id", "node-2")])
def test_key_does_not_span_assignments(field, value):
    digest = request_hash({"message": "fix"})
    base = idempotency_key(assignment=_assignment(), operation=GitHubOperation.PUBLISH_COMMIT, request_hash=digest)
    other = idempotency_key(assignment=_assignment(**{field: value}), operation=GitHubOperation.PUBLISH_COMMIT, request_hash=digest)
    assert base != other


def test_key_does_not_span_operations():
    digest = request_hash({"message": "fix"})
    assignment = _assignment()
    commit = idempotency_key(assignment=assignment, operation=GitHubOperation.PUBLISH_COMMIT, request_hash=digest)
    review = idempotency_key(assignment=assignment, operation=GitHubOperation.PUBLISH_REVIEW, request_hash=digest)
    assert commit != review


# --- Content that could perform a gated action ----------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/deploy.yml",
        ".github/workflows/deploy.yaml",
        ".github/actions/publish/action.yml",
        "tools/composite/action.yml",
    ],
)
def test_workflow_content_is_refused_when_merge_is_gated(path):
    """A commit that installs repository automation can merge or deploy through it,
    so it needs those actions autonomously — the branch it lands on is irrelevant."""
    assert escalating_paths([path, "src/app.py"], policy=_policy()) == (path,)


@pytest.mark.parametrize(
    "spelling",
    [
        ".github/workflows/merge.yml",
        "./.github/workflows/merge.yml",
        ".github\\workflows\\merge.yml",
        "/.github/workflows/merge.yml",
        "././.github/workflows/merge.yml",
    ],
)
def test_every_spelling_of_an_automation_path_is_refused(spelling):
    """A spelling difference must not decide whether content is checked.

    Stripping `./` with `str.lstrip` removes *characters*, so it consumes the
    leading dot of `.github` and leaves an ordinary-looking `github/...` path that
    matches no automation pattern — the check would then pass on exactly the content
    it exists to refuse. Every spelling must reduce to the one canonical path.
    """
    assert escalating_paths([spelling], policy=_policy()) == (".github/workflows/merge.yml",)


def test_branch_naming_alone_does_not_admit_workflow_content():
    """`pull_request_target`, `workflow_run` and a later merge all run a definition
    from a ref the branch name does not bound."""
    assignment = _assignment()
    assert assignment.branch.startswith("agent/issue-")
    assert escalating_paths([".github/workflows/ci.yml"], policy=_policy())


def test_workflow_content_is_permitted_when_both_effects_are_autonomous():
    permissive = _policy(allowed=(Action.DEVELOP, Action.MERGE, Action.DEPLOY), gated=())
    assert escalating_paths([".github/workflows/ci.yml"], policy=permissive) == ()


def test_deploy_gated_alone_still_refuses_workflow_content():
    """Both implied effects are required; merge autonomy alone is not enough."""
    merge_only = _policy(allowed=(Action.DEVELOP, Action.MERGE), gated=())
    assert escalating_paths([".github/workflows/ci.yml"], policy=merge_only) == (".github/workflows/ci.yml",)


def test_ordinary_paths_are_not_escalating():
    assert escalating_paths(["src/app.py", "docs/readme.md", ".github/ISSUE_TEMPLATE/bug.md"], policy=_policy()) == ()


def test_traversing_and_unusable_paths_are_refused():
    for paths in ([".github/workflows/../../etc/passwd"], [""], [None], ["a/../b"]):
        with pytest.raises(OperationRefusedError):
            escalating_paths(paths, policy=_policy())


def test_a_bare_string_is_not_a_path_collection():
    """A string is iterable; treating one as a path list would check it per-character."""
    with pytest.raises(OperationRefusedError):
        escalating_paths(".github/workflows/ci.yml", policy=_policy())
