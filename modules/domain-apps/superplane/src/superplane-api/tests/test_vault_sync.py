"""Tests for VaultSyncReconciler — credential rotation propagation (US-H2).

Uses mock session factory and fake ExternalSecret client to test
reconciliation logic without real database or cluster connections.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.vault_sync import (
    EXTERNAL_SECRET_NAMESPACE,
    ExternalSecretClient,
    VaultSyncReconciler,
    _build_secret_name,
)


# --- Helpers ---


def _make_credential(
    org_id: uuid.UUID | None = None,
    provider: str = "nebius",
    friendly_name: str = "Nebius GPU Key",
    secret_arn: str = "arn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-key",
    status: str = "Active",
    last_rotated_at: datetime | None = None,
    expires_at: datetime | None = None,
    kms_key_id: str | None = None,
) -> MagicMock:
    """Create a mock CredentialRegistry row."""
    cred = MagicMock()
    cred.id = uuid.uuid4()
    cred.org_id = org_id or uuid.uuid4()
    cred.provider = provider
    cred.friendly_name = friendly_name
    cred.credential_type = "api_key"
    cred.secret_arn = secret_arn
    cred.kms_key_id = kms_key_id
    cred.expires_at = expires_at
    cred.last_rotated_at = last_rotated_at
    cred.status = status
    return cred


def _make_cluster(
    org_id: uuid.UUID | None = None,
    name: str = "data-plane-1",
    endpoint: str = "https://eks.us-west-2.amazonaws.com/cluster-1",
    status: str = "Active",
) -> MagicMock:
    """Create a mock Cluster row."""
    cluster = MagicMock()
    cluster.id = uuid.uuid4()
    cluster.org_id = org_id or uuid.uuid4()
    cluster.name = name
    cluster.endpoint = endpoint
    cluster.status = status
    return cluster


def _make_assignment(
    cluster_id: uuid.UUID | None = None,
    credential_id: uuid.UUID | None = None,
    status: str = "Pending",
    synced_at: datetime | None = None,
) -> MagicMock:
    """Create a mock ClusterVaultAssignment row."""
    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.cluster_id = cluster_id or uuid.uuid4()
    assignment.credential_registry_id = credential_id or uuid.uuid4()
    assignment.status = status
    assignment.synced_at = synced_at
    return assignment


class FakeExternalSecretClient(ExternalSecretClient):
    """Fake client for testing — tracks calls and allows configurable responses."""

    def __init__(self, should_succeed: bool = True):
        self.apply_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.should_succeed = should_succeed

    async def apply_external_secret(
        self,
        cluster_endpoint: str,
        secret_name: str,
        secret_arn: str,
        namespace: str,
        kms_key_id: str | None = None,
    ) -> dict:
        self.apply_calls.append(
            {
                "cluster_endpoint": cluster_endpoint,
                "secret_name": secret_name,
                "secret_arn": secret_arn,
                "namespace": namespace,
                "kms_key_id": kms_key_id,
            }
        )
        return {"synced": self.should_succeed}

    async def delete_external_secret(
        self,
        cluster_endpoint: str,
        secret_name: str,
        namespace: str,
    ) -> bool:
        self.delete_calls.append(
            {
                "cluster_endpoint": cluster_endpoint,
                "secret_name": secret_name,
                "namespace": namespace,
            }
        )
        return True


# --- Tests for _build_secret_name ---


class TestBuildSecretName:
    def test_basic_name(self):
        cred = _make_credential(provider="nebius", friendly_name="GPU API Key")
        name = _build_secret_name(cred)
        assert name == "superplane-nebius-gpu-api-key"

    def test_special_characters_removed(self):
        cred = _make_credential(provider="lambda", friendly_name="My Key! @#$%")
        name = _build_secret_name(cred)
        assert name == "superplane-lambda-my-key"

    def test_underscores_converted(self):
        cred = _make_credential(provider="coreweave", friendly_name="my_api_key")
        name = _build_secret_name(cred)
        assert name == "superplane-coreweave-my-api-key"

    def test_consecutive_hyphens_collapsed(self):
        cred = _make_credential(provider="nebius", friendly_name="key--with---hyphens")
        name = _build_secret_name(cred)
        assert "--" not in name

    def test_long_name_truncated(self):
        cred = _make_credential(provider="nebius", friendly_name="a" * 300)
        name = _build_secret_name(cred)
        assert len(name) <= 253

    def test_lowercase(self):
        cred = _make_credential(provider="AWS", friendly_name="My PRODUCTION Key")
        name = _build_secret_name(cred)
        assert name == name.lower()


# --- Tests for VaultSyncReconciler ---


class TestVaultSyncReconciler:
    """Unit tests for the VaultSyncReconciler's sync logic.

    These tests mock the database session and use FakeExternalSecretClient
    to verify reconciliation behavior without real infrastructure.
    """

    def _make_reconciler(
        self, es_client: ExternalSecretClient | None = None
    ) -> VaultSyncReconciler:
        """Create a reconciler with mock session factory."""
        session_factory = AsyncMock()
        reconciler = VaultSyncReconciler(
            session_factory=session_factory,
            es_client=es_client or FakeExternalSecretClient(),
            reconcile_interval=1,
            retry_backoff=60,
            lock_ttl=30,
        )
        return reconciler

    @pytest.mark.asyncio
    async def test_sync_assignment_reports_delivery_unavailable(self):
        """Issue #5046 (U13b): the reconciler no longer replicates a secret by ARN.

        REPLACES `test_sync_assignment_success`, which asserted
        `call["secret_arn"] == credential.secret_arn` — that the reconciler copied the
        secret's Secrets Manager address into the target cluster's ExternalSecret. That is
        the behavior this story removes: the workload cluster then read the secret
        directly, so the ADP vault (which owns rotation and revocation) never saw the
        access and could not stop it.

        The domain record now holds only an opaque ADP credential ID that only the vault
        can resolve, so the reconciler cannot build such a manifest. It must fail the
        assignment explicitly rather than invent an address, which is what this pins.

        Almost no working capability was lost: `KubernetesExternalSecretClient` only BUILT
        a manifest and returned `{"synced": True}` without ever applying it, so the old
        "success" was never evidence of delivery. Vault-brokered delivery is U7/U7b.
        """
        es_client = FakeExternalSecretClient(should_succeed=True)
        reconciler = self._make_reconciler(es_client)

        org_id = uuid.uuid4()
        credential = _make_credential(org_id=org_id)
        cluster = _make_cluster(org_id=org_id)
        assignment = _make_assignment(
            cluster_id=cluster.id,
            credential_id=credential.id,
            status="Pending",
        )

        # Mock session
        session = AsyncMock()
        # Mock lock acquisition — no existing lock
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        session.add = MagicMock()

        result = await reconciler._sync_assignment(
            session, assignment, credential, cluster
        )

        assert result is False, "delivery is unavailable, so the sync must not claim success"
        assert es_client.apply_calls == [], (
            "no ExternalSecret may be applied: doing so would require a secret ARN the "
            "domain record must no longer hold"
        )

    @pytest.mark.asyncio
    async def test_sync_assignment_no_endpoint(self):
        """Test that sync fails gracefully when cluster has no endpoint."""
        es_client = FakeExternalSecretClient()
        reconciler = self._make_reconciler(es_client)

        credential = _make_credential()
        cluster = _make_cluster(endpoint="")  # No endpoint
        assignment = _make_assignment()

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        session.add = MagicMock()

        result = await reconciler._sync_assignment(
            session, assignment, credential, cluster
        )

        assert result is False
        assert len(es_client.apply_calls) == 0

    @pytest.mark.asyncio
    async def test_sync_assignment_es_failure(self):
        """Test that sync returns False when ExternalSecret apply fails."""
        es_client = FakeExternalSecretClient(should_succeed=False)
        reconciler = self._make_reconciler(es_client)

        credential = _make_credential()
        cluster = _make_cluster()
        assignment = _make_assignment()

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        session.add = MagicMock()

        result = await reconciler._sync_assignment(
            session, assignment, credential, cluster
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_mark_synced(self):
        """Test that _mark_synced updates the assignment status."""
        reconciler = self._make_reconciler()
        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()

        assignment = _make_assignment(status="Pending")
        await reconciler._mark_synced(session, assignment)

        # Verify execute was called (for the UPDATE statement)
        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_mark_failed(self):
        """Test that _mark_failed updates the assignment status."""
        reconciler = self._make_reconciler()
        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()

        assignment = _make_assignment(status="Pending")
        await reconciler._mark_failed(session, assignment, "test error")

        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_audit_log(self):
        """Test that audit log entries are created."""
        reconciler = self._make_reconciler()
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()

        credential = _make_credential()
        cluster = _make_cluster()

        await reconciler._write_audit_log(session, credential, cluster, "sync_success")

        session.add.assert_called_once()
        session.commit.assert_called_once()

        # Verify the audit log entry has correct fields
        audit_entry = session.add.call_args[0][0]
        assert audit_entry.org_id == credential.org_id
        assert audit_entry.credential_registry_id == credential.id
        assert audit_entry.cluster_id == cluster.id
        assert audit_entry.action == "sync_success"
        assert audit_entry.accessed_by == "VaultSyncReconciler"

    @pytest.mark.asyncio
    async def test_emit_event(self):
        """Test that platform events are emitted."""
        reconciler = self._make_reconciler()
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()

        org_id = uuid.uuid4()
        resource_id = uuid.uuid4()

        await reconciler._emit_event(
            session,
            org_id=org_id,
            resource_type="ClusterVaultAssignment",
            resource_id=resource_id,
            event_type="VaultSyncSucceeded",
            message="Test sync succeeded",
        )

        session.add.assert_called_once()
        session.commit.assert_called_once()

        event = session.add.call_args[0][0]
        assert event.org_id == org_id
        assert event.event_type == "VaultSyncSucceeded"

    @pytest.mark.asyncio
    async def test_check_expiry_warnings(self):
        """Test that expiry warnings are emitted for credentials near expiry."""
        reconciler = self._make_reconciler()

        # Create a credential that expires in 3 days
        soon_cred = _make_credential(
            expires_at=datetime.now(timezone.utc) + timedelta(days=3)
        )

        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()

        # Mock the query to return the expiring credential
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [soon_cred]
        mock_result.scalars.return_value = mock_scalars
        session.execute.return_value = mock_result

        count = await reconciler._check_expiry_warnings(session)

        assert count == 1
        # Should have emitted an event
        session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_trigger_sync_manual(self):
        """Test manual trigger_sync with specific credential_id."""
        es_client = FakeExternalSecretClient(should_succeed=True)

        org_id = uuid.uuid4()
        credential = _make_credential(org_id=org_id)
        cluster = _make_cluster(org_id=org_id, endpoint="https://cluster.example.com")
        assignment = _make_assignment(
            cluster_id=cluster.id, credential_id=credential.id
        )

        # Build mock session factory
        session = AsyncMock()

        # First execute call: the main query returns assignments
        # Subsequent calls: for lock, status updates, etc.
        call_count = 0

        async def mock_execute(stmt):
            nonlocal call_count
            call_count += 1

            if call_count == 1:
                # Main query — return assignment tuple
                mock_result = MagicMock()
                mock_result.all.return_value = [(assignment, credential, cluster)]
                return mock_result
            else:
                # Lock checks and updates
                mock_result = MagicMock()
                mock_result.scalar_one_or_none.return_value = None
                return mock_result

        session.execute = mock_execute
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        session.add = MagicMock()

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=session_ctx)

        reconciler = VaultSyncReconciler(
            session_factory=session_factory,
            es_client=es_client,
            reconcile_interval=1,
        )

        stats = await reconciler.trigger_sync(credential_id=credential.id)

        # Issue #5046 (U13b): the assignment is still FOUND and processed — the manual
        # trigger and its query still work — but it is no longer counted as synced,
        # because delivery from a copied secret ARN was withdrawn. Previously this
        # asserted synced == 1, describing a sync that never applied anything to a cluster.
        #
        # It lands in `skipped` rather than `failed` because `trigger_sync` buckets by
        # `_sync_assignment`'s return value, counting `failed` only for a raised exception;
        # a False return has always meant `skipped` here, including for the pre-existing
        # no-endpoint case. The assignment row itself IS marked "Failed" with the reason.
        # That counter imprecision predates this story and is left alone as out of scope.
        assert stats["total"] == 1
        assert stats["synced"] == 0
        assert stats["skipped"] == 1

    @pytest.mark.asyncio
    async def test_lifecycle_start_stop(self):
        """Test that start/stop lifecycle works cleanly."""
        reconciler = self._make_reconciler()

        # Patch reconcile to avoid actual DB calls
        reconciler.reconcile = AsyncMock(
            return_value={"synced": 0, "failed": 0, "skipped": 0, "expiry_warnings": 0}
        )

        await reconciler.start()
        assert reconciler._running is True
        assert reconciler._task is not None

        await reconciler.stop()
        assert reconciler._running is False

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        """Test that calling start twice doesn't create duplicate tasks."""
        reconciler = self._make_reconciler()
        reconciler.reconcile = AsyncMock(
            return_value={"synced": 0, "failed": 0, "skipped": 0, "expiry_warnings": 0}
        )

        await reconciler.start()
        task1 = reconciler._task

        await reconciler.start()  # Should warn and skip
        task2 = reconciler._task

        assert task1 is task2  # Same task, not a new one

        await reconciler.stop()


class TestExternalSecretClientInterface:
    """Test the ExternalSecretClient abstract interface."""

    @pytest.mark.asyncio
    async def test_base_class_raises(self):
        """Base ExternalSecretClient methods should raise NotImplementedError."""
        client = ExternalSecretClient()

        with pytest.raises(NotImplementedError):
            await client.apply_external_secret("endpoint", "name", "arn", "ns")

        with pytest.raises(NotImplementedError):
            await client.delete_external_secret("endpoint", "name", "ns")

    @pytest.mark.asyncio
    async def test_fake_client_tracks_calls(self):
        """FakeExternalSecretClient should track all calls."""
        client = FakeExternalSecretClient()

        result = await client.apply_external_secret(
            "https://cluster-1", "my-secret", "arn:aws:...", "default"
        )

        assert result["synced"] is True
        assert len(client.apply_calls) == 1
        assert client.apply_calls[0]["secret_name"] == "my-secret"

        deleted = await client.delete_external_secret(
            "https://cluster-1", "my-secret", "default"
        )
        assert deleted is True
        assert len(client.delete_calls) == 1
