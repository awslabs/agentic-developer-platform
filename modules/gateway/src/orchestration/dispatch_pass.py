"""The caller that turns a recorded dispatch into an actual one.

Issue #4313 (EPIC #4191, intent #4120), implementing the ruling in
`docs/design-notes/4303-engine-genesis-transport.md` (spike #4303).

Wave 4 shipped `genesis.py` (attribution), `dispatch.py` (state transition) and
`deviation.py` (off-graph detection) with **no caller**, because no transport for
the approver identity existed that did not weaken one of the EPIC's fail-closed
controls. The ruling's answer was that the question was mis-framed: there is no
transport, because there is no boundary to cross. The tick already holds VPC
attachment, `rds-db:connect` and the `adp-gateway` image containing this package,
so it resolves genesis and produces the agent envelope **in the same process, one
function call apart**. `decision_id` never leaves the gateway.

This module is that caller. Without it the engine is a bookkeeper: it advances
nodes to `ready`, commits a dispatch record, and no run ever starts — the failure
being invisible being worse than the failure, which is what #4077 was filed about.

--------------------------------------------------------------------------------
Why this is a new module and not a few lines in `tick.py`
--------------------------------------------------------------------------------

`tick.py`'s docstring states that "the tick performs **no dispatch**", and that
sentence is load-bearing rather than descriptive: it is what lets the tick's tests
pin the tick's behaviour exactly. #4211 kept `tick.py` byte-identical for the same
reason. Adding dispatch there would make the docstring false and put two concerns
behind one set of tests.

--------------------------------------------------------------------------------
Commit-then-publish, and why the pass is split in two
--------------------------------------------------------------------------------

`dispatch_node` does not commit, so the SQS send cannot be inside the DB
transaction. The ordering is chosen deliberately (hazard 3 of the ruling, and the
`knowledge/dispatch.py:7-14` row-before-publish invariant): **commit first, then
publish.**

- Commit, then publish, and the publish fails: the node is `running` with no run.
  Recovered by the stall/halt detector from #4211, which is merged — a node stuck
  in `running` is exactly what `stall.py` exists to find. The failure is also
  counted (`publish_failed`) and forces a non-success report, so it is never
  silent.
- Publish, then commit, and the commit fails: a run exists with no `running` node,
  which `deviation.py` correctly flags as off-graph work. Noisy but safe — and
  strictly worse than the above, because it manufactures work the graph does not
  know about.

That ordering is why this module exposes **two** functions rather than one.
:func:`run_dispatch_pass` does the database half and returns the envelopes it
*intends* to publish; :func:`publish_pending` sends them and must be called by the
handler **after** its commit. A single function could not honour the ordering
without owning the transaction, which would break the handler's one-commit shape
that `test_tick.py` pins.

--------------------------------------------------------------------------------
The dedup key must NOT be the webhook path's key shape (hazard 1)
--------------------------------------------------------------------------------

`sqs_publisher.py:73-74` builds `MessageDeduplicationId` as
`f"{arrived_at}_{repo}_{issue}"`, and the queue also sets
`content_based_deduplication = true`. The FIFO dedup window is **5 minutes** and
the tick's schedule is `rate(5 minutes)` — the same order of magnitude, which is
the dangerous case, not the safe one.

Reusing that shape would let two nodes on the same issue collapse to one message.
SQS accepts the duplicate and discards it, returning a MessageId, so
`dispatch_node` commits `running` and the publish *looks* successful while no run
ever starts. That is the worst failure available here because it is invisible.

So the key derives from `node_id` + `decision_id` + the attempt number
(:func:`message_deduplication_id`). Two distinct nodes sharing an `issue_ref`
therefore produce two distinct ids, and a legitimate re-dispatch of the same node
after a human resume (which increments `attempts`) is not swallowed as a
duplicate of the original.

--------------------------------------------------------------------------------
The group id must be per node, not per issue (hazard 2)
--------------------------------------------------------------------------------

`sqs_publisher.py:70` uses `MessageGroupId = f"{tenant_id}#{repo}#{issue}"`.
`OrchestrationNode.issue_ref` is nullable (`models.py`), so every gate and eval
node in a tenant would share the group `tenant##` — reintroducing precisely the
tenant-wide head-of-line blocking that `sqs_publisher.py:3-8` says the per-run
group was chosen to avoid. :func:`message_group_id` groups per node, so no node's
message can ever block another's.

--------------------------------------------------------------------------------
`spawn_persona` is deliberately NOT called (hazard 4)
--------------------------------------------------------------------------------

`spawn_persona` is the *webhook* path's enforcement point. It bundles self-mention
and self-re-trigger guards, cross-persona loop detection, `MAX_CHAIN_DEPTH`
capping and DynamoDB correlation-pointer writes. The engine either does not need
those or must not inherit them: pointer provenance is advisory and **agent-writable**
(#4304), so the engine must not source any authority from that store. The envelope
is built explicitly here, and the only thing reused is the envelope *contract*.

Note that `publish_envelope` itself is not importable from here either. It lives at
`webhook-ingress/lambda/common/sqs_publisher.py`, and the gateway Dockerfile copies
only `src/`, `alembic/` and `cli/` — so that module is absent from the image the
tick runs. Importing it would raise `ImportError` in Lambda while passing locally,
where the repo checkout has the file on disk. This is the same Lambda-side/gateway-side
split `stall.py` documents for `MAX_CHAIN_DEPTH`, and the same resolution: mirror
the small piece that is needed, and pin the shared contract with a test. The parts
that would otherwise have been shared — the two key shapes — are exactly the parts
the ruling forbids sharing, so nothing of substance is duplicated.

--------------------------------------------------------------------------------
What the envelope's identity fields may and may not claim
--------------------------------------------------------------------------------

The envelope carries `correlation.root_human_id` and `is_human_rooted`. This
ruling **does** put a resolved identity in a message, and says so plainly rather
than claiming "identity is never transported".

The narrower, true claim: the producer is the same trust domain that owns the
decision rows; agent pods cannot produce onto this queue at all
(`scaledjob-iam.tf` grants `ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes`/
`ChangeMessageVisibility` and **no `SendMessage`**, so forging an engine dispatch
requires an IAM change and therefore a review moment); and the pod never
re-presents these fields to obtain anything. The authoritative record is the
`orchestration_decisions` row written inside the committed transaction. **The
envelope fields are attribution for the run's audit trail, not a credential.**

--------------------------------------------------------------------------------
Scope: stories and evaluations require complete issue routing
--------------------------------------------------------------------------------

Stories and evaluations dispatch to existing GitHub issues. A dispatch requires
an issue number, exactly one tenant installation and a configured repository.
Evaluations use the operations persona; gates are presented by the tick and
never consume a worker. Missing configuration leaves a node ready and reports
it as undispatchable. Dispatch does not create issues or invent test results.

"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.shared.models.base import utcnow
from src.shared.models.organization import Organization

from .dispatch import DispatchStatus, dispatch_node
from .flow_execution import flow_is_paused
from .genesis import APPROVAL_DECISION_KINDS, EngineGenesis, GenesisRefusedError, resolve_engine_genesis
from .handoff import HANDOFF_RECEIPT_CONTRACT_VERSION
from .models import DecisionKind, NodeKind, OrchestrationDecision, OrchestrationFlow, OrchestrationNode
from .policy_admission import authorize_node_dispatch
from .state import ActorKind, NodeState

logger = logging.getLogger("bedrockgateway.orchestration.dispatch_pass")

__all__ = [
    "DEFAULT_MAX_DISPATCHES_PER_TICK",
    "DispatchPassConfig",
    "DispatchPassReport",
    "PendingPublish",
    "message_deduplication_id",
    "message_group_id",
    "publish_pending",
    # Exported for the flow-creation route (#4320), so submission reports
    # dispatchability by the same rule dispatch enforces. See its docstring.
    "resolve_installation_id",
    "run_dispatch_pass",
    # Exported for the same reason, and for the same caller (#4334): the issue
    # routing rule below is the SECOND precondition dispatch enforces, and the
    # route must report it by calling this rather than restating it.
    "RoutingBlocker",
    "issue_number_for_dispatch",
    "node_requires_issue_routing",
    "routing_blocker_for_node",
]


class RoutingBlocker(StrEnum):
    """Why one node's issue routing cannot produce a dispatch.

    A closed vocabulary with stable ids rather than prose, so the submission
    report can name a cause a client keys off while the human-readable detail
    stays free to be reworded. Ordered as dispatch checks them.

    `MISSING_ISSUE_REF` and `MALFORMED_ISSUE_REF` are deliberately distinct even
    though dispatch refuses both: they need different fixes. The first means
    nobody has materialised the node's issue yet; the second means an issue
    reference exists but is not an issue number, which is an authoring error in
    the plan document and will not resolve itself.
    """

    MISSING_ISSUE_REF = "missing_issue_ref"
    MALFORMED_ISSUE_REF = "malformed_issue_ref"


def node_requires_issue_routing(kind: str) -> bool:
    """Whether a node of this kind must carry a routable issue to dispatch.

    Story and evaluation nodes dispatch to an existing GitHub issue; gates are
    presented by the tick and never consume a worker, so a gate with no
    `issue_ref` is correct rather than blocked. This mirrors the kind filter
    `_fetch_ready_nodes` applies in SQL, and exists so the route can apply the
    same scope without reimplementing that predicate — a route that flagged
    gates would report a healthy plan as blocked.
    """
    return kind in (NodeKind.STORY.value, NodeKind.EVAL.value)


def issue_number_for_dispatch(issue_ref: str | None) -> int | None:
    """The positive issue number `issue_ref` denotes, or None if there isn't one.

    The single parse of a node's issue reference. Accepts a bare number and a
    `#`-prefixed one; rejects anything else, including zero and negatives, since
    `source_ref.issue` must address a real issue.

    Module-public and shared with the flow-creation route for the same reason
    `resolve_installation_id` is — see its docstring. This one matters more, not
    less: it is checked FIRST by dispatch, so a route that knew only about the
    installation rule could report a plan dispatchable that dispatch refuses
    before it ever looks at the installation.
    """
    if not issue_ref:
        return None
    try:
        issue = int(str(issue_ref).lstrip("#"))
    except ValueError:
        return None
    return issue if issue > 0 else None


def routing_blocker_for_node(*, kind: str, issue_ref: str | None) -> RoutingBlocker | None:
    """The issue-routing blocker for one node, or None if it is routable.

    Pure: no session, no network, no environment. That is what lets the
    submission route call it on nodes it already holds without a second query,
    and what lets one test assert the route's verdict and the dispatch guard
    agree on the same node rather than merely look similar.
    """
    if not node_requires_issue_routing(kind):
        return None
    if not issue_ref:
        return RoutingBlocker.MISSING_ISSUE_REF
    if issue_number_for_dispatch(issue_ref) is None:
        return RoutingBlocker.MALFORMED_ISSUE_REF
    return None


# Environment variables, stamped in by Terraform. Read from the environment and
# never hard-coded, following `notify.py`'s `TOPIC_ARN_ENV` precedent: an
# unconfigured environment must be visible as unconfigured rather than looking
# like a successful no-op.
QUEUE_URL_ENV = "BG_ORCH_DISPATCH_QUEUE_URL"
REPO_ENV = "BG_ORCH_DISPATCH_REPO"
PERSONA_ENV = "BG_ORCH_DISPATCH_PERSONA"
MAX_PER_TICK_ENV = "BG_ORCH_DISPATCH_MAX_PER_TICK"

# The per-tick dispatch cap. The ruling names "number of dispatches per tick" as
# the one unbounded surface to bound deliberately: every dispatch is agent
# capacity and model spend, so dispatching every `ready` node in one pass turns a
# large flow into an unbounded cost spike. Ten is small enough that a runaway
# graph costs one tick's worth of runs rather than a whole wave's, and the next
# tick picks up where this one stopped — the cap delays work, it never drops it.
DEFAULT_MAX_DISPATCHES_PER_TICK = 10

# The persona engine dispatches as. `developer` because a story node is delivery
# work. Configurable, but NOT per-node: persona is not authority (R-O5d), so
# nothing downstream may read it as such.
DEFAULT_PERSONA = "developer"
EVALUATION_PERSONA = "operations"


def attempt_run_id(node_id: str, attempt: int) -> str:
    """Stable identity for one engine attempt; retries get distinct ids."""
    return "orch:" + str(uuid5(NAMESPACE_URL, f"adp:orchestration:{node_id}:{attempt}"))


# SQS caps both FIFO key fields at 128 characters.
_MAX_SQS_KEY_LEN = 128

# SQS message size limit.
MAX_SQS_MESSAGE_BYTES = 256 * 1024

# Envelope schema version. Matches the webhook path's `_build_envelope` so the
# worker parses both producers' messages with one code path — the envelope
# contract is what is shared with the webhook path, and the only thing that is.
_ENVELOPE_VERSION = "1.0"


class SQSClient(Protocol):
    """The `send_message` subset of boto3's SQS client.

    A Protocol rather than a concrete client so tests inject a double, matching
    `knowledge/dispatch.py`'s shape. Real AWS calls in unit tests would make the
    dedup-key and group-id assertions untestable, and those are three of this
    issue's acceptance criteria.
    """

    def send_message(self, **kwargs: Any) -> dict[str, Any]: ...


_sqs_client: SQSClient | None = None


def _get_sqs_client(region: str) -> SQSClient:
    global _sqs_client
    if _sqs_client is None:
        import boto3

        _sqs_client = boto3.client("sqs", region_name=region)
    return _sqs_client


class DispatchPassConfigError(ValueError):
    """A dispatch configuration that cannot produce a valid envelope."""


@dataclass(frozen=True)
class DispatchPassConfig:
    """Where dispatches go and how many may go per tick.

    Frozen: the cap and the queue are read once per invocation, so a mid-pass
    mutation could not have a coherent meaning.
    """

    queue_url: str
    # `owner/name` of the repository the engine dispatches into. Configuration
    # rather than graph state because `OrchestrationNode` carries no repo — see
    # the module docstring's scope section.
    repo: str
    persona: str = DEFAULT_PERSONA
    max_dispatches_per_tick: int = DEFAULT_MAX_DISPATCHES_PER_TICK
    aws_region: str = "us-east-1"

    def __post_init__(self) -> None:
        if self.max_dispatches_per_tick < 1:
            raise DispatchPassConfigError(f"max_dispatches_per_tick must be at least 1; got {self.max_dispatches_per_tick}")

    @property
    def configured(self) -> bool:
        """Whether this config can actually produce a dispatch.

        Both fields are required. An empty queue url means there is nowhere to
        publish; an empty repo means no complete `source_ref` can be built. Either
        way the honest outcome is "nothing was dispatched and here is why", which
        is what `undispatchable` records.
        """
        return bool(self.queue_url and self.repo)

    @classmethod
    def from_env(cls) -> DispatchPassConfig:
        """Build from the process environment. Terraform stamps these in."""
        raw_cap = (os.environ.get(MAX_PER_TICK_ENV) or "").strip()
        try:
            cap = int(raw_cap) if raw_cap else DEFAULT_MAX_DISPATCHES_PER_TICK
        except ValueError:
            # A malformed cap falls back to the default rather than raising: an
            # unparseable number must not take the whole tick down, and the
            # default is the conservative value anyway.
            logger.warning(
                "orchestration dispatch: %s=%r is not an integer; using default %d",
                MAX_PER_TICK_ENV,
                raw_cap,
                DEFAULT_MAX_DISPATCHES_PER_TICK,
            )
            cap = DEFAULT_MAX_DISPATCHES_PER_TICK

        return cls(
            queue_url=(os.environ.get(QUEUE_URL_ENV) or "").strip(),
            repo=(os.environ.get(REPO_ENV) or "").strip(),
            persona=(os.environ.get(PERSONA_ENV) or "").strip() or DEFAULT_PERSONA,
            max_dispatches_per_tick=cap,
            aws_region=os.environ.get("AWS_REGION") or os.environ.get("BG_AWS_REGION") or "us-east-1",
        )


def message_group_id(*, org_id: str, node_id: str) -> str:
    """The FIFO group for one node's dispatch. **Per node, never per issue.**

    Guards hazard 2. The webhook path groups by `tenant#repo#issue`, which for a
    node with `issue_ref = NULL` collapses to `tenant##` and serialises every
    gate/eval node in the tenant behind one another. Grouping by node id means
    every dispatch is in its own group, so no message can head-of-line block
    another — and there is no ordering requirement between two nodes' runs to
    lose by doing so.
    """
    return f"{org_id}#{node_id}"[:_MAX_SQS_KEY_LEN]


def message_deduplication_id(*, node_id: str, decision_id: str, attempt: int) -> str:
    """The FIFO dedup id for one dispatch. **Never the webhook path's key shape.**

    Guards hazard 1. Derived from what actually makes a dispatch unique:

    - `node_id` — so two nodes sharing an `issue_ref` never collapse into one
      message inside the 5-minute dedup window.
    - `decision_id` — so a re-plan that re-approves the work is a new dispatch.
    - `attempt` — so a legitimate re-dispatch after a human resume (which
      increments `attempts`) is not swallowed as a duplicate of the original.

    Deliberately **not** derived from `arrived_at`/`repo`/`issue`: a timestamp
    makes the key change on every attempt, which defeats dedup entirely, and
    repo/issue are not unique per node.
    """
    return f"orch:{node_id}:{decision_id}:{attempt}"[:_MAX_SQS_KEY_LEN]


@dataclass(frozen=True)
class PendingPublish:
    """One envelope that has been committed to the database and not yet sent.

    Exists because publish happens **after** the transaction commits. Holding the
    intent as data between the two phases is what makes the ordering explicit and
    testable, rather than an accident of where the `await` happens.
    """

    node_id: str
    org_id: str
    envelope: dict[str, Any]
    group_id: str
    deduplication_id: str
    genesis: EngineGenesis | None = None
    node_attempt: int = 0
    node_kind: str = NodeKind.STORY.value

    def invocation_id(self) -> str:
        """The protected invocation this dispatch becomes. Never recomputed elsewhere."""
        return attempt_run_id(self.node_id, self.node_attempt)


@dataclass
class DispatchPassReport:
    """What one dispatch pass did. Every field exists to be surfaced.

    `dispatched` is the field an operator reads to confirm the new code is live —
    only this code emits it — so it is not optional polish.
    """

    nodes_examined: int = 0
    dispatches_attempted: int = 0
    dispatched: int = 0
    # A `GenesisRefusedError`: no approval row could root this dispatch. Fail-closed
    # and counted, never a downgrade to an unrooted dispatch.
    genesis_refused: int = 0
    # A node that cannot produce a complete `source_ref` — a gate/eval node, a
    # missing issue, an ambiguous installation, or an unconfigured target repo.
    # Counted rather than published as a malformed envelope.
    undispatchable: int = 0
    # Committed to `running` but the SQS send failed. Recoverable by #4211's stall
    # detector; forces a non-success report so it is never silent.
    publish_failed: int = 0
    # `transition()` refused the edge, and the refusal was recorded as a decision.
    transitions_rejected: int = 0
    # A concurrent pass dispatched the node first. Normal overlap, not a failure.
    lost_races: int = 0
    # Refused by the flow's accepted execution policy (#5128). Deliberately NOT
    # folded into `undispatchable`: that counter means "this node could not produce a
    # valid envelope", a defect to fix, while this means "the envelope was fine and
    # the owner's policy did not authorize it" — working as intended. Merging them
    # would make a correctly-enforced boundary look like a malformed graph.
    #
    # It is also not an `error`: a policy refusal must not make the pass unsuccessful,
    # or every tick would report failure for as long as a policy legitimately
    # withheld an action.
    policy_blocked: int = 0
    # #5144: a policy-bound story whose durable execution could not be admitted, so
    # nothing was published and a typed block was recorded. Its own counter for the
    # same reason `policy_blocked` is: this is a boundary working, not a malformed
    # graph (`undispatchable`) and not a bug (`errors`). Folding it into either would
    # hide the one number that says "policy-bound work is being refused rather than
    # silently downgraded to no-receipt legacy".
    admission_refused: int = 0
    errors: int = 0
    # True when the per-tick cap stopped the pass early. Work is delayed, not
    # dropped — but "we ran out of budget" must never read as "there was nothing
    # left to do", which is why this is reported rather than inferred.
    capped: bool = False
    # False when the queue url or target repo is unset. Surfaced so an unwired
    # environment is visible as unwired instead of looking like an idle one.
    enabled: bool = True
    pending: list[PendingPublish] = field(default_factory=list)
    # PMM-07: per-invocation model-policy snapshot receipts from
    # `prepare_pending`, keyed by invocation id. Report-only evidence only --
    # `{"status": "available"|"unavailable", ...}`. Deliberately NOT folded into
    # `errors`/`publish_failed`: unavailable model-policy evidence must not make a
    # correct dispatch look failed or stop it publishing while the posture is
    # report-only. A protected-authority failure is different and *is* counted, as
    # `publish_failed`, because that dispatch genuinely does not reach the queue.
    model_policy_receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    per_org: dict[str, dict[str, int]] = field(default_factory=dict)
    # Typed `DenyReason` value -> count, for the pass's own observability. A dict
    # rather than a counter field per reason because #5128's reason vocabulary is
    # owned by `execution_policy` and read by #5122; mirroring it as dataclass fields
    # here would guarantee the two drift.
    policy_block_reasons: dict[str, int] = field(default_factory=dict)
    _repository_evaluation_attempted: bool = field(default=False, repr=False)

    @property
    def success(self) -> bool:
        """False if anything failed, including a publish that did not land.

        A dispatch that committed `running` and never reached the queue is the
        exact invisible failure this issue exists to end, so it counts against
        success rather than being a footnote on a green pass.
        """
        return self.errors == 0 and self.publish_failed == 0

    def _org(self, org_id: str) -> dict[str, int]:
        return self.per_org.setdefault(
            org_id,
            {
                "nodes_examined": 0,
                "dispatches_attempted": 0,
                "dispatched": 0,
                "genesis_refused": 0,
                "undispatchable": 0,
                "publish_failed": 0,
                "transitions_rejected": 0,
                "lost_races": 0,
                "policy_blocked": 0,
                "admission_refused": 0,
                "errors": 0,
            },
        )

    def record(self, org_id: str, key: str, amount: int = 1) -> None:
        """Increment a counter both in total and for one org."""
        setattr(self, key, getattr(self, key) + amount)
        self._org(org_id)[key] += amount


async def _fetch_ready_nodes(session: AsyncSession, *, limit: int) -> list[OrchestrationNode]:
    """The `ready` execution nodes this pass may dispatch, ordered by id.

    Story and evaluation nodes, filtered in SQL rather than skipped in Python — see the
    scope section of the module docstring. `org_id` is read off each row and used
    as the tenant for everything downstream, so it comes from this query's own
    context and never from a message (the issue's tenant-isolation requirement).

    Ordered by id for a stable, resumable sweep: the cap stops the pass partway,
    and a deterministic order means the next tick continues rather than
    re-examining an arbitrary subset.
    """
    stmt = (
        select(OrchestrationNode)
        .join(OrchestrationFlow, (OrchestrationFlow.id == OrchestrationNode.flow_id) & (OrchestrationFlow.org_id == OrchestrationNode.org_id))
        .where(
            OrchestrationFlow.execution_paused.is_(False),
            OrchestrationNode.state == NodeState.READY.value,
            OrchestrationNode.kind.in_([NodeKind.STORY.value, NodeKind.EVAL.value]),
        )
        .order_by(
            (OrchestrationNode.kind == NodeKind.EVAL.value).asc(),
            case((OrchestrationNode.kind == NodeKind.EVAL.value, OrchestrationNode.updated_at)).asc().nullsfirst(),
            OrchestrationNode.id,
        )
        .limit(limit)
        .with_for_update(of=OrchestrationNode, skip_locked=True)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _latest_approval_decision_id(session: AsyncSession, *, org_id: str, flow_id: str) -> str | None:
    """The most recent human approval for this flow, as an opaque id.

    This is the only thing handed to `resolve_engine_genesis`, and it is a primary
    key — the approver is read from the row **there**, server-side. Nothing here
    reads or passes a `root_human_id`, which is what makes a caller-supplied root
    unrepresentable rather than merely unused.

    Filtered by `org_id` in SQL as well as `flow_id`, so a flow id from another
    tenant resolves to nothing. `APPROVAL_DECISION_KINDS` is imported from
    `genesis.py` rather than restated, so the two cannot disagree about what
    counts as an approval.
    """
    stmt = (
        select(OrchestrationDecision.id)
        .where(
            OrchestrationDecision.org_id == org_id,
            OrchestrationDecision.flow_id == flow_id,
            OrchestrationDecision.kind.in_(sorted(APPROVAL_DECISION_KINDS)),
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def resolve_installation_id(session: AsyncSession, *, org_id: str) -> int | None:
    """The org's single GitHub installation, or None if that is not unambiguous.

    Fail-closed on both zero and more than one. An org with no installation cannot
    have work delivered to it; an org with several gives no basis to choose, and
    guessing would dispatch into a repository nobody asked for. `None` becomes
    `undispatchable`, which leaves the node in `ready` for a later tick once the
    ambiguity is resolved.

    Module-public (no leading underscore) because the flow-creation route calls it
    too (issue #4320). It is shared rather than copied deliberately: the route
    needs to tell a submitter "this plan can never dispatch" using the *same* rule
    dispatch will later apply, and a second implementation of a fail-closed check
    is free to drift from this one. The drift would be invisible in the worst
    direction — the route reporting a plan as dispatchable that dispatch then
    silently counts `undispatchable`, which is exactly the invisible-stall class
    this EPIC exists to remove.
    """
    raw = (
        await session.execute(
            select(Organization.github_installation_ids).where(Organization.id == org_id),
        )
    ).scalar_one_or_none()

    ids = [str(i).strip() for i in (raw or []) if str(i).strip()]
    if len(ids) != 1:
        return None
    try:
        return int(ids[0])
    except ValueError:
        return None


def _build_envelope(
    *,
    node: OrchestrationNode,
    genesis: EngineGenesis,
    graph_address: str,
    installation_id: int,
    issue: int,
    config: DispatchPassConfig,
    user_id: str,
    cognito_sub: str,
) -> dict[str, Any]:
    """Build the agent envelope explicitly. No `spawn_persona` (hazard 4).

    The shape mirrors the webhook path's `_build_envelope` because the **envelope
    contract** is what the worker consumes and is genuinely shared. What is not
    reused is everything `spawn_persona` wraps around it: the self-mention guards,
    the cross-persona loop detection, the `MAX_CHAIN_DEPTH` cap and the DynamoDB
    correlation-pointer write. Pointer provenance there is advisory and
    agent-writable (#4304), so the engine must not source authority from it.

    `correlation.root_human_id` / `is_human_rooted` are **attribution, not a
    credential** — see the module docstring. `is_human_rooted` reads
    `genesis.is_human_rooted`, which is a property of the object's existence
    rather than a settable field, so there is nothing here that could claim
    human-rootedness without a resolved approval row behind it.
    """
    run_id = attempt_run_id(node.id, node.attempts)
    persona = EVALUATION_PERSONA if node.kind == NodeKind.EVAL.value else config.persona
    return {
        "version": _ENVELOPE_VERSION,
        "message_id": run_id,
        "actor": {"user_id": user_id, "org_id": genesis.org_id},
        "cognito_sub": cognito_sub,
        # The engine is its own channel. Not "github": nothing here came from a
        # GitHub event, and labelling it so would make an engine dispatch
        # indistinguishable from a webhook trigger in every downstream log.
        "channel": "orchestration",
        "tenant_id": genesis.org_id,
        "persona": persona,
        "source_ref": {
            "installation_id": installation_id,
            "repo": config.repo,
            "issue": issue,
        },
        "intent": {
            "trigger": "engine_dispatch",
            "label": None,
            "persona": persona,
        },
        "correlation": {
            "correlation_id": run_id,
            "root_human_id": user_id,
            "is_human_rooted": genesis.is_human_rooted,
            "chain_depth": 0,
        },
        # The graph address the run reports cost against, and the decision that
        # authorised it. Carried so an operator reading a message can answer "which
        # node is this, and who approved it?" without a database query.
        "orchestration": {
            "node_id": node.id,
            # Snapshot the accepted node's display title before publication.
            # Engine runs bypass the webhook writer that normally sets topic.
            "title": node.title,
            "flow_id": genesis.flow_id,
            "graph_address": graph_address,
            "root_decision_id": genesis.decision_id,
            "attempt": node.attempts,
        },
        "payload": {},
        "arrived_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


async def _dispatch_one_unclaimed(
    session: AsyncSession,
    node: OrchestrationNode,
    *,
    config: DispatchPassConfig,
    report: DispatchPassReport,
    repository_id: int | None = None,
    work_claim_required: bool = False,
    claim: dict | None = None,
) -> None:
    """Resolve genesis, dispatch, and queue the envelope for publication.

    Ordered so nothing is written until a complete envelope is known to be
    buildable. A node that would produce a malformed `source_ref` is refused
    **before** `dispatch_node` moves it to `running` — otherwise the worker would
    reject the message after the node had already committed to running, which is
    the invisible-dispatch failure by another route.
    """
    # READY may predate an amendment that added a prerequisite. Recheck under
    # the candidate row lock before dispatching against the current topology.
    from .tick import _predecessor_states, _unsatisfied

    if _unsatisfied(await _predecessor_states(session, org_id=node.org_id, node_id=node.id)):
        return
    org_id = node.org_id
    observed_attempts = node.attempts

    # --- Everything needed for a valid envelope, checked before any write. ---
    if not config.configured:
        logger.warning(
            "orchestration dispatch: node %s is ready but dispatch is unconfigured (%s / %s unset) — not dispatching",
            node.id,
            QUEUE_URL_ENV,
            REPO_ENV,
        )
        report.record(org_id, "undispatchable")
        return

    # The issue-routing rule, evaluated by the shared predicate rather than
    # restated here (#4334). An execution node without a routable issue has
    # nothing for an agent to act on, and materialising an issue is out of scope
    # (see the module docstring). The submission route calls the same function,
    # so what it reports and what this refuses cannot drift.
    blocker = routing_blocker_for_node(kind=node.kind, issue_ref=node.issue_ref)
    if blocker is RoutingBlocker.MISSING_ISSUE_REF:
        logger.warning("orchestration dispatch: execution node %s has no issue_ref — not dispatching", node.id)
        report.record(org_id, "undispatchable")
        return
    if blocker is RoutingBlocker.MALFORMED_ISSUE_REF:
        logger.error("orchestration dispatch: node %s has issue_ref=%r which is not an issue number — not dispatching", node.id, node.issue_ref)
        report.record(org_id, "undispatchable")
        return

    issue = issue_number_for_dispatch(node.issue_ref)
    if issue is None:
        # Unreachable: `routing_blocker_for_node` returned None for an execution
        # node, which means the reference parses. Guarded so a future change to
        # either function cannot silently produce a `source_ref` with no issue.
        logger.error("orchestration dispatch: node %s issue_ref=%r did not resolve to an issue number — not dispatching", node.id, node.issue_ref)
        report.record(org_id, "undispatchable")
        return

    installation_id = await resolve_installation_id(session, org_id=org_id)
    if installation_id is None:
        logger.warning(
            "orchestration dispatch: org %s has no single unambiguous GitHub installation — not dispatching node %s",
            org_id,
            node.id,
        )
        report.record(org_id, "undispatchable")
        return

    # PR identity is required independently of optional work ownership claims.
    # Resolve before dispatch so an unavailable provider cannot create an
    # unregistrable run or silently label new work as legacy.
    if node.kind == NodeKind.STORY.value and repository_id is None:
        from .work_admission import resolve_repository_id

        try:
            repository_id = await resolve_repository_id(org_id=org_id, installation_id=installation_id, repo=config.repo)
        except Exception:
            logger.warning("orchestration PR binding: repository identity unavailable node=%s", node.id)
            report.record(org_id, "undispatchable")
            return

    # --- Genesis: resolved here, server-side, from a real approval row. ---
    decision_id = await _latest_approval_decision_id(session, org_id=org_id, flow_id=node.flow_id)
    if decision_id is None:
        # No approval exists for this flow, so nothing human authorised this work.
        # Refusing is the only fail-closed reading (D-R12 / AC-30).
        logger.warning(
            "orchestration dispatch: flow %s (org %s) has no approval decision to root node %s — refusing",
            node.flow_id,
            org_id,
            node.id,
        )
        report.record(org_id, "genesis_refused")
        return

    report.record(org_id, "dispatches_attempted")

    try:
        genesis = await resolve_engine_genesis(session, org_id=org_id, decision_id=decision_id)
    except GenesisRefusedError as exc:
        # Fail-closed: the node stays in `ready` and nothing is published. The
        # refusal is counted so "the engine is refusing to dispatch" is visible
        # rather than looking like an idle tick.
        logger.warning("orchestration dispatch: genesis refused for node %s: %s", node.id, exc)
        report.record(org_id, "genesis_refused")
        return

    # Resolve both namespaces from the attributed approver, within this tenant.
    # The worker/vault and root ledger use users.id; personal context uses sub.
    from src.shared.identity.resolver import resolve_root_user_entity_id, resolve_user_entity_id

    user_id = await resolve_root_user_entity_id(session, org_id, genesis.root_human_id)
    cognito_sub = await resolve_user_entity_id(session, org_id, user_id)

    from datetime import datetime
    from types import SimpleNamespace

    from .evaluation_correction_state import link_id, validate_correction
    from .evaluation_issue_provider import EvaluationIssueProvider
    from .execution_policy import Action
    from .review_cycle import CycleBlockedError

    try:
        correction = await validate_correction(session, node)
        if correction is not None:
            detail = correction.detail
            if (config.repo, repository_id, installation_id) != (detail["repo"], detail["provider_repository_id"], detail["installation_id"]):
                raise CycleBlockedError("evaluation_correction_repository_changed")
            issue_state = await EvaluationIssueProvider().find(
                SimpleNamespace(org_id=org_id, repo=config.repo, provider_repository_id=repository_id, installation_id=installation_id),
                detail["content"],
                since=datetime.fromisoformat(detail["since"]),
                issue_number=detail["issue"]["number"],
            )
            if issue_state is None or issue_state.state != "open":
                raise CycleBlockedError("evaluation_correction_human_refusal")
    except (CycleBlockedError, ValueError, KeyError, TypeError):
        report.record(org_id, "policy_blocked")
        return

    # --- Policy admission (#5128): the last check before the node commits to
    # running. Placed here deliberately, after genesis and before `dispatch_node`:
    # a refusal must leave the node in `ready` with nothing published, exactly like
    # a genesis refusal. Checking after `dispatch_node` would mean the node had
    # already moved to `running` and incremented `attempts` for work that was never
    # admitted, burning an attempt against the policy's own limit.
    #
    # A flow with no accepted policy permits here, preserving legacy semantics.
    admission = await authorize_node_dispatch(
        session,
        node=node,
        # The canonical `users.id` of the attributed approver, resolved server-side
        # above. The membership lookups are keyed on this namespace.
        principal_user_id=user_id,
        target_repository=config.repo,
        installation_resolved=True,
        provider_repository_id=repository_id,
        expected_invocation_id=attempt_run_id(node.id, node.attempts + 1),
        action_override=Action.REPAIR if correction is not None else None,
    )
    if not admission.permitted:
        # `reason` is a typed `DenyReason` (#5122 renders these), so it is logged as
        # its own field rather than folded into prose a consumer would have to parse.
        logger.warning(
            "orchestration dispatch: node %s refused by execution policy reason=%s detail=%s — not dispatching",
            node.id,
            admission.reason.value if admission.reason else "",
            admission.detail,
        )
        report.record(org_id, "policy_blocked")
        if admission.reason is not None:
            # Which reason, kept out of the counter set: `record` writes named
            # fields, and the typed reasons are an open vocabulary that #5122 owns.
            report.policy_block_reasons[admission.reason.value] = report.policy_block_reasons.get(admission.reason.value, 0) + 1
        from .admission_diagnostics import description

        code = admission.reason.value if admission.reason else "authority_unverifiable"
        owner, detail, required_input = description(code)
        raise _AdmissionRefusedError(_admission_refused(code, owner=owner, required_input=required_input, detail=detail))

    selection = None
    from src.admin.persona_models.dispatch_selection import mapping_enabled, select_for_dispatch

    if mapping_enabled() and (os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true" or await _shared_continuation(session, node)):
        try:
            selection = await select_for_dispatch(
                session,
                org_id=org_id,
                user_id=user_id,
                persona=EVALUATION_PERSONA if node.kind == NodeKind.EVAL.value else config.persona,
            )
        except Exception:
            logger.exception("Saved persona model unavailable for node %s; node remains ready", node.id)
            report.record(org_id, "policy_blocked")
            report.policy_block_reasons["persona_model_selection_unavailable"] = (
                report.policy_block_reasons.get("persona_model_selection_unavailable", 0) + 1
            )
            return

    # A repair inherits the implementation identity, not an old approval. Resolve
    # provider truth before consuming an attempt; failure leaves the node READY.
    prior_binding = None
    repair_pr = None
    prior_revision = None
    if node.kind == NodeKind.STORY.value and observed_attempts > 0:
        from .pr_bindings import active_binding_for_node, binding_scope_matches, completion_candidate
        from .pr_identity import resolve_pr_identity

        prior_binding = await active_binding_for_node(session, org_id=org_id, node_id=node.id, attempt=observed_attempts)
        if prior_binding is not None:
            if completion_candidate(prior_binding) or not binding_scope_matches(prior_binding, node):
                report.record(org_id, "policy_blocked")
                report.policy_block_reasons["repair_binding_scope_changed"] = 1
                return
            if (
                prior_binding.repo.lower() != config.repo.lower()
                or prior_binding.provider_repository_id != repository_id
                or prior_binding.installation_id != installation_id
            ):
                report.record(org_id, "policy_blocked")
                report.policy_block_reasons["repair_binding_repository_changed"] = 1
                return
            try:
                repair_pr = await resolve_pr_identity(
                    org_id=org_id,
                    installation_id=installation_id,
                    repo=prior_binding.repo,
                    pr_number=prior_binding.pr_number,
                )
                if (repair_pr.provider_repository_id, repair_pr.provider_pr_node_id) != (
                    prior_binding.provider_repository_id,
                    prior_binding.provider_pr_node_id,
                ):
                    raise ValueError("implementation identity changed")
            except Exception:
                report.record(org_id, "policy_blocked")
                report.policy_block_reasons["repair_binding_unverifiable"] = 1
                return
            prior_revision = prior_binding.revision

    outcome = await dispatch_node(session, node, genesis)

    if outcome.status is DispatchStatus.REJECTED:
        # Already recorded as a `TRANSITION_REJECTED` decision row inside
        # `dispatch_node`. Under RULING 5 that row is the primary detector for
        # off-plan activity, so it must survive as evidence.
        report.record(org_id, "transitions_rejected")
        return

    if outcome.status is DispatchStatus.ALREADY_RUNNING:
        # Another pass got there first. Exactly one run in total is the correct
        # outcome (R-NF2), so this attempt creates none.
        report.record(org_id, "lost_races")
        return

    if outcome.status is DispatchStatus.NOT_FOUND or outcome.run is None:
        logger.warning("orchestration dispatch: node %s could not be dispatched: %s", node.id, outcome.reason)
        report.record(org_id, "undispatchable")
        return

    run = outcome.run
    envelope = _build_envelope(
        node=node,
        genesis=genesis,
        graph_address=run.graph_address,
        installation_id=installation_id,
        issue=issue,
        config=config,
        user_id=user_id,
        cognito_sub=cognito_sub,
    )
    if selection is not None:
        envelope["model_selection"] = selection
        if selection["model"] is not None:
            envelope["model_resolved"] = selection["model"]

    if correction is not None:
        detail = correction.detail
        envelope["orchestration"]["correction"] = {
            "receipt_id": link_id(node.id),
            **{key: detail[key] for key in ("parent_run_id", "parent_principal", "parent_grant_id", "parent_grant_epoch", "chain_depth")},
        }
        envelope["correlation"].update(parent_principal=detail["parent_principal"], chain_depth=detail["chain_depth"])
    if repository_id is not None:
        envelope["source_ref"]["provider_repository_id"] = repository_id
    if work_claim_required:
        envelope["work_claim_required"] = True

    # #5301: a story dispatch must produce a durable PR binding, and the marker is
    # recorded on BOTH the envelope and the decision below.
    #
    # On the envelope, so the worker knows to register the PR it opens. On the
    # decision, because that is what `results.binding_required` reads to decide
    # whether a missing binding is a hold (this contract) or a fallback to the old
    # issue-closure evidence (a legacy dispatch). Reading it from the run's own
    # dispatch record is what makes the boundary deterministic rather than a
    # deploy-time or wall-clock inference.
    #
    binding_required = node.kind == NodeKind.STORY.value
    if binding_required:
        envelope["pr_binding_required"] = True

    # #5144: the same envelope-and-decision pattern, for the same reason. A story
    # dispatch owes a durable continuation receipt before its worker's exit means
    # anything, and `handoff.handoff_required` reads this off the decision to decide
    # whether a missing receipt holds the node or is simply not applicable.
    #
    # The marker is only set when the receipt has a *producer*: an execution row this
    # dispatch actually admitted. `work_claim_required` alone is not that — a held
    # work claim says who owns the issue, not that the engine has a durable execution
    # to commit a receipt against. Promising a receipt that nothing can issue would
    # hold every such story forever, which is worse than the defect being closed.
    #
    # Policy-bound only, for the reason #5128 states once: a flow with no accepted
    # policy keeps legacy semantics exactly, so it gets no marker and reaches the
    # code it reached before.
    admission = _NO_POLICY
    if binding_required and work_claim_required and claim is not None:
        admission = await _admit_execution(
            session,
            node=node,
            claim=claim,
        )
    if admission.kind is AdmissionKind.REFUSED:
        # Abandon the dispatch. Previously a refusal here fell through as "no
        # marker", publishing a policy-bound story that could then complete with no
        # continuation receipt — the F3 defect. Raised rather than returned so the
        # ownership reservation and this node's `running` transition unwind with it:
        # the node is left `ready`, nothing is published, and the caller records the
        # typed refusal after the rollback (where it can survive).
        raise _AdmissionRefusedError(admission)
    execution_admitted = admission.kind is AdmissionKind.ADMITTED
    shared_continuation = execution_admitted and await _shared_continuation(session, node)
    receipt_required = execution_admitted and not shared_continuation
    if execution_admitted:
        if receipt_required:
            envelope["handoff_required"] = True
        # #5144 item 1: the fences the worker must see echoed back before it may
        # report an accepted handoff. Dispatch facts for comparison only — the
        # gateway resolves its own authority from protected state and trusts nothing
        # the worker returns. Without these the worker can verify the server said
        # "yes" but not that the "yes" was about its own work.
        identity = admission.identity
        assert identity is not None
        envelope["handoff_expect"] = {
            "contract_version": HANDOFF_RECEIPT_CONTRACT_VERSION,
            "execution_id": admission.execution_id,
            "policy_id": admission.policy_id,
            "policy_hash": admission.policy_hash,
            "org_id": identity.org_id,
            "flow_id": node.flow_id,
            "node_id": identity.node_id,
            "cycle": identity.cycle,
            "accepted_plan_version": identity.accepted_plan_version,
            "claim_id": identity.claim_id,
            "claim_generation": identity.claim_generation,
        }
        if shared_continuation:
            envelope["execution_continuation"] = dict(envelope["handoff_expect"])
            envelope["action"] = Action.REPAIR.value if observed_attempts else Action.DEVELOP.value

    session.add(
        OrchestrationDecision(
            org_id=org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=DecisionKind.NODE_DISPATCHED.value,
            actor_id="system:orchestration-dispatch",
            actor_role="engine",
            actor_kind=ActorKind.SERVICE.value,
            from_state=NodeState.READY.value,
            to_state=NodeState.RUNNING.value,
            reason=json.dumps(
                {
                    "run_id": envelope["message_id"],
                    "attempt": node.attempts,
                    "arrived_at": envelope["arrived_at"],
                    "repo": config.repo,
                    "issue": issue,
                    "root_decision_id": genesis.decision_id,
                    "pr_binding_required": binding_required,
                    "handoff_required": receipt_required,
                    "provider_repository_id": repository_id,
                    "installation_id": installation_id,
                }
            ),
        )
    )
    await session.flush()

    if prior_binding is not None:
        from .pr_bindings import binding_summary, carry_forward_binding, resolve_registration_target

        current_binding = await carry_forward_binding(
            session,
            node=node,
            previous_attempt=observed_attempts,
            target=await resolve_registration_target(session, run_id=envelope["message_id"], expected_org_id=org_id),
            pr=repair_pr,
            expected_revision=prior_revision,
        )
        envelope["bound_pull_request"] = {
            **binding_summary(current_binding),
            "provider_repository_id": current_binding.provider_repository_id,
            "provider_pr_node_id": current_binding.provider_pr_node_id,
        }

    # Activate only after gateway, worker, migration and signing material have
    # been verified. The assignment commits atomically with this exact dispatch.
    from .report_dispatch import reporting_enabled

    if binding_required and reporting_enabled() and (shared_continuation or os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true"):
        from .run_reports import prepare_run_report

        await prepare_run_report(session, envelope)

    # Queued, not sent. The send happens in `publish_pending` after the caller
    # commits — see the module docstring on commit-then-publish.
    report.pending.append(
        PendingPublish(
            node_id=run.node_id,
            org_id=org_id,
            genesis=genesis,
            node_attempt=observed_attempts + 1,
            node_kind=node.kind,
            envelope=envelope,
            group_id=message_group_id(org_id=org_id, node_id=run.node_id),
            deduplication_id=message_deduplication_id(
                node_id=run.node_id,
                decision_id=genesis.decision_id,
                # `dispatch_node` incremented `attempts`, so the committed value
                # is one past what we observed. Using the committed value keeps a
                # human resume's re-dispatch distinct from the original.
                attempt=observed_attempts + 1,
            ),
        )
    )
    report.record(org_id, "dispatched")

    logger.info(
        "orchestration dispatch: node %s dispatched address=%s root_decision=%s org=%s — envelope queued for publish",
        run.node_id,
        run.graph_address,
        genesis.decision_id,
        org_id,
    )


async def _admit_execution(session: AsyncSession, *, node: OrchestrationNode, claim: dict) -> _ExecutionAdmission:
    """Create this dispatch's durable execution, so a handoff receipt has a producer (#5144).

    Returns one of three distinct answers (:class:`AdmissionKind`) rather than a
    bool. The bool was the F3 defect: it answered "no" for a flow with no policy
    *and* for a policy-bound flow whose authority could not be verified, and the
    caller published the second case as an unmarked dispatch — a story able to
    complete with no receipt, which is the hole this issue exists to close. Genuine
    absence of policy keeps legacy semantics; a refusal abandons the dispatch.

    On admission the **identity** is returned, not discarded, because the worker must
    be told which fences to expect. A worker handed only "you owe a receipt" can
    check that the server said yes but not that the receipt is *for its own
    dispatch*; the fences travel on the envelope so the readback has something to be
    compared against. They are dispatch facts, not authority — the gateway still
    resolves every fence it acts on from protected state, and nothing the worker
    echoes back is ever trusted.

    Creates nothing else and enables nothing. The row is inert unless
    `FEATURE_ORCHESTRATION_ENGINE_ENABLED` is literally "true", because the #5143
    runner is what acts on a due execution; this only records the identity a receipt
    can be committed against.
    """
    from .execution_state import ExecutionIdentity, ExecutionStoreError, OutcomeKind
    from .execution_store import create_execution
    from .policy_admission import load_in_force_policy

    claim_id, generation = claim.get("claim_id"), claim.get("generation")
    if not claim_id or type(generation) is not int or generation < 1:
        # Unusable ownership fence. This is a refusal, not absence of policy: work
        # ownership was required for this dispatch to get here, so unreadable claim
        # data is a broken invariant. The receipt's whole value is that it names the
        # generation that produced it, so publishing anyway would mean a story that
        # cannot be fenced running with no continuation.
        return _admission_refused(
            "authority_unverifiable",
            owner="platform-operator",
            required_input="reconcile the work claim for this issue before re-dispatching",
            detail="the admitted work claim carried no usable claim id or generation",
        )

    inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
    if inputs.refusal is not None:
        # A policy exists and could not be read. Distinguished from absence *because*
        # the two were previously identical here: falling back to legacy on a refusal
        # is how a policy-bound story was published with no receipt requirement.
        return _admission_refused(
            "authority_unverifiable",
            owner="platform-operator",
            required_input="resolve the in-force execution policy for this flow",
            detail=f"in-force policy could not be resolved: {inputs.refusal}",
        )
    if inputs.policy is None:
        # Genuine absence. Legacy semantics, untouched — the ONLY path that may
        # publish an unmarked dispatch. #5128 owns this rule; it is read from
        # `load_in_force_policy` and never re-decided here.
        return _NO_POLICY

    if not inputs.policy.policy_id or not inputs.policy.policy_hash:
        return _admission_refused(
            "authority_unverifiable",
            owner="platform-operator",
            required_input="restore the accepted policy identity before dispatching",
            detail="the accepted policy has no server-stamped identity",
        )

    try:
        identity = ExecutionIdentity(
            org_id=node.org_id,
            node_id=node.id,
            # One execution per delivery cycle. `attempts` has already been
            # incremented by `dispatch_node`, so this dispatch's attempt number is the
            # cycle: a retry of the same story is a new cycle with its own ledger and
            # its own receipt, which is what stops a retry from inheriting the
            # previous attempt's receipt and reading as already handed off.
            cycle=node.attempts,
            accepted_plan_version=inputs.plan_version,
            claim_id=str(claim_id),
            claim_generation=generation,
        )
    except ExecutionStoreError as exc:
        # A malformed identity under an in-force policy is a refusal. Previously this
        # let the story run unmarked, which meant a dispatch-side bug silently removed
        # the receipt requirement from policy-bound work.
        logger.warning("orchestration dispatch: node %s could not form an execution identity for #5144: %s", node.id, exc)
        return _admission_refused(
            "authority_unverifiable",
            owner="platform-operator",
            required_input="correct the execution identity for this node before re-dispatching",
            detail=f"execution identity could not be formed: {exc}",
        )

    outcome = await create_execution(session, identity=identity, flow_id=node.flow_id)
    if outcome.kind is not OutcomeKind.APPLIED or outcome.record is None:
        # CONFLICT means the stored row's authority disagrees with what this dispatch
        # was admitted under. Refusing the *dispatch* is the fail-closed direction;
        # refusing only the marker published the story anyway.
        logger.info(
            "orchestration dispatch: node %s has no admissible execution for #5144 (%s) — refusing dispatch",
            node.id,
            outcome.reason,
        )
        return _admission_refused(
            "authority_unverifiable",
            owner="platform-operator",
            required_input="reconcile this node's execution record with its current ownership generation",
            detail=f"execution admission was not applied: {outcome.reason}",
        )
    return _ExecutionAdmission(
        kind=AdmissionKind.ADMITTED,
        identity=identity,
        execution_id=outcome.record.id,
        policy_id=inputs.policy.policy_id,
        policy_hash=inputs.policy.policy_hash,
    )


class AdmissionKind(StrEnum):
    """The three genuinely different answers to "does this dispatch owe a receipt?" (#5144).

    A bool could not express this, and conflating two of the three is the defect
    being closed here: a refusal that answered "no" was published as an *unmarked*
    dispatch, so a story whose authority could not be verified went out able to
    complete with no continuation receipt at all — the precise hole #5144 exists to
    shut.

    * ``NO_POLICY`` — the flow has no accepted policy. Legacy semantics, untouched:
      no marker, no execution, and `results` reaches exactly the code it reached
      before. #5128 owns this rule.
    * ``ADMITTED`` — an execution exists under the claim generation this dispatch was
      admitted with, so a receipt has a producer. The marker is set.
    * ``REFUSED`` — a policy *does* apply and something could not be verified. Not a
      quiet downgrade to legacy: the dispatch is abandoned, nothing is published, the
      node is left `ready`, and a typed reason is recorded.
    """

    NO_POLICY = "no_policy"
    ADMITTED = "admitted"
    REFUSED = "refused"


@dataclass(frozen=True)
class _ExecutionAdmission:
    """The outcome of admitting a dispatch's durable execution.

    ``identity`` is present exactly for :attr:`AdmissionKind.ADMITTED` — it is what
    the envelope carries so the worker can check a receipt is for *its own* dispatch.
    ``block_code``/``required_input``/``detail`` are present exactly for
    :attr:`AdmissionKind.REFUSED`, so the refusal is recorded as a resolvable
    condition with an owner rather than as log prose.
    """

    kind: AdmissionKind
    identity: Any | None = None
    execution_id: str | None = None
    policy_id: str | None = None
    policy_hash: str | None = None
    block_code: str | None = None
    owner: str | None = None
    required_input: str | None = None
    detail: str | None = None


_NO_POLICY = _ExecutionAdmission(kind=AdmissionKind.NO_POLICY)


def _admission_refused(code: str, *, owner: str, required_input: str, detail: str) -> _ExecutionAdmission:
    return _ExecutionAdmission(
        kind=AdmissionKind.REFUSED,
        block_code=code,
        owner=owner,
        required_input=required_input,
        detail=detail,
    )


class _AdmissionUnusedError(Exception):
    """Roll back an ownership reservation when no dispatch was produced."""


class _AdmissionRefusedError(Exception):
    """Unwind a dispatch whose #5144 execution admission was refused.

    Carries the refusal so the caller can persist a typed block *after* the
    rollback. Recording it inside the nested transaction would roll the evidence
    back along with the dispatch, leaving a refusal nobody can see — the failure
    mode this whole issue is about.
    """

    def __init__(self, admission: _ExecutionAdmission) -> None:
        super().__init__(admission.detail or admission.block_code or "execution admission refused")
        self.admission = admission


async def _shared_continuation(session, node) -> bool:
    from .models import OrchestrationAcceptedPlan
    from .review_cycle import CycleBlockedError
    from .shared_policy import shared_inputs

    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == node.org_id,
            OrchestrationAcceptedPlan.flow_id == node.flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    if plan is None or (plan.plan_document or {}).get("execution_continuation") is None:
        return False
    try:
        # Accepted mode wins even when protected authority is enabled globally.
        # An invalid/disabled shared contract is a refusal, never a fallback.
        await shared_inputs(session, org_id=node.org_id, flow_id=node.flow_id)
        from .report_dispatch import reporting_enabled

        if not reporting_enabled():
            raise CycleBlockedError("shared_run_reporting_disabled")
        return True
    except CycleBlockedError as exc:
        raise _AdmissionRefusedError(
            _admission_refused(
                "authority_unverifiable",
                owner="platform-operator",
                required_input="restore the accepted shared continuation transport or explicitly amend the plan",
                detail=exc.reason,
            )
        ) from None


async def _dispatch_one_attempt(session, node, *, config, report, scope) -> None:
    from .evaluation_plan import accepted_evaluation, managed_evaluation
    from .work_admission import admit, enabled, require_authority, resolve_repository_id
    from .work_claims import ClaimOwner, OwnerKind, WorkClaimError

    if await managed_evaluation(session, node):
        from .repository_evaluation import observe_repository_evaluation

        accepted = await accepted_evaluation(session, node)
        if accepted is not None and accepted[1].evidence_schema == "repository-evaluation/v1":
            if not report._repository_evaluation_attempted:
                report._repository_evaluation_attempted = True
                await observe_repository_evaluation(session, node)
            return
        if accepted is None:
            await _record_admission_refusal(
                session,
                node_id=node.id,
                org_id=node.org_id,
                flow_id=node.flow_id,
                admission=_admission_refused(
                    "evaluation_specification_missing",
                    owner="flow-owner",
                    required_input="accept an explicit machine evidence specification for this evaluation",
                    detail="Machine evaluation has no executable evidence specification; worker dispatch cannot substitute human acceptance.",
                ),
                scope=scope,
            )
            report.record(node.org_id, "admission_refused")
        # The machine controller owns admission. Do not claim its issue lane or
        # create an operations worker while it is waiting for genuine evidence.
        return
    if not config.configured:
        await _dispatch_one_unclaimed(session, node, config=config, report=report)
        return
    shared_continuation = await _shared_continuation(session, node)
    if not enabled() and not shared_continuation:
        from .policy_admission import load_in_force_policy

        inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
        if inputs.policy is not None or inputs.refusal is not None:
            await _record_admission_refusal(
                session,
                node_id=node.id,
                org_id=node.org_id,
                flow_id=node.flow_id,
                admission=_admission_refused(
                    "authority_unverifiable",
                    owner="platform-operator",
                    required_input="enable work-claim admission before dispatching governed work",
                    detail="work claims are disabled; governed work cannot use legacy dispatch",
                ),
                scope=scope,
            )
            report.record(node.org_id, "admission_refused")
            return
        await _dispatch_one_unclaimed(session, node, config=config, report=report)
        return
    before = len(report.pending)
    if not shared_continuation:
        require_authority()
    installation = await resolve_installation_id(session, org_id=node.org_id)
    if installation is None:
        raise WorkClaimError("installation_unresolved", "Ownership requires a tenant installation.")
    repository_id = await resolve_repository_id(org_id=node.org_id, installation_id=installation, repo=config.repo)
    issue = int(str(node.issue_ref).lstrip("#"))
    async with session.begin_nested():
        claim = await admit(
            session,
            org_id=node.org_id,
            repository_id=repository_id,
            issue=issue,
            owner=ClaimOwner(OwnerKind.ENGINE_FLOW, node.flow_id),
            invocation_id=attempt_run_id(node.id, node.attempts + 1),
        )
        await _dispatch_one_unclaimed(
            session,
            node,
            config=config,
            report=report,
            repository_id=repository_id,
            work_claim_required=True,
            claim=claim,
        )
        if len(report.pending) == before:
            raise _AdmissionUnusedError()


async def _dispatch_one(session, node, *, config, report) -> None:
    from .admission_diagnostics import description, safe_claim_code
    from .policy_admission import load_in_force_policy
    from .work_claims import WorkClaimError

    # Capture scalar identity before any savepoint rollback expires the ORM node.
    node_id, org_id, flow_id = node.id, node.org_id, node.flow_id
    if await flow_is_paused(session, org_id=org_id, flow_id=flow_id, lock=True):
        return
    inputs = await load_in_force_policy(session, org_id=org_id, flow_id=flow_id)
    scope = {
        "attempt": node.attempts,
        "accepted_plan_version": inputs.plan_version,
        "policy_hash": inputs.policy.policy_hash if inputs.policy else None,
    }
    try:
        await _dispatch_one_attempt(session, node, config=config, report=report, scope=scope)
    except _AdmissionUnusedError:
        return
    except _AdmissionRefusedError as exc:
        # The claim/dispatch savepoint has rolled back. Only this diagnostic is
        # persisted; no claim, execution, dispatch record or attempt is retained.
        await _record_admission_refusal(session, node_id=node_id, org_id=org_id, flow_id=flow_id, admission=exc.admission, scope=scope)
        report.record(org_id, "admission_refused")
        logger.warning("orchestration admission refused node=%s reason=%s", node_id, exc.admission.block_code)
    except WorkClaimError as exc:
        code = safe_claim_code(exc.code)
        owner, detail, required_input = description(code)
        await _record_admission_refusal(
            session,
            node_id=node_id,
            org_id=org_id,
            flow_id=flow_id,
            admission=_admission_refused(code, owner=owner, required_input=required_input, detail=detail),
            scope=scope,
        )
        report.record(org_id, "undispatchable")
        report.record(org_id, "admission_refused")
        logger.warning("orchestration ownership refused node=%s reason=%s", node_id, code)


async def _record_admission_refusal(
    session: AsyncSession,
    *,
    node_id: str,
    org_id: str,
    flow_id: str,
    admission: _ExecutionAdmission,
    scope: dict | None = None,
) -> None:
    """Persist a refused execution admission as attributed, queryable evidence (#5144).

    Written as `TRANSITION_REJECTED` because that is exactly what happened — an edge
    to `running` was refused — and because RULING 5 makes those rows the primary
    detector for work that did not go where the graph said it would. The typed block
    code, its owner and the required input travel in the structured
    `rejection_reason` so an operator gets a resolvable condition rather than prose,
    and so a reader does not have to parse a message to route it.
    """
    from .admission_diagnostics import ACTOR, CONTRACT

    session.add(
        OrchestrationDecision(
            org_id=org_id,
            flow_id=flow_id,
            node_id=node_id,
            kind=DecisionKind.TRANSITION_REJECTED.value,
            actor_id=ACTOR,
            actor_role="engine",
            actor_kind=ActorKind.SERVICE.value,
            from_state=NodeState.READY.value,
            # No `to_state`: the node went nowhere. A recorded destination here would
            # read as a dispatch that happened and was then undone.
            to_state=None,
            reason="execution admission refused; no dispatch was published and the node remains ready",
            rejection_reason=json.dumps(
                {
                    "issue": "5144",
                    **({"contract": CONTRACT, **scope} if scope is not None else {}),
                    "block_code": admission.block_code,
                    "owner": admission.owner,
                    "required_input": admission.required_input,
                    "detail": admission.detail,
                }
            ),
        )
    )
    await session.flush()


async def run_dispatch_pass(
    session: AsyncSession,
    config: DispatchPassConfig | None = None,
) -> DispatchPassReport:
    """The database half of dispatch. **Commits nothing.**

    Selects `ready` execution nodes up to the per-tick cap, resolves each one's human
    root from a real approval row, and moves it to `running`. The envelopes it
    intends to publish are returned on the report; the caller must commit and then
    call :func:`publish_pending`.

    Never raises for a per-node failure — it records the error and continues, so
    one bad node cannot stall every other flow. The failure is still reported:
    `report.success` is False and the error count is non-zero (R-NF3).

    Args:
        session: Caller-owned session. Nothing is committed here, so the state
            changes and their decision rows land atomically or not at all.
        config: Where dispatches go. Read from the environment when omitted.
    """
    cfg = config if config is not None else DispatchPassConfig.from_env()
    report = DispatchPassReport(enabled=cfg.configured)

    if not cfg.configured:
        # Not an error, but not a success story either: if there are `ready` nodes
        # they are counted as `undispatchable` below, so an unwired environment is
        # visible rather than reading as idle.
        logger.warning(
            "orchestration dispatch: pass is unconfigured (%s=%r, %s=%r); ready nodes will be reported as undispatchable",
            QUEUE_URL_ENV,
            cfg.queue_url,
            REPO_ENV,
            cfg.repo,
        )

    try:
        # Look beyond the dispatch cap so a few misconfigured nodes do not
        # consume all execution slots. Both the scan and actual sends are bounded.
        candidates = await _fetch_ready_nodes(session, limit=max(100, cfg.max_dispatches_per_tick + 1))
    except Exception:
        logger.exception("orchestration dispatch: failed to fetch ready nodes")
        report.errors += 1
        return report

    for node in candidates:
        if report.dispatched >= cfg.max_dispatches_per_tick:
            report.capped = True
            break
        node_id, org_id = node.id, node.org_id
        report.record(org_id, "nodes_examined")
        try:
            async with session.begin_nested():
                await _dispatch_one(session, node, config=cfg, report=report)
        except Exception:
            # Per-node containment, matching `run_tick`: log, count, force
            # non-success, keep going.
            logger.exception("orchestration dispatch: failed to dispatch node %s (org %s)", node_id, org_id)
            report.record(org_id, "errors")

    from .report_dispatch import recover_pending_reports

    try:
        await recover_pending_reports(session, config=cfg, report=report)
    except Exception:
        logger.exception("orchestration dispatch: durable report outbox could not be recovered")
        report.errors += 1

    return report


async def prepare_pending(
    session: AsyncSession,
    report: DispatchPassReport,
    *,
    writer: Any | None = None,
) -> DispatchPassReport:
    """Provision protected authority and attach the model-policy snapshot.

    **Post-commit, pre-publication.** Call this after the caller commits and
    before :func:`publish_pending` (PMM-07).

    Why here and nowhere else. A snapshot can only attach to a protected
    execution that is still `pending`, and that record has exactly one such
    window:

    - *Earlier is impossible.* `_dispatch_one` reserves the work claim inside the
      tick transaction, before any protected record exists, so
      `admit_pending()` -- which is what carries `ensure_snapshot_report_only`
      on every other path -- can only refuse with `dispatch_unresolved`.
    - *Later is refused.* Once the worker bootstraps, its pod bind flips the
      execution to `active` and `_persist_snapshot` refuses with
      `dispatch_not_pending`. Attaching after a worker can bind would be racing
      the run it is supposed to describe.

    Provisioning here does not move the publish ahead of the commit: the rows are
    already durable, and `publish_pending` remains the only thing that sends. The
    writer's own provisioning is idempotent, so `publish_pending` re-provisioning
    the same dispatch is harmless and a retry cannot double-publish.

    Containment is per node, matching the rest of the pass. A protected-authority
    failure drops that one dispatch and counts `publish_failed` -- it genuinely
    will not reach the queue, and `publish_pending` would have failed it anyway.
    Unavailable *model-policy* evidence is different and deliberately weaker: it
    is recorded as a receipt and the dispatch still publishes unchanged, because
    report-only must not let a proposal defect alter what executes or relax an
    unrelated authority or work gate. The later runtime-posture stage is what
    makes these failures enforcing, and only when enforcement is explicitly active.

    The envelope is never mutated here beyond what the writer itself returns, so
    the already-digested message `publish_pending` sends is byte-identical to the
    one this function saw.
    """
    if not report.pending:
        return report
    if os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true":
        return report

    from src.agentauth.model_policy import ensure_snapshot_report_only

    async def _writer() -> Any:
        """Resolve the writer, building it at most once, inside the caller's guard.

        Construction is a real failure point, not a formality: `BootstrapStore`
        raises `AuthorityStoreError` when `AGENT_AUTHORITY_TABLE` is unset, and
        boto3 client creation can fail on credentials or configuration. It must
        therefore happen *inside* the per-node `try`, the way `publish_pending`
        has always done it -- hoisted above the loop it escapes `_run` after the
        SQL commit and skips the command and projection flushes that follow, so a
        misconfigured table would stall the whole tick rather than fail the one
        dispatch that needed authority.

        Both the import and the client construction block, so this runs off the
        event loop as well.
        """
        nonlocal writer
        if writer is None:

            def _build():
                from src.agentauth.engine import get_engine_authority_writer

                return get_engine_authority_writer()

            writer = await run_in_threadpool(_build)
        return writer

    prepared: list[PendingPublish] = []
    for pending in report.pending:
        if pending.envelope.get("execution_continuation") and pending.envelope.get("run_report"):
            prepared.append(pending)
            continue
        invocation_id = pending.invocation_id()
        try:
            # Blocking DynamoDB writes: off the event loop so a slow round trip
            # cannot stall the tick's other work.
            resolved = await _writer()
            envelope = await run_in_threadpool(resolved.provision, pending)
        except Exception:
            logger.exception(
                "orchestration dispatch: protected authority unavailable for node %s during preparation; no message sent",
                pending.node_id,
            )
            report.record(pending.org_id, "publish_failed")
            continue

        # The writer returns the envelope it provisioned against. Replacing the
        # pending envelope with it keeps a single source of truth for what is
        # published, and `publish_pending`'s idempotent re-provision then returns
        # the same thing rather than a divergent one.
        prepared.append(replace(pending, envelope=envelope))

        # Now, and only now, is there a `pending` protected record to attach to.
        # `ensure_snapshot_report_only` already converts every specific failure
        # into a receipt, so this cannot raise a model-policy error into the tick.
        # `resolved`, not `writer`: the snapshot must attach to the same store the
        # record was just provisioned into.
        receipt = await ensure_snapshot_report_only(session, store=resolved.store, invocation_id=invocation_id)
        report.model_policy_receipts[invocation_id] = receipt
        if receipt.get("status") != "available":
            logger.warning(
                "orchestration dispatch: model-policy evidence unavailable for node %s reason=%s; dispatch is unaffected",
                pending.node_id,
                receipt.get("reason"),
            )

    report.pending = prepared
    return report


def publish_pending(
    report: DispatchPassReport,
    config: DispatchPassConfig | None = None,
    *,
    client: SQSClient | None = None,
    run_store: Any | None = None,
) -> DispatchPassReport:
    """Send the committed dispatches. Call this **after** the caller commits.

    Separate from :func:`run_dispatch_pass` so commit-then-publish is explicit
    rather than incidental (hazard 3). A send that fails leaves the node `running`
    with no run — recoverable by #4211's stall detector, and counted as
    `publish_failed` so the pass reports non-success rather than looking green.

    Mutates and returns the same report, so the caller's single object carries the
    final counts.
    """
    if not report.pending:
        return report

    cfg = config if config is not None else DispatchPassConfig.from_env()
    if not cfg.configured:
        # Unreachable through `run_dispatch_pass`, which produces no pending
        # publishes when unconfigured. Guarded anyway: sending to an empty queue
        # url would raise per message rather than failing once, clearly.
        for pending in report.pending:
            logger.error("orchestration dispatch: cannot publish node %s — dispatch is unconfigured", pending.node_id)
            report.record(pending.org_id, "publish_failed")
        report.pending = []
        return report

    sqs = client if client is not None else _get_sqs_client(cfg.aws_region)

    for pending in report.pending:
        envelope = pending.envelope
        shared = bool(envelope.get("execution_continuation") and envelope.get("run_report"))
        protected = not shared and os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() == "true"
        if protected:
            try:
                from src.agentauth.engine import get_engine_authority_writer

                envelope = get_engine_authority_writer().provision(pending)
            except Exception:
                logger.error("orchestration dispatch: protected authority unavailable for node %s; no message sent", pending.node_id)
                report.record(pending.org_id, "publish_failed")
                continue
        body = json.dumps(envelope, default=str)
        if len(body.encode("utf-8")) > MAX_SQS_MESSAGE_BYTES:
            # The engine's envelope carries no raw webhook payload, so this is not
            # reachable with today's shape. Refusing rather than truncating is
            # still the right failure: a truncated envelope is a malformed one,
            # and the node is already `running` and therefore stall-recoverable.
            logger.error("orchestration dispatch: envelope for node %s exceeds the SQS size limit — not publishing", pending.node_id)
            report.record(pending.org_id, "publish_failed")
            continue

        try:
            if not protected:
                from .run_store import EngineRunStore

                store = run_store if run_store is not None else EngineRunStore.from_env()
                store.register(envelope)
            # Protected dispatch already wrote this same reporting row together
            # with the execution/grant in one conditional DynamoDB transaction.
            response = sqs.send_message(
                QueueUrl=cfg.queue_url,
                MessageBody=body,
                MessageGroupId=pending.group_id,
                MessageDeduplicationId=pending.deduplication_id,
            )
        except Exception:
            # Counted, never swallowed. The node stays `running`; #4211's stall
            # detector is the named recovery path.
            logger.exception(
                "orchestration dispatch: publish failed for node %s — node remains 'running' and is recoverable by the stall detector",
                pending.node_id,
            )
            report.record(pending.org_id, "publish_failed")
            continue

        logger.info(
            "orchestration dispatch: published node %s sqs_message_id=%s group=%s",
            pending.node_id,
            (response or {}).get("MessageId", ""),
            pending.group_id,
        )

    report.pending = []
    return report
