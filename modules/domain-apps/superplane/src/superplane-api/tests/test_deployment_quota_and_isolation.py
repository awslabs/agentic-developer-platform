"""Maintained GPU reservation, quota signal and durable namespace confinement.

The PostgreSQL controller suite covers actual approved API admission and replay;
the quota concurrency suite covers real row locks. These tests exercise the quota
owner directly, without fabricating an alternate controller execution path.
"""

import inspect
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy import select, func

from app.middleware.auth import create_access_token
from app.middleware.quota import QuotaEnforcementMiddleware
from app.models.cluster import Cluster
from app.models.deployment import Deployment
from app.models.node import Node
from app.models.node_pool import NodePool
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.services import proxy as proxy_module
from app.services.quota import reserve_deployment_gpus
from tests.conftest import async_session_test

ORG_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000000a")
CLUSTER_ID = uuid.UUID("cccccccc-0000-0000-0000-00000000000c")
WS_A = uuid.UUID("aaaa1111-1111-1111-1111-11111111111a")
WS_B = uuid.UUID("bbbb2222-2222-2222-2222-22222222222b")
NS_A, NS_B = "ws-alpha", "ws-beta"


def _auth():
    token, _ = create_access_token(ORG_ID)
    return {"Authorization": f"Bearer {token}"}


async def _seed(
    *,
    ws_a_max_gpus: int | None = 8,
    ws_b_max_gpus: int | None = 8,
    ws_a_namespace: str | None = NS_A,
    node_gpus: int | None = None,
) -> None:
    """Seed one org and TWO workspaces sharing a single cluster.

    Flushed in dependency order because these models declare no ORM relationships, so
    SQLAlchemy has no graph to sort the inserts by and the FK constraint fires.
    """
    async with async_session_test() as session:
        session.add(Organization(id=ORG_ID, name="org-a", billing_plan="enterprise"))
        await session.flush()

        session.add(
            Cluster(
                id=CLUSTER_ID,
                org_id=ORG_ID,
                name="shared",
                cloud_provider="aws",
                cluster_type="eks",
                eks_cluster_arn="arn:aws:eks:eu-west-1:123456789012:cluster/shared",
                endpoint="https://abc.gr7.eu-west-1.eks.amazonaws.com",
                status="Active",
            )
        )
        await session.flush()

        # Flushed one at a time. Adding both in a single flush makes SQLAlchemy batch
        # them through `insertmanyvalues`, whose sentinel processor mis-coerces these
        # all-numeric UUID hex strings as floats and raises inside uuid.UUID.
        session.add(
            Workspace(
                id=WS_A,
                org_id=ORG_ID,
                name="alpha",
                isolation_mode="namespace",
                cluster_id=CLUSTER_ID,
                namespace_name=ws_a_namespace,
                budget_max_gpus=ws_a_max_gpus,
                status="Active",
            )
        )
        await session.flush()
        session.add(
            Workspace(
                id=WS_B,
                org_id=ORG_ID,
                name="beta",
                isolation_mode="namespace",
                cluster_id=CLUSTER_ID,
                namespace_name=NS_B,
                budget_max_gpus=ws_b_max_gpus,
                status="Active",
            )
        )
        await session.flush()

        if node_gpus is not None:
            pool_id = uuid.uuid4()
            session.add(
                NodePool(
                    id=pool_id,
                    cluster_id=CLUSTER_ID,
                    org_id=ORG_ID,
                    name="pool",
                    desired_count=1,
                )
            )
            await session.flush()
            session.add(
                Node(
                    id=uuid.uuid4(),
                    node_pool_id=pool_id,
                    cluster_id=CLUSTER_ID,
                    org_id=ORG_ID,
                    gpu_count=node_gpus,
                    status="Running",
                )
            )

        await session.commit()


async def _seed_deployment(
    dep_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID,
    name: str,
    namespace: str,
    gpus: int = 1,
    status: str = "Created",
) -> None:
    async with async_session_test() as session:
        session.add(
            Deployment(
                id=dep_id,
                cluster_id=CLUSTER_ID,
                org_id=ORG_ID,
                workspace_id=workspace_id,
                name=name,
                namespace=namespace,
                desired_replicas=1,
                gpu_per_replica=gpus,
                status=status,
            )
        )
        await session.commit()


async def reserve(gpus, *, workspace_id=WS_A, request=None):
    async with async_session_test() as db:
        return await reserve_deployment_gpus(
            workspace_id,
            ORG_ID,
            gpus,
            db,
            deployment_kwargs={
                "cluster_id": CLUSTER_ID,
                "name": "model-" + uuid.uuid4().hex[:12],
                "namespace": NS_A if workspace_id == WS_A else NS_B,
                "desired_replicas": 1,
                "gpu_per_replica": gpus,
            },
            request=request,
        )


class TestDeploymentGpuQuota:
    @pytest.mark.parametrize("requested,accepted", [(3, True), (4, True), (5, False)])
    async def test_inclusive_budget_boundary_commits_only_accepted_intents(
        self, requested, accepted
    ):
        await _seed(ws_a_max_gpus=4)
        if accepted:
            result = await reserve(requested)
            assert result.status == "Pending"
        else:
            with pytest.raises(HTTPException) as refused:
                await reserve(requested)
            assert refused.value.status_code == 429
        async with async_session_test() as db:
            assert await db.scalar(select(func.count()).select_from(Deployment)) == int(
                accepted
            )

    async def test_sequential_requests_recount_committed_cumulative_capacity(self):
        await _seed(ws_a_max_gpus=4)
        await reserve(2)
        await reserve(2)
        for _ in range(2):
            with pytest.raises(HTTPException) as refused:
                await reserve(2)
            assert refused.value.status_code == 429
        async with async_session_test() as db:
            assert await db.scalar(select(func.count()).select_from(Deployment)) == 2

    async def test_existing_nodes_do_not_double_charge_model_gpus(self):
        await _seed(ws_a_max_gpus=4, node_gpus=4)
        assert (await reserve(4)).status == "Pending"

    @pytest.mark.parametrize(
        "org_quotas", [None, "{}", '{"max_nodes":12}', '{"max_gpus":null}']
    )
    async def test_absent_budget_falls_back_to_enterprise_limit(self, org_quotas):
        await _seed(ws_a_max_gpus=None)
        async with async_session_test() as db:
            org = await db.get(Organization, ORG_ID)
            org.quotas_json = org_quotas
            await db.commit()
        await reserve(256)
        with pytest.raises(HTTPException) as refused:
            await reserve(1)
        assert refused.value.status_code == 429

    @pytest.mark.parametrize(
        "retained",
        ["Pending", "Created", "Provisioning", "Unknown", "NeedsRecovery", "Deleting"],
    )
    async def test_capacity_remains_reserved_until_owned_absence_is_projected(
        self, retained
    ):
        await _seed(ws_a_max_gpus=4)
        original = await reserve(4)
        async with async_session_test() as db:
            row = await db.get(Deployment, original.id)
            row.status = retained
            await db.commit()
        with pytest.raises(HTTPException) as refused:
            await reserve(1)
        assert refused.value.status_code == 429
        # Finalizer observation projection is covered through actual RPC in the
        # controller PG suite. Here we assert the quota owner's status contract.
        async with async_session_test() as db:
            row = await db.get(Deployment, original.id)
            row.status = "Deleted"
            await db.commit()
        assert (await reserve(4)).status == "Pending"

    async def test_shared_cluster_workspaces_have_independent_model_budgets(self):
        await _seed(ws_a_max_gpus=4, ws_b_max_gpus=4)
        assert (await reserve(4)).workspace_id == WS_A
        assert (await reserve(4, workspace_id=WS_B)).workspace_id == WS_B

    def test_the_reservation_locks_the_workspace_row(self):
        assert "with_for_update()" in inspect.getsource(reserve_deployment_gpus)


class TestQuotaEnforcementHonesty:
    @pytest.mark.parametrize("requested,status", [(1, 201), (2, 429)])
    async def test_real_quota_decision_controls_the_middleware_header(
        self, requested, status
    ):
        await _seed(ws_a_max_gpus=1)
        request = Request(
            {"type": "http", "method": "POST", "path": "/deployments", "headers": []}
        )

        async def call_next(request):
            try:
                await reserve(requested, request=request)
                return Response(status_code=201)
            except HTTPException as exc:
                return Response(status_code=exc.status_code)

        response = await QuotaEnforcementMiddleware(
            lambda scope, receive, send: None
        ).dispatch(request, call_next)
        assert response.status_code == status
        assert response.headers["X-Quota-Enforcement"] == "active"

    @pytest.mark.parametrize("absent", [True, False])
    async def test_unconfigured_api_never_claims_quota_enforcement(
        self, client, monkeypatch, absent
    ):
        from app.composition import Composition
        from tests.conftest import app

        monkeypatch.setattr(
            app.state,
            "trust_composition",
            None if absent else Composition(),
            raising=False,
        )
        await _seed()
        response = await client.post(
            f"/workspaces/{WS_A}/deployments",
            json={"name": "model", "model_name": "example/model"},
            headers=_auth(),
        )
        assert response.status_code == 503
        assert (
            response.json()["detail"]
            == "governed controller operation store is unavailable"
        )
        assert "X-Quota-Enforcement" not in response.headers
        async with async_session_test() as db:
            assert await db.scalar(select(func.count()).select_from(Deployment)) == 0

    async def test_health_does_not_claim_quota_enforcement(self, client):
        assert "X-Quota-Enforcement" not in (await client.get("/health")).headers

    def test_the_middleware_does_not_assert_enforcement_unconditionally(self):
        """The header must be derived from a recorded decision, not set on every reply.

        Guards the mechanism rather than one route: a regression that reinstated the
        blanket header would keep every behavioural test above passing except the
        ``/health`` one, and the mechanism is worth failing loudly on too.
        """
        from app.middleware import quota as quota_middleware

        source = inspect.getsource(quota_middleware.QuotaEnforcementMiddleware)
        assert "QUOTA_DECISION_ATTR" in source, (
            "the enforcement header is no longer conditioned on a recorded decision"
        )

    def test_every_declared_enforcement_routine_has_a_non_test_caller(self):
        """No enforcement routine may exist without something calling it.

        The original defect was not a wrong check — it was a CORRECT check with no
        caller. Reviewing the quota service in isolation looked fine, and the middleware
        reported enforcement, so neither the code nor its output revealed the gap. This
        walks the application package and fails if a declared enforcement entry point is
        never referenced outside tests.
        """
        import ast
        import pathlib

        app_dir = pathlib.Path(proxy_module.__file__).resolve().parent.parent
        quota_path = app_dir / "services" / "quota.py"
        quota_src = quota_path.read_text()

        declared = {
            node.name
            for node in ast.walk(ast.parse(quota_src))
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and (node.name.startswith("enforce_") or node.name.startswith("reserve_"))
        }
        assert declared, "no enforcement routines found — has quota.py moved?"

        callers: dict[str, set[str]] = {name: set() for name in declared}
        for path in app_dir.rglob("*.py"):
            if path == quota_path:
                continue  # a definition is not a caller
            text = path.read_text()
            for name in declared:
                if f"{name}(" in text:
                    callers[name].add(str(path.relative_to(app_dir)))

        uncalled = sorted(name for name, found in callers.items() if not found)

        # `enforce_node_provisioning_quota` is knowingly uncalled: this service exposes
        # no node-provisioning route for it to guard. Issue #5671 records that rather
        # than inventing a route to justify the function. Listing it explicitly is the
        # point — it is acknowledged in a test that fails if anything ELSE joins it,
        # instead of being invisible the way the deployment check was.
        assert uncalled == ["enforce_node_provisioning_quota"], (
            f"enforcement routines with no caller outside tests: {uncalled}"
        )


class TestDurableDeploymentConfinement:
    async def test_caller_supplied_namespace_is_rejected(self, client):
        await _seed()
        response = await client.post(
            f"/workspaces/{WS_A}/deployments",
            json={
                "name": "model",
                "model_name": "example/model",
                "namespace": "kube-system",
            },
            headers=_auth(),
        )
        assert response.status_code == 422

    async def test_raw_kubernetes_name_is_refused(self, client):
        await _seed()
        response = await client.request(
            "DELETE",
            f"/workspaces/{WS_A}/deployments/raw-model-name",
            json={"operation_id": str(uuid.uuid4())},
            headers=_auth(),
        )
        assert response.status_code == 422

    @pytest.mark.parametrize("namespace", [None, "kube-system"])
    async def test_unresolved_namespace_prevents_even_a_durable_list(
        self, client, namespace
    ):
        await _seed(ws_a_namespace=namespace)
        response = await client.get(f"/workspaces/{WS_A}/deployments", headers=_auth())
        assert response.status_code == 409

    async def test_list_uses_durable_workspace_namespace_and_uuid_without_cluster_access(
        self, client, monkeypatch
    ):
        await _seed()
        expected = uuid.uuid4()
        for deployment_id, workspace_id, namespace, status in [
            (expected, WS_A, NS_A, "Created"),
            (uuid.uuid4(), WS_B, NS_B, "Created"),
            (uuid.uuid4(), WS_A, NS_B, "Created"),
            (uuid.uuid4(), WS_A, NS_A, "Deleted"),
        ]:
            await _seed_deployment(
                deployment_id,
                workspace_id=workspace_id,
                name="model-" + deployment_id.hex[:8],
                namespace=namespace,
                status=status,
            )

        def no_credentials(*args, **kwargs):
            raise AssertionError(
                "durable listing must not request provider credentials"
            )

        monkeypatch.setattr(proxy_module, "assume_role_for_cluster", no_credentials)
        response = await client.get(
            f"/workspaces/{WS_A}/deployments?namespace={NS_B}", headers=_auth()
        )
        assert response.status_code == 200, response.text
        rows = response.json()["deployments"]
        assert len(rows) == 1
        assert rows[0]["deployment_id"] == str(expected)
        assert rows[0]["namespace"] == NS_A
        assert rows[0]["provider_uid"] is None

    async def test_delete_preview_cannot_cross_workspace_or_mutate_legacy_intent(self):
        from app.services.deployment_operations import preview_delete
        from app.services.provisioning import ProvisioningRefused
        from app.schemas.proxy import DeleteDeploymentRequest

        await _seed()
        deployment_id = uuid.uuid4()
        await _seed_deployment(
            deployment_id, workspace_id=WS_B, namespace=NS_B, name="legacy"
        )

        def no_operations():
            raise AssertionError(
                "unregistered intent must refuse before the operation store"
            )

        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    trust_composition=SimpleNamespace(operation_connect=no_operations)
                )
            )
        )
        for workspace_id, reason in [(WS_A, "not available"), (WS_B, "legacy")]:
            async with async_session_test() as db:
                with pytest.raises(ProvisioningRefused, match=reason):
                    await preview_delete(
                        request,
                        db,
                        ORG_ID,
                        workspace_id,
                        deployment_id,
                        DeleteDeploymentRequest(operation_id=uuid.uuid4()),
                    )


class TestNamespaceResolver:
    """Unit coverage for the resolver's fail-closed behaviour."""

    def _workspace(self, namespace_name: str | None) -> Workspace:
        return Workspace(
            id=WS_A,
            org_id=ORG_ID,
            name="alpha",
            isolation_mode="namespace",
            namespace_name=namespace_name,
        )

    @pytest.mark.parametrize("reserved", ["default", "kube-system", "adp-gateway"])
    def test_a_reserved_namespace_is_refused(self, reserved):
        """Including ``default``: a shared namespace is never a tenant's namespace."""
        from app.services.workspace_namespace import (
            NamespaceResolutionError,
            resolve_workspace_namespace,
        )

        with pytest.raises(NamespaceResolutionError):
            resolve_workspace_namespace(self._workspace(reserved))

    @pytest.mark.parametrize(
        "bad", ["Not A Namespace", "-leading", "trailing-", "a" * 64]
    )
    def test_a_malformed_namespace_is_refused(self, bad):
        """Refused rather than sanitised: two inputs must not normalise to one target."""
        from app.services.workspace_namespace import (
            NamespaceResolutionError,
            resolve_workspace_namespace,
        )

        with pytest.raises(NamespaceResolutionError):
            resolve_workspace_namespace(self._workspace(bad))

    @pytest.mark.parametrize("blank", [None, "", "   "])
    def test_a_blank_namespace_requires_ownership_reconciliation(self, blank):
        """Whitespace is treated as absent, so it cannot resolve to an empty target."""
        from app.services.workspace_namespace import resolve_workspace_namespace

        from app.services.workspace_namespace import NamespaceResolutionError

        with pytest.raises(NamespaceResolutionError):
            resolve_workspace_namespace(self._workspace(blank))

    def test_resolution_is_deterministic(self):
        """A create and a later delete must resolve to the same place."""
        from app.services.workspace_namespace import resolve_workspace_namespace

        workspace = self._workspace(NS_A)
        assert resolve_workspace_namespace(workspace) == resolve_workspace_namespace(
            workspace
        )

    def test_a_legacy_namespace_is_not_invented_by_migration(self):
        from pathlib import Path

        migration = (
            Path(__file__).parents[1]
            / "alembic/versions/028_deployment_namespace_quota.py"
        )
        assert "UPDATE workspaces" not in migration.read_text()


async def test_expected_namespace_does_not_override_workspace(monkeypatch):
    from unittest.mock import AsyncMock
    from pydantic import ValidationError
    from app.schemas.proxy import CreateDeploymentRequest
    from app.services import deployment_operations
    from app.services.provisioning import ProvisioningRefused

    body = CreateDeploymentRequest(name="model-a", model_name="model", profile_id="profile", expected_namespace="other")
    monkeypatch.setattr(deployment_operations,"get_workspace_cluster",AsyncMock(return_value=(object(),None)))
    monkeypatch.setattr(deployment_operations,"resolve_workspace_namespace",lambda workspace:"owned")
    with pytest.raises(ProvisioningRefused,match="workspace-owned namespace"):
        await deployment_operations.preview_create(None,uuid.uuid4(),uuid.uuid4(),body)
    with pytest.raises(ValidationError):
        CreateDeploymentRequest(name="model-a",model_name="model",namespace="other")
