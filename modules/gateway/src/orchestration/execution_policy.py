"""The accepted execution policy: what an owner authorized, and the one check that enforces it.

Issue #5128 (EPIC #4191). #4529 authors a policy conversationally; #5122 consumes
the decision this module returns. This module owns the schema and the rule, and
nothing else owns either.

--------------------------------------------------------------------------------
The gap this closes
--------------------------------------------------------------------------------

An accepted plan says *what* work to do. It says nothing about **where** agents may
act or **how much** they may spend, so every autonomous action so far has been
admitted on the strength of "a human approved this plan" alone. That is a real
human decision (`genesis.py` resolves it from a real row), but it is not a
*permission*: it cannot distinguish "deploy to the environment I registered" from
"deploy to any environment reachable with the platform's credentials", and it
carries no allowance, so a repair loop that fans out spends until some unrelated
per-run cap happens to catch it.

This module makes the authorization explicit and *bounded*, recorded once at
acceptance and re-checked immediately before every dispatch, merge, deploy and
machine acceptance.

--------------------------------------------------------------------------------
Why the policy rides the plan document instead of getting its own table
--------------------------------------------------------------------------------

`orchestration_accepted_plans` already stores the accepted document verbatim, is
already versioned, and already marks superseded versions rather than rewriting
them (`029_orchestration_graph.py`). Those are exactly the three properties a
policy needs: *one bounded policy per accepted plan version*, an amendment
producing a new version, and "what was authorized at that gate" staying
answerable afterwards. A policy table would re-derive all three and could then
disagree with the plan it belongs to about which version is in force.

So there is **no migration here**, for the same reason `compile.py` gives for
`spec_revision`: a column for a field already inside the stored document would be
two homes for one value.

--------------------------------------------------------------------------------
Why the policy id is DERIVED, not minted
--------------------------------------------------------------------------------

The id and hash are server-stamped — a document arriving with either one set is
**rejected**, not overwritten, because silently replacing a caller's value would
let an author believe they had named a policy that the server actually renamed.

But they are stamped by *deriving* them from the policy's own content, not by
generating a fresh uuid. That is not a style choice, and getting it wrong breaks
retries specifically:

`plan_hash` is what idempotency compares (`compile.py`), and the policy is inside
the hashed document. A minted-per-acceptance id would change the document hash on
every submission, so a retried acceptance of the *same* policy — a dropped
connection, a duplicate delivery — would no longer match its own in-force plan.
It would fall past the idempotency return and be refused as a plan-of-record
rewrite, turning a network blip into a permanent failure. A content-derived id
converges instead.

The principal binding is the one field that is genuinely *resolved* rather than
derived: it comes from the acceptance context's authenticated identity, and a
caller-supplied `principal_id` is refused. A policy that could name its own
principal could name any principal, which is the same forgery `genesis.py` refuses
for `root_human_id` — and refused the same way, by making it unrepresentable
rather than by checking for it.

--------------------------------------------------------------------------------
Why `authorize_action` is a PURE function
--------------------------------------------------------------------------------

Every fact it needs is *live* — current membership, current connection ownership,
observed spend, the version actually in force — and every one of those has an
existing owner elsewhere in the codebase. So this module does not resolve them; it
takes them as an :class:`AuthorizationContext` and decides.

That split is what makes the rule testable. The interesting cases here are
adversarial (revoked membership, a stale version, unknown spend), and a rule that
opened its own database connection could only be tested through fixtures that
simulate those states in the store. As a pure function each one is a literal. It
is also the interface #5122 consumes, which is why it is published from here
rather than inlined into `dispatch_pass.py`.

The load-bearing consequence: **a caller that cannot resolve a fact must pass
`None`, and `None` denies.** It must never pass a convenient default. Two fields
are typed `| None` for exactly this and are documented at their definitions —
`observed_spend_usd` and `credential_scope` — because a `0` for unknown spend and
a `True` for unverified credential scope are the two substitutions that would
turn this check into a rubber stamp while every test still passed.

--------------------------------------------------------------------------------
Why this is not `agentauth/grants.py` (R-N2a: enums are imported, never redefined)
--------------------------------------------------------------------------------

`agentauth.grants.evaluate_grant` is also a pure authorization function, and
`AgentAction` is also an action enum, so the reuse question is a fair one. They
arbitrate different questions and share no members:

- `AgentAction` is **run-control** vocabulary — dispatch, monitor, pause, resume,
  steer, abort. It answers "may this execution identity control *that running
  agent*?", keyed on a principal of the form `<invocation_id>#<attempt>`, and its
  targets are runs.
- :class:`Action` here is **delivery-lifecycle** vocabulary — develop, review,
  repair, merge, deploy, evaluate (#5128's six), plus `coordinate` (#5224). It
  answers "did the plan owner authorize *this kind of work*, here, within these
  bounds?", keyed on a human principal, and its targets are repositories and
  environments.

`coordinate` is the one member that authorizes *requesting* work rather than doing
any, and it is still not `AgentAction.DISPATCH`: that answers "may this execution
identity control that running agent?", while this answers "did the owner accept a
coordinator over this node set, and is this child inside its bounds?". A coordinator
needs both, from the two different planes, and neither substitutes for the other.

Merging them would put "may I abort a sibling run?" and "may we deploy to
production?" in one enum, and every grant would then have to enumerate members
meaningless to it. R-N2a exists to stop two checks that agree on the day they are
written from drifting apart; two checks that never overlapped cannot drift.

What IS shared deliberately is the *wire vocabulary* for refusals:
`repository_not_permitted`, `grant_revoked` and `action_not_permitted` reuse the
reason strings `evaluate_grant` already returns, so an operator reading two
different denials of the same shape reads the same word. The difference is that
these are a :class:`DenyReason` enum rather than bare strings, because #5122
renders them.

--------------------------------------------------------------------------------
What this module deliberately does NOT do
--------------------------------------------------------------------------------

- **It never approves a human gate.** :class:`AcceptanceMode` says whether an
  *evaluation* may be concluded by machine. It is not authority over a gate node,
  and the two mechanisms are independent: an action listed in `human_gates` is
  refused with `HUMAN_GATE_REQUIRED` no matter what every evaluation's acceptance
  mode says, and the human-only recovery edges in `state.py` are untouched. An
  owner authorizing autonomous delivery is not an owner delegating their own
  approval.
- **It never widens itself.** The policy is read from the accepted document; there
  is no path here that writes one. A worker cannot reach these tables at all
  (`compile.py`), and nothing here reads authority from an envelope, an issue
  comment or a row a worker can write (R-O5d).
- **It never falls back to a platform credential.** Version 1 requires `SCOPED`.
  Version 2 additionally permits `USER_GRANTED` for explicitly accepted user
  credentials/roles and actions. Their provider permissions and lifetime remain
  user-configured; no existing policy silently acquires this authority.
- **Coordination authority never substitutes for a child's own authority.** A
  policy accepting `COORDINATE` (schema v3, bounded by :class:`CoordinationScope`)
  lets a coordinator read progress and *request* an eligible child through
  :func:`authorize_child_request`. That permit means "the coordinator was allowed to
  ask" and nothing more: the child still needs its own accepted action, its own live
  work claim and its own bounded grant. A coordinator cannot approve, accept or
  resume a human gate, conclude an evaluation, merge, deploy, widen its own scope or
  issue arbitrary credentials — none of those are reachable from `coordinate`, and
  `MERGE`/`DEPLOY`/`EVALUATE` are refused as child actions at acceptance *and* at
  admission. Version 1 and 2 documents carry no scope, and **absence grants
  nothing** rather than defaulting to one.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .address import ADDRESS_PATTERN

__all__ = [
    "POLICY_SCHEMA_VERSION",
    "SUPPORTED_POLICY_SCHEMA_VERSIONS",
    "UserCredentialAuthority",
    "Action",
    "AcceptanceMode",
    "AuthorizationContext",
    "ChildPersona",
    "CoordinationScope",
    "CoordinationSummary",
    "CredentialScope",
    "Decision",
    "DenyReason",
    "ExecutionPolicy",
    "PolicyLimits",
    "PolicyRejectedError",
    "PolicySummary",
    "ResourceRef",
    "authorize_action",
    "authorize_child_request",
    "flow_budget_binding",
    "policy_hash",
    "stamp_policy",
    "summarize_policy",
]


# The policy schema's own version, bumped when a change would make an older
# accepted policy read differently. Stamped into the document rather than inferred
# from which fields are present: an accepted policy is read back long after the
# code that wrote it, and "which rules did this mean?" must not be guessed from
# its shape. `authorize_action` denies a version it was not built for
# (`schema_unsupported`) rather than interpreting it optimistically.
POLICY_SCHEMA_VERSION = 1
SUPPORTED_POLICY_SCHEMA_VERSIONS = frozenset({1, 2, 3})

# The version that carries `coordination`. Version 3 rather than a new optional
# field on 2 because coordinate authority changes what an *already accepted*
# document would mean if it were reinterpreted: a v1/v2 policy has no coordination
# scope, and the correct reading of that absence is "no coordinator", not "a
# coordinator with default bounds". Pinning it to a version makes that reading
# structural — `_coordination_requires_v3` refuses the combination outright rather
# than silently upgrading an older accepted policy (#5224).
COORDINATION_SCHEMA_VERSION = 3

# Which versions carry the v2 user-credential contract. One name, read by both the
# parse-time validator and the admission rule, because those two disagreeing is a
# silent authority change in either direction: a validator that accepts a document
# the rule then ignores, or a rule that honours a field the validator refuses.
_USER_CREDENTIAL_SCHEMA_VERSIONS = frozenset({2, COORDINATION_SCHEMA_VERSION})


class Action(StrEnum):
    """The autonomous actions a policy can permit.

    A closed set, and the closure is the point: an unknown action string fails
    validation at acceptance rather than being carried into the document and
    silently permitted (or silently ignored) at admission. Either of those would
    make the policy's text disagree with its effect.

    `MERGE` and `DEPLOY` are separate from `DEVELOP` because they are the two
    actions with effects outside the platform — a merged branch and a changed
    environment survive the run that made them. An owner permitting delivery work
    is not thereby permitting either.

    `COORDINATE` is separate from all of them for a different reason: it is
    authority to *request* work, not to perform any. See :class:`CoordinationScope`.
    """

    DEVELOP = "develop"
    REVIEW = "review"
    REPAIR = "repair"
    MERGE = "merge"
    DEPLOY = "deploy"
    EVALUATE = "evaluate"
    # Authority to read progress and request an eligible child dispatch, and
    # nothing else. A coordinator holding this cannot itself develop, review,
    # merge, deploy or conclude an evaluation — each child needs its own accepted
    # action, its own live claim and its own bounded grant, which is why
    # `authorize_child_request` is a separate check rather than a branch in the
    # action test. Bounded by :class:`CoordinationScope`; requires schema v3.
    COORDINATE = "coordinate"


class AcceptanceMode(StrEnum):
    """Who may conclude one evaluation.

    `HUMAN` means a person decides. `MACHINE` means the engine may conclude *this
    evaluation* from its own evidence — a test suite that either passed or did not.

    `MACHINE` is never authority over a gate node. Evaluations and gates are
    different node kinds with different meanings (`proposal.py` rule 4), and the
    distinction is what keeps "the engine may read a green test run" apart from
    "the engine may approve its own work". :func:`authorize_action` enforces that
    separation directly rather than trusting callers to respect it.
    """

    HUMAN = "human"
    MACHINE = "machine"


class CredentialScope(StrEnum):
    """Whether a narrowly-scoped credential could actually be issued for an action.

    Resolved by the caller from the existing credential services, never by this
    module. Distinct values preserve the source and limits of authority:

    - `SCOPED` — a credential limited to this policy's repositories/connections.
    - `USER_GRANTED` — version 2 explicitly accepts a selected user's credential
      or role with its configured provider permissions and provider-managed lifetime.
    - `UNSCOPABLE` — the provider cannot express a credential this narrow. A block,
      and the issue's named follow-up: the missing provider capability becomes a
      linked implementation prerequisite rather than a reason to relax the rule.
    - `UNKNOWN` — the caller could not determine it (the service was unreachable).
      Also a block.

    Collapsing these into a bool would make the two block cases indistinguishable
    in the deny reason, and an operator reading `credential_scope_unavailable`
    needs to know whether to wait for a service or to go implement a capability.
    """

    SCOPED = "scoped"
    USER_GRANTED = "user_granted"
    UNSCOPABLE = "unscopable"
    UNKNOWN = "unknown"


class DenyReason(StrEnum):
    """Why an action was refused. **Typed for #5122**, which renders these.

    An enum rather than prose because the consumer is a machine surface: #5122
    persists and displays the reason, and a caller matching on message text would
    break the moment the wording improved. The `Decision.detail` string carries the
    human explanation alongside.

    Every member is reachable from at least one test, and no member is a catch-all
    — "denied for some reason" is not auditable, which is the whole complaint the
    parent EPIC was filed about.
    """

    SCHEMA_UNSUPPORTED = "schema_unsupported"
    STALE_POLICY_VERSION = "stale_policy_version"
    POLICY_EXPIRED = "policy_expired"
    WALL_CLOCK_LIMIT_EXCEEDED = "wall_clock_limit_exceeded"
    GRANT_REVOKED = "grant_revoked"
    MEMBERSHIP_REVOKED = "membership_revoked"
    ROLE_REVOKED = "role_revoked"
    ORG_MISMATCH = "org_mismatch"
    TEAM_NOT_PERMITTED = "team_not_permitted"
    ACTION_NOT_PERMITTED = "action_not_permitted"
    HUMAN_GATE_REQUIRED = "human_gate_required"
    REPOSITORY_NOT_PERMITTED = "repository_not_permitted"
    ENVIRONMENT_NOT_PERMITTED = "environment_not_permitted"
    WORK_NOT_OWNED = "work_not_owned"
    CREDENTIAL_SCOPE_UNAVAILABLE = "credential_scope_unavailable"
    SPEND_UNKNOWN = "spend_unknown"
    BUDGET_UNAVAILABLE = "budget_unavailable"
    SPEND_LIMIT_EXCEEDED = "spend_limit_exceeded"
    ATTEMPT_LIMIT_EXCEEDED = "attempt_limit_exceeded"
    CONCURRENCY_LIMIT_EXCEEDED = "concurrency_limit_exceeded"
    MACHINE_ACCEPTANCE_NOT_PERMITTED = "machine_acceptance_not_permitted"
    # --- Coordination (#5224) ---------------------------------------------
    # Distinct from `ACTION_NOT_PERMITTED` because the operator responses differ:
    # the scope refusals mean "this coordinator is bounded and you are outside the
    # bounds" (amend the accepted scope), while `COORDINATION_NOT_PERMITTED` means
    # the policy never accepted a coordinator at all.
    COORDINATION_NOT_PERMITTED = "coordination_not_permitted"
    COORDINATION_NODE_NOT_ASSIGNED = "coordination_node_not_assigned"
    CHILD_PERSONA_NOT_PERMITTED = "child_persona_not_permitted"
    CHILD_ACTION_NOT_PERMITTED = "child_action_not_permitted"


class PolicyRejectedError(ValueError):
    """A policy document that cannot be accepted as written.

    Raised by :func:`stamp_policy` for the two things a caller must not supply —
    server-stamped provenance and a caller-invented principal. A `ValueError`
    subclass so the acceptance routes' existing 422 mapping covers it without a
    new handler; the malformed-policy case the issue specifies as 422 is exactly
    this.
    """


class PolicyLimits(BaseModel):
    """The bounds an accepted policy places on autonomous work.

    **Every field is required and every field must be positive**, and there is no
    sentinel for "no limit". That is the issue's "unbounded values fail
    validation" requirement made structural rather than checked: an owner cannot
    author an unbounded policy even by accident, because the schema has no way to
    express one. A `None`-means-unlimited default would have made unbounded the
    *easiest* policy to write.

    Positive rather than non-negative: a zero limit authorizes nothing, so a policy
    carrying one is either a mistake or a misunderstanding of the field, and
    accepting it would produce a policy that permits actions the owner listed while
    blocking every one of them at admission.
    """

    model_config = ConfigDict(extra="forbid")

    # Shared wall-clock ceiling from the flow's first committed dispatch.
    max_wall_clock_seconds: int = Field(gt=0, le=86_400)
    # Total spend this policy authorizes, reserved plus settled, across every
    # descendant. NOT per run and NOT per child — see `flow_budget_binding` for why
    # the allowance has to be shared to mean anything.
    max_spend_usd: Decimal = Field(gt=0, le=Decimal("100000"))
    # Attempts for any single node, which is what bounds a repair loop.
    max_attempts_per_node: int = Field(gt=0, le=100)
    # Simultaneous admitted actions under this policy, which is what bounds fan-out.
    max_concurrent_actions: int = Field(gt=0, le=100)


class UserCredentialAuthority(BaseModel):
    """Explicit approval of user-configured provider permissions, not platform fallback.

    ADP gates credential issuance for the approved work. The provider controls
    permissions and lifetime of issued credentials, including copied API keys.
    """

    model_config = ConfigDict(extra="forbid")
    permission_mode: Literal["user_configured"]
    lifetime: Literal["provider_managed"]
    vault_credential_ids: list[str] = Field(default_factory=list, max_length=64)
    aws_role_arns: list[str] = Field(default_factory=list, max_length=32)
    actions: list[Action] = Field(min_length=1, max_length=len(Action))

    @model_validator(mode="after")
    def _explicit_targets(self):
        if not self.vault_credential_ids and not self.aws_role_arns:
            raise ValueError("user credential authority requires named credentials or IAM roles")
        for name in ("vault_credential_ids", "aws_role_arns", "actions"):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicates")
        if any(not value.strip() or len(value) > 255 for value in self.vault_credential_ids):
            raise ValueError("invalid vault credential identifier")
        if any(not re.fullmatch(r"arn:aws(?:-[a-z-]+)?:iam::[0-9]{12}:role/[a-zA-Z0-9+=,.@_/-]+", arn) for arn in self.aws_role_arns):
            raise ValueError("IAM roles must be exact role ARNs without wildcards")
        return self


class ChildPersona(StrEnum):
    """The worker personas a coordinator may request.

    A closed set for the same reason :class:`Action` is closed: an unknown persona
    string in an accepted document would either be silently permitted or silently
    ignored, and both make the policy's text disagree with its effect.

    `OPERATIONS` is deliberately absent. A coordinator that could request another
    coordinator could build an unbounded tree of them, and every limit in
    :class:`PolicyLimits` is per-policy rather than per-level, so the tree would
    share one allowance while multiplying the requesters spending it. Coordinating
    a successor wave is the existing engine dispatch path's job, not a child
    request (#5224 design point 4: one clear owner for the lane).
    """

    DEVELOPER = "developer"
    REVIEWER = "reviewer"


class CoordinationScope(BaseModel):
    """The exact bounds of a coordinator's authority. **Required for `COORDINATE`.**

    There is no "coordinate everything" spelling, and that is the point — the same
    structural argument :class:`PolicyLimits` makes about unbounded limits. A
    coordinator is the one role whose whole job is to cause *other* work, so an
    unbounded one multiplies every other authority in the policy. `min_length=1` on
    both lists means an owner cannot accept a coordinator that names no node set or
    no child action even by accident.

    Note what is NOT here: no spend, attempt, concurrency or wall-clock fields. Those
    stay in :class:`PolicyLimits` and are shared with every other action under this
    policy. A coordinator with its own allowance would be a second budget for the
    same delivery, and the flow meter (`flow_budget_binding`) exists precisely so
    that a coordinator's children charge the *same* meter as everything else. A
    coordinator cannot widen its flow's allowance by fanning out.
    """

    model_config = ConfigDict(extra="forbid")

    # The exact graph addresses this coordinator is assigned to. Full addresses
    # rather than a prefix or a glob: a prefix would silently extend authority to
    # every node added under it after acceptance, so the owner would be accepting a
    # scope whose membership changes without them. Validated against
    # `ADDRESS_PATTERN` below for the same reason `evaluation_acceptance` keys are.
    assigned_node_addresses: list[str] = Field(min_length=1, max_length=512)
    # Which personas this coordinator may request. See :class:`ChildPersona`.
    allowed_child_personas: list[ChildPersona] = Field(min_length=1, max_length=len(ChildPersona))
    # The actions a requested child may take. Constrained further at admission:
    # `authorize_child_request` also requires each one to be in the policy's own
    # `allowed_actions` and refuses any that the policy gates to a human, so a
    # coordinator can never route around a gate by naming the action here.
    allowed_child_actions: list[Action] = Field(min_length=1, max_length=len(Action))

    @model_validator(mode="after")
    def _bounded_and_canonical(self) -> CoordinationScope:
        for name in ("assigned_node_addresses", "allowed_child_personas", "allowed_child_actions"):
            values = [str(value) for value in getattr(self, name)]
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                raise ValueError(f"{name} repeats: {', '.join(duplicates)}")
        malformed = sorted(address for address in self.assigned_node_addresses if not ADDRESS_PATTERN.match(address))
        if malformed:
            raise ValueError(f"assigned_node_addresses must be graph addresses of the form 'flow/epic/wave/node': {', '.join(malformed)}")
        # A coordinator that may request a coordinator is the unbounded-tree case
        # `ChildPersona` documents. Refused structurally as well as by the enum, so
        # adding a member to `ChildPersona` cannot quietly enable it.
        if Action.COORDINATE in self.allowed_child_actions:
            raise ValueError("allowed_child_actions must not include 'coordinate'; a coordinator cannot delegate coordination authority onward")
        # Machine acceptance and the two out-of-platform actions are human-gate
        # territory. Naming them here would read as a control an owner granted, and
        # `authorize_child_request` denies them anyway — an entry that cannot take
        # effect but looks like authority is the misreading `_human_gates_are_declared_actions` refuses too.
        forbidden = sorted(set(self.allowed_child_actions) & {Action.MERGE, Action.DEPLOY, Action.EVALUATE})
        if forbidden:
            raise ValueError(
                f"allowed_child_actions must not include {', '.join(forbidden)}; merging, deploying and concluding an evaluation "
                "are not delegable through coordination authority"
            )
        return self


class ExecutionPolicy(BaseModel):
    """What a plan owner authorized, recorded on the accepted plan version.

    `extra="forbid"` is doing real work here beyond tidiness. It is what makes "no
    secret material" structural: there is no free-form field on this model, so a
    document carrying `token`, `password` or any other unrecognised key is a 422
    rather than a value quietly persisted into an accepted plan and returned by
    every read of it. A permissive model would have made the policy a place to
    stash credentials by accident.

    The three server-stamped fields are `None` on submission and set by
    :func:`stamp_policy`. They are on this model rather than a wrapper so the
    accepted document is self-describing — a policy read back out of
    `plan_document` carries its own identity without a join.
    """

    model_config = ConfigDict(extra="forbid")

    # Pinned, not defaulted-and-ignored: `authorize_action` refuses a version it
    # was not built for. A `Literal` so an unknown version is a 422 at the
    # boundary, which is where a document this build cannot interpret should stop.
    schema_version: Literal[1, 2, 3] = POLICY_SCHEMA_VERSION
    user_credentials: UserCredentialAuthority | None = None
    # The bounded coordination scope, when this policy accepts a coordinator.
    # `None` — which is every v1/v2 document — grants nothing: `authorize_action`
    # denies `COORDINATE` on absence rather than defaulting a scope, and the
    # serializer below drops the key entirely so an older accepted document's bytes
    # and hash are unchanged by this field existing (#5224).
    coordination: CoordinationScope | None = None

    # --- Server-stamped. A submitted document must leave all three unset. ---
    # Not "may leave unset": :func:`stamp_policy` rejects a document that sets any
    # of them rather than overwriting it, so an author is never told their value
    # was accepted when it was replaced.
    policy_id: str | None = None
    policy_hash: str | None = None
    # The principal whose authority this policy carries, resolved from the
    # acceptance context's authenticated identity. A caller-supplied value here is
    # the forgery `genesis.py` refuses for `root_human_id`, and it is refused the
    # same way — by having no accepted path in.
    principal_id: str | None = None

    # --- Binding: where this authority applies -----------------------------
    # The tenant. Compared against the acceptance context's server-resolved org by
    # `compile.py`'s existing Gate 2, which already refuses to re-home a document.
    org_id: str = Field(min_length=1)
    # Teams whose members may act under this policy. Empty means the binding is at
    # org level only — deliberately expressible, because a single-team org would
    # otherwise have to invent a team to authorize anything.
    team_ids: list[str] = Field(default_factory=list, max_length=64)
    # Repositories agents may act in. At least one: a policy permitting actions in
    # no repository permits nothing, and accepting it would produce a policy whose
    # every admission fails a scope check the author cannot see.
    repository_ids: list[str] = Field(min_length=1, max_length=256)
    # Registered environment connections agents may deploy to. Empty is meaningful
    # and is the safe default: a policy that permits no deployment target, which is
    # correct for a flow that only develops and reviews. `DEPLOY` against an empty
    # list therefore denies, which is why this is not `min_length=1`.
    environment_connection_ids: list[str] = Field(default_factory=list, max_length=64)

    # --- Authority: what may happen without asking again -------------------
    allowed_actions: list[Action] = Field(min_length=1, max_length=len(Action))
    # Actions that ALWAYS need a person, even when listed above. Listing an action
    # in both is not a contradiction to resolve — it is how an owner says "agents
    # may prepare this, a human releases it" (the usual shape for `MERGE`/`DEPLOY`).
    # `authorize_action` checks this second, so the gate wins.
    human_gates: list[Action] = Field(default_factory=list, max_length=len(Action))
    # Graph address -> who may conclude that evaluation. An address absent from the
    # mapping means `HUMAN`: the default has to be the restrictive one, or a typo in
    # an address would silently promote an evaluation to machine acceptance.
    evaluation_acceptance: dict[str, AcceptanceMode] = Field(default_factory=dict, max_length=512)

    # --- Lifetime ----------------------------------------------------------
    # Required. A policy with no expiry is a permanent grant, which is the thing
    # "scoped delegated authority" is defined against.
    expires_at: datetime

    limits: PolicyLimits

    @model_serializer(mode="wrap")
    def _preserve_v1_document(self, handler):
        """Drop optional-and-absent keys so older documents serialize byte-identically.

        This is what keeps `policy_hash` stable for every already-accepted v1/v2
        policy as new optional fields are added. Emitting `"coordination": null`
        would change the canonical JSON of every existing document and therefore its
        hash — which `compile.plan_hash` compares for idempotency, so a retried
        acceptance of an unchanged policy would stop matching its own in-force plan.
        """
        document = handler(self)
        if self.user_credentials is None:
            document.pop("user_credentials", None)
        if self.coordination is None:
            document.pop("coordination", None)
        return document

    @model_validator(mode="after")
    def _coordination_requires_v3(self) -> ExecutionPolicy:
        """`coordinate` and a coordination scope imply each other, and imply v3.

        All four combinations are decided explicitly rather than left to the
        admission check, because each one would otherwise be a policy whose text and
        effect disagree:

        * scope on a v1/v2 document — **refused**, not upgraded. Silently reading an
          older accepted policy as v3 is exactly the "silently upgrading old accepted
          policies" the issue forbids.
        * `coordinate` with no scope — refused. An unbounded coordinator has no safe
          default, so there is nothing to fall back to.
        * a scope with no `coordinate` — refused. It would be dead configuration that
          reads as granted authority, the same misreading `human_gates` refuses.
        """
        coordinate_allowed = Action.COORDINATE in self.allowed_actions
        if self.coordination is not None and self.schema_version != COORDINATION_SCHEMA_VERSION:
            raise ValueError(f"a coordination scope requires policy schema_version {COORDINATION_SCHEMA_VERSION}")
        if coordinate_allowed and self.coordination is None:
            raise ValueError(
                "allowed_actions names 'coordinate' but the policy declares no coordination scope; an unbounded coordinator is not accepted"
            )
        if self.coordination is not None and not coordinate_allowed:
            raise ValueError("a coordination scope has no effect unless allowed_actions names 'coordinate'")
        if self.coordination is not None:
            # Every child action must ALSO be an action this policy authorizes, and
            # must not be one it gates. Checked here so an owner reading the accepted
            # document cannot see a child action the policy would refuse at dispatch.
            undeclared = sorted(set(self.coordination.allowed_child_actions) - set(self.allowed_actions))
            if undeclared:
                raise ValueError(f"allowed_child_actions names action(s) absent from allowed_actions: {', '.join(undeclared)}")
            gated = sorted(set(self.coordination.allowed_child_actions) & set(self.human_gates))
            if gated:
                raise ValueError(
                    f"allowed_child_actions names human-gated action(s): {', '.join(gated)}; coordination cannot route around a human gate"
                )
        return self

    @model_validator(mode="after")
    def _user_credentials_require_v2(self):
        if self.user_credentials is not None:
            # v3 is a superset of v2 rather than an alternative to it: a flow that
            # accepted user credentials must not have to give them up to accept a
            # coordinator. v1 still refuses, so no already-accepted v1 document
            # acquires this authority.
            if self.schema_version not in _USER_CREDENTIAL_SCHEMA_VERSIONS:
                raise ValueError("user credential permissions require policy schema_version 2")
            if not set(self.user_credentials.actions) <= set(self.allowed_actions):
                raise ValueError("credential actions must be declared policy actions")
        return self

    @model_validator(mode="after")
    def _human_gates_are_declared_actions(self) -> ExecutionPolicy:
        """A human gate must name an action this policy otherwise permits.

        Gating an action that is not in `allowed_actions` is already denied by the
        action check, so the entry has no effect — but it *reads* as a control the
        owner put in place. An ineffective entry in a permission document is worse
        than a missing one: it invites the reader to believe a boundary exists.
        """
        stray = sorted(set(self.human_gates) - set(self.allowed_actions))
        if stray:
            raise ValueError(
                f"human_gates names action(s) absent from allowed_actions: {', '.join(stray)}; "
                "a gate on an unpermitted action has no effect and misreads as a control"
            )
        return self

    @model_validator(mode="after")
    def _evaluation_addresses_are_graph_addresses(self) -> ExecutionPolicy:
        """Evaluation keys must be real graph addresses.

        The mapping's keys are the one caller-controlled *key* space on this model,
        so they are constrained to the same `flow/epic/wave/node` form every other
        address in the document uses (`proposal.py` rule 1). An unconstrained key
        would let arbitrary strings — including secret-shaped ones — into an
        accepted policy through the one field `extra="forbid"` cannot cover.

        A malformed address can also never match a real node, so the entry would be
        permanently dead while appearing to configure something.
        """
        malformed = sorted(key for key in self.evaluation_acceptance if not ADDRESS_PATTERN.match(key))
        if malformed:
            raise ValueError(f"evaluation_acceptance keys must be graph addresses of the form 'flow/epic/wave/node': {', '.join(malformed)}")
        return self

    @model_validator(mode="after")
    def _no_duplicate_bindings(self) -> ExecutionPolicy:
        """Reject duplicates in the binding lists.

        A repeated repository id does not change what is permitted, so this is not
        a security check — it is a readability one for the accepted document, which
        is the artifact an owner and an auditor read back. It also keeps the
        content hash stable against a list that differs only by repetition.
        """
        for field_name in ("team_ids", "repository_ids", "environment_connection_ids", "allowed_actions", "human_gates"):
            values = [str(value) for value in getattr(self, field_name)]
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                raise ValueError(f"{field_name} repeats: {', '.join(duplicates)}")
        return self

    def permits(self, action: Action) -> bool:
        """Whether `action` is listed and not gated to a human.

        A convenience for callers that need to *describe* the policy — the plan
        summary, #4529's authoring surface. It is deliberately NOT the admission
        check: it reads only the document and knows nothing about membership,
        expiry, scope or limits. :func:`authorize_action` is the check.
        """
        return action in self.allowed_actions and action not in self.human_gates


class CoordinationSummary(BaseModel):
    """A coordinator's bounds, in the shape an owner reads before accepting them.

    Counts the assigned nodes rather than listing their addresses, for the same
    reason `PolicySummary` omits `evaluation_acceptance`: graph addresses are
    internal and §7.2 makes them non-renderable. The child personas and actions ARE
    listed, because "what can this thing cause to happen?" is the question an owner
    is actually answering when they accept a coordinator, and a count would not
    answer it.
    """

    model_config = ConfigDict(extra="forbid")

    assigned_node_count: int
    allowed_child_personas: list[ChildPersona]
    allowed_child_actions: list[Action]


class PolicySummary(BaseModel):
    """What an owner authorized, in the shape a reader needs (#5128 design point 2).

    A **projection for display**, deliberately narrower than `ExecutionPolicy`. The
    accepted document is the authority; this is the answer to "what did I agree to?"
    and nothing decides anything from it.

    Two omissions are the point of having a separate model rather than rendering the
    policy directly:

    * **No `policy_hash`, `policy_id` or `principal_id`.** Identity and provenance
      belong in an audit view, and putting a hash on a summary invites a reader to
      treat matching hashes as the check — which it is not, because authority also
      depends on live membership, expiry and limits that no document carries.
    * **No `evaluation_acceptance` map.** Its keys are internal graph addresses,
      which §7.2 makes non-renderable. The count of machine-accepted evaluations is
      the fact a reader needs, so that is what this carries.

    `autonomous_actions` and `human_decisions` are computed rather than copied
    straight from `allowed_actions`/`human_gates`, because those two fields overlap
    by design: an action in both means "agents may prepare this, a person releases
    it". Copying `allowed_actions` verbatim would show `merge` as autonomous on
    exactly the policy that gated it, which is the one reading a summary must never
    produce.
    """

    model_config = ConfigDict(extra="forbid")

    # Where the authority applies. Repository and connection ids are the operator's
    # own names for things, not internal addresses, so they are shown as given.
    repository_ids: list[str]
    user_credentials: UserCredentialAuthority | None = None
    environment_connection_ids: list[str]
    team_ids: list[str]
    # What may happen without asking again — `allowed_actions` minus `human_gates`.
    autonomous_actions: list[Action]
    # What always waits for a person, even though the policy otherwise permits it.
    human_decisions: list[Action]
    # How many evaluations an owner marked for machine acceptance. A count, not the
    # addresses (see the class docstring).
    machine_accepted_evaluations: int
    # The accepted coordinator's bounds, or `None` when the policy accepts no
    # coordinator. Absent rather than an empty summary: a `CoordinationSummary`
    # reading "0 nodes, no personas" describes an accepted-but-useless coordinator,
    # which is a different fact from "no coordinator was accepted" and would be the
    # more alarming of the two to show an owner who accepted neither.
    coordination: CoordinationSummary | None = None
    expires_at: datetime
    limits: PolicyLimits


def summarize_policy(policy: ExecutionPolicy) -> PolicySummary:
    """Project an accepted policy into its display summary.

    One function so the plan summary, an amendment preview and #4529's authoring
    surface all describe a policy the same way. A second derivation of
    "autonomous versus gated" is a second chance to get the overlap backwards.

    Order is canonical (`Action` declaration order) rather than the order the author
    happened to list actions in, so the same policy always reads identically and two
    summaries can be compared by eye.
    """
    return PolicySummary(
        repository_ids=list(policy.repository_ids),
        user_credentials=policy.user_credentials.model_copy(deep=True) if policy.user_credentials is not None else None,
        environment_connection_ids=list(policy.environment_connection_ids),
        team_ids=list(policy.team_ids),
        autonomous_actions=[action for action in Action if policy.permits(action)],
        # Read from `human_gates` directly rather than as "allowed minus autonomous":
        # the validator already requires every gate to name a permitted action, so
        # the two agree — and this spelling stays correct if that ever loosens.
        human_decisions=[action for action in Action if action in policy.human_gates],
        machine_accepted_evaluations=sum(1 for mode in policy.evaluation_acceptance.values() if mode is AcceptanceMode.MACHINE),
        coordination=(
            CoordinationSummary(
                assigned_node_count=len(policy.coordination.assigned_node_addresses),
                # Canonical declaration order, like `autonomous_actions` above, so the
                # same accepted scope always reads identically to an owner comparing two
                # summaries by eye.
                allowed_child_personas=[persona for persona in ChildPersona if persona in policy.coordination.allowed_child_personas],
                allowed_child_actions=[action for action in Action if action in policy.coordination.allowed_child_actions],
            )
            if policy.coordination is not None
            else None
        ),
        expires_at=policy.expires_at,
        limits=policy.limits,
    )


def policy_hash(policy: ExecutionPolicy) -> str:
    """Stable SHA-256 of a policy's *authorizing content*.

    The three server-stamped fields are excluded, which is what makes the hash
    computable before they are set and stable after — `policy_id` is derived from
    this value, so including it would be circular, and including `policy_hash`
    would be doubly so.

    `principal_id` is excluded for a different and more debatable reason, so it is
    stated rather than left to be inferred: the hash answers "are these the same
    permissions?", and two owners authorizing identical scope have authored the
    same policy. Provenance is carried by the accepted-plan row's own decision
    reference and by `principal_id` itself, both of which survive independently of
    the hash. Nothing authorizes on the hash, so this is not a bypass — it is why
    `stamp_policy` binds the principal explicitly instead of relying on identity.

    Canonicalised with sorted keys and no incidental whitespace, matching
    `compile.plan_hash`, so field order cannot change a policy's identity.
    """
    canonical = json.dumps(
        policy.model_dump(mode="json", exclude=_STAMPED_FIELDS),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# The fields the server sets and the content hash therefore cannot cover. A
# frozenset rather than an inline literal so a field added here cannot be missed
# by a reader who only greps for the field name — same reasoning as
# `compile.HASH_EXCLUDED_FIELDS`.
_STAMPED_FIELDS = frozenset({"policy_id", "policy_hash", "principal_id"})


def stamp_policy(policy: ExecutionPolicy, *, principal_id: str, org_id: str) -> ExecutionPolicy:
    """Bind a submitted policy to its principal and stamp its server-side identity.

    The **only** way a policy acquires an id, a hash or a principal. Called from
    the acceptance path with values from the server-resolved acceptance context.

    Both refusals below reject rather than overwrite, deliberately. Overwriting a
    caller's value would leave the author believing the value they sent was
    accepted, and for `principal_id` that belief is exactly the dangerous one — an
    author who thinks they successfully named a principal has to be told they did
    not, because the alternative is discovering it from an audit trail that names
    somebody else.

    Args:
        policy: The submitted policy. Must carry none of the stamped fields.
        principal_id: The authenticated acceptor's identity, from the acceptance
            context. Never from the document.
        org_id: The server-resolved tenant, for the same reason.

    Returns:
        A new `ExecutionPolicy` with `principal_id`, `policy_hash` and `policy_id`
        set. The input is not mutated — pydantic models are copied, so a caller
        holding the submitted document keeps the document as submitted.

    Raises:
        PolicyRejectedError: The document set a server-stamped field, or the
            declared `org_id` is not the acceptance context's tenant.
    """
    supplied = sorted(name for name in sorted(_STAMPED_FIELDS) if getattr(policy, name) is not None)
    if supplied:
        raise PolicyRejectedError(
            f"execution policy must not declare server-stamped field(s): {', '.join(supplied)}; "
            "the policy id, hash and principal are resolved at acceptance and a submitted value is refused rather than replaced"
        )

    if not principal_id:
        # A policy with no principal is an unattributed grant. Same refusal
        # `genesis.py` makes for a human-kind decision row carrying no `actor_id`:
        # a binding that identifies nobody is worse than no binding at all.
        raise PolicyRejectedError("execution policy requires a resolved principal_id; none was supplied by the acceptance context")

    if policy.org_id != org_id:
        # Compared, never substituted — the same rule `compile.py`'s Gate 2 applies
        # to the plan document, restated here because `stamp_policy` is reachable
        # from any acceptance path and must not depend on its caller having checked.
        raise PolicyRejectedError(
            f"execution policy declares org_id {policy.org_id!r} but the acceptance context resolved {org_id!r}; a policy is never re-homed"
        )

    bound = policy.model_copy(update={"principal_id": principal_id})
    digest = policy_hash(bound)
    # Derived from the content digest, not minted — see the module docstring on why
    # a fresh uuid per acceptance would break retried acceptances. The prefix makes
    # the value self-identifying in a log without a lookup.
    return bound.model_copy(update={"policy_hash": digest, "policy_id": f"pol_{digest[:32]}"})


def flow_budget_binding(flow_id: str) -> str:
    """The stable allowance key every descendant of a flow shares.

    Derived from `flow_id` — which is server-issued at flow creation
    (`compile._resolve_flow`) and never changes — so it is stable across gates,
    retries, restarts and new run ids by construction. There is nothing to persist
    and nothing to propagate: any component holding the flow id computes the same
    key.

    **That derivation is the whole control.** A binding minted per run, or stored
    on a row a worker could write, would reset the allowance exactly when it
    matters most — a repair loop restarting is the case where a fresh allowance is
    both most convenient and most wrong. Because this is a pure function of an id
    the worker cannot choose, "each child gets its own budget" is not reachable by
    a caller mistake.

    Distinct from the existing run and chain scopes rather than reusing either
    (`budget/enforcement_service.py`): a run scope is per-run and resets on every
    child, and a chain scope keys on `correlation_id`, which the engine's dispatch
    sets to a fresh per-attempt run id (`dispatch_pass._build_envelope`) — so a
    retry would start a new chain and a new allowance. Neither can express "one
    allowance for this plan's whole delivery".

    Note this returns the *key*; the amount is `PolicyLimits.max_spend_usd` and the
    observed total is resolved by the caller. See the module docstring on why
    unknown spend must arrive as `None`.
    """
    if not flow_id:
        raise ValueError("flow_budget_binding requires a flow_id; an empty binding would pool every flow's spend into one allowance")
    return f"flow:{flow_id}"


@dataclass(frozen=True)
class ResourceRef:
    """What an action is about to touch.

    Frozen, like `EngineGenesis`, because it is an input to an authority decision:
    a caller that could mutate `repository_id` after the check returned would have
    made the decision describe a different resource than the one acted on.

    Every field is optional because the actions differ in what they name — a
    `DEVELOP` names a repository and no environment, a `DEPLOY` names both. An
    action that requires a field and does not get it **denies**, which is why the
    defaults are `None` and not `""`: an empty string could match an empty policy
    list, while `None` cannot match anything.
    """

    repository_id: str | None = None
    environment_connection_id: str | None = None
    # The graph address the action is for. Required for `EVALUATE` (it selects the
    # acceptance mode) and used in deny detail for every other action.
    node_address: str | None = None
    # The tenant that owns the work. Compared against the policy's `org_id` so a
    # policy cannot authorize an action against another tenant's node even if the
    # caller resolved everything else correctly.
    org_id: str | None = None
    user_credential_id: str | None = None
    aws_role_arn: str | None = None


@dataclass(frozen=True)
class AuthorizationContext:
    """The live facts an admission decision needs, resolved by the caller.

    Nothing here is read from an envelope, an issue comment, or any row an agent
    can write (R-O5d). Each field names the service that owns it, and a caller
    that cannot reach that service passes `None` where the type allows and does not
    call at all where it does not.

    Read the module docstring on why this is separate from the rule. The short
    version: these are the facts that make the adversarial cases testable as
    literals instead of as simulated database states.
    """

    policy: ExecutionPolicy
    # The plan version the policy was accepted on, and the version the caller
    # believes is in force. Passed as a pair rather than resolved here so the
    # comparison is visible in the decision: a caller acting on a superseded plan
    # is the stale case, and it must deny rather than fall back to the newest
    # policy, which the owner may never have seen applied to this work.
    accepted_plan_version: int
    in_force_plan_version: int

    # Whose action this is, and what the identity services say about them NOW.
    principal_id: str
    # The principal's current org, from the membership service. `None` means no
    # current membership — which is what a revoked member looks like, and it must
    # deny even though the policy still names them.
    member_org_id: str | None
    member_team_ids: frozenset[str] = frozenset()
    principal_can_authorize: bool = False

    # Whether the delegated grant itself has been revoked, independent of
    # membership. Revocation blocks new admissions immediately; already-running
    # remote work is reconciled by existing controls, not here (this function
    # admits, it does not terminate).
    grant_revoked: bool = False

    # `now` is injected rather than read from the clock so expiry is testable
    # without freezing time, matching `reservations.ReservationStore`'s clock
    # injection. Callers pass `utcnow()`.
    now: datetime | None = None

    # Whether a credential narrow enough for this action could be issued. See
    # :class:`CredentialScope`: only `SCOPED` permits, and `UNKNOWN` is the value a
    # caller passes when the credential service was unreachable. **Never pass
    # `SCOPED` for "we did not check"** — that substitution is the broad-token
    # fallback this story exists to make unreachable.
    credential_scope: CredentialScope = CredentialScope.UNKNOWN

    # Reserved plus settled spend against this flow's binding, in USD.
    #
    # **`None` means UNKNOWN and denies.** Missing usage is not zero: a caller that
    # cannot read the ledger and passes `0` would mint the full allowance again,
    # which is precisely the "child resets allowance" blast radius the issue names.
    # The reconciliation path restores headroom by supplying a real number, not by
    # defaulting this one.
    observed_spend_usd: Decimal | None = None
    # Attempts already made on this node, and actions currently admitted under this
    # policy. Ints rather than optionals: both are counted from engine-owned rows
    # (`OrchestrationNode.attempts`, the admitted-action count), so a caller that
    # cannot read them cannot construct a context at all.
    observed_attempts: int = 0
    observed_concurrency: int = 0

    # Whether the work belongs to the flow this policy was accepted for. Resolved
    # by the caller from the node's own `flow_id`; a policy for one flow must not
    # admit an action against another's node.
    work_owned_by_policy_flow: bool = True


@dataclass(frozen=True)
class Decision:
    """Permit or block, with a typed reason when it blocks.

    Frozen, and `reason` is `None` exactly when `permitted` is True — enforced in
    `__post_init__` rather than trusted, because a permitted decision carrying a
    deny reason (or a block carrying none) is the shape that makes a caller's
    `if decision.reason:` check silently wrong.

    There is deliberately no `Decision(permitted=True)` shortcut that skips the
    invariant: the two constructors are :meth:`permit` and :meth:`block`.
    """

    permitted: bool
    reason: DenyReason | None = None
    # Human-readable explanation, for the audit record and the operator surface.
    # Never echoes a credential, a token, or the policy's full binding — a refusal
    # is not an oracle for what the policy permits, the same rule `genesis.py`
    # applies to its own refusals.
    detail: str = ""

    def __post_init__(self) -> None:
        if self.permitted and self.reason is not None:
            raise ValueError("a permitted decision cannot carry a deny reason")
        if not self.permitted and self.reason is None:
            raise ValueError("a blocked decision must carry a typed deny reason for #5122")

    @classmethod
    def permit(cls, detail: str = "") -> Decision:
        return cls(permitted=True, detail=detail)

    @classmethod
    def block(cls, reason: DenyReason, detail: str) -> Decision:
        return cls(permitted=False, reason=reason, detail=detail)


# Actions that touch something outside the platform and therefore need a
# deployment target resolved from the policy's registered connections.
_ENVIRONMENT_ACTIONS = frozenset({Action.DEPLOY})

# Actions that act inside a repository. `EVALUATE` is absent: an evaluation reads
# evidence about work already done and does not itself need repository authority,
# so requiring one would block a policy that legitimately names no repository for
# its evaluation nodes.
#
# `COORDINATE` is absent for a stronger reason: a coordinator does not act in a
# repository at all. It reads progress and requests children, and each child's own
# admission (`authorize_child_request` plus a full `authorize_action` for the child)
# is where the repository check belongs. Putting `COORDINATE` here would grant a
# coordinator repository authority it never needs, and would make the *coordinator's*
# repository the one checked rather than the child's.
_REPOSITORY_ACTIONS = frozenset({Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.MERGE})


def authorize_action(
    context: AuthorizationContext,
    action: Action,
    resource: ResourceRef,
    policy_version: int,
) -> Decision:
    """Decide whether one autonomous action may proceed. **Call immediately before it.**

    The single admission check for every dispatch, merge, deploy and machine
    acceptance. "Immediately before" is part of the contract, not advice: every
    fact in `context` is live, and a decision cached across a gate could admit an
    action under a membership or an allowance that has since changed.

    Checks run cheapest-and-most-structural first, so a stale or expired policy
    denies without consulting membership or spend, and the deny reason names the
    outermost thing that was wrong rather than an incidental inner one. Within
    that, the ordering encodes two deliberate precedences:

    - **A human gate beats a permitted action.** `human_gates` is checked after
      `allowed_actions`, so an action in both denies with `HUMAN_GATE_REQUIRED`.
    - **Unknown beats exceeded.** `SPEND_UNKNOWN` is checked before the limit
      comparison, because "we cannot tell" and "we can tell, and it is too much"
      need different operator responses and the first must not be reported as the
      second.

    Args:
        context: Live facts, resolved by the caller. See
            :class:`AuthorizationContext` on which fields deny when unknown.
        action: The action about to be taken.
        resource: What it would touch.
        policy_version: The plan version the CALLER is acting on behalf of,
            compared against the version in force. A caller that passes the
            accepted version blindly defeats the staleness check — it must pass the
            version it actually read the work from.

    Returns:
        A :class:`Decision`. `permitted` False always carries a typed
        :class:`DenyReason`.
    """
    policy = context.policy

    # --- Schema: refuse a document this build cannot interpret --------------
    # `schema_version` is a `Literal`, so a policy parsed through pydantic cannot
    # reach here with a bad value. This guards the other route in: a policy
    # reconstructed from a stored `plan_document` dict by a future reader, or
    # constructed directly in a test. Interpreting an unknown version optimistically
    # is the one failure mode a version field exists to prevent.
    if policy.schema_version not in SUPPORTED_POLICY_SCHEMA_VERSIONS:
        return Decision.block(
            DenyReason.SCHEMA_UNSUPPORTED,
            f"policy schema version {policy.schema_version} is not supported by this build (supported {sorted(SUPPORTED_POLICY_SCHEMA_VERSIONS)})",
        )

    # --- Version: act only on the policy actually in force ------------------
    # Two comparisons, and both matter. The caller's asserted version must be the
    # one in force (it may have read the work from a superseded plan), and the
    # policy handed in must be the accepted one for that version.
    if policy_version != context.in_force_plan_version or context.accepted_plan_version != context.in_force_plan_version:
        return Decision.block(
            DenyReason.STALE_POLICY_VERSION,
            f"action asserted plan version {policy_version} against policy accepted at v{context.accepted_plan_version}, "
            f"but v{context.in_force_plan_version} is in force; an amendment must be re-read before the next action",
        )

    # --- Lifetime: expiry and revocation ------------------------------------
    if context.grant_revoked:
        # Checked before expiry: a revoked grant is an operator's explicit act and
        # is the more informative reason when both are true.
        return Decision.block(DenyReason.GRANT_REVOKED, "the delegated grant for this policy has been revoked; no further action is admitted")

    if context.now is None:
        # A caller that cannot supply a clock cannot evaluate expiry, and an
        # unexpirable policy is a permanent grant. Fail closed, consistent with
        # every other unknown here.
        return Decision.block(DenyReason.POLICY_EXPIRED, "policy expiry could not be evaluated because no current time was supplied")

    if context.now >= policy.expires_at:
        return Decision.block(
            DenyReason.POLICY_EXPIRED,
            f"policy expired at {policy.expires_at.isoformat()}; a new acceptance is required to authorize further action",
        )

    # --- Identity: who is acting, verified against CURRENT membership -------
    # The policy names a principal, but naming is not authority: membership is
    # re-read on every admission so a removed member's work stops at the next
    # action rather than at the next acceptance.
    if context.member_org_id is None:
        return Decision.block(
            DenyReason.MEMBERSHIP_REVOKED,
            f"principal {context.principal_id!r} has no current organization membership; the policy's grant does not survive removal",
        )

    if context.member_org_id != policy.org_id:
        return Decision.block(
            DenyReason.ORG_MISMATCH,
            f"principal's current organization does not match the policy's tenant {policy.org_id!r}",
        )

    if resource.org_id is not None and resource.org_id != policy.org_id:
        # The work's own tenant, checked separately from the principal's. Both must
        # agree with the policy, or a correctly-scoped principal could act on
        # another tenant's node.
        return Decision.block(
            DenyReason.ORG_MISMATCH,
            f"the target work belongs to a different tenant than the policy's {policy.org_id!r}",
        )

    if policy.team_ids and not (context.member_team_ids & set(policy.team_ids)):
        # Empty `team_ids` means org-level binding, so this check is skipped rather
        # than denying — see the field's own comment.
        return Decision.block(
            DenyReason.TEAM_NOT_PERMITTED,
            f"principal {context.principal_id!r} is not a member of any team this policy binds",
        )

    if not context.principal_can_authorize:
        return Decision.block(DenyReason.ROLE_REVOKED, "the policy principal no longer has permission to authorize execution in this tenant")

    if not context.work_owned_by_policy_flow:
        return Decision.block(
            DenyReason.WORK_NOT_OWNED,
            "the target work does not belong to the flow this policy was accepted for",
        )

    # --- Authority: is this action delegated at all? ------------------------
    if action not in policy.allowed_actions:
        return Decision.block(
            DenyReason.ACTION_NOT_PERMITTED,
            f"action {action.value!r} is not among the actions this policy authorizes",
        )

    if action in policy.human_gates:
        # After the membership and action checks so the reason is the gate rather
        # than an incidental scope problem: an operator seeing this must understand
        # a person needs to act, not go looking for a misconfiguration.
        return Decision.block(
            DenyReason.HUMAN_GATE_REQUIRED,
            f"action {action.value!r} is explicitly gated to a human decision by this policy",
        )

    # --- Coordination: bounded to the accepted node set ---------------------
    # Reached only when `COORDINATE` is in `allowed_actions` and not gated, so the
    # refusals below are scope refusals rather than authority ones. The scope is
    # re-read from the accepted document on every request (#5224 design point 3):
    # an amendment that narrows the assigned set takes effect at the next child
    # request, not at the next acceptance.
    if action is Action.COORDINATE:
        scope = policy.coordination
        if scope is None:
            # Unreachable through pydantic (`_coordination_requires_v3` requires the
            # pair), but reachable from a raw-dict rehydration by a future reader —
            # the same route `SCHEMA_UNSUPPORTED` guards. An absent scope grants
            # nothing rather than defaulting to one.
            return Decision.block(
                DenyReason.COORDINATION_NOT_PERMITTED,
                "policy permits 'coordinate' but declares no coordination scope; no coordination is admitted without accepted bounds",
            )
        if resource.node_address is None or resource.node_address not in scope.assigned_node_addresses:
            return Decision.block(
                DenyReason.COORDINATION_NODE_NOT_ASSIGNED,
                f"coordination at {resource.node_address or '(unnamed)'} is outside the node set this policy assigns to a coordinator",
            )

    # Machine acceptance is a MODE for an evaluation, never authority over a gate.
    if action is Action.EVALUATE:
        address = resource.node_address
        mode = policy.evaluation_acceptance.get(address) if address else None
        if mode is not AcceptanceMode.MACHINE:
            # Absent, unknown-address and explicit-`HUMAN` all land here, and all
            # three are correct: `HUMAN` is the default because a typo in an address
            # must not promote an evaluation to machine acceptance.
            return Decision.block(
                DenyReason.MACHINE_ACCEPTANCE_NOT_PERMITTED,
                f"evaluation at {address or '(unnamed)'} is not marked for machine acceptance; a human must conclude it",
            )

    # --- Scope: the specific resource this action would touch ---------------
    if action in _REPOSITORY_ACTIONS:
        if resource.repository_id is None or resource.repository_id not in policy.repository_ids:
            return Decision.block(
                DenyReason.REPOSITORY_NOT_PERMITTED,
                f"action {action.value!r} targets a repository this policy does not authorize",
            )

    if action in _ENVIRONMENT_ACTIONS:
        if resource.environment_connection_id is None or resource.environment_connection_id not in policy.environment_connection_ids:
            # Reached for every `DEPLOY` when `environment_connection_ids` is empty,
            # which is the intended reading of a policy that registered no target.
            return Decision.block(
                DenyReason.ENVIRONMENT_NOT_PERMITTED,
                f"action {action.value!r} targets an environment connection this policy does not authorize",
            )

    # --- Delegated identity: scoped or explicitly accepted user authority --
    # USER_GRANTED is a distinct v2 contract, never a fallback from SCOPED. v3 is
    # a superset of v2, so the set here must match `_user_credentials_require_v2`
    # exactly: pinning `== 2` would let a flow that accepted a coordinator lose
    # user-credential authority it had already been granted, which is a silent
    # downgrade rather than a refusal. v1 still has no such contract to honour.
    user_authority = policy.user_credentials
    user_granted = (
        context.credential_scope is CredentialScope.USER_GRANTED
        and policy.schema_version in _USER_CREDENTIAL_SCHEMA_VERSIONS
        and user_authority is not None
        and action in user_authority.actions
        and (resource.user_credential_id is not None or resource.aws_role_arn is not None)
        and (resource.user_credential_id is None or resource.user_credential_id in user_authority.vault_credential_ids)
        and (resource.aws_role_arn is None or resource.aws_role_arn in user_authority.aws_role_arns)
    )
    if context.credential_scope is not CredentialScope.SCOPED and not user_granted:
        return Decision.block(
            DenyReason.CREDENTIAL_SCOPE_UNAVAILABLE,
            f"a credential scoped to this policy could not be issued for {action.value!r} ({context.credential_scope.value}); "
            "a broad platform credential is never substituted",
        )

    # --- Limits: bounded work, on a shared allowance ------------------------
    # Unknown before exceeded, deliberately — see the docstring.
    if context.observed_spend_usd is None:
        return Decision.block(
            DenyReason.SPEND_UNKNOWN,
            "spend against this flow's allowance could not be determined; new spend is blocked until it is reconciled",
        )

    if context.observed_spend_usd >= policy.limits.max_spend_usd:
        return Decision.block(
            DenyReason.SPEND_LIMIT_EXCEEDED,
            f"reserved plus settled spend has reached the {policy.limits.max_spend_usd} USD this policy authorizes",
        )

    if context.observed_attempts >= policy.limits.max_attempts_per_node:
        # This is what bounds a repair loop. Exhaustion needs an explicit authorized
        # recovery — it is not self-clearing, and nothing here resets the count.
        return Decision.block(
            DenyReason.ATTEMPT_LIMIT_EXCEEDED,
            f"this node has reached the {policy.limits.max_attempts_per_node} attempt(s) this policy authorizes; an authorized recovery is required",
        )

    if context.observed_concurrency >= policy.limits.max_concurrent_actions:
        return Decision.block(
            DenyReason.CONCURRENCY_LIMIT_EXCEEDED,
            f"{context.observed_concurrency} action(s) are already admitted under this policy, at its limit of "
            f"{policy.limits.max_concurrent_actions}",
        )

    return Decision.permit(f"action {action.value!r} is authorized by policy {policy.policy_id or '(unstamped)'} at plan v{policy_version}")


def authorize_child_request(
    context: AuthorizationContext,
    child_persona: ChildPersona | str,
    child_action: Action,
    resource: ResourceRef,
    policy_version: int,
) -> Decision:
    """Decide whether a coordinator may request ONE eligible child. **Not the child's own admission.**

    Two checks, deliberately in this order and deliberately not collapsed into one:

    1. The coordinator's own `COORDINATE` authority at its assigned node, via
       :func:`authorize_action`. Everything a normal admission checks — version,
       expiry, revocation, membership, role, credential scope, shared spend, attempts
       and concurrency — therefore applies to the *request itself*, so a coordinator
       whose flow has exhausted its allowance cannot keep asking.
    2. The requested child against the accepted coordination scope.

    **What this deliberately does NOT do, and it is the whole safety argument:**
    permitting here does not authorize the child. The child still needs its own
    `authorize_action` under its own action, its own live work claim and its own
    bounded grant — this function only establishes that a coordinator was *allowed to
    ask*. `graph_dispatch` calls both, and the child's admission is what mints
    anything. Collapsing the two would let coordinator authority substitute for a
    child's, which is the substitution #5224 forbids outright.

    A coordinator therefore cannot approve, accept or resume a human gate, conclude
    an evaluation, merge, deploy, change scope or issue credentials by virtue of
    holding `coordinate`: none of those are reachable from here. `MERGE`, `DEPLOY`
    and `EVALUATE` are refused at acceptance by `CoordinationScope`, and refused
    again below for a document that reached this build without validation.

    Args:
        context: The COORDINATOR's live facts — not the child's. The caller resolves
            a fresh context for the child before admitting it.
        child_persona: The persona to be dispatched. A plain string is accepted (it
            arrives from a request body) and an unrecognised one denies rather than
            raising, so a malformed request is a typed refusal like any other.
        child_action: The action the child would take.
        resource: The COORDINATOR's assigned node and tenant.
        policy_version: The version the caller read the work from.

    Returns:
        A :class:`Decision`. A permit means "this request was allowed to be made".
    """
    coordinator = authorize_action(context, Action.COORDINATE, resource, policy_version)
    if not coordinator.permitted:
        return coordinator

    # `authorize_action` already refused an absent scope above, so this is a
    # narrowing for the type checker rather than a second policy decision.
    scope = context.policy.coordination
    if scope is None:  # pragma: no cover - unreachable; authorize_action blocks first
        return Decision.block(DenyReason.COORDINATION_NOT_PERMITTED, "no coordination scope is accepted on this policy")

    try:
        persona = ChildPersona(child_persona)
    except ValueError:
        # An unexpected persona string is a refusal, never a pass-through. The set of
        # dispatchable personas is closed at acceptance, so a request naming one the
        # owner never accepted must not reach dispatch on the strength of the
        # coordinator's own authority.
        return Decision.block(
            DenyReason.CHILD_PERSONA_NOT_PERMITTED,
            f"requested child persona {str(child_persona)!r} is not a persona a coordinator may request",
        )

    if persona not in scope.allowed_child_personas:
        return Decision.block(
            DenyReason.CHILD_PERSONA_NOT_PERMITTED,
            f"this policy's coordination scope does not permit requesting a {persona.value!r} child",
        )

    if child_action not in scope.allowed_child_actions:
        return Decision.block(
            DenyReason.CHILD_ACTION_NOT_PERMITTED,
            f"this policy's coordination scope does not permit a child performing {child_action.value!r}",
        )

    # Re-checked rather than trusted from acceptance, for a document that reached
    # this build without passing the validators (a raw-dict rehydration).
    if child_action not in context.policy.allowed_actions or child_action in context.policy.human_gates:
        return Decision.block(
            DenyReason.CHILD_ACTION_NOT_PERMITTED,
            f"child action {child_action.value!r} is not autonomously authorized by this policy; coordination cannot substitute for it",
        )

    return Decision.permit(
        f"a {persona.value!r} child performing {child_action.value!r} may be requested by the coordinator at {resource.node_address} "
        f"under policy {context.policy.policy_id or '(unstamped)'}; the child's own admission still applies"
    )
