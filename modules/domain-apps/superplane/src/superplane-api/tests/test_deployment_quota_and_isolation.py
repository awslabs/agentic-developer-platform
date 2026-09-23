"""Deployment quota enforcement and workspace namespace/ownership confinement.

Issue #5671 (A15). Covers findings:
  * ``f-96315bea-5e27-4d48-aacb-ca91301cf971`` — GPU/spend quota was never enforced on
    the model-deployment path, and the enforcement header claimed otherwise.
  * ``f-f27c7448-7885-4cf7-abf5-4b89f96260a3`` — the caller chose the target namespace,
    and on delete could name a raw Kubernetes object.

These tests assert the INVARIANTS rather than reproducing the defects, so they keep
holding if the implementation is reorganised.

Three deliberate choices about what is asserted:

*   A refused request is verified by the cluster client being **uncalled**, not by the
    response status. A check that refuses *after* provisioning already happened returns
    the same 429 while still having spent the money — the status alone cannot tell the
    two apart, which is close to the original defect.
*   Namespace assertions read the value **passed to the cluster client**, not the value
    in the response body. The body echoes what the fake returns, so asserting on it
    would pass even if the caller's namespace had been used.
*   The two workspaces share one cluster. That is the configuration where namespace and
    ownership confinement are the only things separating the tenants, and it is the
    shape the acceptance criteria name.

Known limitation, stated rather than papered over: this suite runs on SQLite, which has
no row-level locking, so ``with_for_update()`` compiles away and a genuinely concurrent
test here would pass with or without the lock. What is testable offline is covered here
(the ordering invariant, plus a source-level assertion pinning the lock); the contended
case lives in ``test_deployment_quota_concurrency_postgres.py``, which domain CI runs
against a real server.
"""

import inspect
import uuid
from unittest.mock import MagicMock, patch

import pytest
from app.middleware.auth import create_access_token
from app.models.cluster import Cluster
from app.models.deployment import Deployment
from app.models.node import Node
from app.models.node_pool import NodePool
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.routers import proxy as router_module
from app.services import proxy as proxy_module
from kubernetes.client.rest import ApiException

from tests.conftest import async_session_test

# Fixed ids so failures name the same workspace every run. Every literal contains a
# hex letter on purpose: an all-digit UUID hex string acquires SQLite's numeric affinity
# and comes back as a float, which blows up inside uuid.UUID on the way out.
ORG_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000000a")
CLUSTER_ID = uuid.UUID("cccccccc-0000-0000-0000-00000000000c")
WS_A = uuid.UUID("aaaa1111-1111-1111-1111-11111111111a")
WS_B = uuid.UUID("bbbb2222-2222-2222-2222-22222222222b")
NS_A = "ws-alpha"
NS_B = "ws-beta"

OWNER_LABEL = proxy_module.WORKSPACE_OWNER_LABEL

# Enterprise plan default, from app.services.quota.PLAN_DEFAULTS. Named here so the
# "absent budget is not unlimited" test states which ceiling it expects to fall back to.
ENTERPRISE_DEFAULT_MAX_GPUS = 256


def _auth(org_id: uuid.UUID = ORG_ID) -> dict:
    token, _ = create_access_token(org_id)
    return {"Authorization": f"Bearer {token}"}


def _body(**overrides) -> dict:
    body = {
        "name": "llama-8b",
        "model_name": "meta-llama/Llama-3.1-8B-Instruct",
        "replicas": 1,
        "gpu_per_replica": 1,
    }
    body.update(overrides)
    return body


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


def _k8s_object(owner: str | None) -> MagicMock:
    """A fake cluster object, optionally carrying the workspace ownership label."""
    labels = {"superplane.io/component": "model-serving"}
    if owner is not None:
        labels[OWNER_LABEL] = owner
    obj = MagicMock()
    obj.metadata.labels = labels
    return obj


def _apply_result(name: str, namespace: str, replicas: int) -> MagicMock:
    result = MagicMock()
    result.metadata.name = name
    result.metadata.namespace = namespace
    result.spec.replicas = replicas
    return result


_UNLABELLED = object()


class _FakeAppsApi:
    """Records every call, so tests can assert what did and did not reach the cluster.

    Args:
        conflict_owner: if set, ``create`` raises 409 and the ownership read reports an
            object owned by that workspace id — the conflict-then-replace path.
        existing_owner: owner label the ownership read should report. ``None`` (the
            default, absent a ``conflict_owner``) means nothing is there, so the read
            raises 404. ``_UNLABELLED`` means the object exists with no owner label.
        create_error: if set, ``create`` raises it.
    """

    def __init__(
        self,
        *,
        conflict_owner: str | None = None,
        existing_owner=None,
        create_error: Exception | None = None,
    ):
        self.created: list[tuple[str, dict]] = []
        self.replaced: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.reads: list[tuple[str, str]] = []
        self._conflict_owner = conflict_owner
        self._existing_owner = (
            existing_owner if existing_owner is not None else conflict_owner
        )
        self._create_error = create_error

    # -- reads --
    def read_namespaced_deployment(self, name, namespace):
        self.reads.append((namespace, name))
        if self._existing_owner is None:
            raise ApiException(status=404, reason="Not Found")
        if self._existing_owner is _UNLABELLED:
            return _k8s_object(None)
        return _k8s_object(self._existing_owner)

    def list_namespaced_deployment(self, namespace, label_selector):
        self.reads.append((namespace, label_selector))
        result = MagicMock()
        result.items = []
        return result

    # -- mutations --
    def create_namespaced_deployment(self, namespace, body):
        if self._create_error is not None:
            raise self._create_error
        if self._conflict_owner is not None:
            raise ApiException(status=409, reason="Conflict")
        self.created.append((namespace, body))
        return _apply_result(
            body["metadata"]["name"], namespace, body["spec"]["replicas"]
        )

    def replace_namespaced_deployment(self, name, namespace, body):
        self.replaced.append((namespace, name))
        return _apply_result(name, namespace, body["spec"]["replicas"])

    def delete_namespaced_deployment(self, name, namespace):
        self.deleted.append((namespace, name))

    @property
    def mutations(self) -> list:
        """Every call that changed cluster state. Must be empty for a refusal."""
        return self.created + self.replaced + self.deleted


def _patch_clients(apps: _FakeAppsApi):
    """Fake only the AWS/EKS boundary, leaving the logic under test real.

    Patched at ``app.routers.proxy`` because that is the name the handler resolves.
    Namespace resolution, the quota reservation and the ownership checks all still run
    for real — they are what these tests are about.
    """

    async def _fake(ws_id, org_id, db):
        from sqlalchemy import select

        workspace = (
            await db.execute(select(Workspace).where(Workspace.id == ws_id))
        ).scalar_one()
        cluster = (
            await db.execute(select(Cluster).where(Cluster.id == CLUSTER_ID))
        ).scalar_one()
        return MagicMock(), apps, workspace, cluster

    return patch.object(router_module, "get_k8s_clients", _fake)


# ---------------------------------------------------------------------------
# Finding f-96315bea: GPU / spend quota was never enforced on this path.
# ---------------------------------------------------------------------------


class TestDeploymentGpuQuota:
    """f-96315bea-5e27-4d48-aacb-ca91301cf971 — quota enforced before provisioning."""

    @pytest.mark.asyncio
    async def test_request_over_budget_is_refused_and_never_reaches_the_cluster(
        self, client
    ):
        """Over-budget request: 429 AND nothing applied.

        The uncalled assertion is the load-bearing one — a check that ran after the
        apply would also return 429, having already provisioned the GPUs.
        """
        await _seed(ws_a_max_gpus=4)
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(replicas=4, gpu_per_replica=2),  # 8 > 4
                headers=_auth(),
            )

        assert response.status_code == 429, response.text
        assert apps.mutations == [], "refused request still reached the cluster"

    @pytest.mark.asyncio
    async def test_request_exactly_at_the_limit_is_accepted(self, client):
        """The boundary is inclusive: exactly the limit fits."""
        await _seed(ws_a_max_gpus=4)
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(replicas=2, gpu_per_replica=2),  # == 4
                headers=_auth(),
            )

        assert response.status_code == 201, response.text
        assert len(apps.created) == 1

    @pytest.mark.asyncio
    async def test_one_gpu_above_the_limit_is_refused(self, client):
        await _seed(ws_a_max_gpus=3)
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(replicas=4, gpu_per_replica=1),  # 4 > 3
                headers=_auth(),
            )

        assert response.status_code == 429, response.text
        assert apps.mutations == []

    @pytest.mark.asyncio
    async def test_the_limit_is_on_cumulative_capacity_not_per_request(self, client):
        """Repeated in-budget requests are refused once their TOTAL crosses the budget.

        This is the property that makes a budget a budget, and it is the defect's most
        expensive form: a per-request check admits every one of these calls, because no
        single one exceeds 4.
        """
        await _seed(ws_a_max_gpus=4)
        apps = _FakeAppsApi()
        statuses = []

        with _patch_clients(apps):
            for i in range(4):
                response = await client.post(
                    f"/workspaces/{WS_A}/deployments",
                    json=_body(name=f"dep-{i}", replicas=1, gpu_per_replica=2),
                    headers=_auth(),
                )
                statuses.append(response.status_code)

        # 2 + 2 = 4 fits; the third and fourth must not.
        assert statuses == [201, 201, 429, 429], statuses
        assert len(apps.created) == 2, "more capacity was provisioned than the budget"

    @pytest.mark.asyncio
    async def test_existing_node_capacity_counts_against_the_same_budget(self, client):
        """Node GPUs and deployment GPUs share one budget, not one each.

        ``budget_max_gpus`` is a single figure for the workspace's GPU footprint, so a
        workspace already holding 3 node GPUs has 1 left, not 4.
        """
        await _seed(ws_a_max_gpus=4, node_gpus=3)
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(replicas=2, gpu_per_replica=1),  # 3 + 2 > 4
                headers=_auth(),
            )

        assert response.status_code == 429, response.text
        assert apps.mutations == []

    @pytest.mark.asyncio
    async def test_a_workspace_with_no_recorded_budget_is_not_unlimited(self, client):
        """An unset budget falls back to the plan default, not to no ceiling.

        An absent budget is an unconfigured workspace; reading that as "no limit" is how
        the unconfigured tenant becomes the expensive one.
        """
        await _seed(ws_a_max_gpus=None)
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            at_limit = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(replicas=32, gpu_per_replica=8),  # == 256, the plan default
                headers=_auth(),
            )
            assert at_limit.status_code == 201, at_limit.text

            over = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(name="over", replicas=1, gpu_per_replica=1),
                headers=_auth(),
            )

        assert over.status_code == 429, (
            f"absent budget was treated as unlimited beyond "
            f"{ENTERPRISE_DEFAULT_MAX_GPUS} GPUs: {over.text}"
        )

    @pytest.mark.asyncio
    async def test_sequential_requests_see_each_other_s_reservations(self, client):
        """A reservation is committed before the next request counts usage.

        The ordering half of the atomicity guarantee: the row is written and committed
        inside ``reserve_deployment_gpus``, not left pending until the cluster write
        succeeds, so a following request cannot be admitted against a total that predates
        it.

        The genuinely CONCURRENT case is not testable here. SQLite has no row-level
        locking, so ``with_for_update()`` compiles to nothing and two interleaved requests
        are admitted whether or not the lock exists — a passing test would prove nothing.
        It lives in ``test_deployment_quota_concurrency_postgres.py`` against a real
        server, and ``test_the_reservation_locks_the_workspace_row`` below pins the lock
        so it cannot be dropped in an environment where this file is all that runs.
        """
        await _seed(ws_a_max_gpus=4)
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            first = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(name="dep-one", replicas=4, gpu_per_replica=1),
                headers=_auth(),
            )
            second = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(name="dep-two", replicas=4, gpu_per_replica=1),
                headers=_auth(),
            )

        assert first.status_code == 201, first.text
        assert second.status_code == 429, (
            "the second request did not see the first's reservation: " + second.text
        )
        assert len(apps.created) == 1

    def test_the_reservation_locks_the_workspace_row(self):
        """The reservation's workspace read must be ``FOR UPDATE``.

        Asserted against the source because the offline suite runs on SQLite, where
        ``with_for_update()`` compiles to nothing — so no behavioural test in this file
        can distinguish a locked read from an unlocked one. The whole concurrency
        guarantee rests on this one call, and without this assertion it could be dropped
        in a refactor with every test in this file still passing.
        """
        from app.services.quota import reserve_deployment_gpus

        source = inspect.getsource(reserve_deployment_gpus)
        assert "with_for_update()" in source, (
            "reserve_deployment_gpus no longer locks the workspace row; concurrent "
            "requests can both be admitted against the same headroom"
        )

    @pytest.mark.asyncio
    async def test_a_failed_cluster_write_releases_the_reservation(self, client):
        """A failed apply must not leave capacity held.

        Otherwise the tenant is throttled below their real entitlement by a deployment
        that does not exist — and a retry of the very same request is refused.
        """
        await _seed(ws_a_max_gpus=4)

        failing = _FakeAppsApi(
            create_error=ApiException(status=500, reason="Internal Server Error")
        )
        with _patch_clients(failing):
            attempt = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(name="doomed", replicas=4, gpu_per_replica=1),
                headers=_auth(),
            )
        assert attempt.status_code >= 500, attempt.text

        # The full budget must be available again.
        working = _FakeAppsApi()
        with _patch_clients(working):
            retry = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(name="retry", replicas=4, gpu_per_replica=1),
                headers=_auth(),
            )

        assert retry.status_code == 201, (
            "reserved capacity leaked after a failed cluster write: " + retry.text
        )

    @pytest.mark.asyncio
    async def test_a_deleted_deployment_returns_its_capacity(self, client):
        """Capacity held by a deleted deployment is released, not held forever."""
        await _seed(ws_a_max_gpus=4)
        dep_id = uuid.uuid4()
        await _seed_deployment(
            dep_id, workspace_id=WS_A, name="old", namespace=NS_A, gpus=4
        )

        blocked = _FakeAppsApi()
        with _patch_clients(blocked):
            refused = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(name="new", replicas=1, gpu_per_replica=1),
                headers=_auth(),
            )
        assert refused.status_code == 429, refused.text

        deleting = _FakeAppsApi(existing_owner=str(WS_A))
        with _patch_clients(deleting):
            removed = await client.delete(
                f"/workspaces/{WS_A}/deployments/{dep_id}", headers=_auth()
            )
        assert removed.status_code == 200, removed.text

        after = _FakeAppsApi()
        with _patch_clients(after):
            allowed = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(name="new", replicas=1, gpu_per_replica=1),
                headers=_auth(),
            )
        assert allowed.status_code == 201, (
            "a deleted deployment still holds capacity: " + allowed.text
        )

    @pytest.mark.asyncio
    async def test_each_workspace_has_its_own_budget_on_a_shared_cluster(self, client):
        """Two workspaces on one cluster are not charged for each other's deployments.

        Counting by cluster instead of by workspace would throttle both tenants below
        their real entitlement — an over-strict failure that still looks like
        enforcement working.
        """
        await _seed(ws_a_max_gpus=4, ws_b_max_gpus=4)
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            a = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(name="a-dep", replicas=4, gpu_per_replica=1),
                headers=_auth(),
            )
            b = await client.post(
                f"/workspaces/{WS_B}/deployments",
                json=_body(name="b-dep", replicas=4, gpu_per_replica=1),
                headers=_auth(),
            )

        assert a.status_code == 201, a.text
        assert b.status_code == 201, (
            "workspace B was charged for workspace A's deployments: " + b.text
        )


class TestQuotaEnforcementHonesty:
    """The enforcement signal must not claim more than was actually checked."""

    @pytest.mark.asyncio
    async def test_enforcement_is_reported_when_a_decision_was_made(self, client):
        await _seed(ws_a_max_gpus=4)
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(replicas=1, gpu_per_replica=1),
                headers=_auth(),
            )

        assert response.status_code == 201, response.text
        assert response.headers.get("X-Quota-Enforcement") == "active"

    @pytest.mark.asyncio
    async def test_enforcement_is_reported_on_a_refusal_too(self, client):
        """A refusal is a decision — the signal covers it, not just the happy path."""
        await _seed(ws_a_max_gpus=1)
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(replicas=4, gpu_per_replica=2),
                headers=_auth(),
            )

        assert response.status_code == 429, response.text
        assert response.headers.get("X-Quota-Enforcement") == "active"

    @pytest.mark.asyncio
    async def test_enforcement_is_not_claimed_where_no_decision_was_made(self, client):
        """A path that makes no quota decision must not advertise enforcement.

        This is the defect in its own right: the header used to be set on every
        response, so anyone checking whether enforcement was on got "yes" from a service
        that was not enforcing anything. A false signal is worse than no signal, because
        the gap cannot then be noticed from the system's own output.
        """
        response = await client.get("/health")

        assert "X-Quota-Enforcement" not in response.headers, (
            "a path with no quota decision still advertises enforcement"
        )

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


# ---------------------------------------------------------------------------
# Finding f-f27c7448: the caller chose the namespace and the delete target.
# ---------------------------------------------------------------------------


class TestNamespaceConfinement:
    """f-f27c7448-7885-4cf7-abf5-4b89f96260a3 — server owns namespace and target."""

    @pytest.mark.asyncio
    async def test_the_namespace_does_not_vary_with_anything_the_caller_sends(
        self, client
    ):
        """Vary every caller-controlled field; the namespace handed to K8s is constant.

        Asserted on what reached the cluster client, not the response body, because the
        body echoes the fake and would agree either way.
        """
        await _seed()
        seen = set()

        variations = [
            {},
            {"replicas": 2},
            {"model_name": "mistralai/Mistral-7B-v0.1"},
            {"precision": "bf16", "serving_framework": "sglang"},
        ]
        for index, extra in enumerate(variations):
            apps = _FakeAppsApi()
            with _patch_clients(apps):
                response = await client.post(
                    f"/workspaces/{WS_A}/deployments",
                    json=_body(name=f"dep-{index}", **extra),
                    headers=_auth(),
                )
            assert response.status_code == 201, response.text
            seen.update(namespace for namespace, _ in apps.created)

        assert seen == {NS_A}, f"namespace varied with caller input: {seen}"

    @pytest.mark.asyncio
    async def test_supplying_a_namespace_is_rejected_rather_than_ignored(self, client):
        """A caller-supplied namespace is a 422, not a silently dropped field.

        Accepting-and-discarding it would leave automation believing it still selects
        the namespace, and the request would keep looking like it worked as intended.
        """
        await _seed()
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(namespace="kube-system"),
                headers=_auth(),
            )

        assert response.status_code == 422, response.text
        assert apps.mutations == []

    @pytest.mark.asyncio
    async def test_two_workspaces_sharing_a_cluster_land_in_separate_namespaces(
        self, client
    ):
        """The shared-cluster case: byte-identical requests, different namespaces."""
        await _seed()
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            for workspace_id in (WS_A, WS_B):
                response = await client.post(
                    f"/workspaces/{workspace_id}/deployments",
                    json=_body(name="same-name"),
                    headers=_auth(),
                )
                assert response.status_code == 201, response.text

        assert [namespace for namespace, _ in apps.created] == [NS_A, NS_B]

    @pytest.mark.asyncio
    async def test_an_unresolvable_namespace_fails_closed(self, client):
        """A workspace whose recorded namespace is a platform namespace is refused.

        Never served out of a shared fallback — that fallback IS the exposure, so the
        safe outcome here is no deployment at all.
        """
        await _seed(ws_a_namespace="kube-system")
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments", json=_body(), headers=_auth()
            )

        assert response.status_code == 500, response.text
        assert apps.mutations == []
        assert "kube-system" in response.text, (
            "the refusal does not name the namespace an operator has to fix"
        )

    @pytest.mark.asyncio
    async def test_a_workspace_with_no_recorded_namespace_never_uses_default(
        self, client
    ):
        """A legacy row with no namespace gets its derived one, not ``default``.

        Failing closed must not strand rows written before the namespace was recorded,
        and the fallback must not be the shared namespace every tenant can reach.
        """
        await _seed(ws_a_namespace=None)
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments", json=_body(), headers=_auth()
            )

        assert response.status_code == 201, response.text
        assert [namespace for namespace, _ in apps.created] == [f"ws-{WS_A}"]

    @pytest.mark.asyncio
    async def test_created_objects_carry_the_workspace_ownership_label(self, client):
        """Ownership is stamped at creation — replace and delete read it back."""
        await _seed()
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments", json=_body(), headers=_auth()
            )
        assert response.status_code == 201, response.text

        _, manifest = apps.created[0]
        assert manifest["metadata"]["labels"][OWNER_LABEL] == str(WS_A)
        assert manifest["metadata"]["namespace"] == NS_A


class TestDeletionConfinement:
    """Deletion reaches only what this workspace owns."""

    @pytest.mark.asyncio
    async def test_a_raw_kubernetes_name_is_no_longer_an_accepted_target(self, client):
        """Naming a raw cluster object is refused by the path type, before any lookup.

        The delete route used to accept a Kubernetes object name as well as a deployment
        id, which combined with a caller-supplied namespace let a caller remove a
        neighbour's workload.
        """
        await _seed()
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            response = await client.delete(
                f"/workspaces/{WS_A}/deployments/victim-deployment", headers=_auth()
            )

        assert response.status_code == 422, response.text
        assert apps.mutations == []

    @pytest.mark.asyncio
    async def test_another_workspace_s_deployment_id_is_refused(self, client):
        """A deployment owned by workspace B is a 404 for workspace A, not a delete."""
        await _seed()
        b_dep = uuid.uuid4()
        await _seed_deployment(
            b_dep, workspace_id=WS_B, name="b-workload", namespace=NS_B
        )

        apps = _FakeAppsApi(existing_owner=str(WS_B))
        with _patch_clients(apps):
            response = await client.delete(
                f"/workspaces/{WS_A}/deployments/{b_dep}", headers=_auth()
            )

        assert response.status_code == 404, response.text
        assert apps.mutations == [], "deleted another workspace's workload"

    @pytest.mark.asyncio
    async def test_delete_uses_the_namespace_recorded_at_creation(self, client):
        """Delete targets where the object is, not where current config would put it."""
        await _seed()
        dep_id = uuid.uuid4()
        await _seed_deployment(
            dep_id, workspace_id=WS_A, name="a-workload", namespace=NS_A
        )

        apps = _FakeAppsApi(existing_owner=str(WS_A))
        with _patch_clients(apps):
            response = await client.delete(
                f"/workspaces/{WS_A}/deployments/{dep_id}", headers=_auth()
            )

        assert response.status_code == 200, response.text
        assert apps.deleted == [(NS_A, "a-workload")]

    @pytest.mark.asyncio
    async def test_an_object_owned_by_another_workspace_is_not_deleted(self, client):
        """Right namespace, wrong owner label: still refused.

        Being in the namespace is not proof of ownership — on a shared cluster a
        namespace can hold an object the platform did not create for this workspace.
        """
        await _seed()
        dep_id = uuid.uuid4()
        await _seed_deployment(
            dep_id, workspace_id=WS_A, name="contested", namespace=NS_A
        )

        apps = _FakeAppsApi(existing_owner=str(WS_B))  # owned by the neighbour
        with _patch_clients(apps):
            response = await client.delete(
                f"/workspaces/{WS_A}/deployments/{dep_id}", headers=_auth()
            )

        assert response.status_code == 409, response.text
        assert apps.deleted == []

    @pytest.mark.asyncio
    async def test_an_unlabelled_object_is_not_deleted(self, client):
        """A missing owner label is treated as NOT owned.

        Adopting unlabelled objects would make anything the platform did not create
        removable by whichever workspace names it — the defect in a lenient disguise.
        """
        await _seed()
        dep_id = uuid.uuid4()
        await _seed_deployment(
            dep_id, workspace_id=WS_A, name="unlabelled", namespace=NS_A
        )

        apps = _FakeAppsApi(existing_owner=_UNLABELLED)
        with _patch_clients(apps):
            response = await client.delete(
                f"/workspaces/{WS_A}/deployments/{dep_id}", headers=_auth()
            )

        assert response.status_code == 409, response.text
        assert apps.deleted == []


class TestReplaceConfinement:
    """The conflict-then-replace path must not overwrite a neighbour's workload."""

    @pytest.mark.asyncio
    async def test_a_conflict_with_another_workspace_s_object_is_refused(self, client):
        """Create collides with someone else's object -> reported, not resolved."""
        await _seed()
        apps = _FakeAppsApi(conflict_owner=str(WS_B))

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(name="contested"),
                headers=_auth(),
            )

        assert response.status_code == 409, response.text
        assert apps.replaced == [], "overwrote another workspace's workload"

    @pytest.mark.asyncio
    async def test_a_conflict_with_the_workspace_s_own_object_is_replaced(self, client):
        """Re-applying your own deployment still works — the fix is not a lockout."""
        await _seed()
        apps = _FakeAppsApi(conflict_owner=str(WS_A))

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(name="mine"),
                headers=_auth(),
            )

        assert response.status_code == 201, response.text
        assert apps.replaced == [(NS_A, "mine")]

    @pytest.mark.asyncio
    async def test_an_unlabelled_object_is_not_replaced(self, client):
        await _seed()
        apps = _FakeAppsApi(conflict_owner=str(WS_B), existing_owner=_UNLABELLED)

        with _patch_clients(apps):
            response = await client.post(
                f"/workspaces/{WS_A}/deployments",
                json=_body(name="adopted"),
                headers=_auth(),
            )

        assert response.status_code == 409, response.text
        assert apps.replaced == []


class TestListingConfinement:
    """Listing is scoped to the workspace's own namespace and ownership label."""

    @pytest.mark.asyncio
    async def test_listing_cannot_be_pointed_at_another_namespace(self, client):
        """The removed `namespace` query parameter has no effect if still supplied."""
        await _seed()
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            response = await client.get(
                f"/workspaces/{WS_A}/deployments?namespace={NS_B}", headers=_auth()
            )

        assert response.status_code == 200, response.text
        namespaces = [namespace for namespace, _ in apps.reads]
        assert namespaces == [NS_A], (
            f"listed outside the workspace's namespace: {apps.reads}"
        )

    @pytest.mark.asyncio
    async def test_listing_is_scoped_to_the_workspace_owner_label(self, client):
        """Namespace scoping alone is not enough — the selector carries ownership too."""
        await _seed()
        apps = _FakeAppsApi()

        with _patch_clients(apps):
            await client.get(f"/workspaces/{WS_A}/deployments", headers=_auth())

        _, selector = apps.reads[0]
        assert f"{OWNER_LABEL}={WS_A}" in selector, selector


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
    def test_a_blank_namespace_resolves_to_the_derived_name(self, blank):
        """Whitespace is treated as absent, so it cannot resolve to an empty target."""
        from app.services.workspace_namespace import resolve_workspace_namespace

        assert resolve_workspace_namespace(self._workspace(blank)) == f"ws-{WS_A}"

    def test_resolution_is_deterministic(self):
        """A create and a later delete must resolve to the same place."""
        from app.services.workspace_namespace import resolve_workspace_namespace

        workspace = self._workspace(None)
        assert resolve_workspace_namespace(workspace) == resolve_workspace_namespace(
            workspace
        )

    def test_the_derived_name_matches_the_migration_backfill_rule(self):
        """The resolver and migration 018 must agree, or runtime and data diverge.

        Migration 018 backfills ``'ws-' || id``. If this helper's rule changed without
        the migration, already-backfilled workspaces would resolve to a namespace their
        workloads are not in, and deletes would silently miss.
        """
        from app.services.workspace_namespace import derive_namespace_name

        assert derive_namespace_name(WS_A) == f"ws-{WS_A}"
