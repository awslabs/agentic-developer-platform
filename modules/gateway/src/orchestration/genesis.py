"""Engine genesis: a human root for a service that has none.

Issue #4204 (EPIC #4191, intent #4120), ruling **D-R12**.

The engine is a service. Every other way a chain starts in this platform is a
human doing something — a comment, a label, an SSO-authenticated click — and the
trigger path is explicit that an agent cannot mint a root: it returns
`422 unknown_chain` rather than inventing one (`agent_trigger.py`). That leaves
the engine unable to dispatch anything at all, which is the gap this module
closes.

D-R12 closes it by finding a real human act behind every engine dispatch: **the
SSO-attributed gate approver who accepted the plan**. A gate approval is a
genuine human decision with a genuine identity, already recorded, already
attributed. The engine does not need a synthetic root because a real one exists
one row away.

**The whole security design is one distinction.** The engine supplies a
`decision_id` — an opaque reference — and the approver is resolved **here**,
server-side, from the decision row. Nothing accepts a caller-supplied
`root_human_id`. The difference matters because a caller-supplied root is a
claim, and a resolved root is a fact: if the engine (or anything that can reach
the engine) could name its own root human, it could name *any* human, and every
downstream consumer would see a forgery that is indistinguishable from a real
human-rooted run. Resolution by reference removes the ability to lie rather than
adding a check for lying.

**This is an ADDITIONAL root source, not a weaker one.** Existing marker
verification stays exactly as fail-closed as it was: a forged signature still
starts a new chain, and an unsigned marker claiming `is_human_rooted=true` still
has the claim stripped (`marker_verify.py`, `handler.py`). Nothing here is
reachable by presenting a marker, so nothing here can be reached by forging one.
AC-30's regression clause is the real test of that claim: the existing
marker-verification tests must stay green **unchanged**. If they needed editing,
this path weakened them.

Three refusals, all fail-closed, all tested adversarially (AC-30):

- **No such decision** — refused. An id that resolves to nothing roots nothing.
- **`actor_kind` is `service`** — refused. This is the load-bearing one. The
  decisions table records service decisions too (the tick writes
  `TRANSITION_REJECTED` rows as `SERVICE`), so without this check the engine
  could root a chain in *its own* prior decision and bootstrap human authority
  out of nothing. `actor_kind` is a real column precisely so this question is
  answerable (`models.py`); the store story's docstring spells out why a string
  convention inside `actor_id` was not enough.
- **Another org's decision** — refused. Filtered in SQL, not compared in Python,
  so a cross-org id returns no row and is indistinguishable from absent. It
  cannot be used to probe which decision ids exist in other tenants.

Authority is never read from an envelope (R-O5d). Not `persona`, not
`AGENT_TYPE`, not a caller ARN. IAM cannot express per-persona authority here
anyway — every persona's pod presents an identical ARN (one shared role, one
service account, one registry entry), so an ARN-derived authority check would be
a check that always passes. The only input this module trusts is a primary key,
and the only thing it trusts about it is what the database says.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DecisionKind, OrchestrationDecision
from .state import ActorKind

logger = logging.getLogger("bedrockgateway.orchestration.genesis")

__all__ = [
    "APPROVAL_DECISION_KINDS",
    "EngineGenesis",
    "GenesisRefusedError",
    "resolve_engine_genesis",
]


# Which decision kinds represent a human accepting work, and so can root a
# dispatch. Spelled out as data rather than inferred from "is it human-authored",
# because those are different questions: a human *rejecting* a gate is also a
# human decision with a human identity, and rooting a dispatch in it would mean
# the engine dispatches work a human just refused.
#
# `TRANSITION_REJECTED` and `HALT_OVERRIDDEN` are deliberately absent.
# `HALT_OVERRIDDEN` is a human act, but it clears a halt rather than accepting a
# plan; the acceptance that authorises the work is the row this looks for.
APPROVAL_DECISION_KINDS: frozenset[str] = frozenset(
    {
        DecisionKind.PLAN_ACCEPTED.value,
        DecisionKind.PLAN_AMENDED.value,
        DecisionKind.GATE_APPROVED.value,
    }
)


class GenesisRefusedError(Exception):
    """Raised when a `decision_id` cannot root a chain.

    An exception rather than a `None` return, and deliberately so: a caller that
    forgets to check a falsy return dispatches **unrooted**, which is the exact
    failure this module exists to prevent. The refusal has to be impossible to
    ignore by accident.

    The message names the reason but never echoes the resolved approver — a
    refusal is not an oracle for who approved what.
    """


@dataclass(frozen=True)
class EngineGenesis:
    """A human root for one engine dispatch, resolved from a decision row.

    Frozen because this is an authority object. Once resolved it describes what
    the database said; a caller that could mutate `root_human_id` after
    resolution would have re-created the caller-supplied root that D-R12 forbids.

    There is deliberately **no** constructor that takes a `root_human_id`
    directly — the only way to obtain one of these is
    :func:`resolve_engine_genesis`, i.e. by reference to a real row. A test
    helper that fabricated one would be building the forgery path.
    """

    # The gate approver's identity, as recorded on the decision row. This is the
    # chain's root human.
    root_human_id: str
    # The approver's role AT DECISION TIME (snapshotted on the row). Carried so
    # attribution reflects the authority held when the decision was made, not
    # whatever the actor holds now.
    root_human_role: str
    # The decision this genesis derives from, for the audit trail.
    decision_id: str
    # The flow the decision belongs to. Resolved, not supplied — a caller naming
    # its own flow could point a real approval at unrelated work.
    flow_id: str
    org_id: str
    kind: str

    @property
    def is_human_rooted(self) -> bool:
        """Always True, and not a stored field.

        A boolean that can be *set* is a boolean that can be set to the wrong
        value. This object only exists when a human-kind decision row was
        resolved, so human-rootedness is a property of its existence rather than
        of a field somebody could assign. The unsigned-marker path in
        `handler.py` strips exactly this claim when it arrives as data; here
        there is no field to strip.
        """
        return True


async def resolve_engine_genesis(
    session: AsyncSession,
    *,
    org_id: str,
    decision_id: str,
) -> EngineGenesis:
    """Resolve an engine dispatch's human root from a gate-approval decision.

    This is the only way to obtain an :class:`EngineGenesis`. Everything it
    trusts is read from the row; the only caller input is the id to look up and
    the org to look it up in.

    Args:
        session: Caller-owned session. This function reads and never writes, so
            it composes inside the dispatch transaction.
        org_id: The tenant to resolve within. Applied as a SQL filter, so a
            `decision_id` belonging to another org resolves to nothing.
        decision_id: Opaque reference to the `orchestration_decisions` row whose
            actor approved this work.

    Returns:
        The resolved :class:`EngineGenesis`.

    Raises:
        GenesisRefusedError: If `decision_id` is empty, resolves to no row in
            this org, records a non-approval decision kind, was made by a
            `service` actor, or carries no `actor_id`. Every case is a refusal to
            dispatch, never a downgrade to an unrooted dispatch.
    """
    if not decision_id:
        # An absent id is not "no genesis requested" — the caller asked for
        # engine genesis and supplied nothing to root it in.
        raise GenesisRefusedError("engine genesis requires a decision_id; none was supplied")

    if not org_id:
        # Without an org there is no tenant filter, and an unfiltered lookup
        # would resolve any tenant's decision row. Refusing is the only safe
        # reading of a missing tenant.
        raise GenesisRefusedError("engine genesis requires an org_id; refusing an unscoped decision lookup")

    # org_id is part of the WHERE clause, not a post-hoc comparison. A cross-org
    # id therefore returns no row and produces the identical refusal as an id
    # that does not exist — so this cannot be used to discover whether some
    # decision id is real in another tenant.
    stmt = select(OrchestrationDecision).where(
        OrchestrationDecision.org_id == org_id,
        OrchestrationDecision.id == decision_id,
    )
    decision = (await session.execute(stmt)).scalar_one_or_none()

    if decision is None:
        logger.warning(
            "orchestration genesis: refused — decision_id=%r not found in org=%r",
            decision_id,
            org_id,
        )
        raise GenesisRefusedError(f"decision_id {decision_id!r} does not exist in this org; it cannot root a chain")

    # The load-bearing check (D-R12): a service decision cannot root a chain.
    # Without it the engine could root a dispatch in its own prior decision row
    # — the tick writes plenty of them — and manufacture human authority from
    # nothing at all.
    if decision.actor_kind != ActorKind.HUMAN.value:
        logger.warning(
            "orchestration genesis: refused — decision_id=%r has actor_kind=%r; only a human decision roots a chain",
            decision_id,
            decision.actor_kind,
        )
        raise GenesisRefusedError(
            f"decision {decision_id!r} was made by actor_kind={decision.actor_kind!r}; only a human decision can root an engine dispatch"
        )

    # A human decision, but not one that accepted anything. Rooting a dispatch in
    # a human's *refusal* would have the engine dispatch work that a human just
    # declined — an inversion, not a gap.
    if decision.kind not in APPROVAL_DECISION_KINDS:
        logger.warning(
            "orchestration genesis: refused — decision_id=%r kind=%r is not an approval",
            decision_id,
            decision.kind,
        )
        raise GenesisRefusedError(
            f"decision {decision_id!r} has kind={decision.kind!r}, which is not an approval; expected one of {sorted(APPROVAL_DECISION_KINDS)}"
        )

    # `actor_id` is NOT NULL in the schema, so an empty value means something
    # wrote an unattributed row. Treating "" as a root human would give the chain
    # a root that identifies nobody, which is worse than having none.
    if not decision.actor_id:
        logger.error(
            "orchestration genesis: refused — decision_id=%r is human-kind but carries no actor_id",
            decision_id,
        )
        raise GenesisRefusedError(f"decision {decision_id!r} carries no actor_id; there is no human to root the chain in")

    genesis = EngineGenesis(
        root_human_id=decision.actor_id,
        root_human_role=decision.actor_role,
        decision_id=decision.id,
        flow_id=decision.flow_id,
        org_id=decision.org_id,
        kind=decision.kind,
    )

    # Observability only (issue #4321). This log is a PROJECTION of the object
    # above, never a source of authority: it is emitted from the resolved
    # `genesis`, after resolution, so a reader cannot see a human root here that
    # the authority object did not assert. Anything needing the root human must
    # read `EngineGenesis`; a log line (or anything derived from one) is not an
    # authority path.
    #
    # Two audiences, one call, which is why the fields appear twice:
    #  - `extra=` keys become TOP-LEVEL JSON keys under the deployed
    #    `StructuredJsonFormatter` (`src/shared/logging.py`), which is what makes
    #    them queryable in CloudWatch Insights rather than trapped in a string.
    #  - the message text repeats the stable `engine_genesis` token and the flag
    #    because JSON renders the field as `"is_human_rooted": true`, so an
    #    operator grepping `is_human_rooted=true` would match nothing without it.
    #
    # `root_human_id` is a STRUCTURED FIELD ONLY and deliberately absent from the
    # message text. It is the opaque internal identity already on the decision
    # row; no email, profile or credential data is logged. The refusal branches
    # above stay as they are — a refusal names its reason and never echoes the
    # approver, so it cannot become an oracle for who approved what.
    logger.info(
        "orchestration genesis: resolved decision_id=%s kind=%s flow=%s org=%s event=engine_genesis is_human_rooted=%s",
        decision_id,
        decision.kind,
        decision.flow_id,
        org_id,
        # Rendered lowercase to match both the JSON boolean and the operator grep,
        # and read off the object rather than hardcoded so the text cannot claim a
        # human root the authority object did not assert.
        str(genesis.is_human_rooted).lower(),
        extra={
            "event": "engine_genesis",
            "decision_id": genesis.decision_id,
            "kind": genesis.kind,
            "flow_id": genesis.flow_id,
            # Note: `StructuredJsonFormatter` also injects `org_id` from its
            # request contextvar when one is set, which wins over this key. Both
            # describe the same tenant — resolution is org-filtered in SQL — so
            # the emitted value is correct either way.
            "org_id": genesis.org_id,
            "is_human_rooted": genesis.is_human_rooted,
            "root_human_id": genesis.root_human_id,
        },
    )

    return genesis
