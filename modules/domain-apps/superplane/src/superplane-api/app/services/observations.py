"""Receiver for the v1 observation contract — issue #5056 (U15).

This module is the receiving half of the contract U8 (#5043) defined. It holds
the four things the contract deliberately left to the receiver, because each one
needs storage or a trusted clock and the contract package has neither:

1. **Submitter resolution** — a credential to an authenticated identity and its
   workspace grant, from deployment configuration.
2. **Ownership resolution** — a cluster to the workspace that owns it, from
   storage. The contract requires this be independent of the payload: the body's
   claimed workspace is a claim, and comparing a claim against itself authorizes
   nothing.
3. **Replay/idempotency state** — a signature proves authorship, not novelty.
4. **Persistence** — writing the validated observation to the domain tables the
   monitor is losing direct access to.

Nothing here logs a credential, a signing key or a signature.

## The ownership lookup, and why it uses only one of the two available columns

There are two columns that appear to relate a cluster to a workspace, and only
one of them can authorize a write:

* ``workspaces.cluster_id`` — a workspace's own dedicated cluster. At most one
  workspace points at a given cluster this way, so it resolves to a single owner.
* ``workspaces.shared_cluster_id`` — a cluster shared by many workspaces. This is
  many-to-one *by design*, so it cannot answer "who owns this cluster?" at all.

Resolving through the shared column would mean any tenant on a shared cluster
could write fleet state for every other tenant on it, which is precisely the
cross-workspace hole acceptance 3 exists to close. So a shared cluster resolves
to no owner and its submissions are refused. That is a real functional limit, not
an oversight: per-cluster observations for a shared cluster do not have a single
authorized submitter, and inventing one would be the vulnerability.

``clusters.workspace_id`` also exists and looks like the obvious answer. It is
never written by any application code in this tree — verified by searching for
writes to it — so trusting it would fail *open* for every cluster: a NULL would
have to mean either "refuse everything" or "no owner check". It is not used here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.cluster import Cluster
from app.models.event import Event
from app.models.observation import ObservationReceipt
from app.models.workspace import Workspace

from superplane_contracts import (
    AuthResult,
    CheckStatus,
    Observation,
    Submitter,
    authorize_read,
    authorize_submit,
    verify_submission,
)

# Refusal reasons owned by the receiver. Deliberately as uninformative as the
# contract's own: they name the requirement that failed without confirming
# whether a cluster or workspace exists. `_UNKNOWN_SUBJECT` is used for both "no
# such cluster" and "cluster owned by someone else" for that reason.
_UNKNOWN_SUBJECT = "subject not found or not owned by this submitter"
_REPLAYED = "observation already recorded or superseded"


class ObservationRefused(Exception):
    """A submission the receiver will not accept.

    Carries a caller-safe reason and the HTTP status the route should return.
    Raised rather than returned so no caller can accidentally proceed past a
    refusal by ignoring a result object.
    """

    def __init__(self, reason: str, status_code: int = 401) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True)
class _ConfiguredSubmitter:
    """One entry from `settings.observation_submitters`."""

    submitter_id: str
    credential: str
    signing_key: bytes
    workspaces: frozenset[str]
    lease_scopes: frozenset[str] = frozenset()


class ConfiguredSubmitterResolver:
    """Resolves credentials against deployment configuration.

    Implements the contract's `SubmitterResolver` protocol. Comparison is
    constant-time and over every configured entry, so a rejected credential does
    not reveal through timing how many entries exist or how far it matched.
    """

    def __init__(self, entries: list[_ConfiguredSubmitter]) -> None:
        self._entries = entries

    def resolve(self, credential: str) -> Submitter | None:
        import hmac

        matched: _ConfiguredSubmitter | None = None
        for entry in self._entries:
            if hmac.compare_digest(
                credential.encode("utf-8"), entry.credential.encode("utf-8")
            ):
                matched = entry
        if matched is None:
            return None
        return Submitter(
            submitter_id=matched.submitter_id,
            workspaces=matched.workspaces,
            lease_scopes=matched.lease_scopes,
        )

    def signing_key_for(self, credential: str) -> bytes | None:
        """The signing key bound to this credential.

        Per-credential rather than one global key: a shared signing key means any
        submitter can forge any other submitter's bodies, which would make the
        identity in the credential meaningless.
        """
        import hmac

        for entry in self._entries:
            if hmac.compare_digest(
                credential.encode("utf-8"), entry.credential.encode("utf-8")
            ):
                return entry.signing_key
        return None


def load_submitters(raw: str | None = None) -> ConfiguredSubmitterResolver:
    """Parse the configured submitter list.

    Fail-closed on every error path, including malformed JSON: a deployment with
    an unparseable submitter list authorizes nobody rather than falling back to a
    permissive default. Entries missing any required field are skipped rather
    than defaulted, since a default grant is the failure this avoids.
    """
    source = settings.observation_submitters if raw is None else raw
    if not source or not source.strip():
        return ConfiguredSubmitterResolver([])
    try:
        parsed = json.loads(source)
    except ValueError:
        # Not logged with the value attached — it contains credentials.
        return ConfiguredSubmitterResolver([])
    if not isinstance(parsed, list):
        return ConfiguredSubmitterResolver([])

    entries: list[_ConfiguredSubmitter] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        submitter_id = item.get("submitter_id")
        credential = item.get("credential")
        signing_key = item.get("signing_key")
        workspaces = item.get("workspaces")
        lease_scopes = item.get("lease_scopes", [])
        if not isinstance(submitter_id, str) or not submitter_id.strip():
            continue
        if not isinstance(credential, str) or not credential:
            continue
        if not isinstance(signing_key, str) or not signing_key:
            continue
        if not isinstance(workspaces, list) or not all(
            isinstance(w, str) for w in workspaces
        ):
            continue
        if not isinstance(lease_scopes, list) or not all(
            isinstance(v, str) for v in lease_scopes
        ):
            continue
        entries.append(
            _ConfiguredSubmitter(
                submitter_id=submitter_id,
                credential=credential,
                signing_key=signing_key.encode("utf-8"),
                workspaces=frozenset(w for w in workspaces if w),
                lease_scopes=frozenset(lease_scopes),
            )
        )
    return ConfiguredSubmitterResolver(entries)


async def resolve_cluster_workspace(
    db: AsyncSession, cluster_id: uuid.UUID
) -> str | None:
    """The immutable ID of the workspace that owns `cluster_id`, or None.

    None means "no single owner could be established" and must be treated as a
    refusal by the caller — never as "skip the ownership check". See the module
    docstring for why only the dedicated-cluster relation is consulted.
    """
    result = await db.execute(
        select(Workspace.id, Workspace.org_id, Cluster.org_id)
        .join(Cluster, Workspace.cluster_id == Cluster.id)
        .where(Workspace.cluster_id == cluster_id)
    )
    owners = result.all()
    if len(owners) != 1 or owners[0][1] != owners[0][2]:
        # Zero: no workspace claims this cluster as its dedicated one (it may not
        # exist, or may only be shared). More than one: the data contradicts
        # itself, and guessing an owner during an authorization decision is how a
        # cross-tenant write gets approved.
        return None
    return str(owners[0][0])


def _body_digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


async def _check_and_record_receipt(
    db: AsyncSession,
    *,
    cluster_id: uuid.UUID,
    workspace: str,
    submitter_id: str,
    observation: Observation,
    body: bytes,
) -> bool:
    """Enforce replay/monotonicity. True if this submission is new work.

    Returns False for an exact retry of the most recent submission — the same
    bytes, which a sender's retry after a lost response legitimately produces.
    That is idempotent, so the caller reports success without writing again.

    Raises `ObservationRefused` for a *different* body at or before the recorded
    instant: that is either a replay of an older observation or a same-instant
    contradiction, and accepting it would let a captured request overwrite newer
    state inside the freshness window.
    """
    digest = _body_digest(body)
    existing = await db.get(ObservationReceipt, cluster_id)
    if existing is None:
        db.add(
            ObservationReceipt(
                cluster_id=cluster_id,
                workspace=workspace,
                submitter_id=submitter_id,
                last_reported_at=observation.reported_at,
                last_body_sha256=digest,
                last_status=observation.status.value,
            )
        )
        return True

    recorded_at = existing.last_reported_at
    if recorded_at.tzinfo is None:
        # SQLite (used by the test suite) returns naive datetimes even for
        # timezone-aware columns. The stored instant is UTC by construction, so
        # attaching UTC restores the comparison rather than assuming a local zone.
        recorded_at = recorded_at.replace(tzinfo=UTC)

    if observation.reported_at < recorded_at:
        raise ObservationRefused(_REPLAYED, status_code=409)
    if observation.reported_at == recorded_at:
        if existing.last_body_sha256 == digest:
            return False
        raise ObservationRefused(_REPLAYED, status_code=409)

    existing.workspace = workspace
    existing.submitter_id = submitter_id
    existing.last_reported_at = observation.reported_at
    existing.last_body_sha256 = digest
    existing.last_status = observation.status.value
    return True


# How a contract health status maps onto the `clusters.health_status` vocabulary
# the rest of the API already reads (`_effective_health_status` in
# routers/workspaces.py, the console, the events table).
#
# `not_checked` maps to "Unknown" because that column has no way to say "nothing
# was measured", and "Unknown" is the closest honest word in a vocabulary that
# predates the distinction. It must NOT map to "Healthy": the entire point of
# acceptance 4 is that an unperformed probe cannot report health. The contract
# keeps the finer distinction, and the receipt row records the contract status
# verbatim so the difference is not lost at the boundary.
_STATUS_TO_HEALTH = {
    CheckStatus.HEALTHY: "Healthy",
    CheckStatus.DEGRADED: "Degraded",
    CheckStatus.UNREACHABLE: "Unhealthy",
    CheckStatus.UNKNOWN: "Unknown",
    CheckStatus.NOT_CHECKED: "Unknown",
}


async def record_observation(
    db: AsyncSession,
    *,
    body: bytes,
    headers: dict[str, str],
    now: datetime | None = None,
) -> tuple[Observation, bool]:
    """Authenticate, authorize, and persist one submission.

    Returns the validated observation and whether it was newly applied (False for
    an idempotent retry). Raises `ObservationRefused` on every failure path.

    The order is deliberate and is the contract's: authenticate the raw bytes,
    then resolve ownership from storage, then authorize the *authenticated*
    submitter against the *stored* owner, and only then write. Each step's
    evidence is unusable as the next step's evidence.
    """
    resolver = load_submitters()
    credential = ""
    for key, value in headers.items():
        if key.lower() == "authorization":
            credential = (value or "").strip()
            break

    # A signing key is looked up by credential before verification, because the
    # signature must be checked against the key bound to the claimed identity.
    # An unknown credential yields no key, and the contract then refuses with its
    # own non-enumerating reason.
    signing_key = resolver.signing_key_for(credential) if credential else None

    result: AuthResult = verify_submission(
        body,
        headers,
        resolver,
        signing_key if signing_key is not None else b"",
        now=now,
    )
    if not result.authenticated or result.observation is None:
        raise ObservationRefused(result.reason or "unauthenticated", status_code=401)

    observation = result.observation
    submitter = result.submitter
    assert submitter is not None  # guaranteed by an authenticated AuthResult

    try:
        cluster_id = uuid.UUID(observation.subject.cluster_id)
    except (ValueError, AttributeError, TypeError):
        # The contract guarantees a non-blank cluster_id, not that it is a UUID.
        raise ObservationRefused(_UNKNOWN_SUBJECT, status_code=404) from None

    cluster = await db.get(Cluster, cluster_id)
    if cluster is None:
        raise ObservationRefused(_UNKNOWN_SUBJECT, status_code=404)

    owner = await resolve_cluster_workspace(db, cluster_id)
    decision = authorize_submit(submitter, observation, cluster_workspace=owner)
    if not decision.allowed:
        # 403, not 404: the caller authenticated successfully and the refusal is
        # about authority. The reason string still does not distinguish a missing
        # workspace from a forbidden one.
        raise ObservationRefused(decision.reason, status_code=403)

    # `owner` is not None here — authorize_submit refuses a None cluster_workspace.
    applied = await _check_and_record_receipt(
        db,
        cluster_id=cluster_id,
        workspace=owner or "",
        submitter_id=submitter.submitter_id,
        observation=observation,
        body=body,
    )
    if applied:
        # Read the previous status BEFORE applying: `_apply_to_cluster` overwrites
        # `cluster.health_status`, so capturing it afterwards would compare the new
        # value against itself and never record a transition.
        previous_health = cluster.health_status
        _apply_to_cluster(cluster, observation, submitter_id=submitter.submitter_id)
        _record_transition_event(db, cluster, observation, previous=previous_health)
    await db.commit()
    return observation, applied


async def list_scoped_clusters(
    db: AsyncSession, submitter: Submitter, *, statuses: tuple[str, ...]
) -> list[tuple[Cluster, str]]:
    """Clusters in `statuses` that this submitter may observe, with their owner.

    The monitor's replacement for ``SELECT ... FROM clusters``. Two properties
    matter more than the query shape:

    **The scope filter is applied in the query, not by the caller.** The owning
    workspace is joined in rather than looked up afterwards, so there is no code
    path that produces an unfiltered list which a later mistake could return.

    **The join is the same dedicated-cluster relation used for submit
    authorization** (see the module docstring). A cluster with no single owning
    workspace is absent from this list for exactly the reason its submissions are
    refused, so a monitor cannot observe a cluster it could not write to.
    """
    # A display-name grant is not authority over any workspace, even if its name
    # happens to match. Parse before SQL so malformed config never causes a UUID
    # cast error in PostgreSQL.
    workspace_ids = []
    for value in submitter.workspaces:
        try:
            workspace_ids.append(uuid.UUID(value))
        except (ValueError, TypeError, AttributeError):
            continue
    if not workspace_ids:
        return []
    single_owner = (
        select(Workspace.cluster_id)
        .where(Workspace.cluster_id.is_not(None))
        .group_by(Workspace.cluster_id)
        .having(func.count(Workspace.id) == 1)
    )
    result = await db.execute(
        select(Cluster, Workspace.id)
        .join(Workspace, Workspace.cluster_id == Cluster.id)
        .where(
            Cluster.status.in_(statuses),
            Workspace.id.in_(workspace_ids),
            Workspace.org_id == Cluster.org_id,
            Cluster.id.in_(single_owner),
        )
        .order_by(Cluster.last_heartbeat.asc().nullsfirst())
    )
    return [(row[0], str(row[1])) for row in result.all()]


async def authorized_cluster(
    db: AsyncSession, submitter: Submitter, cluster_id: uuid.UUID
) -> tuple[Cluster, str]:
    """Load a cluster the submitter is authorized for, or refuse.

    Refuses with the same 404 and the same reason whether the cluster is absent
    or owned by another workspace, so this cannot be used to test which cluster
    UUIDs exist. Shared by the event-write and cost-history routes so neither can
    drift into a more informative refusal than the other.
    """
    cluster = await db.get(Cluster, cluster_id)
    owner = await resolve_cluster_workspace(db, cluster_id)
    if cluster is None or owner is None or not authorize_read(submitter, owner).allowed:
        raise ObservationRefused(_UNKNOWN_SUBJECT, status_code=404)
    return cluster, owner


async def cost_history(
    db: AsyncSession, cluster: Cluster, *, since: datetime
) -> list[float]:
    """Hourly cost values recorded for this cluster since `since`.

    Replaces the monitor's ``details_json->>'cost_hourly'`` query. The extraction
    is done in Python rather than in SQL because `Event.details_json` is a `Text`
    column, not JSONB, so the old `->>'...'` cast only worked by PostgreSQL's
    implicit text-to-json coercion — which the test suite's SQLite has no
    equivalent for. A malformed or cost-free row contributes nothing rather than
    a zero, since a zero would drag the rolling average down and mask a spike.
    """
    result = await db.execute(
        select(Event.details_json)
        .where(
            Event.resource_type == "cluster",
            Event.resource_id == cluster.id,
            Event.created_at > since,
        )
        .order_by(Event.created_at.desc())
    )
    costs: list[float] = []
    for (raw,) in result.all():
        if not raw:
            continue
        try:
            value = json.loads(raw).get("cost_hourly")
        except (ValueError, AttributeError):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            costs.append(float(value))
    return costs


def record_scoped_event(
    db: AsyncSession,
    cluster: Cluster,
    *,
    event_type: str,
    message: str,
    details: dict,
    submitter_id: str,
) -> None:
    """Write an event about a cluster the caller is already authorized for.

    `org_id` and `resource_id` come from the loaded cluster row, never from the
    request: a caller that could name the org it was writing an event against
    could attribute its events to another tenant's audit trail. `submitter_id` is
    recorded from the authenticated identity for the same reason.
    """
    db.add(
        Event(
            org_id=cluster.org_id,
            action="updated",
            resource_type="cluster",
            resource_id=cluster.id,
            event_type=event_type,
            message=message,
            details_json=json.dumps({**details, "submitter_id": submitter_id}),
        )
    )


def _apply_to_cluster(
    cluster: Cluster, observation: Observation, *, submitter_id: str
) -> None:
    """Fold a validated observation into the cluster row.

    Only checks that were actually performed contribute a value. A check the
    submitter reported as `not_checked` writes no observed value and instead
    records that it was not checked, so a stale value from an earlier cycle is
    never re-presented as a current reading.

    ## Check results live under `observation`, never at the top level

    The top level of `actual_state_json` is the *controller's* namespace: the
    legacy heartbeat route merges `skypilot_healthy`, `vault_sync_status`,
    `node_summary`, `cost_hourly` and `cost_hourly_avg` into it, and the monitor
    parses exactly those keys back out (`db.HeartbeatPayload`). Monitor check
    names are drawn from the same vocabulary — `vault_sync_status` is *both* a
    controller-reported fact and a monitor dimension name — so writing a check
    status at the top level overwrites the input the check was judging. A
    controller-reported `vault_sync_status: "failed"` would become the monitor's
    own verdict `"degraded"`, which is outside the producer's `ok|pending|failed`
    vocabulary and so reads back as Unknown, destroying the actionable
    "re-trigger credential sync" signal after a single cycle. Recording a
    `not_checked` result by deleting the top-level key does the same damage more
    directly.

    Both directions are avoided by keeping every check result inside the
    `observation` sub-document, where the names cannot collide with a producer's.
    Nothing is lost: `checks_performed` and `checks_not_performed` below already
    carry each dimension's status and each unperformed dimension's reason, and
    they are what the read path and the tests consult.
    """
    state = dict(cluster.actual_state_json or {})
    performed: dict[str, str] = {}
    not_checked: dict[str, str] = {}
    for check in observation.checks:
        if check.status is CheckStatus.NOT_CHECKED:
            not_checked[check.name] = check.reason or "not checked"
            continue
        performed[check.name] = check.status.value

    state["observation"] = {
        "contract_version": observation.contract_version,
        "kind": observation.kind,
        "status": observation.status.value,
        "reported_at": observation.reported_at.isoformat(),
        "reporter": observation.reporter,
        "submitter_id": submitter_id,
        "checks_performed": performed,
        "checks_not_performed": not_checked,
    }
    cluster.actual_state_json = state
    cluster.health_status = _STATUS_TO_HEALTH[observation.status]
    # `last_reconciled_at`, NOT `last_heartbeat`. This is the column the SQL this
    # path replaces wrote (`UpdateClusterHealth` set `health_status`,
    # `last_reconciled_at`, `actual_state_json`), and the distinction is
    # load-bearing rather than cosmetic:
    #
    # `last_heartbeat` means "when the data-plane *controller* last reported in".
    # It is the input to two independent staleness judgements — the monitor's own
    # `checkHeartbeatFreshness`, which reads it back through the scoped cluster
    # list, and `_effective_health_status` in routers/workspaces.py, which
    # degrades a cluster whose heartbeat has aged out. Stamping it here would make
    # the monitor's own submission the thing that refreshes the timestamp it uses
    # to detect a silent controller: every poll would reset the clock, the
    # heartbeat dimension could never reach Degraded or Unreachable again, and a
    # cluster whose controller had died would read as freshly alive forever. A
    # receiver must not be able to manufacture liveness evidence for a producer
    # it does not speak for.
    #
    # `last_reconciled_at` says "the monitor last completed a cycle against this
    # cluster", which is what actually happened here and what makes the
    # monitor's own liveness observable without forging the controller's.
    cluster.last_reconciled_at = observation.reported_at


def _record_transition_event(
    db: AsyncSession,
    cluster: Cluster,
    observation: Observation,
    *,
    previous: str | None,
) -> None:
    """Write an audit event when the cluster's health status changed.

    `previous` is passed in rather than read from `cluster`, because by the time
    this is called the row already carries the new status.

    `action` is set explicitly. The legacy heartbeat route omits it even though
    `Event.action` is NOT NULL; this path supplies it, matching
    `services/audit.py:log_event`, which is the correct precedent.
    """
    current = _STATUS_TO_HEALTH[observation.status]
    if previous == current:
        return
    db.add(
        Event(
            org_id=cluster.org_id,
            action="updated",
            resource_type="cluster",
            resource_id=cluster.id,
            event_type="health_status_changed",
            message=(
                f"Cluster health changed from {previous or 'unknown'} to {current}"
            ),
            details_json=json.dumps(
                {
                    "previous_health_status": previous,
                    "current_health_status": current,
                    "contract_version": observation.contract_version,
                    "contract_status": observation.status.value,
                    "reporter": observation.reporter,
                }
            ),
        )
    )


async def authorize_lease_scope(
    db: AsyncSession, submitter: Submitter, resource_type: str, resource_id: str
) -> str:
    """Authorize before lease storage and return the canonical resource identity.

    The existing budget monitor requires a non-cluster global lease. It needs an
    explicit deployment-owned grant; workspace membership never implies global
    coordination authority. Arbitrary resource types are always refused.
    """
    if resource_type == "cluster_health":
        try:
            cluster_id = uuid.UUID(resource_id)
        except (ValueError, AttributeError, TypeError):
            raise ObservationRefused(_UNKNOWN_SUBJECT, status_code=404) from None
        await authorized_cluster(db, submitter, cluster_id)
        return str(cluster_id)
    if (resource_type, resource_id) == ("budget_monitor", "global") and (
        "budget_monitor/global" in submitter.lease_scopes
    ):
        return resource_id
    raise ObservationRefused(_UNKNOWN_SUBJECT, status_code=404)
