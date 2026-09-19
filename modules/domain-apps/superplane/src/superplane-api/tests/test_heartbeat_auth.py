"""The authenticated observation receiver — issue #5056 (U15), R11 acceptances 2-4.

These tests are written against the *properties the story names*, not against the
implementation's branches, so they would still fail if the receiver were rewritten
in a way that reopened any of the holes:

* acceptance 2 — a submission without a credential or without a valid signature is
  refused, and knowing a cluster UUID authorizes nothing.
* acceptance 3 — authenticated is not sufficient: cross-workspace *submission* and
  cross-workspace *read* each fail.
* acceptance 4 — a probe never reports health it did not check; a `not_checked`
  result must not become "Healthy" anywhere on the write path.
* replay/idempotency — the receiver's own responsibility, since a signature proves
  authorship and not novelty.

The signing keys and credentials below are test fixtures generated in-file. They
are not real credentials and authorize nothing outside this suite.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.cluster import Cluster
from app.models.event import Event
from app.models.observation import ObservationLease, ObservationReceipt
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.services import observations as observation_service
from tests.conftest import async_session_test

from superplane_contracts import (
    CheckResult,
    CheckStatus,
    ClusterRef,
    Observation,
    canonical_body,
    compute_signature,
)

# Test-only submitter identities. Distinct signing keys per submitter, because a
# shared key would let one forge the other's bodies and the cross-workspace tests
# below would pass for the wrong reason.
_MONITOR_KEY = b"test-signing-key-monitor-not-a-credential"
_OTHER_KEY = b"test-signing-key-other-not-a-credential"
_MONITOR_CRED = "test-credential-monitor"
_OTHER_CRED = "test-credential-other"

_WS_OWNED = "a1111111-a111-4111-8111-a11111111111"
_WS_FOREIGN = "b2222222-b222-4222-8222-b22222222222"


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    """Clear the rate limiter's process-wide sliding windows between tests.

    `RateLimitMiddleware` holds its buckets on the middleware instance, which the
    app builds once at import, so counts accumulate across every test in the
    session under the shared `global:anonymous` key. This suite makes enough calls
    per test to reach the 60/minute limit, which would otherwise turn a later test
    into a 429 depending on how many ran before it.

    Scoped to this file rather than fixed in `conftest.py`: the leakage is
    pre-existing and shared by every suite here, and quietly changing global test
    setup is a wider change than this story asked for.

    The middleware instance only exists once Starlette builds the stack (on the
    first request), so this walks the built stack and clears whatever it finds,
    before and after each test. A no-op on the very first test, which needs no
    clearing.
    """
    from app.main import app

    def _clear() -> None:
        node = app.middleware_stack
        for _ in range(12):
            if node is None:
                return
            if type(node).__name__ == "RateLimitMiddleware":
                node._buckets.clear()
                return
            node = getattr(node, "app", None)

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _configure_submitters(monkeypatch):
    """Configure two submitters with disjoint grants.

    Disjoint on purpose: it makes "authenticated but not authorized for this
    workspace" a reachable state, which is the whole of acceptance 3.
    """
    monkeypatch.setattr(
        settings,
        "observation_submitters",
        json.dumps(
            [
                {
                    "submitter_id": "monitor-1",
                    "credential": _MONITOR_CRED,
                    "signing_key": _MONITOR_KEY.decode(),
                    "workspaces": [_WS_OWNED],
                },
                {
                    "submitter_id": "other-1",
                    "credential": _OTHER_CRED,
                    "signing_key": _OTHER_KEY.decode(),
                    "workspaces": [_WS_FOREIGN],
                },
            ]
        ),
    )


async def _seed_cluster(workspace_name: str = _WS_OWNED) -> uuid.UUID:
    """Create an org, a cluster, and the workspace that owns it.

    Ownership is established through `workspaces.cluster_id` — the dedicated
    relation — because that is the only one that resolves to a single owner.
    """
    org_id = uuid.uuid4()
    cluster_id = uuid.uuid4()
    async with async_session_test() as session:
        session.add(Organization(id=org_id, name=f"org-{org_id.hex[:8]}"))
        await session.flush()
        session.add(
            Cluster(
                id=cluster_id,
                org_id=org_id,
                name="c1",
                status="Active",
                health_status="Healthy",
            )
        )
        await session.flush()
        session.add(
            Workspace(
                id=uuid.UUID(workspace_name),
                org_id=org_id,
                name="prod",
                isolation_mode="namespace",
                status="Active",
                cluster_id=cluster_id,
            )
        )
        await session.commit()
    return cluster_id


def _observation(
    cluster_id: uuid.UUID,
    workspace: str = _WS_OWNED,
    *,
    checks=None,
    reported_at: datetime | None = None,
) -> Observation:
    when = reported_at or datetime.now(UTC)
    return Observation(
        kind="fleet_health",
        subject=ClusterRef(cluster_id=str(cluster_id), workspace=workspace),
        reported_at=when,
        reporter="test-monitor",
        checks=tuple(
            checks
            if checks is not None
            else (
                CheckResult(
                    name="eks_reachability",
                    status=CheckStatus.HEALTHY,
                    observed_at=when,
                    detail="reachable",
                ),
            )
        ),
    )


def _headers(body: bytes, credential: str, key: bytes) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "x-superplane-contract-version": "v1",
        "authorization": credential,
        "x-superplane-signature": compute_signature(body, key),
    }


async def _submit(client, observation, credential=_MONITOR_CRED, key=_MONITOR_KEY):
    body = canonical_body(observation)
    return await client.post(
        "/internal/observations", content=body, headers=_headers(body, credential, key)
    )


# --- submitter configuration parses fail-closed -------------------------------


class TestSubmitterConfigurationFailsClosed:
    """A misconfigured submitter list must authorize nobody, never everybody.

    Tested directly rather than through the route because the failure mode is
    silent: a malformed entry that got *defaulted* instead of skipped would
    produce a working credential with an unintended grant, which no request-level
    test would flag as wrong.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("", id="empty"),
            pytest.param("   ", id="whitespace"),
            pytest.param("not json at all", id="malformed-json"),
            pytest.param('{"submitter_id": "m"}', id="object-not-list"),
            pytest.param('["a-bare-string"]', id="entry-not-object"),
            pytest.param("[]", id="empty-list"),
        ],
    )
    def test_an_unusable_configuration_resolves_no_credential(self, raw):
        resolver = observation_service.load_submitters(raw)

        assert resolver.resolve("test-credential-monitor") is None
        assert resolver.resolve("") is None

    @pytest.mark.parametrize(
        "entry",
        [
            pytest.param(
                {"credential": "c", "signing_key": "k", "workspaces": []}, id="no-id"
            ),
            pytest.param(
                {
                    "submitter_id": "  ",
                    "credential": "c",
                    "signing_key": "k",
                    "workspaces": [],
                },
                id="blank-id",
            ),
            pytest.param(
                {"submitter_id": "m", "signing_key": "k", "workspaces": []},
                id="no-credential",
            ),
            pytest.param(
                {
                    "submitter_id": "m",
                    "credential": "",
                    "signing_key": "k",
                    "workspaces": [],
                },
                id="blank-credential",
            ),
            pytest.param(
                {"submitter_id": "m", "credential": "c", "workspaces": []},
                id="no-signing-key",
            ),
            pytest.param(
                {
                    "submitter_id": "m",
                    "credential": "c",
                    "signing_key": "",
                    "workspaces": [],
                },
                id="blank-signing-key",
            ),
            pytest.param(
                {"submitter_id": "m", "credential": "c", "signing_key": "k"},
                id="no-workspaces",
            ),
            pytest.param(
                {
                    "submitter_id": "m",
                    "credential": "c",
                    "signing_key": "k",
                    "workspaces": "ws-1",
                },
                id="workspaces-not-a-list",
            ),
            pytest.param(
                {
                    "submitter_id": "m",
                    "credential": "c",
                    "signing_key": "k",
                    "workspaces": [1],
                },
                id="workspace-not-a-string",
            ),
        ],
    )
    def test_an_incomplete_entry_is_skipped_rather_than_defaulted(self, entry):
        resolver = observation_service.load_submitters(json.dumps([entry]))

        assert resolver.resolve("c") is None
        assert resolver.signing_key_for("c") is None

    def test_a_valid_entry_alongside_a_broken_one_still_works(self):
        """One bad entry must not disable the rest, nor be silently repaired."""
        resolver = observation_service.load_submitters(
            json.dumps(
                [
                    {"submitter_id": "broken"},
                    {
                        "submitter_id": "good",
                        "credential": "good-cred",
                        "signing_key": "good-key",
                        "workspaces": ["ws-1", ""],
                    },
                ]
            )
        )

        resolved = resolver.resolve("good-cred")

        assert resolved is not None
        assert resolved.submitter_id == "good"
        # The blank workspace is dropped: a blank grant entry must not become
        # authority over a blank workspace name.
        assert resolved.workspaces == frozenset({"ws-1"})
        assert resolver.signing_key_for("good-cred") == b"good-key"


# --- acceptance 2: submission is authenticated -------------------------------


class TestSubmissionRequiresAuthentication:
    async def test_a_cluster_uuid_alone_authorizes_nothing(self, client):
        """The hole this story closes, stated as a test.

        A body naming a real cluster, correct in every respect except that it
        carries no identity, must get nowhere.
        """
        cluster_id = await _seed_cluster()
        body = canonical_body(_observation(cluster_id))

        response = await client.post(
            "/internal/observations",
            content=body,
            headers={
                "content-type": "application/json",
                "x-superplane-contract-version": "v1",
            },
        )

        assert response.status_code == 401
        assert "credential" in response.json()["detail"]

    async def test_an_unresolvable_credential_is_refused(self, client):
        cluster_id = await _seed_cluster()
        response = await _submit(
            client, _observation(cluster_id), credential="not-a-real-credential"
        )

        assert response.status_code == 401

    async def test_an_unsigned_body_is_refused(self, client):
        cluster_id = await _seed_cluster()
        body = canonical_body(_observation(cluster_id))

        response = await client.post(
            "/internal/observations",
            content=body,
            headers={
                "content-type": "application/json",
                "x-superplane-contract-version": "v1",
                "authorization": _MONITOR_CRED,
            },
        )

        assert response.status_code == 401
        assert "signature" in response.json()["detail"]

    async def test_a_body_signed_with_the_wrong_key_is_refused(self, client):
        """A valid credential does not make any signature acceptable.

        Signed with the *other* submitter's key while presenting the monitor's
        credential — which is what a submitter that got hold of someone else's
        token but not their key looks like.
        """
        cluster_id = await _seed_cluster()
        response = await _submit(
            client, _observation(cluster_id), credential=_MONITOR_CRED, key=_OTHER_KEY
        )

        assert response.status_code == 401

    async def test_a_tampered_body_no_longer_matches_its_signature(self, client):
        """Signature covers the transmitted bytes, so any edit invalidates it."""
        cluster_id = await _seed_cluster()
        observation = _observation(cluster_id)
        body = canonical_body(observation)
        headers = _headers(body, _MONITOR_CRED, _MONITOR_KEY)

        tampered = json.loads(body)
        tampered["reporter"] = "someone-else"

        response = await client.post(
            "/internal/observations",
            content=json.dumps(tampered).encode(),
            headers=headers,
        )

        assert response.status_code == 401

    async def test_a_valid_submission_is_accepted(self, client):
        cluster_id = await _seed_cluster()
        response = await _submit(client, _observation(cluster_id))

        assert response.status_code == 202
        assert response.json()["applied"] is True

    async def test_an_unconfigured_deployment_authorizes_nobody(
        self, client, monkeypatch
    ):
        """Fail-closed: no configured submitters means no valid credential exists."""
        monkeypatch.setattr(settings, "observation_submitters", "")
        cluster_id = await _seed_cluster()

        response = await _submit(client, _observation(cluster_id))

        assert response.status_code == 401


# --- acceptance 3: authenticated is not sufficient ---------------------------


class TestSubjectOwnershipIsEnforced:
    async def test_a_submitter_cannot_write_another_workspaces_cluster(self, client):
        """Cross-workspace submission. Authenticated, correctly signed, refused.

        `other-1` signs a valid body for a cluster owned by `ws-owned`, which its
        grant does not cover.
        """
        cluster_id = await _seed_cluster(workspace_name=_WS_OWNED)

        response = await _submit(
            client,
            _observation(cluster_id, workspace=_WS_OWNED),
            credential=_OTHER_CRED,
            key=_OTHER_KEY,
        )

        assert response.status_code == 403

    async def test_claiming_a_workspace_in_the_payload_does_not_grant_it(self, client):
        """The payload is a claim, not an ownership lookup.

        `other-1` labels the subject with its *own* workspace while naming a
        cluster owned by another. If ownership came from the body this would
        succeed; it must be compared against storage instead.
        """
        cluster_id = await _seed_cluster(workspace_name=_WS_OWNED)

        response = await _submit(
            client,
            _observation(cluster_id, workspace=_WS_FOREIGN),
            credential=_OTHER_CRED,
            key=_OTHER_KEY,
        )

        assert response.status_code == 403

    async def test_a_cluster_with_no_resolvable_owner_is_refused(self, client):
        """Missing ownership fails closed rather than skipping the check."""
        org_id = uuid.uuid4()
        cluster_id = uuid.uuid4()
        async with async_session_test() as session:
            session.add(Organization(id=org_id, name="orphan-org"))
            await session.flush()
            session.add(
                Cluster(id=cluster_id, org_id=org_id, name="orphan", status="Active")
            )
            await session.commit()

        response = await _submit(client, _observation(cluster_id))

        assert response.status_code == 403

    async def test_a_shared_cluster_has_no_single_owner_and_is_refused(self, client):
        """A cluster shared by two workspaces cannot authorize a per-cluster write.

        Resolving ownership through `shared_cluster_id` would let any tenant on a
        shared cluster write fleet state for every other tenant on it.
        """
        org_id = uuid.uuid4()
        cluster_id = uuid.uuid4()
        async with async_session_test() as session:
            session.add(Organization(id=org_id, name="shared-org"))
            await session.flush()
            session.add(
                Cluster(id=cluster_id, org_id=org_id, name="shared", status="Active")
            )
            await session.flush()
            for name in (_WS_OWNED, _WS_FOREIGN):
                session.add(
                    Workspace(
                        id=uuid.uuid4(),
                        org_id=org_id,
                        name=name,
                        isolation_mode="namespace",
                        status="Active",
                        shared_cluster_id=cluster_id,
                    )
                )
            await session.commit()

        response = await _submit(client, _observation(cluster_id))

        assert response.status_code == 403

    async def test_an_unknown_cluster_is_refused(self, client):
        response = await _submit(client, _observation(uuid.uuid4()))

        assert response.status_code == 404

    async def test_a_submitter_cannot_read_another_workspaces_observation(self, client):
        """Cross-workspace read. A disclosure even though nothing is written."""
        cluster_id = await _seed_cluster()
        assert (await _submit(client, _observation(cluster_id))).status_code == 202

        response = await client.get(
            f"/internal/observations/{cluster_id}",
            headers={"authorization": _OTHER_CRED},
        )

        assert response.status_code == 404

    async def test_the_owner_can_read_its_own_observation(self, client):
        cluster_id = await _seed_cluster()
        assert (await _submit(client, _observation(cluster_id))).status_code == 202

        response = await client.get(
            f"/internal/observations/{cluster_id}",
            headers={"authorization": _MONITOR_CRED},
        )

        assert response.status_code == 200
        assert response.json()["workspace"] == _WS_OWNED

    async def test_an_unauthenticated_read_is_refused(self, client):
        cluster_id = await _seed_cluster()
        await _submit(client, _observation(cluster_id))

        response = await client.get(f"/internal/observations/{cluster_id}")

        assert response.status_code == 401

    async def test_a_forbidden_read_is_indistinguishable_from_a_missing_one(
        self, client
    ):
        """Refusals must not become an enumeration oracle."""
        cluster_id = await _seed_cluster()
        await _submit(client, _observation(cluster_id))

        forbidden = await client.get(
            f"/internal/observations/{cluster_id}",
            headers={"authorization": _OTHER_CRED},
        )
        missing = await client.get(
            f"/internal/observations/{uuid.uuid4()}",
            headers={"authorization": _OTHER_CRED},
        )

        assert forbidden.status_code == missing.status_code == 404
        assert forbidden.json() == missing.json()


# --- replay and idempotency: the receiver's own responsibility ---------------


class TestReplayAndIdempotency:
    async def test_an_identical_retry_is_idempotent_not_a_double_write(self, client):
        """A sender retrying after a lost response must not write twice."""
        cluster_id = await _seed_cluster()
        observation = _observation(cluster_id)

        first = await _submit(client, observation)
        second = await _submit(client, observation)

        assert first.status_code == 202
        assert first.json()["applied"] is True
        assert second.status_code == 202
        assert second.json()["applied"] is False

    async def test_an_older_observation_cannot_overwrite_a_newer_one(self, client):
        """Replay within the freshness window is still a replay.

        The window bounds how *old* a captured request can be, not how many times
        it may be replayed inside it, which is why this is the receiver's job.
        """
        cluster_id = await _seed_cluster()
        now = datetime.now(UTC)

        recent = await _submit(client, _observation(cluster_id, reported_at=now))
        older = await _submit(
            client,
            _observation(cluster_id, reported_at=now - timedelta(minutes=2)),
        )

        assert recent.status_code == 202
        assert older.status_code == 409

    async def test_a_different_body_at_the_same_instant_is_refused(self, client):
        """Same timestamp, contradictory content: not a retry, so not idempotent."""
        cluster_id = await _seed_cluster()
        now = datetime.now(UTC)

        first = await _submit(client, _observation(cluster_id, reported_at=now))
        contradiction = await _submit(
            client,
            _observation(
                cluster_id,
                reported_at=now,
                checks=(
                    CheckResult(
                        name="eks_reachability",
                        status=CheckStatus.UNREACHABLE,
                        observed_at=now,
                        error="probe failed",
                    ),
                ),
            ),
        )

        assert first.status_code == 202
        assert contradiction.status_code == 409

    async def test_a_stale_observation_is_outside_the_freshness_window(self, client):
        cluster_id = await _seed_cluster()

        response = await _submit(
            client,
            _observation(
                cluster_id, reported_at=datetime.now(UTC) - timedelta(hours=2)
            ),
        )

        assert response.status_code == 401
        assert "freshness" in response.json()["detail"]

    async def test_the_receipt_records_the_authenticated_submitter(self, client):
        """Attribution comes from the credential, not the body's `reporter` label."""
        cluster_id = await _seed_cluster()
        await _submit(client, _observation(cluster_id))

        async with async_session_test() as session:
            receipt = await session.get(ObservationReceipt, cluster_id)

        assert receipt is not None
        assert receipt.submitter_id == "monitor-1"
        assert receipt.workspace == _WS_OWNED


# --- acceptance 4: a probe never reports health it did not check --------------


class TestUncheckedProbesNeverReportHealth:
    async def test_a_not_checked_result_does_not_become_healthy(self, client):
        """The core of acceptance 4 at the receiver boundary."""
        cluster_id = await _seed_cluster()

        response = await _submit(
            client,
            _observation(
                cluster_id,
                checks=(
                    CheckResult.not_checked("eks_reachability", "no prober configured"),
                ),
            ),
        )

        assert response.status_code == 202
        assert response.json()["status"] == CheckStatus.NOT_CHECKED.value

        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)
            assert cluster.health_status != "Healthy"
            assert cluster.health_status == "Unknown"

    async def test_an_unchecked_dimension_does_not_retain_a_stale_reading(self, client):
        """Last cycle's "healthy" must not stand in for this cycle's "nobody looked"."""
        cluster_id = await _seed_cluster()
        now = datetime.now(UTC)

        await _submit(
            client,
            _observation(
                cluster_id,
                reported_at=now - timedelta(seconds=30),
                checks=(
                    CheckResult(
                        name="vault_sync",
                        status=CheckStatus.HEALTHY,
                        observed_at=now - timedelta(seconds=30),
                        detail="synced",
                    ),
                ),
            ),
        )
        await _submit(
            client,
            _observation(
                cluster_id,
                reported_at=now,
                checks=(CheckResult.not_checked("vault_sync", "CRD not installed"),),
            ),
        )

        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)

        state = cluster.actual_state_json
        assert "vault_sync" not in state
        assert state["observation"]["checks_not_performed"] == {
            "vault_sync": "CRD not installed"
        }

    async def test_a_fleet_health_body_with_no_checks_is_refused(self, client):
        """A probe that performed nothing may not occupy the "last known state" slot.

        Built as raw wire bytes rather than via `Observation`, because the contract
        refuses to *construct* such an observation at all. Sending it anyway is what
        a hand-rolled or downgraded sender would do, so the receiver must refuse it
        rather than rely on its own use of the constructor.
        """
        cluster_id = await _seed_cluster()
        body = json.dumps(
            {
                "contract_version": "v1",
                "kind": "fleet_health",
                "subject": {"cluster_id": str(cluster_id), "workspace": _WS_OWNED},
                "reported_at": datetime.now(UTC).isoformat(),
                "reporter": "test-monitor",
                "status": CheckStatus.NOT_CHECKED.value,
                "checks": [],
            }
        ).encode()

        response = await client.post(
            "/internal/observations",
            content=body,
            headers=_headers(body, _MONITOR_CRED, _MONITOR_KEY),
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "invalid observation body"

        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)
        assert cluster.health_status == "Healthy"  # the seeded value, untouched

    async def test_unreachable_outranks_unknown_in_the_persisted_status(self, client):
        """Corrected severity: a failed probe is worse than an unexplained one."""
        cluster_id = await _seed_cluster()
        now = datetime.now(UTC)

        response = await _submit(
            client,
            _observation(
                cluster_id,
                reported_at=now,
                checks=(
                    CheckResult.failed("vault_sync", now, "timed out"),
                    CheckResult(
                        name="eks_reachability",
                        status=CheckStatus.UNREACHABLE,
                        observed_at=now,
                        error="connection refused",
                    ),
                ),
            ),
        )

        assert response.json()["status"] == CheckStatus.UNREACHABLE.value
        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)
        assert cluster.health_status == "Unhealthy"

    async def test_the_persisted_state_separates_performed_from_unperformed(
        self, client
    ):
        """ "A probe reports only checks actually made" — visible in stored state."""
        cluster_id = await _seed_cluster()
        now = datetime.now(UTC)

        await _submit(
            client,
            _observation(
                cluster_id,
                reported_at=now,
                checks=(
                    CheckResult(
                        name="node_health",
                        status=CheckStatus.HEALTHY,
                        observed_at=now,
                        detail="ready",
                    ),
                    CheckResult.not_checked("cost_anomaly", "no cost data"),
                ),
            ),
        )

        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)

        observation_state = cluster.actual_state_json["observation"]
        assert observation_state["checks_performed"] == {"node_health": "healthy"}
        assert observation_state["checks_not_performed"] == {
            "cost_anomaly": "no cost data"
        }


# --- continuity: heartbeats and events still work through the contract -------


class TestHeartbeatContinuity:
    async def test_an_observation_records_the_monitor_cycle_not_a_heartbeat(
        self, client
    ):
        """The receiver records that the *monitor* ran, and does not forge a heartbeat.

        `last_reconciled_at` is what the replaced SQL wrote (`UpdateClusterHealth`
        set `health_status`, `last_reconciled_at`, `actual_state_json`), and it is
        the honest target: the monitor completing a cycle is the event that
        happened. `last_heartbeat` means something the monitor did not witness —
        the data-plane controller reporting in.
        """
        cluster_id = await _seed_cluster()
        reported_at = datetime.now(UTC)

        await _submit(client, _observation(cluster_id, reported_at=reported_at))

        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)

        assert cluster.last_reconciled_at is not None
        assert cluster.last_heartbeat is None

    async def test_an_observation_never_refreshes_the_controller_heartbeat(
        self, client
    ):
        """A receiver may not manufacture liveness evidence for a producer.

        The monitor reads `last_heartbeat` back (through the scoped cluster list)
        to decide whether the controller has gone silent, and
        `_effective_health_status` in routers/workspaces.py degrades a cluster
        whose heartbeat has aged out. If a submission stamped that column, the
        monitor's own poll would reset the clock it measures against: the
        "heartbeat missing" escalation could never fire and a cluster whose
        controller had died would read as freshly alive forever.

        Submitting an *explicitly unreachable* heartbeat observation is the sharp
        case — the very report that says "this controller is silent" must not be
        the thing that marks it alive.
        """
        cluster_id = await _seed_cluster()
        now = datetime.now(UTC)
        stale = now - timedelta(hours=6)
        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)
            cluster.last_heartbeat = stale
            await session.commit()

        await _submit(
            client,
            _observation(
                cluster_id,
                reported_at=now,
                checks=(
                    CheckResult(
                        name="heartbeat_freshness",
                        status=CheckStatus.UNREACHABLE,
                        observed_at=now,
                        error="Heartbeat missing for 6h0m0s",
                    ),
                ),
            ),
        )

        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)

        recorded = cluster.last_heartbeat
        if recorded is not None and recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=UTC)
        assert recorded == stale, (
            "the monitor's own submission moved last_heartbeat, so the staleness "
            "it exists to detect can never be observed again"
        )

    async def test_a_check_name_does_not_overwrite_a_controller_reported_value(
        self, client
    ):
        """Check results must not share a namespace with the producer's facts.

        `vault_sync_status` is both a controller-reported key in
        `actual_state_json` (merged by the legacy heartbeat route, parsed back by
        `db.HeartbeatPayload`) and a monitor dimension name. Writing the check's
        status at the top level replaces the value the check was judging: the
        actionable `"failed"` becomes the monitor's `"degraded"`, which is outside
        the producer's `ok|pending|failed` vocabulary and so reads back as
        Unknown, losing the "re-trigger credential sync" signal.
        """
        cluster_id = await _seed_cluster()
        now = datetime.now(UTC)
        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)
            cluster.actual_state_json = {
                "vault_sync_status": "failed",
                "skypilot_healthy": True,
                "cost_hourly": 4.25,
            }
            await session.commit()

        await _submit(
            client,
            _observation(
                cluster_id,
                reported_at=now,
                checks=(
                    CheckResult(
                        name="vault_sync_status",
                        status=CheckStatus.DEGRADED,
                        observed_at=now,
                        detail="Vault credential sync failed — re-trigger recommended",
                    ),
                ),
            ),
        )

        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)
        state = cluster.actual_state_json

        assert state["vault_sync_status"] == "failed", (
            "the monitor's verdict overwrote the controller-reported fact it was "
            "judging"
        )
        assert state["skypilot_healthy"] is True
        assert state["cost_hourly"] == 4.25
        # The check result is still recorded — inside the observation document,
        # where a dimension name cannot collide with a producer's key.
        assert state["observation"]["checks_performed"] == {
            "vault_sync_status": "degraded"
        }

    async def test_an_unchecked_dimension_does_not_delete_a_controller_value(
        self, client
    ):
        """`not_checked` records that nobody looked; it does not erase the input.

        The same collision in the other direction: a `not_checked` result for a
        dimension named after a controller key must not remove the controller's
        reported value from the state the next cycle parses.
        """
        cluster_id = await _seed_cluster()
        now = datetime.now(UTC)
        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)
            cluster.actual_state_json = {"vault_sync_status": "failed"}
            await session.commit()

        await _submit(
            client,
            _observation(
                cluster_id,
                reported_at=now,
                checks=(
                    CheckResult.not_checked("vault_sync_status", "CRD not installed"),
                ),
            ),
        )

        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)
        state = cluster.actual_state_json

        assert state["vault_sync_status"] == "failed"
        assert state["observation"]["checks_not_performed"] == {
            "vault_sync_status": "CRD not installed"
        }

    async def test_a_health_transition_writes_an_event_with_an_action(self, client):
        """`Event.action` is NOT NULL; this path must supply it.

        The legacy heartbeat route omits it. Asserted here so the contract path
        does not inherit that defect.
        """
        cluster_id = await _seed_cluster()
        now = datetime.now(UTC)

        await _submit(
            client,
            _observation(
                cluster_id,
                reported_at=now,
                checks=(
                    CheckResult(
                        name="eks_reachability",
                        status=CheckStatus.UNREACHABLE,
                        observed_at=now,
                        error="connection refused",
                    ),
                ),
            ),
        )

        async with async_session_test() as session:
            events = (
                (
                    await session.execute(
                        select(Event).where(Event.resource_id == cluster_id)
                    )
                )
                .scalars()
                .all()
            )

        assert len(events) == 1
        assert events[0].event_type == "health_status_changed"
        assert events[0].action == "updated"

    async def test_no_event_is_written_when_health_is_unchanged(self, client):
        """Stable health is not a transition."""
        cluster_id = await _seed_cluster()
        now = datetime.now(UTC)

        await _submit(client, _observation(cluster_id, reported_at=now))
        await _submit(
            client,
            _observation(cluster_id, reported_at=now + timedelta(seconds=5)),
        )

        async with async_session_test() as session:
            events = (
                (
                    await session.execute(
                        select(Event).where(Event.resource_id == cluster_id)
                    )
                )
                .scalars()
                .all()
            )

        assert events == []


# --- the legacy route and its retirement ------------------------------------


class TestLegacyHeartbeatRetirement:
    async def test_it_is_available_while_enabled_so_the_receiver_can_ship_first(
        self, client, internal_token_header
    ):
        """Deploy receiver before sender: the shared-token path keeps working."""
        cluster_id = await _seed_cluster()

        response = await client.post(
            "/internal/heartbeat",
            headers=internal_token_header,
            json={
                "cluster_id": str(cluster_id),
                "health_status": "Healthy",
                "actual_state_json": {},
            },
        )

        assert response.status_code == 200
        assert response.json()["accepted"] is True

    async def test_it_refuses_once_retired(
        self, client, monkeypatch, internal_token_header
    ):
        """After cutover the authenticated contract is the only write path."""
        monkeypatch.setattr(settings, "legacy_heartbeat_enabled", False)
        cluster_id = await _seed_cluster()

        response = await client.post(
            "/internal/heartbeat",
            headers=internal_token_header,
            json={
                "cluster_id": str(cluster_id),
                "health_status": "Healthy",
                "actual_state_json": {},
            },
        )

        assert response.status_code == 410
        assert "/internal/observations" in response.json()["detail"]

    async def test_the_contract_path_still_works_once_the_legacy_route_is_retired(
        self, client, monkeypatch
    ):
        """Continuity across the cutover, which is what acceptance 1 rests on."""
        monkeypatch.setattr(settings, "legacy_heartbeat_enabled", False)
        cluster_id = await _seed_cluster()

        response = await _submit(client, _observation(cluster_id))

        assert response.status_code == 202


# --- the scoped reads and writes that replace the monitor's remaining SQL ----


class TestScopedClusterList:
    """`GET /observations/clusters`, replacing `SELECT ... FROM clusters`.

    Acceptance 1 is about the *whole* grant, not just the observation write: the
    monitor also read the cluster list, read cost history from `events`, and wrote
    events. Each of those needs an authenticated equivalent or the grant cannot be
    withdrawn, so each is scoped and tested here as strictly as the write path.
    """

    async def _list(self, client, credential=_MONITOR_CRED, **params):
        return await client.get(
            "/internal/observations/clusters",
            params=params,
            headers={"authorization": credential},
        )

    async def test_an_unauthenticated_caller_gets_no_list(self, client):
        await _seed_cluster()

        response = await client.get("/internal/observations/clusters")

        assert response.status_code == 401

    async def test_it_returns_a_cluster_the_caller_owns(self, client):
        cluster_id = await _seed_cluster()

        response = await self._list(client)

        assert response.status_code == 200
        assert [c["cluster_id"] for c in response.json()] == [str(cluster_id)]

    async def test_it_omits_a_cluster_owned_by_another_workspace(self, client):
        """The listing is scoped, so a foreign cluster is not merely unwritable.

        If it appeared here, the monitor would try to submit for it every cycle and
        the boundary would rest on the write path alone.
        """
        await _seed_cluster(workspace_name=_WS_FOREIGN)

        response = await self._list(client)

        assert response.json() == []

    async def test_it_reports_the_resolved_owning_workspace(self, client):
        """So a sender can name its subject without guessing at ownership."""
        await _seed_cluster()

        response = await self._list(client)

        assert response.json()[0]["workspace"] == _WS_OWNED

    async def test_a_cluster_outside_the_requested_statuses_is_omitted(self, client):
        await _seed_cluster()

        response = await self._list(client, statuses="Terminated")

        assert response.json() == []

    async def test_an_empty_status_filter_is_refused_rather_than_ignored(self, client):
        """Silently listing every status would widen the read on a typo."""
        await _seed_cluster()

        response = await self._list(client, statuses=" , ")

        assert response.status_code == 400

    async def test_a_shared_cluster_is_absent_because_it_has_no_single_owner(
        self, client
    ):
        """`shared_cluster_id` is many-to-one, so it cannot authorize anything.

        Absent from the list for exactly the reason its submissions are refused —
        the two must agree, or the monitor would poll a cluster it can never
        write.
        """
        org_id = uuid.uuid4()
        cluster_id = uuid.uuid4()
        async with async_session_test() as session:
            session.add(Organization(id=org_id, name=f"org-{org_id.hex[:8]}"))
            await session.flush()
            session.add(
                Cluster(id=cluster_id, org_id=org_id, name="shared", status="Active")
            )
            await session.flush()
            session.add(
                Workspace(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    name=_WS_OWNED,
                    isolation_mode="namespace",
                    status="Active",
                    shared_cluster_id=cluster_id,
                )
            )
            await session.commit()

        response = await self._list(client)

        assert response.json() == []


class TestScopedCostHistory:
    """`GET /observations/{id}/cost-history`, replacing the monitor's events query.

    Spend history is tenant data, so this is scoped exactly as the observation
    read is — reading another workspace's costs is a disclosure even though it
    writes nothing.
    """

    async def _seed_cost_event(self, cluster_id, payload: str) -> None:
        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)
            session.add(
                Event(
                    org_id=cluster.org_id,
                    action="updated",
                    resource_type="cluster",
                    resource_id=cluster_id,
                    event_type="monitor_health_changed",
                    message="cycle",
                    details_json=payload,
                )
            )
            await session.commit()

    async def test_an_unauthenticated_caller_cannot_read_costs(self, client):
        cluster_id = await _seed_cluster()

        response = await client.get(f"/internal/observations/{cluster_id}/cost-history")

        assert response.status_code == 401

    async def test_it_returns_recorded_hourly_costs(self, client):
        cluster_id = await _seed_cluster()
        await self._seed_cost_event(cluster_id, json.dumps({"cost_hourly": 2.5}))

        response = await client.get(
            f"/internal/observations/{cluster_id}/cost-history",
            headers={"authorization": _MONITOR_CRED},
        )

        assert response.status_code == 200
        assert response.json()["costs"] == [2.5]

    async def test_another_workspaces_costs_are_not_readable(self, client):
        """Cross-workspace *read*, which acceptance 3 names explicitly."""
        cluster_id = await _seed_cluster(workspace_name=_WS_FOREIGN)
        await self._seed_cost_event(cluster_id, json.dumps({"cost_hourly": 99.0}))

        response = await client.get(
            f"/internal/observations/{cluster_id}/cost-history",
            headers={"authorization": _MONITOR_CRED},
        )

        assert response.status_code == 404

    async def test_an_absent_cluster_is_indistinguishable_from_a_foreign_one(
        self, client
    ):
        """Otherwise this route enumerates which cluster UUIDs exist."""
        foreign_id = await _seed_cluster(workspace_name=_WS_FOREIGN)

        absent = await client.get(
            f"/internal/observations/{uuid.uuid4()}/cost-history",
            headers={"authorization": _MONITOR_CRED},
        )
        foreign = await client.get(
            f"/internal/observations/{foreign_id}/cost-history",
            headers={"authorization": _MONITOR_CRED},
        )

        assert absent.status_code == foreign.status_code == 404
        assert absent.json()["detail"] == foreign.json()["detail"]

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("not json", id="malformed"),
            pytest.param(json.dumps({"other": 1}), id="no-cost-field"),
            pytest.param(json.dumps({"cost_hourly": "2.5"}), id="cost-as-string"),
            pytest.param(json.dumps({"cost_hourly": None}), id="cost-null"),
            pytest.param(json.dumps({"cost_hourly": True}), id="cost-bool"),
        ],
    )
    async def test_an_unusable_row_contributes_nothing_rather_than_a_zero(
        self, client, payload
    ):
        """A zero would drag the rolling average down and mask a real spike."""
        cluster_id = await _seed_cluster()
        await self._seed_cost_event(cluster_id, payload)

        response = await client.get(
            f"/internal/observations/{cluster_id}/cost-history",
            headers={"authorization": _MONITOR_CRED},
        )

        assert response.json()["costs"] == []

    async def test_a_non_positive_window_is_refused(self, client):
        """A zero or negative window is a bug in the caller, not an empty result."""
        cluster_id = await _seed_cluster()

        response = await client.get(
            f"/internal/observations/{cluster_id}/cost-history",
            params={"window_seconds": 0},
            headers={"authorization": _MONITOR_CRED},
        )

        assert response.status_code == 400


class TestScopedEventWrites:
    """`POST /observations/{id}/events`, replacing the monitor's `INSERT INTO events`."""

    async def test_an_unauthenticated_caller_cannot_write_an_event(self, client):
        cluster_id = await _seed_cluster()

        response = await client.post(
            f"/internal/observations/{cluster_id}/events",
            json={"event_type": "budget.cost_anomaly", "message": "spike"},
        )

        assert response.status_code == 401

    async def test_an_authorized_event_is_recorded(self, client):
        cluster_id = await _seed_cluster()

        response = await client.post(
            f"/internal/observations/{cluster_id}/events",
            json={"event_type": "budget.cost_anomaly", "message": "spike"},
            headers={"authorization": _MONITOR_CRED},
        )

        async with async_session_test() as session:
            events = (
                (
                    await session.execute(
                        select(Event).where(Event.resource_id == cluster_id)
                    )
                )
                .scalars()
                .all()
            )

        assert response.status_code == 201
        assert [e.event_type for e in events] == ["budget.cost_anomaly"]

    async def test_an_event_cannot_be_written_against_a_foreign_cluster(self, client):
        """Cross-workspace *write* on the event path, not just the observation path."""
        cluster_id = await _seed_cluster(workspace_name=_WS_FOREIGN)

        response = await client.post(
            f"/internal/observations/{cluster_id}/events",
            json={"event_type": "budget.cost_anomaly", "message": "spike"},
            headers={"authorization": _MONITOR_CRED},
        )

        async with async_session_test() as session:
            events = (
                (
                    await session.execute(
                        select(Event).where(Event.resource_id == cluster_id)
                    )
                )
                .scalars()
                .all()
            )

        assert response.status_code == 404
        assert events == []

    async def test_the_org_comes_from_the_cluster_and_not_from_the_request(
        self, client
    ):
        """The old call passed `org_id` as an argument, so a caller chose it.

        A caller that could name the org would be choosing whose audit trail it
        wrote to. `org_id` is therefore not on the request model — but `details` is
        a free-form dict, so that is the channel a caller would actually reach
        through, and it is the one asserted here. (Sending `org_id` at the top
        level is also covered, and is simply dropped by the model.)
        """
        cluster_id = await _seed_cluster()
        attacker_org = uuid.uuid4()

        await client.post(
            f"/internal/observations/{cluster_id}/events",
            json={
                "event_type": "budget.cost_anomaly",
                "message": "spike",
                "org_id": str(attacker_org),
                "details": {"org_id": str(attacker_org)},
            },
            headers={"authorization": _MONITOR_CRED},
        )

        async with async_session_test() as session:
            cluster = await session.get(Cluster, cluster_id)
            event = (
                (
                    await session.execute(
                        select(Event).where(Event.resource_id == cluster_id)
                    )
                )
                .scalars()
                .one()
            )

        assert event.org_id == cluster.org_id
        assert event.org_id != attacker_org

    async def test_the_authenticated_submitter_is_recorded_on_the_event(self, client):
        """So an event's origin is attributable after the grant withdrawal."""
        cluster_id = await _seed_cluster()

        await client.post(
            f"/internal/observations/{cluster_id}/events",
            json={"event_type": "budget.cost_anomaly", "message": "spike"},
            headers={"authorization": _MONITOR_CRED},
        )

        async with async_session_test() as session:
            event = (
                (
                    await session.execute(
                        select(Event).where(Event.resource_id == cluster_id)
                    )
                )
                .scalars()
                .one()
            )

        assert json.loads(event.details_json)["submitter_id"] == "monitor-1"

    async def test_a_recorded_cost_event_is_readable_as_cost_history(self, client):
        """The write and read halves must agree, or the anomaly check sees nothing.

        Written as one flow rather than two unit tests because the monitor's cost
        anomaly detection depends on the round trip: it writes `cost_hourly` into
        an event and reads it back on a later cycle.
        """
        cluster_id = await _seed_cluster()

        await client.post(
            f"/internal/observations/{cluster_id}/events",
            json={
                "event_type": "monitor_health_changed",
                "message": "cycle",
                "details": {"cost_hourly": 3.25},
            },
            headers={"authorization": _MONITOR_CRED},
        )
        history = await client.get(
            f"/internal/observations/{cluster_id}/cost-history",
            headers={"authorization": _MONITOR_CRED},
        )

        assert history.json()["costs"] == [3.25]


# --- leases: the replacement for the reconcile_locks grant -------------------


class TestLeases:
    @pytest.fixture(autouse=True)
    async def _lease_cluster(self):
        self.cluster_id = str(await _seed_cluster())

    async def _acquire(self, client, credential=_MONITOR_CRED, **overrides):
        payload = {
            "resource_type": "cluster_health",
            "resource_id": self.cluster_id,
            "instance_id": "pod-a",
        }
        payload.update(overrides)
        return await client.post(
            "/internal/observations/leases",
            json=payload,
            headers={"authorization": credential},
        )

    async def test_an_unauthenticated_caller_cannot_take_a_lease(self, client):
        response = await client.post(
            "/internal/observations/leases",
            json={"resource_type": "cluster_health", "resource_id": self.cluster_id},
        )

        assert response.status_code == 401

    async def test_a_lease_is_granted_with_a_fence_token(self, client):
        response = await self._acquire(client)

        assert response.status_code == 200
        assert response.json()["fence_token"] == 1

    async def test_the_fence_token_advances_on_every_grant(self, client):
        """Monotonic, so a stalled holder's token can be recognised as stale."""
        first = await self._acquire(client)
        second = await self._acquire(client)

        assert second.json()["fence_token"] > first.json()["fence_token"]

    async def test_a_held_lease_is_refused_to_another_holder(self, client):
        await self._acquire(client, instance_id="pod-a")

        response = await self._acquire(client, instance_id="pod-b")

        assert response.status_code == 409

    async def test_the_same_holder_may_renew(self, client):
        """A monitor extending its own lease is not contention."""
        await self._acquire(client, instance_id="pod-a")

        response = await self._acquire(client, instance_id="pod-a")

        assert response.status_code == 200

    async def test_a_duration_above_the_ceiling_is_refused(self, client):
        """An unbounded lease would reinstate the table lock this replaces."""
        response = await self._acquire(client, duration_seconds=60 * 60 * 24)

        assert response.status_code == 400

    async def test_a_lease_cannot_be_released_by_another_caller(self, client):
        """Freeing another's lease recreates the concurrency the lease prevents."""
        granted = await self._acquire(client, instance_id="pod-a")
        token = granted.json()["fence_token"]

        response = await client.post(
            "/internal/observations/leases/release",
            json={
                "resource_type": "cluster_health",
                "resource_id": self.cluster_id,
                "instance_id": "pod-b",
                "fence_token": token,
            },
            headers={"authorization": _MONITOR_CRED},
        )

        assert response.status_code == 409

    async def test_a_release_with_a_superseded_token_is_refused(self, client):
        """A late release from an old grant must not free the current holder's lease."""
        await self._acquire(client, instance_id="pod-a")
        current = await self._acquire(client, instance_id="pod-a")

        response = await client.post(
            "/internal/observations/leases/release",
            json={
                "resource_type": "cluster_health",
                "resource_id": self.cluster_id,
                "instance_id": "pod-a",
                "fence_token": current.json()["fence_token"] - 1,
            },
            headers={"authorization": _MONITOR_CRED},
        )

        assert response.status_code == 409

    async def test_the_holder_can_release_and_the_scope_becomes_free(self, client):
        granted = await self._acquire(client, instance_id="pod-a")

        released = await client.post(
            "/internal/observations/leases/release",
            json={
                "resource_type": "cluster_health",
                "resource_id": self.cluster_id,
                "instance_id": "pod-a",
                "fence_token": granted.json()["fence_token"],
            },
            headers={"authorization": _MONITOR_CRED},
        )
        reacquired = await self._acquire(client, instance_id="pod-b")

        assert released.status_code == 200
        assert reacquired.status_code == 200

    async def test_release_retains_the_token_so_it_never_restarts(self, client):
        """A DELETE-on-release would reset the sequence and break fencing."""
        granted = await self._acquire(client, instance_id="pod-a")
        await client.post(
            "/internal/observations/leases/release",
            json={
                "resource_type": "cluster_health",
                "resource_id": self.cluster_id,
                "instance_id": "pod-a",
                "fence_token": granted.json()["fence_token"],
            },
            headers={"authorization": _MONITOR_CRED},
        )

        reacquired = await self._acquire(client, instance_id="pod-b")

        assert reacquired.json()["fence_token"] > granted.json()["fence_token"]
        async with async_session_test() as session:
            row = await session.get(
                ObservationLease, f"cluster_health/{self.cluster_id}"
            )
        assert row is not None

    async def test_a_lease_scope_need_not_be_a_cluster(self, client, monkeypatch):
        """The budget monitor holds ("budget_monitor", "global"), which has no row."""
        entries = json.loads(settings.observation_submitters)
        entries[0]["lease_scopes"] = ["budget_monitor/global"]
        monkeypatch.setattr(settings, "observation_submitters", json.dumps(entries))
        response = await self._acquire(
            client, resource_type="budget_monitor", resource_id="global"
        )

        assert response.status_code == 200


async def test_only_configured_controller_identity_advances_heartbeat(client, monkeypatch):
    from dataclasses import replace

    controller_credential = "test-controller-credential"
    controller_key = b"test-controller-signing-key"
    entries = json.loads(settings.observation_submitters)
    entries.append({"submitter_id": "controller-1", "credential": controller_credential, "signing_key": controller_key.decode(), "workspaces": [_WS_OWNED]})
    monkeypatch.setattr(settings, "observation_submitters", json.dumps(entries))
    monkeypatch.setattr(settings, "controller_observation_submitter_id", "controller-1")
    cluster_id = await _seed_cluster()
    # An authenticated monitor claiming to be the controller is still a monitor.
    spoof = replace(_observation(cluster_id), reporter="controller-1")
    assert (await _submit(client, spoof)).status_code == 202
    async with async_session_test() as session:
        cluster = await session.get(Cluster, cluster_id)
        assert cluster.last_heartbeat is None
        monitor_at = cluster.last_reconciled_at
    genuine = replace(_observation(cluster_id), reporter="arbitrary-payload-label")
    assert (await _submit(client, genuine, controller_credential, controller_key)).status_code == 202
    async with async_session_test() as session:
        cluster = await session.get(Cluster, cluster_id)
        assert cluster.last_heartbeat.replace(tzinfo=UTC) == genuine.reported_at
        assert cluster.last_reconciled_at == monitor_at
