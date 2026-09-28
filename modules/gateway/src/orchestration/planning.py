"""Derive a reviewable initial plan from hosted intent refinement.

The generated graph is a single-wave starting point. A selected repository is
resolved by the route and included in a proposed, bounded policy. The policy is
inert until a human accepts the exact preview revision; generation never grants
execution authority. Missing repository/issue inputs remain visible prerequisites.
For richer dependencies and executable evaluation specifications, use a prepared
LoopProposal through the same preview and approval services.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.orchestration.execution_policy import Action, ExecutionPolicy, PolicyLimits
from src.orchestration.proposal import EpicDisplay, EpicMetadata, LoopProposal, ProposedEdge, ProposedNode, WaveDisplay, WaveMetadata

__all__ = [
    "MAX_OUTCOMES",
    "MIN_POLICY_REMAINDER",
    "PLANNER_SPEC_REVISION",
    "POLICY_LIFETIME",
    "PlanningError",
    "PlanningInputs",
    "ResolvedRepository",
    "plan_from_draft",
    "resolve_issue_ref",
    "resolve_repository",
    "slugify",
]

# The contract revision a derived document declares (rule 5). Named for this
# generator rather than reusing a hand-authored plan's revision string, so a plan
# that came out of a conversation is distinguishable from one a person wrote — which
# matters when reading back a plan whose shape looks thin: "one wave, no
# inter-story edges" is this generator's honest output, not an author's omission.
PLANNER_SPEC_REVISION = "issue-5331-intake-r1"

# The wave every derived story lands in. One wave, and see the module docstring for
# why: prose outcomes carry no dependency information, so any further staging would
# be invented.
FIRST_WAVE = "wave-1"
# The single epic a derived plan declares. Named for what it is rather than after
# the intent, so an operator reading an address can tell a generated skeleton from a
# hand-authored plan at a glance.
DEFAULT_EPIC = "epic-1"

# Upper bound on derived stories. A draft with more declared outcomes than this is
# refused rather than truncated: silently dropping outcomes would produce a plan
# that looks complete and quietly omits work the user asked for, which is the worst
# available failure. The number is generous — a plan needing more than this is
# really several plans, and the refusal says so.
MAX_OUTCOMES = 40

# How long a proposed grant lasts, measured from the conversation's LAST ACTIVITY
# rather than from its creation (#5331).
#
# The anchor is the whole point. Measured from creation, the 24 hours were spent by
# the user thinking — and spent entirely by the disconnect-and-return this story
# requires, so a conversation resumed on Thursday and approved produced a grant that
# expired on Tuesday. Nothing refused it: the gate moved, the graph armed, and every
# dispatch was then denied `policy_expired` on a plan the human had just authorized.
#
# Last activity is a real signal and not merely a convenient one. The ingest and
# response Lambdas bump `updated_at` on every turn, and the conversation row's own
# DynamoDB TTL is already `updated_at + 86400` — so this is the same horizon the
# conversation itself is retained for, not a second lifetime invented here.
#
# Deliberately NOT `now + 1 day`, which is the obvious fix and breaks a different
# requirement: the derivation has to be deterministic so that a caller whose response
# was lost re-derives a byte-identical document and re-registers it as the server's
# own `already_registered` case. A wall-clock expiry would make two attempts seconds
# apart two different documents, and therefore two flows for one intent.
POLICY_LIFETIME = timedelta(days=1)

# The least usable life a proposed grant may be offered with. A grant with four
# seconds left is not meaningfully different from an expired one — the human still has
# to read the preview and answer the gate — so the derivation refuses rather than
# emitting bounds that will be dead before they can be approved.
MIN_POLICY_REMAINDER = timedelta(hours=1)

# `owner/name`, the form `policy_admission` matches verbatim. Enforced rather than
# normalized, for the reason that module gives: guessing which owner a bare name
# refers to is not something a server may do on a caller's behalf when the answer
# becomes a grant.
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")

# Address segments are constrained by the proposal's address grammar. Derived
# segments are slugified into this alphabet so a document built from arbitrary prose
# cannot emit an address that rule 1 would then reject — a generated plan that fails
# the server's own validation is a bug here, not an authoring mistake.
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class PlanningError(ValueError):
    """A plan could not be derived, with a `code` a route maps to a status.

    Carries a machine-readable code as well as prose because the caller has to
    distinguish "your draft is not ready" (keep refining) from "that repository is
    not connected" (an operator action) — different next steps that a single message
    string would force a client to pattern-match.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedRepository:
    """A repository the authenticated tenant demonstrably has access to.

    Frozen, and carrying the installation it was found under, because "which
    installation grants this" is the question a later credential-scoping step asks.
    Constructed only by `resolve_repository`, so a `ResolvedRepository` existing at
    all is the evidence that the name was checked against a real installation rather
    than accepted from a caller.
    """

    full_name: str
    installation_id: int
    # True when the repository list came from a live GitHub read rather than a stored
    # snapshot. Surfaced rather than hidden: a match against a stale snapshot is
    # weaker evidence, and the operator is entitled to know which one they got.
    verified_live: bool


@dataclass(frozen=True)
class PlanningInputs:
    """Everything a plan is derived from, all of it server-resolved.

    A single frozen struct rather than a long parameter list, so that adding an input
    later cannot silently default to something permissive at one call site and not
    another — and so a reader can see at a glance that no field here came from a
    client unchecked.
    """

    flow_slug: str
    title: str
    outcomes: tuple[str, ...]
    issue_ref: str | None
    intent: str
    # The AUTHENTICATED tenant, from the route's token context — never a client
    # field. `LoopProposal.org_id` is "the tenant the author believes this plan
    # belongs to. Checked, not trusted", and `compile_proposal` refuses a mismatch;
    # sourcing it from the caller's own identity means the two can never disagree and
    # the refusal is unreachable rather than merely handled.
    org_id: str
    wave_display: WaveDisplay | None = None
    epic_display: EpicDisplay | None = None
    repository: ResolvedRepository | None = None
    # The conversation's LAST ACTIVITY, as a unix epoch — the anchor a proposed
    # grant's expiry is measured from. Named for what it must be rather than for
    # where it comes from, because the defect this field's previous name
    # (`policy_epoch`) permitted was a route passing creation time into it and
    # producing a grant already spent. See `POLICY_LIFETIME`.
    last_activity_epoch: int = 0


def slugify(text: str, *, fallback: str) -> str:
    """Lowercase, hyphen-separated, safe for an address segment.

    `fallback` is used when the text slugifies to nothing — prose that is entirely
    punctuation or non-Latin script is legitimate input, and an empty address
    segment would produce a document the server's own rule 1 rejects. Returning a
    usable segment keeps the failure mode "an unhelpful name", not "a plan that
    cannot be registered".
    """
    slug = _SLUG_STRIP.sub("-", text.strip().lower()).strip("-")
    # Bounded so one long outcome sentence cannot produce an address segment that
    # overruns the column the address is stored in.
    return slug[:48].strip("-") or fallback


def resolve_repository(requested: str | None, available: list[tuple[int, list[str], bool]]) -> ResolvedRepository | None:
    """Match a requested repository against the tenant's real installations.

    `available` is `(installation_id, repository_full_names, live)` per installation,
    which the route builds from the caller's own connections. Passed in rather than
    fetched here so this module stays free of network and database access and can be
    tested against the exact shapes that matter — including the stale-snapshot case,
    which is the one most likely to be wrong in production.

    Returns `None` when nothing was requested. That is a legitimate plan: a document
    with no repository binding cannot dispatch, and the preview reports that as a
    real gap rather than this module inventing a default. Guessing "the only
    repository they have" would be a grant nobody asked for, and would silently pick
    differently for a tenant that later connected a second one.

    Refuses, rather than passes through, a name that no installation carries. The
    alternative is a document that registers cleanly, previews cleanly, gets approved
    by a human, and only then fails at dispatch with `repository_not_permitted` — a
    refusal arriving after the authority was granted, naming a cause the approver
    cannot connect to their decision.
    """
    if requested is None or not requested.strip():
        return None

    name = requested.strip()
    if not _REPOSITORY_PATTERN.fullmatch(name) or any(part in {".", ".."} for part in name.split("/")):
        raise PlanningError(
            "malformed_repository",
            f"'{name}' is not a repository in OWNER/NAME form. ADP matches repository names verbatim against the "
            "dispatch target, so it cannot guess which owner a bare name belongs to.",
        )

    # Case-insensitive comparison, case-preserving result. GitHub treats owner and
    # repository names case-insensitively, so refusing `Acme/App` against a stored
    # `acme/app` would reject a repository the tenant genuinely has. What is carried
    # forward is the name as the INSTALLATION reports it, not as the caller typed it,
    # because that is the form dispatch will compare.
    folded = name.casefold()
    for installation_id, repositories, live in available:
        for repository in repositories:
            if repository.casefold() == folded:
                return ResolvedRepository(full_name=repository, installation_id=installation_id, verified_live=live)

    raise PlanningError(
        "repository_not_connected",
        f"'{name}' is not among the repositories any GitHub App installation on your tenant can reach, so a plan "
        "naming it could never dispatch. Connect it under Settings -> Connections, then retry.",
    )


def resolve_issue_ref(requested: int | str | None) -> str | None:
    """Normalize an optional issue reference, or refuse it.

    Parsed with the same `int(str(value).lstrip("#"))` rule
    `dispatch_pass.issue_number_for_dispatch` uses, because that is the parse that
    decides at dispatch time whether a node routes at all. A planner that accepted a
    reference that parse rejects would emit a document whose story nodes are
    `malformed_issue_ref` — reported only as a `dispatch_blocked_cause` on a plan
    already written.

    Stored back as a bare decimal string, not `#N`: both parse, but one form in the
    database means `unordered_same_issue` and the dispatch lookup agree about when
    two nodes are on the same issue.

    Zero and negatives are refused with the rest, since GitHub issue numbers start
    at 1 and a `0` would parse happily while pointing at nothing.
    """
    if requested is None:
        return None
    if isinstance(requested, str) and not requested.strip():
        return None

    try:
        number = int(str(requested).strip().lstrip("#"))
    except (TypeError, ValueError):
        raise PlanningError(
            "malformed_issue_ref",
            f"'{requested}' is not a GitHub issue number. Pass the number itself, e.g. 5331 or #5331.",
        ) from None

    if number < 1:
        raise PlanningError(
            "malformed_issue_ref",
            f"Issue numbers start at 1, so '{requested}' does not identify an issue.",
        )
    return str(number)


def plan_from_draft(inputs: PlanningInputs) -> LoopProposal:
    """Derive a proposed plan from a refined intent. Nothing here is authoritative.

    The shape, and why it is this shape:

    * **One story per declared outcome**, in the order the user stated them, each
      carrying the resolved issue reference if there is one. The outcomes are the
      user's own words for "what would mean this worked", so mapping them one-to-one
      is a rearrangement of what they said rather than an inference about it.
    * **One eval node**, because rule 4 requires exactly one per wave containing
      stories, and because a wave of work with nothing to assess it cannot conclude.
      It carries the same issue reference as the stories: an eval with none is
      reported by the graph view as a `configuration_problem`, and emitting a node
      already known to be unrunnable would be shipping a defect as a feature.
    * **Every story precedes the eval.** This edge IS supported: an evaluation of
      the wave's work cannot conclude before the work it evaluates.
    * **Inter-story edges only when the stories share an issue.** This is the subtle
      one, and rule 6 (`unordered_same_issue`) is what forces it. When the
      conversation has an intent issue, every derived story materialises as work on
      *that one issue* — and two nodes on one issue with no order between them is not
      parallelism, it is "work this issue twice, independently, at the same time". One
      GitHub issue cannot be delivered by two concurrent workers, and transactional
      work claims would refuse the second at run time on a plan a human had already
      accepted.

      So for a single-issue plan the stories are chained in the order the user listed
      them. That is **not** an inferred dependency: the serialization is forced by the
      shared issue, and the listed order is the only order available and the user's
      own. Without an issue there is nothing to serialize, and the stories carry no
      edges between them — because then an edge really would be a claim the prose does
      not support.

    With a resolved repository, propose a finite development/review policy.
    It grants no merge, deployment or credential authority. The acceptance gate is
    inserted by the shared registration transform. Its expiry is measured from the
    conversation's last activity, and a conversation too idle to carry a usable grant
    is refused rather than handed bounds that expire before they can be approved —
    see `POLICY_LIFETIME`.

    Deterministic: the same draft yields a byte-identical document and therefore the
    same `plan_hash`. That is what makes the registration path's idempotency reachable
    from here — a caller whose response was lost re-derives the same plan and
    re-registers it as the server's own `already_registered` case, instead of
    creating a second flow for the same intent.
    """
    if not inputs.outcomes:
        raise PlanningError(
            "draft_not_ready",
            "This intent has no declared outcomes yet, and an outcome is what a story node delivers — so there is "
            "nothing to plan. Keep refining: say what observable result would mean this worked.",
        )
    if len(inputs.outcomes) > MAX_OUTCOMES:
        raise PlanningError(
            "too_many_outcomes",
            f"This intent declares {len(inputs.outcomes)} outcomes, above the {MAX_OUTCOMES} one plan carries. "
            "Outcomes are not dropped to fit, because a plan that silently omitted some would look complete. "
            "Split this into separate flows.",
        )

    # Resolved before any node is built, so a conversation that cannot carry a usable
    # grant is refused without having produced a document first. Only consulted when a
    # repository was resolved: with no repository there is no policy, so there is no
    # grant to be spent, and refusing would break the module's own documented case of a
    # plan whose missing binding the preview reports as a real prerequisite.
    expires_at = datetime.fromtimestamp(inputs.last_activity_epoch, tz=UTC) + POLICY_LIFETIME
    if inputs.repository and expires_at - datetime.now(tz=UTC) < MIN_POLICY_REMAINDER:
        raise PlanningError(
            "planning_session_idle",
            "This conversation has been idle too long for the bounded authority a plan proposes to still be usable, so "
            "a plan derived now would ask you to approve permissions that expire before they could be used. Continue "
            "the conversation — say anything, or restate what you want — and then request the plan again.",
        )

    flow = inputs.flow_slug
    nodes: list[ProposedNode] = []
    edges: list[ProposedEdge] = []
    used: set[str] = set()

    for index, outcome in enumerate(inputs.outcomes, start=1):
        # The index prefix guarantees uniqueness even when two outcomes slugify
        # identically ("Faster" / "faster!"), which rule 1 would otherwise reject as
        # a duplicate address — a collision in the generator surfacing as the user's
        # validation error.
        segment = f"{index:02d}-{slugify(outcome, fallback='outcome')}"
        while segment in used:  # pragma: no cover — the index prefix already ensures this
            segment = f"{segment}-x"
        used.add(segment)
        address = f"{flow}/{DEFAULT_EPIC}/{FIRST_WAVE}/{segment}"
        nodes.append(
            ProposedNode(
                address=address,
                kind="story",
                # Truncated to the field's bound rather than rejected: an outcome is
                # prose the user wrote for themselves, and refusing a plan over the
                # length of a sentence would be pedantry. The full text survives in
                # the draft the conversation keeps.
                title=outcome.strip()[:512],
                issue_ref=inputs.issue_ref,
            )
        )

    eval_address = f"{flow}/{DEFAULT_EPIC}/{FIRST_WAVE}/assess-outcomes"
    nodes.append(
        ProposedNode(
            address=eval_address,
            kind="eval",
            title="Assess whether the declared outcomes were met",
            issue_ref=inputs.issue_ref,
        )
    )
    story_addresses = [node.address for node in nodes if node.kind == "story"]

    if inputs.issue_ref is not None:
        # Forced by rule 6, not inferred: see the docstring. Every story is work on
        # one issue, so they are a sequence whether or not the prose said so, and a
        # document that left them unordered would be refused by `validate_proposal`
        # — a generator bug surfacing as the author's violation.
        edges.extend(
            ProposedEdge(from_address=earlier, to_address=later) for earlier, later in zip(story_addresses, story_addresses[1:], strict=False)
        )

    # An evaluation cannot conclude before the work it evaluates. Asserted for every
    # story even when they are already chained, so removing the chain cannot silently
    # orphan a story from its own assessment.
    edges.extend(ProposedEdge(from_address=address, to_address=eval_address) for address in story_addresses)

    return LoopProposal(
        flow_slug=flow,
        title=inputs.title,
        org_id=inputs.org_id,
        spec_revision=PLANNER_SPEC_REVISION,
        # The intent issue this plan came from, when the conversation opened one.
        # Same normalized form as the nodes' refs, so "which issue is this flow
        # about" and "which issue does this node materialise as" are comparable.
        intent_ref=inputs.issue_ref,
        # The intent verbatim, so the plan carries the user's own statement of what
        # this is for rather than a restatement of it. Excluded from `plan_hash`
        # (`HASH_EXCLUDED_FIELDS`), so wording changes here cannot invalidate a
        # revision a human already bound their acceptance to.
        description=inputs.intent.strip()[:500] or None,
        epic_metadata=[
            EpicMetadata(
                epic_ref=DEFAULT_EPIC,
                title=inputs.epic_display.title if inputs.epic_display else inputs.title.strip()[:200],
                description=inputs.epic_display.description if inputs.epic_display else inputs.intent.strip()[:3000] or inputs.title,
            )
        ],
        wave_metadata=[
            WaveMetadata(
                epic_ref=DEFAULT_EPIC,
                wave_ref=FIRST_WAVE,
                # New conversations carry model-authored display text. Older
                # drafts remain usable without a naming question or another call.
                title=inputs.wave_display.title if inputs.wave_display else inputs.title.strip()[:120],
                description=inputs.wave_display.description if inputs.wave_display else inputs.intent.strip()[:500] or None,
            )
        ],
        nodes=nodes,
        edges=edges,
        proposed_execution_policy=(
            ExecutionPolicy(
                org_id=inputs.org_id,
                repository_ids=[inputs.repository.full_name],
                allowed_actions=[Action.DEVELOP, Action.REVIEW, Action.REPAIR, Action.EVALUATE],
                expires_at=expires_at,
                limits=PolicyLimits(
                    max_spend_usd="5.00",
                    max_wall_clock_seconds=3600,
                    max_attempts_per_node=2,
                    max_concurrent_actions=1,
                ),
            )
            if inputs.repository
            else None
        ),
    )
