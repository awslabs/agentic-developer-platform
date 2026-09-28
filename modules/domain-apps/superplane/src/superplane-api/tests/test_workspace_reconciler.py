"""Tests for WorkspaceReconciler — failed bootstrap retry + drift detection (US-H3).

Uses a mock session factory and a patched provisioning call to test reconciliation
logic without a real database or cluster connection.

Issue #5058 (U17b) changed what is patched here: the retry path opens an authorized
operation through the provisioning facade instead of dispatching a GitHub Actions
workflow with a foreign-repository personal access token.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.provisioning import (
    STATE_FAILED,
    STATE_PENDING,
    STATE_UNKNOWN,
    OperationProgress,
    ProvisioningUnavailable,
)
from app.services.workspace_reconciler import (
    DRIFT_DETECTION_KEYS,
    MAX_BACKOFF_SECONDS,
    MAX_BOOTSTRAP_RETRIES,
    RETRY_BACKOFF_BASE_SECONDS,
    WorkspaceReconciler,
    compute_backoff,
    detect_drift,
    _parse_json_state,
)
from app.models.workspace import (
    STATUS_ACTIVE,
    STATUS_DRIFT_DETECTED,
    STATUS_FAILED,
    STATUS_MAX_RETRIES_EXCEEDED,
    STATUS_RECONCILING,
)


# --- Helpers ---


def _progress(state: str = STATE_PENDING) -> OperationProgress:
    """A facade progress report for a successfully opened operation.

    A mock report: no real operation facade exists in ADP, so a green run here
    establishes the reconciler's retry accounting, not live provisioning.
    """
    return OperationProgress(operation_id="op-test", state=state)


def _update_values(session) -> dict:
    """Column name -> bound value for the last UPDATE the reconciler issued.

    The session is a mock, so the statement never reaches a database; reading the
    bound values is how a test asserts *which* columns a branch writes. Needed to
    show that a branch does NOT write `status`, which a call-count assertion cannot
    distinguish from writing it.
    """
    for call in reversed(session.execute.await_args_list):
        statement = call.args[0]
        values = getattr(statement, "_values", None)
        if values:
            return {
                getattr(column, "name", str(column)): getattr(bound, "value", bound)
                for column, bound in values.items()
            }
    raise AssertionError("no UPDATE statement was executed")


def _make_workspace(
    org_id: uuid.UUID | None = None,
    name: str = "test-workspace",
    status: str = "Failed",
    isolation_mode: str = "dedicated",
    cluster_id: uuid.UUID | None = None,
    bootstrap_retry_count: int = 0,
    last_bootstrap_at: datetime | None = None,
    last_drift_check_at: datetime | None = None,
    reconcile_error: str | None = None,
) -> MagicMock:
    """Create a mock Workspace row."""
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.org_id = org_id or uuid.uuid4()
    ws.name = name
    ws.status = status
    ws.isolation_mode = isolation_mode
    ws.cluster_id = cluster_id
    ws.bootstrap_retry_count = bootstrap_retry_count
    ws.last_bootstrap_at = last_bootstrap_at
    ws.last_drift_check_at = last_drift_check_at
    ws.reconcile_error = reconcile_error
    return ws


def _make_cluster(
    org_id: uuid.UUID | None = None,
    name: str = "data-plane-1",
    endpoint: str = "https://eks.us-west-2.amazonaws.com/cluster-1",
    status: str = "Active",
    desired_state_json: dict | None = None,
    actual_state_json: dict | None = None,
) -> MagicMock:
    """Create a mock Cluster row."""
    cluster = MagicMock()
    cluster.id = uuid.uuid4()
    cluster.org_id = org_id or uuid.uuid4()
    cluster.name = name
    cluster.endpoint = endpoint
    cluster.status = status
    cluster.desired_state_json = desired_state_json
    cluster.actual_state_json = actual_state_json
    return cluster


def _mock_session_factory():
    """Create a mock async session factory that yields a mock session."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    session.add = MagicMock()

    # The factory must be a regular callable (not AsyncMock) so that factory()
    # returns an object with __aenter__/__aexit__ (async context manager),
    # rather than a coroutine.
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=ctx)

    return factory, session


# ============================================================
# Tests: compute_backoff
# ============================================================


class TestComputeBackoff:
    """Tests for the exponential backoff computation."""

    def test_retry_0_returns_base(self):
        assert compute_backoff(0) == RETRY_BACKOFF_BASE_SECONDS

    def test_retry_1_doubles_base(self):
        assert compute_backoff(1) == RETRY_BACKOFF_BASE_SECONDS * 2

    def test_retry_2_quadruples_base(self):
        assert compute_backoff(2) == RETRY_BACKOFF_BASE_SECONDS * 4

    def test_retry_3(self):
        assert compute_backoff(3) == RETRY_BACKOFF_BASE_SECONDS * 8

    def test_retry_4(self):
        assert compute_backoff(4) == min(
            RETRY_BACKOFF_BASE_SECONDS * 16, MAX_BACKOFF_SECONDS
        )

    def test_high_retry_capped_at_max(self):
        """Very high retry counts should be capped at MAX_BACKOFF_SECONDS."""
        assert compute_backoff(100) == MAX_BACKOFF_SECONDS

    def test_custom_base(self):
        assert compute_backoff(0, base=30) == 30
        assert compute_backoff(1, base=30) == 60
        assert compute_backoff(2, base=30) == 120

    def test_backoff_sequence(self):
        """Verify the full backoff sequence for default base=60."""
        expected = [60, 120, 240, 480, 960]
        for i, expected_val in enumerate(expected):
            assert compute_backoff(i) == expected_val


# ============================================================
# Tests: detect_drift
# ============================================================


class TestDetectDrift:
    """Tests for drift detection between desired and actual state."""

    def test_no_desired_state_no_drift(self):
        """No desired state means nothing to drift from."""
        assert detect_drift(None, {"instance_type": "m5.xlarge"}) == {}
        assert detect_drift({}, {"instance_type": "m5.xlarge"}) == {}

    def test_no_actual_state_reports_all_desired_keys(self):
        """If actual is None but desired has keys, all tracked keys are drift."""
        desired = {"instance_type": "m5.xlarge", "node_count": 3}
        result = detect_drift(desired, None)
        assert "instance_type" in result
        assert "node_count" in result
        assert result["instance_type"]["desired"] == "m5.xlarge"
        assert result["instance_type"]["actual"] is None

    def test_matching_states_no_drift(self):
        desired = {"instance_type": "m5.xlarge", "node_count": 3}
        actual = {"instance_type": "m5.xlarge", "node_count": 3}
        assert detect_drift(desired, actual) == {}

    def test_single_key_drift(self):
        desired = {"instance_type": "m5.xlarge", "node_count": 3}
        actual = {"instance_type": "m5.xlarge", "node_count": 5}
        result = detect_drift(desired, actual)
        assert len(result) == 1
        assert "node_count" in result
        assert result["node_count"]["desired"] == 3
        assert result["node_count"]["actual"] == 5

    def test_multiple_key_drift(self):
        desired = {"instance_type": "m5.xlarge", "gpu_count": 4, "node_count": 2}
        actual = {"instance_type": "g5.xlarge", "gpu_count": 2, "node_count": 2}
        result = detect_drift(desired, actual)
        assert len(result) == 2
        assert "instance_type" in result
        assert "gpu_count" in result

    def test_ignores_non_tracked_keys(self):
        """Keys not in DRIFT_DETECTION_KEYS should be ignored."""
        desired = {"instance_type": "m5.xlarge", "custom_tag": "hello"}
        actual = {"instance_type": "m5.xlarge", "custom_tag": "world"}
        assert detect_drift(desired, actual) == {}

    def test_missing_actual_key_is_drift(self):
        """If desired has a tracked key but actual doesn't, it's drift."""
        desired = {"instance_type": "m5.xlarge"}
        actual = {}
        result = detect_drift(desired, actual)
        assert "instance_type" in result
        assert result["instance_type"]["actual"] is None

    def test_extra_actual_keys_ignored(self):
        """Extra keys in actual (not in desired) are not reported as drift."""
        desired = {"instance_type": "m5.xlarge"}
        actual = {"instance_type": "m5.xlarge", "node_count": 5}
        assert detect_drift(desired, actual) == {}

    def test_empty_both_no_drift(self):
        assert detect_drift({}, {}) == {}

    def test_all_drift_detection_keys(self):
        """Verify all DRIFT_DETECTION_KEYS are compared."""
        desired = {k: f"desired_{k}" for k in DRIFT_DETECTION_KEYS}
        actual = {k: f"actual_{k}" for k in DRIFT_DETECTION_KEYS}
        result = detect_drift(desired, actual)
        assert len(result) == len(DRIFT_DETECTION_KEYS)


# ============================================================
# Tests: _parse_json_state
# ============================================================


class TestParseJsonState:
    """Tests for safe JSON state parsing."""

    def test_none_returns_empty_dict(self):
        assert _parse_json_state(None) == {}

    def test_dict_returns_same(self):
        d = {"key": "value"}
        assert _parse_json_state(d) == d

    def test_json_string_parsed(self):
        assert _parse_json_state('{"key": "value"}') == {"key": "value"}

    def test_invalid_json_returns_empty(self):
        assert _parse_json_state("not json") == {}

    def test_empty_string_returns_empty(self):
        assert _parse_json_state("") == {}


# ============================================================
# Tests: WorkspaceReconciler lifecycle
# ============================================================


class TestReconcilerLifecycle:
    """Tests for start/stop behavior."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        factory, _ = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)
        assert reconciler._running is False
        assert reconciler._task is None

        # We can't easily test the full loop, but we can verify start sets state
        with patch.object(reconciler, "_run_loop", new_callable=AsyncMock):
            await reconciler.start()
            assert reconciler._running is True
            assert reconciler._task is not None
            await reconciler.stop()
            assert reconciler._running is False

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        factory, _ = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        with patch.object(reconciler, "_run_loop", new_callable=AsyncMock):
            await reconciler.start()
            task1 = reconciler._task
            await reconciler.start()  # second start should warn and skip
            assert reconciler._task is task1
            await reconciler.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        factory, _ = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)
        # Should not raise
        await reconciler.stop()
        assert reconciler._running is False


# ============================================================
# Tests: Failed bootstrap retry
# ============================================================


class TestFailedBootstrapRetry:
    """Tests for the failed workspace bootstrap retry logic."""

    @pytest.mark.asyncio
    async def test_retry_triggers_bootstrap(self):
        """A failed workspace with retries remaining should trigger bootstrap."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        ws = _make_workspace(status=STATUS_FAILED, bootstrap_retry_count=0)

        # Mock lock acquisition
        with (
            patch.object(reconciler, "_acquire_lock", return_value=True),
            patch.object(reconciler, "_release_lock", new_callable=AsyncMock),
            patch.object(reconciler, "_emit_event", new_callable=AsyncMock),
            patch(
                "app.services.provisioning.start_provision", new_callable=AsyncMock
            ) as mock_trigger,
        ):
            mock_trigger.return_value = _progress()

            result = await reconciler._retry_bootstrap(session, ws)

            assert result == "retried"
            mock_trigger.assert_called_once_with(
                operation_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"adp:superplane:{ws.org_id}:workspace:{ws.id}:reconcile",
                    )
                ),
                workspace_id=str(ws.id),
                workspace_name=ws.name,
                org_id=str(ws.org_id),
                isolation_mode=ws.isolation_mode,
                account="",
            )

    @pytest.mark.asyncio
    async def test_retry_respects_backoff(self):
        """A workspace retried too recently should be skipped due to backoff."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        now = datetime.now(timezone.utc)
        # Last bootstrap was 30s ago, but backoff for retry_count=0 is 60s
        ws = _make_workspace(
            status=STATUS_FAILED,
            bootstrap_retry_count=0,
            last_bootstrap_at=now - timedelta(seconds=30),
        )

        result = await reconciler._retry_bootstrap(session, ws)
        assert result == "skipped"

    @pytest.mark.asyncio
    async def test_retry_proceeds_after_backoff(self):
        """A workspace should be retried once backoff has elapsed."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        now = datetime.now(timezone.utc)
        # Last bootstrap was 120s ago, backoff for retry_count=0 is 60s — eligible
        ws = _make_workspace(
            status=STATUS_FAILED,
            bootstrap_retry_count=0,
            last_bootstrap_at=now - timedelta(seconds=120),
        )

        with (
            patch.object(reconciler, "_acquire_lock", return_value=True),
            patch.object(reconciler, "_release_lock", new_callable=AsyncMock),
            patch.object(reconciler, "_emit_event", new_callable=AsyncMock),
            patch(
                "app.services.provisioning.start_provision", new_callable=AsyncMock
            ) as mock_trigger,
        ):
            mock_trigger.return_value = _progress()

            result = await reconciler._retry_bootstrap(session, ws)
            assert result == "retried"

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self):
        """A workspace at max retries should be marked as max_retries_exceeded."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        ws = _make_workspace(
            status=STATUS_FAILED,
            bootstrap_retry_count=MAX_BOOTSTRAP_RETRIES,
        )

        with patch.object(
            reconciler, "_mark_max_retries_exceeded", new_callable=AsyncMock
        ) as mock_mark:
            result = await reconciler._retry_bootstrap(session, ws)
            assert result == "max_retries_exceeded"
            mock_mark.assert_called_once_with(session, ws)

    @pytest.mark.asyncio
    async def test_lock_contention_skips(self):
        """If the lock can't be acquired, the workspace should be skipped."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        ws = _make_workspace(status=STATUS_FAILED, bootstrap_retry_count=0)

        with patch.object(reconciler, "_acquire_lock", return_value=False):
            result = await reconciler._retry_bootstrap(session, ws)
            assert result == "skipped"

    @pytest.mark.asyncio
    async def test_trigger_failure_records_error(self):
        """If the operation cannot be opened, the error should be recorded.

        Issue #5058 (U17b): the retry path opens an authorized operation instead of
        dispatching a GitHub Actions workflow. An unavailable facade raises rather
        than returning False, and the reconciler must record it as a consumed retry
        with a reason rather than letting it abort the sweep.
        """
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        ws = _make_workspace(status=STATUS_FAILED, bootstrap_retry_count=1)

        with (
            patch.object(reconciler, "_acquire_lock", return_value=True),
            patch.object(reconciler, "_release_lock", new_callable=AsyncMock),
            patch(
                "app.services.provisioning.start_provision", new_callable=AsyncMock
            ) as mock_trigger,
        ):
            mock_trigger.side_effect = ProvisioningUnavailable(
                "no authorized-operation facade is configured"
            )

            result = await reconciler._retry_bootstrap(session, ws)
            assert result == "skipped"
            # Session.execute should have been called to update reconcile_error
            session.execute.assert_called()

    @pytest.mark.asyncio
    async def test_a_conclusively_failed_report_is_not_a_started_retry(self):
        """An operation that OPENED but reports FAILED must not count as retried.

        The facade's first progress report can already be terminal. Reading "the
        call returned" as "the retry started" would move the workspace to
        `reconciling` and clear `reconcile_error` — and since the sweep only selects
        rows whose status is `Failed` and nothing transitions out of `reconciling`,
        that strands the workspace: never retried again, and no recorded reason.
        """
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        ws = _make_workspace(status=STATUS_FAILED, bootstrap_retry_count=1)

        with (
            patch.object(reconciler, "_acquire_lock", return_value=True),
            patch.object(reconciler, "_release_lock", new_callable=AsyncMock),
            patch.object(reconciler, "_emit_event", new_callable=AsyncMock) as emit,
            patch(
                "app.services.provisioning.start_provision", new_callable=AsyncMock
            ) as mock_trigger,
        ):
            mock_trigger.return_value = _progress(STATE_FAILED)

            result = await reconciler._retry_bootstrap(session, ws)

            assert result == "skipped"
            # Not announced as a retry in progress.
            emit.assert_not_called()
            # The reason is recorded, and the status is NOT set to reconciling.
            values = _update_values(session)
            assert values.get("reconcile_error")
            assert "status" not in values

    @pytest.mark.asyncio
    async def test_an_unknown_report_is_not_read_as_a_failed_retry(self):
        """`unknown` is neither success nor failure, so it stays a started attempt.

        Collapsing it into failure would retry a bootstrap that may have actually
        succeeded. This is the same distinction the router draws.
        """
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        ws = _make_workspace(status=STATUS_FAILED, bootstrap_retry_count=1)

        with (
            patch.object(reconciler, "_acquire_lock", return_value=True),
            patch.object(reconciler, "_release_lock", new_callable=AsyncMock),
            patch.object(reconciler, "_emit_event", new_callable=AsyncMock),
            patch(
                "app.services.provisioning.start_provision", new_callable=AsyncMock
            ) as mock_trigger,
        ):
            mock_trigger.return_value = OperationProgress(
                operation_id="op-test",
                state=STATE_UNKNOWN,
                detail="facade cannot determine the outcome",
            )

            result = await reconciler._retry_bootstrap(session, ws)

            assert result == "retried"

    @pytest.mark.asyncio
    async def test_first_retry_no_last_bootstrap(self):
        """A workspace that has never been bootstrapped (no last_bootstrap_at) should retry immediately."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        ws = _make_workspace(
            status=STATUS_FAILED,
            bootstrap_retry_count=0,
            last_bootstrap_at=None,
        )

        with (
            patch.object(reconciler, "_acquire_lock", return_value=True),
            patch.object(reconciler, "_release_lock", new_callable=AsyncMock),
            patch.object(reconciler, "_emit_event", new_callable=AsyncMock),
            patch(
                "app.services.provisioning.start_provision", new_callable=AsyncMock
            ) as mock_trigger,
        ):
            mock_trigger.return_value = _progress()
            result = await reconciler._retry_bootstrap(session, ws)
            assert result == "retried"

    @pytest.mark.asyncio
    async def test_exponential_backoff_retry_2(self):
        """Retry count 2 should require 240s (4 * base) backoff."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        now = datetime.now(timezone.utc)
        # 200s ago — not enough for 240s backoff at retry 2
        ws = _make_workspace(
            status=STATUS_FAILED,
            bootstrap_retry_count=2,
            last_bootstrap_at=now - timedelta(seconds=200),
        )

        result = await reconciler._retry_bootstrap(session, ws)
        assert result == "skipped"

    @pytest.mark.asyncio
    async def test_exponential_backoff_retry_2_elapsed(self):
        """Retry count 2 with >240s elapsed should proceed."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        now = datetime.now(timezone.utc)
        ws = _make_workspace(
            status=STATUS_FAILED,
            bootstrap_retry_count=2,
            last_bootstrap_at=now - timedelta(seconds=300),
        )

        with (
            patch.object(reconciler, "_acquire_lock", return_value=True),
            patch.object(reconciler, "_release_lock", new_callable=AsyncMock),
            patch.object(reconciler, "_emit_event", new_callable=AsyncMock),
            patch(
                "app.services.provisioning.start_provision", new_callable=AsyncMock
            ) as mock_trigger,
        ):
            mock_trigger.return_value = _progress()
            result = await reconciler._retry_bootstrap(session, ws)
            assert result == "retried"


# ============================================================
# Tests: Drift detection
# ============================================================


class TestDriftDetection:
    """Tests for the drift detection check on active workspaces."""

    @pytest.mark.asyncio
    async def test_drift_detected(self):
        """Workspace with drifted cluster should be marked as drift_detected."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        cluster = _make_cluster(
            desired_state_json={"instance_type": "m5.xlarge", "node_count": 3},
            actual_state_json={"instance_type": "m5.xlarge", "node_count": 5},
        )
        ws = _make_workspace(status=STATUS_ACTIVE, cluster_id=cluster.id)

        with patch.object(reconciler, "_emit_event", new_callable=AsyncMock):
            result = await reconciler._check_drift(session, ws, cluster)
            assert result is True

    @pytest.mark.asyncio
    async def test_no_drift(self):
        """Workspace with matching cluster state should not report drift."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        cluster = _make_cluster(
            desired_state_json={"instance_type": "m5.xlarge", "node_count": 3},
            actual_state_json={"instance_type": "m5.xlarge", "node_count": 3},
        )
        ws = _make_workspace(status=STATUS_ACTIVE, cluster_id=cluster.id)

        result = await reconciler._check_drift(session, ws, cluster)
        assert result is False

    @pytest.mark.asyncio
    async def test_drift_with_null_actual(self):
        """Cluster with desired but no actual state should detect drift."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        cluster = _make_cluster(
            desired_state_json={"instance_type": "m5.xlarge"},
            actual_state_json=None,
        )
        ws = _make_workspace(status=STATUS_ACTIVE, cluster_id=cluster.id)

        with patch.object(reconciler, "_emit_event", new_callable=AsyncMock):
            result = await reconciler._check_drift(session, ws, cluster)
            assert result is True

    @pytest.mark.asyncio
    async def test_drift_emits_event(self):
        """Drift detection should emit an event."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        cluster = _make_cluster(
            desired_state_json={"gpu_count": 4},
            actual_state_json={"gpu_count": 2},
        )
        ws = _make_workspace(status=STATUS_ACTIVE, cluster_id=cluster.id)

        with patch.object(
            reconciler, "_emit_event", new_callable=AsyncMock
        ) as mock_event:
            await reconciler._check_drift(session, ws, cluster)
            mock_event.assert_called_once()
            call_kwargs = mock_event.call_args
            assert call_kwargs.kwargs["event_type"] == "WorkspaceDriftDetected"

    @pytest.mark.asyncio
    async def test_no_drift_no_event(self):
        """No drift should not emit any event."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        cluster = _make_cluster(
            desired_state_json={"instance_type": "m5.xlarge"},
            actual_state_json={"instance_type": "m5.xlarge"},
        )
        ws = _make_workspace(status=STATUS_ACTIVE, cluster_id=cluster.id)

        with patch.object(
            reconciler, "_emit_event", new_callable=AsyncMock
        ) as mock_event:
            await reconciler._check_drift(session, ws, cluster)
            mock_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_drift_with_json_string_state(self):
        """State stored as JSON string should be parsed correctly."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        cluster = _make_cluster(
            desired_state_json='{"instance_type": "m5.xlarge"}',
            actual_state_json='{"instance_type": "g5.xlarge"}',
        )
        ws = _make_workspace(status=STATUS_ACTIVE, cluster_id=cluster.id)

        with patch.object(reconciler, "_emit_event", new_callable=AsyncMock):
            result = await reconciler._check_drift(session, ws, cluster)
            assert result is True

    @pytest.mark.asyncio
    async def test_empty_desired_no_drift(self):
        """Empty desired state means no drift regardless of actual."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        cluster = _make_cluster(
            desired_state_json={},
            actual_state_json={"instance_type": "m5.xlarge"},
        )
        ws = _make_workspace(status=STATUS_ACTIVE, cluster_id=cluster.id)

        result = await reconciler._check_drift(session, ws, cluster)
        assert result is False


# ============================================================
# Tests: Full reconciliation pass
# ============================================================


class TestReconcilePass:
    """Tests for the full reconcile() method."""

    @pytest.mark.asyncio
    async def test_empty_reconcile(self):
        """Reconcile with no workspaces should return zero stats."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        # Mock both queries to return empty results
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_result.all.return_value = []
        session.execute.return_value = mock_result

        stats = await reconciler.reconcile()
        assert stats["retried"] == 0
        assert stats["drift_detected"] == 0
        assert stats["failed"] == 0
        assert stats["skipped"] == 0
        assert stats["max_retries_exceeded"] == 0

    @pytest.mark.asyncio
    async def test_reconcile_retries_failed_workspace(self):
        """Full reconcile should retry failed workspaces."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        ws = _make_workspace(status=STATUS_FAILED, bootstrap_retry_count=0)

        # First call: find_failed_workspaces returns our workspace
        # Second call: find_active_workspaces returns empty
        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:  # Failed workspaces query
                result.scalars.return_value.all.return_value = [ws]
            else:  # Active workspaces or other queries
                result.scalars.return_value.all.return_value = []
                result.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=mock_execute)

        with patch.object(
            reconciler, "_retry_bootstrap", new_callable=AsyncMock
        ) as mock_retry:
            mock_retry.return_value = "retried"
            stats = await reconciler.reconcile()
            assert stats["retried"] == 1
            mock_retry.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconcile_handles_retry_exception(self):
        """If _retry_bootstrap raises, it should be counted as failed."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        ws = _make_workspace(status=STATUS_FAILED, bootstrap_retry_count=0)

        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalars.return_value.all.return_value = [ws]
            else:
                result.scalars.return_value.all.return_value = []
                result.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=mock_execute)

        with (
            patch.object(
                reconciler,
                "_retry_bootstrap",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
            patch.object(reconciler, "_update_reconcile_error", new_callable=AsyncMock),
        ):
            stats = await reconciler.reconcile()
            assert stats["failed"] == 1


# ============================================================
# Tests: Manual reconciliation trigger
# ============================================================


class TestManualReconciliation:
    """Tests for the reconcile_workspace() manual trigger."""

    @pytest.mark.asyncio
    async def test_workspace_not_found(self):
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        result = await reconciler.reconcile_workspace(uuid.uuid4())
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_manual_retry_resets_count(self):
        """Manual reconciliation of a Failed workspace should reset retry count."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        ws = _make_workspace(
            status=STATUS_FAILED,
            bootstrap_retry_count=3,
        )

        # First call: find workspace, second: after reset, third+: for retry
        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count <= 3:
                result.scalar_one_or_none.return_value = ws
            else:
                result.scalar_one_or_none.return_value = None
            return result

        session.execute = AsyncMock(side_effect=mock_execute)

        with patch.object(
            reconciler, "_retry_bootstrap", new_callable=AsyncMock
        ) as mock_retry:
            mock_retry.return_value = "retried"
            result = await reconciler.reconcile_workspace(ws.id)
            assert result["status"] == "ok"
            assert result["action"] == "bootstrap_retry"

    @pytest.mark.asyncio
    async def test_manual_drift_check(self):
        """Manual reconciliation of an active workspace should check drift."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        cluster = _make_cluster(
            desired_state_json={"node_count": 3},
            actual_state_json={"node_count": 5},
        )
        ws = _make_workspace(status=STATUS_ACTIVE, cluster_id=cluster.id)

        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = ws
            elif call_count == 2:
                result.scalar_one_or_none.return_value = cluster
            return result

        session.execute = AsyncMock(side_effect=mock_execute)

        with patch.object(
            reconciler, "_check_drift", new_callable=AsyncMock
        ) as mock_drift:
            mock_drift.return_value = True
            result = await reconciler.reconcile_workspace(ws.id)
            assert result["status"] == "ok"
            assert result["action"] == "drift_check"
            assert result["drift_detected"] is True

    @pytest.mark.asyncio
    async def test_manual_no_action_for_pending(self):
        """Workspace in 'pending' status should get no_action."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        ws = _make_workspace(status="pending", cluster_id=None)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        session.execute.return_value = mock_result

        result = await reconciler.reconcile_workspace(ws.id)
        assert result["status"] == "ok"
        assert result["action"] == "no_action"


# ============================================================
# Tests: Status constants
# ============================================================


class TestStatusConstants:
    """Verify status constants are defined correctly."""

    def test_status_values(self):
        assert STATUS_FAILED == "Failed"
        assert STATUS_ACTIVE == "active"
        assert STATUS_DRIFT_DETECTED == "drift_detected"
        assert STATUS_RECONCILING == "reconciling"
        assert STATUS_MAX_RETRIES_EXCEEDED == "max_retries_exceeded"


# ============================================================
# Tests: Edge cases
# ============================================================


class TestEdgeCases:
    """Edge case tests for robustness."""

    def test_detect_drift_with_nested_values(self):
        """Drift detection should handle nested structures."""
        desired = {"subnet_ids": ["subnet-1", "subnet-2"]}
        actual = {"subnet_ids": ["subnet-1", "subnet-3"]}
        result = detect_drift(desired, actual)
        assert "subnet_ids" in result

    def test_detect_drift_same_nested_values(self):
        desired = {"subnet_ids": ["subnet-1", "subnet-2"]}
        actual = {"subnet_ids": ["subnet-1", "subnet-2"]}
        result = detect_drift(desired, actual)
        assert result == {}

    def test_backoff_at_boundary(self):
        """Backoff at retry 4 with base 60 = 960 which equals MAX_BACKOFF_SECONDS."""
        assert compute_backoff(4) == 960
        assert compute_backoff(4) == MAX_BACKOFF_SECONDS

    def test_backoff_zero_base(self):
        """Zero base should always return 0."""
        assert compute_backoff(0, base=0) == 0
        assert compute_backoff(5, base=0) == 0

    @pytest.mark.asyncio
    async def test_reconciler_custom_params(self):
        """Verify custom parameters are stored correctly."""
        factory, _ = _mock_session_factory()
        reconciler = WorkspaceReconciler(
            session_factory=factory,
            reconcile_interval=30,
            max_retries=10,
            lock_ttl=60,
            backoff_base=30,
        )
        assert reconciler._reconcile_interval == 30
        assert reconciler._max_retries == 10
        assert reconciler._lock_ttl == 60
        assert reconciler._backoff_base == 30

    def test_parse_json_state_with_number(self):
        """Non-string, non-dict, non-None should return empty."""
        assert _parse_json_state(42) == {}

    @pytest.mark.asyncio
    async def test_max_retries_exceeded_manual_then_retry(self):
        """Manual reconciliation of max_retries_exceeded workspace should reset and retry."""
        factory, session = _mock_session_factory()
        reconciler = WorkspaceReconciler(session_factory=factory)

        ws = _make_workspace(
            status=STATUS_MAX_RETRIES_EXCEEDED,
            bootstrap_retry_count=5,
        )

        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.scalar_one_or_none.return_value = ws
            return result

        session.execute = AsyncMock(side_effect=mock_execute)

        with patch.object(
            reconciler, "_retry_bootstrap", new_callable=AsyncMock
        ) as mock_retry:
            mock_retry.return_value = "retried"
            result = await reconciler.reconcile_workspace(ws.id)
            assert result["action"] == "bootstrap_retry"
