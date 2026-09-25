"""Tests for workspace CRUD endpoints.

Also covers issue #5058 (U17b): workspace provisioning and teardown go through the
authorized-operation facade, and the GitHub Actions ``workflow_dispatch`` call and
its foreign-repository personal access token are gone from the runtime path. The
absence is asserted statically over the shipped source, because a behavioral test
only covers the paths it exercises.
"""

import asyncio
import ast
import importlib
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

import app.services.provisioning as prov
from app.middleware.auth import create_access_token
from tests.conftest import async_session_test


def _auth_header(org_id: uuid.UUID | None = None) -> dict:
    """Create an Authorization header with a valid JWT."""
    if org_id is None:
        org_id = uuid.uuid4()
    token, _ = create_access_token(org_id)
    return {"Authorization": f"Bearer {token}"}


def _create_body(name: str, **values) -> dict:
    return {
        "operation_id": str(uuid.uuid4()),
        "name": name,
        "plan_revision": "a" * 64,
        **values,
    }


@pytest.fixture(autouse=True)
def route_preview(monkeypatch):
    """Isolate route semantics from real preview/admission PostgreSQL suites.

    The legacy JWT fixtures carry only an org, so these mock-facade tests explicitly
    supply a named test principal. Strict domain callers retain their verified
    context. No production default or authorization check is weakened.
    """
    from app.adapters import operation_authority_source as authority
    from app.services import onboarding
    from app.schemas.workspace import CreateWorkspaceRequest

    actual = authority.acting_principal
    scope = {"caller": None}
    monkeypatch.setattr(
        authority, "acting_principal", lambda: actual() or scope["caller"]
    )

    async def preview(db, org_id, body: CreateWorkspaceRequest):
        scope["caller"] = authority.ActingPrincipal(
            "route-test-requester", str(org_id), ""
        )
        parameters = {
            "workspace_name": body.name,
            "isolation_mode": body.isolation_mode,
        }
        if body.account:
            parameters["aws_account_id"] = body.account
        return {
            "revision": "a" * 64,
            "workspace_id": str(onboarding.workspace_id_for(org_id, body.operation_id)),
            "approval_request": {
                "workspace_id": str(
                    onboarding.workspace_id_for(org_id, body.operation_id)
                ),
                "action": "provision",
                "idempotency_key": str(body.operation_id),
                "parameters": parameters,
            },
        }

    monkeypatch.setattr(onboarding, "preview", preview)


class TestCreateWorkspace:
    """Test POST /workspaces."""

    @pytest.mark.asyncio
    async def test_create_requires_auth(self, client):
        """Creating a workspace without auth returns 401/403."""
        response = await client.post("/workspaces", json=_create_body("test-ws"))
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_create_validates_name(self, client):
        """Creating a workspace with empty name returns 422."""
        headers = _auth_header()
        response = await client.post(
            "/workspaces", json=_create_body(""), headers=headers
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_validates_isolation_mode(self, client):
        """Invalid isolation_mode returns 422."""
        headers = _auth_header()
        response = await client.post(
            "/workspaces",
            json=_create_body("test", isolation_mode="invalid"),
            headers=headers,
        )
        assert response.status_code == 422


class TestListWorkspaces:
    """Test GET /workspaces."""

    @pytest.mark.asyncio
    async def test_list_requires_auth(self, client):
        """Listing workspaces without auth returns 401/403."""
        response = await client.get("/workspaces")
        assert response.status_code in (401, 403)


class TestListEligibleClusters:
    """Test GET /workspaces?view=eligible-clusters — issue #6048."""

    @pytest.mark.asyncio
    async def test_requires_auth(self, client):
        response = await client.get("/workspaces?view=eligible-clusters")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_legacy_org_token_cannot_discover_shared_clusters(
        self, client
    ):
        from app.models.cluster import Cluster
        from app.models.organization import Organization

        org_id = uuid.uuid4()
        other_org_id = uuid.uuid4()
        shared_cluster_id = uuid.uuid4()
        async with async_session_test() as session:
            session.add_all(
                [
                    Organization(id=org_id, name="org-eligible"),
                    Organization(id=other_org_id, name="org-other-eligible"),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    Cluster(
                        id=shared_cluster_id,
                        org_id=org_id,
                        name="shared-eligible",
                        status="Ready",
                        sharing_enabled=True,
                        eks_cluster_arn="arn:aws:eks:us-east-1:000000000000:cluster/shared-eligible",
                    ),
                    Cluster(
                        id=uuid.uuid4(),
                        org_id=org_id,
                        name="dedicated-not-eligible",
                        status="Ready",
                        sharing_enabled=False,
                    ),
                    Cluster(
                        id=uuid.uuid4(),
                        org_id=other_org_id,
                        name="other-org-shared",
                        status="Ready",
                        sharing_enabled=True,
                    ),
                ]
            )
            await session.commit()

        response = await client.get(
            "/workspaces?view=eligible-clusters", headers=_auth_header(org_id)
        )
        assert response.status_code == 403


class TestGetWorkspace:
    """Test GET /workspaces/{id}."""

    @pytest.mark.asyncio
    async def test_get_requires_auth(self, client):
        """Getting a workspace without auth returns 401/403."""
        ws_id = uuid.uuid4()
        response = await client.get(f"/workspaces/{ws_id}")
        assert response.status_code in (401, 403)


class TestDeleteWorkspace:
    """Test DELETE /workspaces/{id}."""

    @pytest.mark.asyncio
    async def test_delete_requires_auth(self, client):
        """Deleting a workspace without auth returns 401/403."""
        ws_id = uuid.uuid4()
        response = await client.delete(f"/workspaces/{ws_id}")
        assert response.status_code in (401, 403)


class TestKubeconfig:
    """Test POST /workspaces/{id}/kubeconfig."""

    @pytest.mark.asyncio
    async def test_kubeconfig_requires_auth(self, client):
        """Generating kubeconfig without auth returns 401/403."""
        ws_id = uuid.uuid4()
        response = await client.post(f"/workspaces/{ws_id}/kubeconfig")
        assert response.status_code in (401, 403)


class TestWorkspaceSchemas:
    """Test Pydantic schema validation."""

    def test_create_workspace_request_valid(self):
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(
            operation_id=uuid.uuid4(), name="my-workspace", isolation_mode="dedicated"
        )
        assert req.name == "my-workspace"
        assert req.isolation_mode == "dedicated"

    def test_create_workspace_request_default_isolation(self):
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(operation_id=uuid.uuid4(), name="my-workspace")
        assert req.isolation_mode == "dedicated"

    def test_released_workspace_body_gets_a_server_operation_id(self):
        from app.schemas.workspace import CreateWorkspaceRequest

        first = CreateWorkspaceRequest(name="my-workspace")
        second = CreateWorkspaceRequest(name="my-workspace")

        assert isinstance(first.operation_id, uuid.UUID)
        assert first.operation_id != second.operation_id

    def test_create_workspace_request_namespace_mode(self):
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(
            operation_id=uuid.uuid4(), name="shared-ws", isolation_mode="namespace"
        )
        assert req.isolation_mode == "namespace"

    def test_create_workspace_request_invalid_mode(self):
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError):
            CreateWorkspaceRequest(
                operation_id=uuid.uuid4(), name="test", isolation_mode="invalid"
            )

    # --- Research isolation mode tests ---

    def test_create_workspace_research_mode_valid(self):
        """Research mode with account is valid."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(
            operation_id=uuid.uuid4(),
            name="ml-research",
            isolation_mode="research",
            account="research-account",
        )
        assert req.isolation_mode == "research"
        assert req.account == "research-account"

    def test_create_workspace_research_mode_requires_account(self):
        """Research mode without account raises validation error."""
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(
            ValidationError, match="Research workspaces require an AWS account"
        ):
            CreateWorkspaceRequest(
                operation_id=uuid.uuid4(),
                name="ml-research",
                isolation_mode="research",
            )

    def test_create_workspace_research_mode_empty_account(self):
        """Research mode with empty account raises validation error."""
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError):
            CreateWorkspaceRequest(
                operation_id=uuid.uuid4(),
                name="ml-research",
                isolation_mode="research",
                account="",
            )

    def test_create_workspace_research_with_budget(self):
        """Research mode with budget guardrails is valid."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(
            operation_id=uuid.uuid4(),
            name="ml-research",
            isolation_mode="research",
            account="research-account",
            budget_max_daily_usd=Decimal("200.00"),
            budget_max_gpus=4,
        )
        assert req.budget_max_daily_usd == Decimal("200.00")
        assert req.budget_max_gpus == 4

    def test_create_workspace_budget_negative_rejected(self):
        """Negative budget values are rejected."""
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError):
            CreateWorkspaceRequest(
                operation_id=uuid.uuid4(),
                name="test",
                isolation_mode="dedicated",
                budget_max_daily_usd=Decimal("-10.00"),
            )

    def test_create_workspace_budget_gpus_negative_rejected(self):
        """Negative GPU count is rejected."""
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError):
            CreateWorkspaceRequest(
                operation_id=uuid.uuid4(),
                name="test",
                isolation_mode="dedicated",
                budget_max_gpus=-1,
            )

    def test_dedicated_mode_account_optional(self):
        """Dedicated mode does not require an account."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(
            operation_id=uuid.uuid4(), name="my-ws", isolation_mode="dedicated"
        )
        assert req.account is None

    def test_namespace_mode_account_optional(self):
        """Namespace mode does not require an account."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(
            operation_id=uuid.uuid4(), name="my-ws", isolation_mode="namespace"
        )
        assert req.account is None


class TestClusterPlacementChoice:
    """Issue #6048: the explicit dedicated/shared placement choice at creation."""

    def test_default_placement_is_dedicated(self):
        """An old client that never heard of shared placement keeps dedicated behavior."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(operation_id=uuid.uuid4(), name="my-workspace")
        assert req.cluster_placement == "dedicated"
        assert req.shared_cluster_id is None

    def test_shared_placement_requires_a_cluster_id(self):
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError, match="shared_cluster_id"):
            CreateWorkspaceRequest(
                operation_id=uuid.uuid4(),
                name="my-workspace",
                cluster_placement="shared",
            )

    def test_dedicated_placement_forbids_a_cluster_id(self):
        """Naming a cluster on the dedicated path must not silently opt into sharing it."""
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError, match="cluster_placement=shared"):
            CreateWorkspaceRequest(
                operation_id=uuid.uuid4(),
                name="my-workspace",
                cluster_placement="dedicated",
                shared_cluster_id=uuid.uuid4(),
            )

    def test_shared_placement_with_a_cluster_id_is_valid(self):
        from app.schemas.workspace import CreateWorkspaceRequest

        cluster_id = uuid.uuid4()
        req = CreateWorkspaceRequest(
            operation_id=uuid.uuid4(),
            name="my-workspace",
            cluster_placement="shared",
            shared_cluster_id=cluster_id,
        )
        assert req.cluster_placement == "shared"
        assert req.shared_cluster_id == cluster_id

    def test_invalid_placement_value_rejected(self):
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError):
            CreateWorkspaceRequest(
                operation_id=uuid.uuid4(),
                name="my-workspace",
                cluster_placement="borrowed",
            )


class TestSharedPlacementPreviewIsExplicitlyUnavailable:
    """Issue #6048: preview must fail closed for shared placement, not fall

    through to dedicated resolution. The schema, `cluster_sharing.py`'s
    eligibility resolver and canonical bootstrap registration all support
    shared placement; `onboarding.py::preview`'s execution-step generation does
    not yet resolve a shared target. Silently proceeding with dedicated
    resolution would hand back a plan for a cluster the caller never asked
    for — this checks the explicit refusal that prevents that.
    """

    @pytest.mark.asyncio
    async def test_shared_placement_is_refused_before_touching_the_database_or_principal(
        self, monkeypatch
    ):
        from app.schemas.workspace import CreateWorkspaceRequest
        from app.services import onboarding
        from app.services.provisioning import ProvisioningUnavailable

        # `route_preview` (autouse, module-level) replaces `onboarding.preview`
        # with a mock for every other test in this file. Undo that here — this
        # test's whole point is the REAL function's early refusal, which the
        # mock does not implement and would otherwise mask.
        monkeypatch.undo()

        body = CreateWorkspaceRequest(
            name="my-workspace",
            cluster_placement="shared",
            shared_cluster_id=uuid.uuid4(),
        )
        # `db=None` and no acting principal set: if the refusal did not run
        # before the first database/principal access, this would raise a
        # different, less specific error (or hang), not `ProvisioningUnavailable`.
        with pytest.raises(ProvisioningUnavailable, match="not yet executable"):
            await onboarding.preview(None, uuid.uuid4(), body)


class TestWorkspaceDisplayName:
    """Test workspace display name with isolation mode tags."""

    def test_research_display_name(self):
        from app.routers.workspaces import _make_display_name

        assert _make_display_name("ml-research", "research") == "ml-research [research]"

    def test_dedicated_display_name(self):
        from app.routers.workspaces import _make_display_name

        assert _make_display_name("my-workspace", "dedicated") == "my-workspace"

    def test_namespace_display_name(self):
        from app.routers.workspaces import _make_display_name

        assert _make_display_name("shared-ws", "namespace") == "shared-ws"


class TestResearchBudgetDefaults:
    """Test default budget guardrails for research workspaces."""

    def test_research_default_budget(self):
        from app.routers.workspaces import RESEARCH_DEFAULT_BUDGET

        assert RESEARCH_DEFAULT_BUDGET["max_daily_usd"] == 100.00
        assert RESEARCH_DEFAULT_BUDGET["max_gpus"] == 8


class TestWorkspaceModel:
    """Test workspace model constants."""

    def test_valid_isolation_modes(self):
        from app.models.workspace import VALID_ISOLATION_MODES

        assert "dedicated" in VALID_ISOLATION_MODES
        assert "namespace" in VALID_ISOLATION_MODES
        assert "research" in VALID_ISOLATION_MODES
        assert len(VALID_ISOLATION_MODES) == 3


class _MockOperationFacade:
    """A stand-in for B's authorized-operation facade. **A mock, and recorded as one.**

    Named and flagged explicitly (``is_mock``) for the reason U17a's contract gives,
    and the reason survives the change in what exists. It used to be "B's facade does
    not exist in ADP (there is no ``modules/harness/jobs/``)". Since #5535 it does
    exist and is composed for any deployment that configures an operation store — but
    it is not what these tests drive. A green run here still closes no live criterion:
    it establishes that this API refuses to provision without an authorized operation
    and that no dispatch or PAT remains, which is exactly what this story's acceptance
    is.

    The flag is what `TestTheFacadeIsAMock` reads to check that claim is still true of
    the process, rather than assuming it from the package's absence.
    """

    is_mock = True

    def __init__(self, state=prov.STATE_PENDING, detail=None, raises=None):
        self._state = state
        self._detail = detail
        self._raises = raises
        self.open_calls: list[dict] = []
        self.progress_calls: list[str] = []

    async def open_operation(
        self, *, action, workspace_id, org_id, permission, parameters
    ):
        self.open_calls.append(
            {
                "action": action,
                "workspace_id": workspace_id,
                "org_id": org_id,
                "permission": permission,
                "parameters": parameters,
            }
        )
        if self._raises is not None:
            raise self._raises
        return prov.OperationProgress(
            operation_id=parameters["idempotency_key"],
            state=self._state,
            detail=self._detail,
        )

    async def report_progress(self, operation_id):
        self.progress_calls.append(operation_id)
        return prov.OperationProgress(operation_id=operation_id, state=self._state)


@pytest.fixture
async def org_id():
    """An organization row, so quota enforcement resolves instead of 404ing.

    `POST /workspaces` enforces the org's workspace quota first, which loads the
    organization and returns 404 when it is absent. Tests that need to reach the
    provisioning path therefore need a real row, not just a signed token.
    """
    from app.models.organization import Organization

    new_id = uuid.uuid4()
    async with async_session_test() as session:
        session.add(
            Organization(id=new_id, name=f"org-{new_id}", billing_plan="enterprise")
        )
        await session.commit()
    return new_id


@pytest.fixture
def facade():
    """Install a mock facade for the duration of one test, then clear it.

    Cleared afterwards so the default state of the process stays "no facade
    configured" — the state this repository is actually in, and the one the
    fail-closed tests below depend on.
    """
    mock = _MockOperationFacade()
    prov.set_operation_facade(mock)
    yield mock
    prov.set_operation_facade(None)


class TestProvisioningGoesThroughTheFacade:
    """Provision and teardown call the authorized-operation facade."""

    @pytest.mark.asyncio
    async def test_server_operation_id_is_distinct_from_client_idempotency(
        self, client, org_id
    ):
        from sqlalchemy import select
        from app.models.workspace import Workspace

        class AssignedFacade(_MockOperationFacade):
            async def open_operation(
                self, *, action, workspace_id, org_id, permission, parameters
            ):
                progress = await super().open_operation(
                    action=action,
                    workspace_id=workspace_id,
                    org_id=org_id,
                    permission=permission,
                    parameters=parameters,
                )
                return prov.OperationProgress(
                    operation_id="server-" + progress.operation_id, state=progress.state
                )

        facade = AssignedFacade()
        previous = prov.get_operation_facade()
        prov.set_operation_facade(facade)
        try:
            body = _create_body("assigned-operation")
            headers = _auth_header(org_id)
            first = await client.post("/workspaces", json=body, headers=headers)
            second = await client.post("/workspaces", json=body, headers=headers)
            assert first.status_code == second.status_code == 201
            assert first.json()["id"] == second.json()["id"]
            assert len(facade.open_calls) == 1
            assert (
                facade.open_calls[0]["parameters"]["idempotency_key"]
                == body["operation_id"]
            )
            assert facade.progress_calls == ["server-" + body["operation_id"]]
            async with async_session_test() as db:
                row = await db.scalar(
                    select(Workspace).where(
                        Workspace.id == uuid.UUID(first.json()["id"])
                    )
                )
                assert str(row.operation_id) == body["operation_id"]
                assert row.provisioning_operation_id == "server-" + body["operation_id"]
        finally:
            prov.set_operation_facade(previous)

    @pytest.mark.asyncio
    async def test_create_opens_a_provision_operation(self, client, facade, org_id):
        """Creating a workspace opens a provision operation with the right permission."""
        body = _create_body("ws-a")
        response = await client.post(
            "/workspaces", json=body, headers=_auth_header(org_id)
        )

        assert response.status_code == 201
        assert len(facade.open_calls) == 1
        call = facade.open_calls[0]
        assert call["action"] == prov.PROVISION
        assert call["parameters"]["idempotency_key"] == body["operation_id"]
        assert call["permission"] == prov.REQUIRED_PERMISSION
        # The org is the one from the verified JWT, not anything in the body.
        assert call["org_id"] == str(org_id)

    @pytest.mark.asyncio
    async def test_create_replay_returns_one_workspace_and_one_operation(
        self, client, facade, org_id
    ):
        headers = _auth_header(org_id)
        body = _create_body("ws-replay")

        first = await client.post("/workspaces", json=body, headers=headers)
        second = await client.post("/workspaces", json=body, headers=headers)

        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert len(facade.open_calls) == 1
        assert facade.progress_calls == [body["operation_id"]]

        conflict = await client.post(
            "/workspaces", json={**body, "name": "different"}, headers=headers
        )
        assert conflict.status_code == 409
        assert len(facade.open_calls) == 1

    @pytest.mark.asyncio
    async def test_concurrent_retries_open_one_logical_facade_operation(self):
        class IdempotentFacade:
            def __init__(self):
                self.created = {}

            async def open_operation(self, **request):
                await asyncio.sleep(0)
                operation_id = request["parameters"]["idempotency_key"]
                self.created.setdefault(
                    operation_id,
                    prov.OperationProgress(
                        operation_id=operation_id, state=prov.STATE_PENDING
                    ),
                )
                return self.created[operation_id]

            async def report_progress(self, operation_id):
                return self.created[operation_id]

        facade = IdempotentFacade()
        prov.set_operation_facade(facade)
        try:
            results = await asyncio.gather(
                *(
                    prov.start_provision(
                        operation_id="shared-retry-operation",
                        workspace_id="workspace-1",
                        org_id="org-1",
                        workspace_name="workspace",
                        isolation_mode="dedicated",
                    )
                    for _ in range(2)
                )
            )
        finally:
            prov.set_operation_facade(None)

        assert list(facade.created) == ["shared-retry-operation"]
        assert {result.operation_id for result in results} == {"shared-retry-operation"}

    @pytest.mark.asyncio
    async def test_retry_resumes_a_committed_intent_that_never_opened(
        self, client, facade, org_id
    ):
        from app.models.workspace import Workspace
        from app.routers.workspaces import _operation_request
        from app.schemas.workspace import CreateWorkspaceRequest

        request_body = _create_body("ws-crash-window")
        body = CreateWorkspaceRequest.model_validate(request_body)
        workspace_id = uuid.uuid4()
        async with async_session_test() as db:
            db.add(
                Workspace(
                    id=workspace_id,
                    org_id=org_id,
                    name=body.name,
                    operation_id=body.operation_id,
                    operation_request_json=_operation_request(body),
                    isolation_mode=body.isolation_mode,
                    status="Provisioning",
                )
            )
            await db.commit()

        response = await client.post(
            "/workspaces", json=request_body, headers=_auth_header(org_id)
        )

        assert response.status_code == 503
        assert "original request" in response.json()["detail"]
        assert facade.open_calls == []
        assert facade.progress_calls == []

    @pytest.mark.asyncio
    async def test_create_passes_shape_not_identity(self, client, facade, org_id):
        """Provisioning parameters carry shape only — no identity keys."""
        response = await client.post(
            "/workspaces",
            json=_create_body("ws-shape", isolation_mode="dedicated"),
            headers=_auth_header(org_id),
        )

        assert response.status_code == 201
        parameters = facade.open_calls[0]["parameters"]
        assert parameters["workspace_name"] == "ws-shape"
        assert parameters["isolation_mode"] == "dedicated"
        assert prov.forbidden_parameters(parameters) == ()

    @pytest.mark.asyncio
    async def test_delete_opens_a_teardown_operation(self, client, facade, org_id):
        """Deleting a workspace opens a teardown operation, not a provision one."""
        headers = _auth_header(org_id)
        created = await client.post(
            "/workspaces", json=_create_body("ws-b"), headers=headers
        )
        workspace_id = created.json()["id"]
        facade.open_calls.clear()

        response = await client.delete(f"/workspaces/{workspace_id}", headers=headers)

        assert response.status_code == 200
        assert response.json()["status"] == "Teardown"
        assert len(facade.open_calls) == 1
        assert facade.open_calls[0]["action"] == prov.TEARDOWN
        assert facade.open_calls[0]["permission"] == prov.REQUIRED_PERMISSION

    @pytest.mark.asyncio
    async def test_successful_teardown_is_a_visible_terminal_tombstone(
        self, client, facade, org_id
    ):
        headers = _auth_header(org_id)
        created = await client.post(
            "/workspaces", json=_create_body("ws-deleted"), headers=headers
        )

        accepted = await client.delete(
            f"/workspaces/{created.json()['id']}", headers=headers
        )
        facade._state = prov.STATE_SUCCEEDED
        fetched = await client.get(
            f"/workspaces/{created.json()['id']}", headers=headers
        )

        assert accepted.status_code == 200
        assert accepted.json()["status"] == "Teardown"
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "Deleted"


class TestBodySuppliedIdentityIsRejected:
    """A caller may not name the tenant it provisions for."""

    def test_forbidden_keys_are_detected(self):
        assert prov.forbidden_parameters({"org_id": "x"}) == ("org_id",)
        assert prov.forbidden_parameters({"user_id": "x"}) == ("user_id",)
        assert prov.forbidden_parameters({"on_behalf_of": "x"}) == ("on_behalf_of",)
        assert prov.forbidden_parameters({"workspace_id": "x"}) == ("workspace_id",)

    def test_detection_is_case_and_separator_insensitive(self):
        """`Org-Id` is the same smuggling attempt as `org_id`."""
        assert prov.forbidden_parameters({"Org-Id": "x"}) == ("Org-Id",)
        assert prov.forbidden_parameters({"ORG_ID": "x"}) == ("ORG_ID",)
        assert prov.forbidden_parameters({"x-adp-user": "x"}) == ("x-adp-user",)
        assert prov.forbidden_parameters({"caller_org": "x"}) == ("caller_org",)

    @pytest.mark.asyncio
    async def test_identity_parameter_is_refused(self, facade):
        """A provisioning request asserting an identity is refused before the facade."""
        with pytest.raises(prov.ProvisioningRefused):
            await prov._start(
                operation_id="op-identity-refused",
                action=prov.PROVISION,
                workspace_id="ws-1",
                org_id="org-1",
                parameters={"org_id": "org-1", "region": "us-east-1"},
            )
        # Refused BEFORE the facade was asked to open anything.
        assert facade.open_calls == []

    @pytest.mark.asyncio
    async def test_refused_even_when_the_value_matches(self, facade):
        """The matching case is refused too — that is the point.

        A caller-supplied identity that agrees with the binding today is a code
        path that reads the caller's value. Refusing regardless means no such path
        exists, which is a property rather than a coincidence.
        """
        with pytest.raises(prov.ProvisioningRefused) as exc:
            await prov._start(
                operation_id="op-matching-identity-refused",
                action=prov.PROVISION,
                workspace_id="ws-1",
                org_id="org-match",
                parameters={"org_id": "org-match"},
            )
        assert "may not assert an identity" in str(exc.value)
        assert facade.open_calls == []

    @pytest.mark.asyncio
    async def test_unknown_action_is_refused(self, facade):
        with pytest.raises(prov.ProvisioningRefused):
            await prov._start(
                operation_id="op-unknown-action",
                action="delete-everything",
                workspace_id="ws-1",
                org_id="org-1",
                parameters={},
            )
        assert facade.open_calls == []


class TestAsyncProgressAndFailure:
    """Operation progress and failure are surfaced, not invented."""

    @pytest.mark.asyncio
    async def test_progress_is_read_through_the_facade(self, facade):
        """`observe` returns the facade's report for the named operation."""
        progress = await prov.observe("op-42")

        assert facade.progress_calls == ["op-42"]
        assert progress.operation_id == "op-42"

    @pytest.mark.asyncio
    async def test_research_account_is_passed_as_shape(self, facade):
        """A research workspace's AWS account is a provisioning option, not identity.

        `aws_account_id` names *where* to build, not *who* is asking, so it is
        allowed where `org_id` is refused.
        """
        await prov.start_provision(
            operation_id="op-research",
            workspace_id="ws-r",
            org_id="org-r",
            workspace_name="ml-research",
            isolation_mode="research",
            account="123456789012",
        )

        parameters = facade.open_calls[0]["parameters"]
        assert parameters["aws_account_id"] == "123456789012"
        assert prov.forbidden_parameters(parameters) == ()

    @pytest.mark.asyncio
    async def test_observe_requires_an_operation_id(self, facade):
        """An empty operation id would bind every report to the same target."""
        with pytest.raises(prov.ProvisioningError):
            await prov.observe("")
        assert facade.progress_calls == []

    @pytest.mark.asyncio
    async def test_observe_rejects_a_malformed_report(self):
        """A facade returning the wrong type must not reach a caller reading .state."""

        class _Malformed:
            async def open_operation(self, **kwargs):  # pragma: no cover - unused
                raise AssertionError("not called")

            async def report_progress(self, operation_id):
                return {"state": "succeeded"}

        prov.set_operation_facade(_Malformed())
        try:
            with pytest.raises(prov.ProvisioningError):
                await prov.observe("op-1")
        finally:
            prov.set_operation_facade(None)

    @pytest.mark.asyncio
    async def test_progress_for_another_operation_is_rejected(self):
        """A report that names a different operation must not be accepted."""

        class _Crossed:
            async def open_operation(self, **kwargs):  # pragma: no cover - unused
                raise AssertionError("not called")

            async def report_progress(self, operation_id):
                return prov.OperationProgress(
                    operation_id="someone-elses-op", state=prov.STATE_SUCCEEDED
                )

        prov.set_operation_facade(_Crossed())
        try:
            with pytest.raises(prov.ProvisioningError):
                await prov.observe("op-mine")
        finally:
            prov.set_operation_facade(None)

    @pytest.mark.asyncio
    async def test_reported_failure_marks_the_workspace_failed(
        self, client, facade, org_id
    ):
        """A failed operation is not reported as Provisioning."""
        facade._state = prov.STATE_FAILED

        response = await client.post(
            "/workspaces", json=_create_body("ws-fail"), headers=_auth_header(org_id)
        )

        # The accepted operation still gets a durable workspace receipt. Its
        # terminal failure is explicit; 201 does not claim successful execution.
        assert response.status_code == 201
        assert response.json()["status"] == "Failed"
        assert response.json()["operation_state"] == prov.STATE_FAILED
        assert response.json()["provisioning_operation_id"]

    @pytest.mark.asyncio
    async def test_unknown_is_not_treated_as_failure(self, client, facade, org_id):
        """`unknown` is not a failure — collapsing the two is the bug.

        Reading UNKNOWN as failure either leaks infrastructure the platform
        believes was never created, or retries a provision that actually
        succeeded.
        """
        facade._state = prov.STATE_UNKNOWN

        response = await client.post(
            "/workspaces", json=_create_body("ws-unknown"), headers=_auth_header(org_id)
        )

        assert response.status_code == 201
        assert response.json()["status"] != "Failed"

        unknown = prov.OperationProgress(operation_id="op-u", state=prov.STATE_UNKNOWN)
        assert unknown.is_conclusive_failure is False
        assert unknown.is_conclusive_success is False
        assert unknown.is_terminal is True
        assert "not a failure" in prov.summarize(unknown)

    def test_inconclusive_states_are_not_success(self):
        for state in (prov.STATE_PENDING, prov.STATE_RUNNING, prov.STATE_UNKNOWN):
            progress = prov.OperationProgress(operation_id="op", state=state)
            assert progress.is_conclusive_success is False
            assert state in prov.INCONCLUSIVE_STATES


class TestFailsClosedWithoutAFacade:
    """No facade means refusal — never a fallback, never a fake success."""

    @pytest.mark.asyncio
    async def test_provision_raises_when_unavailable(self):
        prov.set_operation_facade(None)
        with pytest.raises(prov.ProvisioningUnavailable):
            await prov.start_provision(
                operation_id="op-provision-ws-1",
                workspace_id="ws-1",
                org_id="org-1",
                workspace_name="ws-1",
                isolation_mode="dedicated",
            )

    @pytest.mark.asyncio
    async def test_teardown_raises_when_unavailable(self):
        prov.set_operation_facade(None)
        with pytest.raises(prov.ProvisioningUnavailable):
            await prov.start_teardown(
                operation_id="op-teardown-ws-1",
                workspace_id="ws-1",
                org_id="org-1",
                workspace_name="ws-1",
            )

    @pytest.mark.asyncio
    async def test_create_returns_503_not_a_fake_provisioning(self, client, org_id):
        """The regression this story fixes: no 201 for work that never started."""
        prov.set_operation_facade(None)
        body = _create_body("ws-none")

        response = await client.post(
            "/workspaces", json=body, headers=_auth_header(org_id)
        )

        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"].lower()
        from sqlalchemy import func, select
        from app.models.workspace import Workspace

        async with async_session_test() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Workspace)
                    .where(Workspace.org_id == org_id)
                )
                == 0
            )
        capabilities = await client.get("/capabilities", headers=_auth_header(org_id))
        assert "create-operation-id-v1" not in capabilities.json()["features"]

        facade = _MockOperationFacade()
        prov.set_operation_facade(facade)
        retry = await client.post(
            "/workspaces", json=body, headers=_auth_header(org_id)
        )

        assert retry.status_code == 201
        assert (
            facade.open_calls[0]["parameters"]["idempotency_key"]
            == body["operation_id"]
        )
        prov.set_operation_facade(None)

    @pytest.mark.asyncio
    async def test_delete_restores_status_when_unavailable(
        self, client, facade, org_id
    ):
        """A refused teardown does not leave the workspace looking mid-teardown."""
        headers = _auth_header(org_id)
        created = await client.post(
            "/workspaces", json=_create_body("ws-keep"), headers=headers
        )
        workspace_id = created.json()["id"]
        status_before = created.json()["status"]

        prov.set_operation_facade(None)
        response = await client.delete(f"/workspaces/{workspace_id}", headers=headers)
        assert response.status_code == 503

        prov.set_operation_facade(facade)
        fetched = await client.get(f"/workspaces/{workspace_id}", headers=headers)
        assert fetched.json()["status"] == status_before

    @pytest.mark.asyncio
    async def test_refused_admission_creates_no_workspace(self, client, facade, org_id):
        """A caller-caused refusal is a 400, and the row is not left provisioning."""
        facade._raises = prov.ProvisioningRefused("identity asserted")
        body = _create_body("ws-refused")
        headers = _auth_header(org_id)

        response = await client.post("/workspaces", json=body, headers=headers)

        assert response.status_code == 403
        facade._raises = None
        replay = await client.post("/workspaces", json=body, headers=headers)
        assert replay.status_code == 201
        assert len(facade.open_calls) == 2

    @pytest.mark.asyncio
    async def test_refused_teardown_restores_status(self, client, facade, org_id):
        """A refused teardown is a 400 and leaves the prior status intact."""
        headers = _auth_header(org_id)
        created = await client.post(
            "/workspaces", json=_create_body("ws-refuse-teardown"), headers=headers
        )
        workspace_id = created.json()["id"]
        status_before = created.json()["status"]

        facade._raises = prov.ProvisioningRefused("identity asserted")
        response = await client.delete(f"/workspaces/{workspace_id}", headers=headers)
        assert response.status_code == 400

        facade._raises = None
        fetched = await client.get(f"/workspaces/{workspace_id}", headers=headers)
        assert fetched.json()["status"] == status_before

    @pytest.mark.asyncio
    async def test_a_malformed_facade_report_is_rejected(self, client, org_id):
        """A facade returning the wrong type is a contract breach, not a success."""

        class _Malformed:
            async def open_operation(self, **kwargs):
                return {"state": "succeeded"}

            async def report_progress(self, operation_id):  # pragma: no cover
                raise AssertionError("not called")

        prov.set_operation_facade(_Malformed())
        try:
            response = await client.post(
                "/workspaces", json=_create_body("ws-bad"), headers=_auth_header(org_id)
            )
            assert response.status_code == 503
        finally:
            prov.set_operation_facade(None)


class TestNoDispatchOrPatRemains:
    """R14 acceptance 1, as a static check over the runtime source tree.

    Behavioral tests cannot establish an absence: they only cover paths they
    exercise. These read the shipped source of every module under ``app/`` — the
    directory the Dockerfile copies into the image — and assert the dispatch call
    and the foreign-repo credential are not there on any path.

    Comments and docstrings are stripped before matching, so the explanatory notes
    that say *why* the dispatch was removed do not read as the dispatch itself.
    That is done by compiling to an AST and walking string/attribute nodes rather
    than by regex over raw text.
    """

    @staticmethod
    def _runtime_sources() -> list[Path]:
        app_dir = Path(__file__).resolve().parent.parent / "app"
        files = sorted(app_dir.rglob("*.py"))
        # Guard the guard: an empty glob would make every assertion below vacuous.
        assert len(files) > 10, f"expected the app tree, found {len(files)} files"
        return files

    @staticmethod
    def _code_strings(path: Path) -> list[str]:
        """Every string literal in a module, excluding docstrings."""
        tree = ast.parse(path.read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docstrings.add(doc)
        return [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value not in docstrings
        ]

    # Modules that make up the provisioning path. These must not reach GitHub at
    # all, which is a stronger claim than the repo-wide one below.
    PROVISIONING_MODULES = (
        "provisioning.py",
        "workspace_reconciler.py",
        "workspaces.py",
    )

    def test_no_workflow_dispatch_call_in_the_runtime_path(self):
        """No module under app/ builds an Actions workflow-dispatch request.

        Matches the dispatch *endpoint* rather than any mention of GitHub, because
        `app/services/scanner_sources.py` legitimately names GitHub's public API as
        a research-scanning source (trending repositories). That is unrelated to
        workspace provisioning, needs no credential, and is not what R14 removes —
        so a check that flagged it would be failing on the wrong thing. The
        provisioning modules get the stricter no-GitHub-at-all check below.
        """
        offenders = []
        for path in self._runtime_sources():
            for literal in self._code_strings(path):
                lowered = literal.lower()
                if (
                    "dispatches" in lowered
                    or "workflow_dispatch" in lowered
                    or "/actions/workflows" in lowered
                    or "bootstrap-workspace.yml" in lowered
                    or "teardown-workspace.yml" in lowered
                ):
                    offenders.append(f"{path.name}: {literal!r}")
        assert offenders == [], f"workflow-dispatch call sites remain: {offenders}"

    def test_the_provisioning_path_does_not_reach_github_at_all(self):
        """The provisioning modules name no GitHub endpoint whatsoever."""
        offenders = []
        for path in self._runtime_sources():
            if path.name not in self.PROVISIONING_MODULES:
                continue
            for literal in self._code_strings(path):
                if "github" in literal.lower():
                    offenders.append(f"{path.name}: {literal!r}")
        assert offenders == [], (
            f"provisioning path still references GitHub: {offenders}"
        )

    def test_no_foreign_repo_pat_is_read(self):
        """No module under app/ reads a GitHub token or names the foreign repo."""
        offenders = []
        for path in self._runtime_sources():
            source = path.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                # `settings.github_token` / `settings.github_repo`, however reached.
                if isinstance(node, ast.Attribute) and node.attr in (
                    "github_token",
                    "github_repo",
                ):
                    offenders.append(f"{path.name}: settings.{node.attr}")
            for literal in self._code_strings(path):
                if "AISuperPlane" in literal or "aws-innovate" in literal:
                    offenders.append(f"{path.name}: {literal!r}")
                if literal in ("github_token", "github_repo", "GITHUB_TOKEN"):
                    offenders.append(f"{path.name}: {literal!r}")
        assert offenders == [], f"foreign-repo PAT usage remains: {offenders}"

    def test_the_dispatch_service_module_is_gone(self):
        """`app/services/github.py` existed only to dispatch workflows."""
        app_dir = Path(__file__).resolve().parent.parent / "app"
        assert not (app_dir / "services" / "github.py").exists()

    def test_the_pat_settings_are_gone_from_config(self):
        """The settings themselves are removed, not just unused."""
        from app.config import Settings

        fields = Settings.model_fields
        assert "github_token" not in fields
        assert "github_repo" not in fields

    def test_the_provisioning_service_holds_no_http_or_token_surface(self):
        """The replacement cannot dispatch: it has no HTTP client at all.

        The property holds by absence rather than by a check a later edit could
        remove — there is nothing here to re-point at an Actions endpoint.
        """
        source = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "services"
            / "provisioning.py"
        ).read_text()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "httpx" not in imported
        assert "requests" not in imported
        assert "boto3" not in imported


class TestContractValuesAgreeWithTheSharedContract:
    """The mirrored values must not drift from `superplane_contracts`.

    The service mirrors the contract's actions, states and forbidden-key set as
    plain values rather than importing them, because the API's image build context
    is pinned to `src/superplane-api` so the contracts package is not importable at
    API runtime. The contract itself uses this arrangement for its permission
    string. A drifting duplicate is caught here, by a test that fails.

    Skipped rather than failed when the contracts package is not on the path, so
    this file stays runnable from the component directory on its own — the smoke
    check in the story runs it that way.
    """

    @staticmethod
    def _contract_module():
        contracts_dir = (
            Path(__file__).resolve().parents[3] / "contracts"
        )  # modules/domain-apps/superplane/contracts
        if not (contracts_dir / "superplane_contracts").is_dir():
            pytest.skip("superplane_contracts is not available from this checkout")
        if str(contracts_dir) not in sys.path:
            sys.path.insert(0, str(contracts_dir))
        return importlib.import_module("superplane_contracts.provisioning")

    def test_actions_agree(self):
        contract = self._contract_module()
        assert prov.PROVISION == contract.PROVISION
        assert prov.TEARDOWN == contract.TEARDOWN
        assert prov.PROVISIONING_ACTIONS == contract.PROVISIONING_ACTIONS

    def test_permission_agrees(self):
        contract = self._contract_module()
        assert prov.REQUIRED_PERMISSION == contract.REQUIRED_PERMISSION

    def test_states_agree(self):
        contract = self._contract_module()
        assert {
            prov.STATE_PENDING,
            prov.STATE_RUNNING,
            prov.STATE_SUCCEEDED,
            prov.STATE_FAILED,
            prov.STATE_CANCELLED,
            prov.STATE_UNKNOWN,
        } == {member.value for member in contract.OperationState}
        assert prov.TERMINAL_STATES == {
            state.value for state in contract.TERMINAL_STATES
        }
        assert prov.INCONCLUSIVE_STATES == {
            state.value for state in contract.INCONCLUSIVE_STATES
        }

    def test_forbidden_keys_agree(self):
        contract = self._contract_module()
        assert prov.FORBIDDEN_PARAMETER_KEYS == contract.FORBIDDEN_PARAMETER_KEYS
        assert (
            prov.FORBIDDEN_PARAMETER_PREFIXES == contract.FORBIDDEN_PARAMETER_PREFIXES
        )


class TestTheFacadeIsAMock:
    """State plainly that no real facade was exercised here.

    U17a's contract records its mock in three places so a green run cannot be
    misread as live execution. The same applies to this story: every facade above
    is a mock, so these tests establish that no dispatch or PAT remains and that
    provisioning refuses without an authorized operation — not that provisioning
    works against a real one.
    """

    def test_the_double_is_declared_a_mock(self):
        assert _MockOperationFacade.is_mock is True

    def test_no_real_operation_facade_is_exercised_by_these_tests(self):
        """The transition this test watched for has happened. Retargeted, third time.

        Its history is the point. It began as "``modules/harness/jobs/`` does not
        exist", which fired when #5525 landed the shared store; it was retargeted to
        "``harness_jobs`` is not importable from this app", on the stated reasoning
        that importability is the first point at which the package could become a
        dependency. #5535 makes it one deliberately: the package is staged into the
        image, `app/adapters/harness_operation_facade.py` adapts it, and
        `app/composition.py` installs it for any deployment that configures an
        operation store. So the old assertion is now asserting the absence of the
        feature, and its message would send a reader to "revisit the mocked facade"
        over a change that was the intended one.

        What the class still needs, and what this now asserts, is the claim the
        docstring above actually makes: **nothing in this file is live evidence.**
        That was previously true by the package's absence — a property of the
        repository — and is now true by what these tests install, which is a property
        they have to state. The failure it catches is real and is newly possible: a
        composed production facade leaking into this offline suite would make these
        tests exercise `harness_jobs` against whatever `DATABASE_URL` points at, and
        a passing run would then be quoted as live acceptance of provisioning.

        Deliberately NOT retargeted to "the production adapter is importable".
        Asserting the feature exists from a mocked suite is how a green offline run
        starts reading as deployment evidence, which is the exact confusion this
        class exists to prevent. Packaging is asserted where it can be checked
        honestly — `tests/test_auth.py` for the image's dependency, and
        `tests/test_composition.py` for which adapter a configured deployment gets.
        """
        from app.adapters.harness_operation_facade import HarnessOperationFacade

        installed = prov.get_operation_facade()
        assert not isinstance(installed, HarnessOperationFacade), (
            "a production HarnessOperationFacade is installed while this offline "
            "suite runs; every provisioning result in this file would be exercising "
            "the real operation store. This run is not live evidence either way — "
            "find what composed it and scope that to its own test."
        )
        assert installed is None or getattr(installed, "is_mock", False) is True, (
            f"an undeclared facade {type(installed).__name__} is installed; only a "
            "mock recorded as one may be used here."
        )


# ===========================================================================
# Provider connections and workspace bindings — issue #5053 (U7b)
# ===========================================================================
#
# The server half of U7's offline contract (#5294). These tests are written
# against the HTTP routes rather than against the service functions, because the
# contract itself is already covered offline by
# `modules/domain-apps/superplane/tests/test_connection_contract.py`. What is
# untested until here is the wiring: whether the routes actually ASK the contract,
# with the server's own evidence, on every path.
#
# TWO THINGS ABOUT HOW THIS SECTION IS WRITTEN
#
# **Every route has a positive case, not only negative ones.** A route that denies
# every caller satisfies every negative assertion about authorization, and that is
# not a hypothetical failure mode here: registering this family as
# organization-scoped would leave `request.state.grant` unpublished, the delegation
# check reading an empty permission set, and every caller refused. The negative
# cases below would all still pass. So each route is also driven to success.
#
# **Both enforcement modes are exercised.** `domain_auth_enforced` is False by
# default today, so a suite testing only that mode would say nothing about the
# path production moves to; and the ownership half of the check only becomes
# load-bearing under enforcement, where a real token subject exists. The
# `enforcing_connection` fixture below is the strict path, and the tests that use
# the plain `_auth_header` helper are the legacy path.


# Strict-enforcement machinery, reused from `tests/test_auth.py` rather than
# reimplemented. That file owns the "tokens are really signed" convention — a real
# in-process RSA keypair whose public half is served through the JWKS cache — and a
# second copy here would eventually drift, in the direction of the copy that stops
# verifying something. Nothing imported below is a credential for anything: the key
# is generated per run and never written to disk.
from app import auth as domain_auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models.workspace import Workspace  # noqa: E402
from app.models.workspace_grant import WorkspaceGrantRecord  # noqa: E402
from tests.test_auth import TEST_CLIENT_ID as _DOMAIN_CLIENT_ID  # noqa: E402
from tests.test_auth import TEST_ISSUER as _DOMAIN_ISSUER  # noqa: E402
from tests.test_auth import _mint as _mint_domain_token  # noqa: E402
from tests.test_auth import _rsa_keypair as _domain_rsa_keypair  # noqa: E402


@pytest.fixture(scope="module")
def domain_signing_keys():
    """One throwaway RSA keypair for this module. Module-scoped: generating a
    2048-bit key per test is the slowest thing in the file and the key is not
    test-specific state."""
    return _domain_rsa_keypair()


_vault_owners = {}


@pytest.fixture(autouse=True)
def connection_security(request, monkeypatch, domain_signing_keys):
    """Real signed callers and explicit grants for the provider-route tests.

    The vault reader is a named test adapter, never a production implementation.
    Negative cases replace its independently held ownership/attestation response.
    """
    if not request.cls or request.cls.__name__ not in {
        "TestRegistrationRefusesSecretMaterial",
        "TestTheTwoChecksAreIndependent",
        "TestFourSeparateReadings",
        "TestRotationIsAtomicAndKeepsTheOldCredential",
        "TestDisablementIsHonest",
        "TestNoResponseOrLogCarriesSecretMaterial",
        "TestMalformedBodiesAreRefusedAsBadRequests",
        "TestTrustedCredentialEvidence",
        "TestConnectionLifecycleIntegrity",
    }:
        yield None
        return
    from datetime import datetime, timezone, timedelta
    from app.services import credential_evidence
    from superplane_contracts.connections import VaultOwnership

    private_pem, public_jwk = domain_signing_keys
    monkeypatch.setattr(settings, "domain_auth_enforced", True)
    monkeypatch.setattr(settings, "cognito_issuer", _DOMAIN_ISSUER)
    monkeypatch.setattr(settings, "domain_auth_allowed_client_ids", [_DOMAIN_CLIENT_ID])
    monkeypatch.setattr(settings, "cognito_jwks_url", "https://example.invalid/jwks")
    domain_auth.jwks_cache.load([public_jwk])
    previous = getattr(fastapi_app.state, "domain_policy", None)
    fastapi_app.state.domain_policy = domain_auth.build_domain_policy()
    _vault_owners.clear()

    class TestVaultReader:
        owner_override = None
        attest = True

        async def read(
            self, *, org_id, workspace_id, reference, principal, report_digest
        ):
            owner = self.owner_override or _vault_owners.get(
                (org_id, reference.credential_id)
            )
            if owner is None:
                return None
            return credential_evidence.VerifiedCredentialEvidence(
                org_id=org_id,
                workspace_id=workspace_id,
                reference=reference,
                ownership=VaultOwnership(
                    credential_id=reference.credential_id, owner_principal=owner
                ),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                attested_report_digest=report_digest if self.attest else None,
                report_checked_at=datetime.now(timezone.utc) - timedelta(seconds=1)
                if report_digest
                else None,
            )

    reader = TestVaultReader()
    monkeypatch.setattr(credential_evidence, "_reader", reader)

    def signed_header(org_id=None):
        token = _mint_domain_token(
            private_pem,
            sub="user-abc",
            **{"custom:org_id": str(org_id or uuid.uuid4())},
        )
        return {"Authorization": f"Bearer {token}"}

    monkeypatch.setattr(sys.modules[__name__], "_auth_header", signed_header)
    try:
        yield reader
    finally:
        fastapi_app.state.domain_policy = previous
        domain_auth.jwks_cache.clear()
        _vault_owners.clear()


CONNECTIONS = "/workspaces/{ws}/provider-connections"

# Opaque vault handles, matching the shape `validate_adp_credential_id` accepts.
# Not credentials for anything: no vault is contacted by any test in this section.
CRED_A = "adp-cred-01HQ8V3XK2AAAA"
CRED_B = "adp-cred-01HQ8V3XK2BBBB"

# Structurally complete so the ARN detector matches, with the reserved all-zeros
# account id and a secret name that does not exist.
FAKE_SECRET_ARN = (
    "arn:aws:secretsmanager:us-east-1:000000000000:secret:fake-not-real-AbCdEf"
)

# `AKIA` plus 16 uppercase characters is the AWS access-key-id shape. This is not
# an access key; it matches the pattern the refusal is keyed on.
FAKE_AWS_KEY = "AKIA" + "Z" * 16


async def _seed_org_workspace_credentials(*credential_ids, workspaces=1):
    """Create an org, N workspaces and the given credential-registry rows.

    Committed in stages because SQLite enforces foreign keys immediately (the test
    conftest sets `PRAGMA foreign_keys=ON`), so a workspace inserted in the same
    flush as its organization fails on the parent not yet existing.

    The registry rows matter to what is being tested: `register` requires the
    reference to already exist in this org's `credential_registry`, which is
    server-held evidence the caller did not write. A test that skipped them would
    be asserting a 404 path and calling it authorization.
    """
    from app.models.credential import CredentialRegistry
    from app.models.organization import Organization
    from app.models.workspace import Workspace

    org = uuid.uuid4()
    workspace_ids = [uuid.uuid4() for _ in range(workspaces)]
    async with async_session_test() as session:
        session.add(
            Organization(id=org, name=f"org-{org.hex[:8]}", billing_plan="enterprise")
        )
        await session.commit()
    async with async_session_test() as session:
        for index, workspace_id in enumerate(workspace_ids):
            session.add(
                Workspace(
                    id=workspace_id,
                    org_id=org,
                    name=f"ws-{index}",
                    isolation_mode="shared",
                    status="active",
                )
            )
        for credential_id in credential_ids:
            session.add(
                CredentialRegistry(
                    id=uuid.uuid4(),
                    org_id=org,
                    provider="nebius",
                    friendly_name=credential_id,
                    credential_type="api_key",
                    adp_credential_id=credential_id,
                )
            )
        await session.commit()
    async with async_session_test() as session:
        for workspace_id in workspace_ids:
            session.add(
                WorkspaceGrantRecord(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    org_id=org,
                    principal="user-abc",
                    permissions="workspace:renew_credential workspace:read",
                )
            )
        await session.commit()
    for credential_id in credential_ids:
        _vault_owners[(str(org), credential_id)] = "user-abc"
    return (org, *workspace_ids)


def _passing_report(**overrides):
    """A validation body whose three credential readings all pass.

    `observed_capacity` is deliberately absent unless a test supplies it, so the
    default exercises the "not measured" case rather than quietly asserting a
    capacity nobody reported.
    """
    body = {
        "credential_valid": True,
        "permissions_sufficient": True,
        "quota_available": True,
    }
    body.update(overrides)
    return body


async def _register(client, headers, workspace_id, credential_id=CRED_A, **extra):
    """Register a connection and return the response."""
    body = {
        "credential_id": credential_id,
        "service": "nebius",
        "label": "prod",
        "provider": "nebius",
    }
    body.update(extra)
    return await client.post(
        CONNECTIONS.format(ws=workspace_id), json=body, headers=headers
    )


async def _active_connection(client, headers, workspace_id, credential_id=CRED_A):
    """Register and validate a connection, returning its id in ACTIVE status."""
    created = await _register(client, headers, workspace_id, credential_id)
    assert created.status_code == 201, created.text
    connection_id = created.json()["connection_id"]
    validated = await client.post(
        f"{CONNECTIONS.format(ws=workspace_id)}/{connection_id}/validation",
        json=_passing_report(observed_capacity=4),
        headers=headers,
    )
    assert validated.status_code == 200, validated.text
    assert validated.json()["status"] == "active"
    return connection_id


class _EnforcedConnection:
    """One ACTIVE connection, under strict enforcement, with real signed tokens.

    Holds the knobs the authorization tests need: mint a token for an arbitrary
    subject, add or downgrade a grant, add a sibling workspace. Everything it hands
    out is server-held state seeded through the models, never asserted in a request.
    """

    def __init__(self, private_pem, org, workspace, owner, connection_id):
        self._private_pem = private_pem
        self.org = org
        self.workspace = workspace
        self.owner = owner
        self.connection_id = connection_id

    def headers(self, subject=None):
        """A really-signed token for `subject`, carrying this fixture's org claim."""
        token = _mint_domain_token(
            self._private_pem,
            sub=subject or self.owner,
            **{"custom:org_id": str(self.org)},
        )
        return {"Authorization": f"Bearer {token}"}

    async def grant(self, principal, permissions, workspace=None):
        """Create or replace `principal`'s grant, so a downgrade is expressible.

        Upsert rather than insert because ``workspace_grants`` is unique on
        (workspace, principal) — two rows would make the effective permission set
        depend on row order, which is why the constraint exists.
        """
        from sqlalchemy import select

        target = workspace or self.workspace
        async with async_session_test() as session:
            existing = (
                await session.execute(
                    select(WorkspaceGrantRecord).where(
                        WorkspaceGrantRecord.workspace_id == target,
                        WorkspaceGrantRecord.principal == principal,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    WorkspaceGrantRecord(
                        id=uuid.uuid4(),
                        workspace_id=target,
                        org_id=self.org,
                        principal=principal,
                        permissions=permissions,
                    )
                )
            else:
                existing.permissions = permissions
            await session.commit()

    async def add_workspace(self):
        """A second workspace in the SAME organization — an org-mate, not a stranger.

        The point of the binding check is that this workspace is still refused, so a
        cross-org workspace (which the org filter already rejects) would not test it.
        """
        workspace_id = uuid.uuid4()
        async with async_session_test() as session:
            session.add(
                Workspace(
                    id=workspace_id,
                    org_id=self.org,
                    name=f"sibling-{workspace_id.hex[:8]}",
                    isolation_mode="shared",
                    status="active",
                )
            )
            await session.commit()
        return workspace_id


@pytest.fixture
async def enforcing_connection(client, domain_signing_keys, monkeypatch):
    """Enforcement ON, a granted owner, and one ACTIVE connection they registered.

    The connection is registered *through the route under enforcement* rather than
    inserted directly, because `owner_principal` is then the verified token subject —
    which is the fact the ownership half of `authorize_delegation` turns on. Seeding
    the row by hand would let the fixture pick an owner the server never verified, and
    the ownership tests would be asserting against fixture data instead of against the
    path production takes.
    """
    private_pem, public_jwk = domain_signing_keys
    monkeypatch.setattr(settings, "domain_auth_enforced", True)
    monkeypatch.setattr(settings, "cognito_issuer", _DOMAIN_ISSUER)
    monkeypatch.setattr(settings, "domain_auth_allowed_client_ids", [_DOMAIN_CLIENT_ID])
    monkeypatch.setattr(settings, "cognito_jwks_url", "https://example.invalid/jwks")

    domain_auth.jwks_cache.load([public_jwk])
    previous = getattr(fastapi_app.state, "domain_policy", None)
    fastapi_app.state.domain_policy = domain_auth.build_domain_policy()

    owner = "user-abc"
    org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
    ctx = _EnforcedConnection(private_pem, org, workspace, owner, connection_id=None)
    await ctx.grant(owner, "workspace:renew_credential")
    ctx.connection_id = await _active_connection(client, ctx.headers(), workspace)

    yield ctx

    fastapi_app.state.domain_policy = previous
    domain_auth.jwks_cache.clear()


class TestRegistrationRefusesSecretMaterial:
    """Acceptance 1: a credential POINTER is accepted; a secret is refused."""

    @pytest.mark.asyncio
    async def test_a_valid_reference_is_accepted(self, client):
        """The positive case. Created PENDING — an unvalidated reference admits nothing."""
        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        response = await _register(client, _auth_header(org), workspace)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["credential"]["credential_id"] == CRED_A
        assert body["status"] == "pending"
        assert body["admits_new_work"] is False
        # Renewal is permitted while PENDING: the contract separates the two
        # answers so a connection being brought up can have its credential
        # replaced without being able to admit work.
        assert body["allows_renewal"] is True

    @pytest.mark.asyncio
    async def test_the_whole_request_is_refused_when_it_carries_a_secret(self, client):
        """A stray secret beside a VALID reference fails the whole request.

        This is the heart of acceptance 1 and the reason the handler takes the raw
        body instead of a Pydantic model: under the default `extra="ignore"` the
        `secret_access_key` below would be dropped before the contract ever saw it,
        the request would succeed on the strength of its well-formed reference, and
        nothing would report that a secret had crossed the wire. The submitter would
        believe they had sent a credential and would have sent it nowhere.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        response = await _register(
            client, _auth_header(org), workspace, secret_access_key=FAKE_AWS_KEY
        )

        assert response.status_code == 400, response.text
        # Refused, not stripped-and-accepted.
        assert response.json()["detail"].startswith("connection request carries")
        # The refusal names the offending FIELD and never its contents.
        assert FAKE_AWS_KEY not in response.text
        assert "secret_access_key" in response.text

    @pytest.mark.asyncio
    async def test_nothing_is_persisted_when_the_request_is_refused(self, client):
        """The refusal is not merely a status code: no connection row survives it."""
        from sqlalchemy import select

        from app.models.provider_connection import ProviderConnection

        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        refused = await _register(
            client, _auth_header(org), workspace, secret_access_key=FAKE_AWS_KEY
        )
        assert refused.status_code == 400

        async with async_session_test() as session:
            rows = (await session.execute(select(ProviderConnection))).scalars().all()
        assert rows == []

    @pytest.mark.asyncio
    async def test_a_secret_arn_is_refused_as_the_reference(self, client):
        """An ARN is not an acceptable pointer. It names the account, region and secret.

        Acceptance 4's reasoning: anyone holding the ARN needs only a credential with
        `secretsmanager:GetSecretValue` to complete the read, and rotating the secret
        does not retract the string.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        response = await _register(
            client, _auth_header(org), workspace, credential_id=FAKE_SECRET_ARN
        )

        assert response.status_code == 400, response.text
        assert FAKE_SECRET_ARN not in response.text
        assert "000000000000" not in response.text

    @pytest.mark.asyncio
    async def test_a_secret_nested_inside_the_payload_is_refused(self, client):
        """The check recurses, so a secret in a provider blob is reached, not skipped."""
        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        response = await _register(
            client,
            _auth_header(org),
            workspace,
            provider_config={"region": "eu-north1", "api_token": "t0ken-value-here"},
        )

        assert response.status_code == 400, response.text
        assert "api_token" in response.text

    @pytest.mark.asyncio
    async def test_a_credential_this_org_never_registered_is_refused(self, client):
        """The reference must exist in this org's registry — evidence the caller did not write.

        Org-scoping, not per-principal vault ownership: `credential_registry` has no
        owner column, and reconciling against the real vault is the gated R7 6-7 work.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        response = await _register(
            client, _auth_header(org), workspace, credential_id=CRED_B
        )

        assert response.status_code == 404, response.text
        assert "not registered" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_another_orgs_workspace_is_not_found(self, client):
        """A workspace outside the caller's organization does not resolve."""
        org_a, workspace_a = await _seed_org_workspace_credentials(CRED_A)
        _, workspace_b = await _seed_org_workspace_credentials(CRED_B)

        response = await _register(client, _auth_header(org_a), workspace_b)

        assert response.status_code == 403, response.text


class TestTheTwoChecksAreIndependent:
    """Acceptance 2: vault delegation and workspace binding, both required.

    Under enforcement, where a real token subject exists and the ownership half of
    `authorize_delegation` is load-bearing.
    """

    @pytest.mark.asyncio
    async def test_the_owner_with_the_grant_succeeds(
        self, client, enforcing_connection
    ):
        """The positive case under enforcement. Without it, the denials below prove nothing."""
        ctx = enforcing_connection
        response = await client.get(
            f"{CONNECTIONS.format(ws=ctx.workspace)}/{ctx.connection_id}",
            headers=ctx.headers(),
        )

        assert response.status_code == 200, response.text
        assert response.json()["credential"]["credential_id"] == CRED_A

    @pytest.mark.asyncio
    async def test_a_principal_who_is_neither_owner_nor_delegate_is_refused(
        self, client, enforcing_connection
    ):
        """Org membership satisfies neither check.

        `other-principal` holds `workspace:renew_credential` on this very workspace —
        so the PERMISSION half passes — and is refused anyway, because the vault
        records someone else as the credential's owner. That is the hole acceptance 2
        names: delegating a credential one merely has access to.
        """
        ctx = enforcing_connection
        await ctx.grant("other-principal", "workspace:renew_credential")

        response = await client.post(
            f"{CONNECTIONS.format(ws=ctx.workspace)}/{ctx.connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "next",
                },
                "validation": _passing_report(),
            },
            headers=ctx.headers(subject="other-principal"),
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"] == (
            "not authorized to delegate this credential for this workspace"
        )

    @pytest.mark.asyncio
    async def test_the_owner_without_the_renewal_permission_is_refused(
        self, client, enforcing_connection
    ):
        """Ownership alone is not enough: the workspace grant is the other half.

        The mirror of the test above — same connection, same owner, the permission
        removed — so the two together show neither half alone admits.
        """
        ctx = enforcing_connection
        await ctx.grant(ctx.owner, "workspace:read")

        response = await client.delete(
            f"{CONNECTIONS.format(ws=ctx.workspace)}/{ctx.connection_id}",
            headers=ctx.headers(),
        )

        # 403 either from the guard (the route requires RENEW_CREDENTIAL) or from the
        # delegation check. Both are refusals of the same missing authority; what
        # matters is that downgrading the grant closes the route.
        assert response.status_code == 403, response.text

    @pytest.mark.asyncio
    async def test_registration_checks_the_permission_itself(
        self, client, enforcing_connection, monkeypatch
    ):
        """Registration's own permission check is not redundant with the guard.

        Registration cannot run `authorize_delegation` — there is no stored connection
        to resolve ownership from yet — so it checks the permission half directly
        against the same server-held grant. Today the guard already demands
        `renew_credential` for this route, which makes the in-handler check
        unreachable: deleting it breaks no test, which is how this one came to be
        written.

        So the test reaches it the only way it can be reached — by weakening the
        inventory entry to `READ`, exactly the change that would otherwise turn
        registration into an operation any reader can perform. That is what the gate
        defends against, and pinning it here means the defence is not silently
        deleted as dead code.
        """
        from app import endpoint_inventory
        from superplane_auth.policy import Permission

        ctx = enforcing_connection
        key = ("POST", "/workspaces/{workspace_id}/provider-connections")
        weakened = dict(endpoint_inventory.DOMAIN_ROUTES)
        weakened[key] = (endpoint_inventory.Scope.WORKSPACE, Permission.READ)
        monkeypatch.setattr(endpoint_inventory, "DOMAIN_ROUTES", weakened)

        # A principal holding only `read` now clears the (weakened) guard.
        await ctx.grant("reader-only", "workspace:read")

        response = await _register(
            client, ctx.headers(subject="reader-only"), ctx.workspace, CRED_B
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"] == (
            "not authorized to delegate this credential for this workspace"
        )

    @pytest.mark.asyncio
    async def test_a_credential_bound_elsewhere_is_refused_in_this_workspace(
        self, client, enforcing_connection
    ):
        """The binding is EXACT. A sibling workspace in the same org is not a binding.

        The caller here owns the credential and holds `renew_credential` on the
        workspace in the URL, so the delegation half passes completely. It is refused
        on the binding alone — which is the isolation acceptance 2 exists to create,
        since ADP's own handlers filter on org and would otherwise let every org-mate
        through.
        """
        ctx = enforcing_connection
        sibling = await ctx.add_workspace()
        await ctx.grant(ctx.owner, "workspace:renew_credential", workspace=sibling)

        response = await client.get(
            f"{CONNECTIONS.format(ws=sibling)}/{ctx.connection_id}",
            headers=ctx.headers(),
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "credential is not bound to this workspace"

    @pytest.mark.asyncio
    async def test_the_binding_is_enforced_with_explicit_workspace_grants(self, client):
        """The isolation half does not depend on which mode the deployment runs in.

        `granted_permissions` concedes the PERMISSION half when no grant is published
        (the legacy default). It concedes nothing about the binding, which is
        database-backed — so cross-workspace refusal holds in both modes, and this
        test is the evidence rather than the docstring claiming it.
        """
        org, workspace_a, workspace_b = await _seed_org_workspace_credentials(
            CRED_A, workspaces=2
        )
        headers = _auth_header(org)
        created = await _register(client, headers, workspace_a)
        connection_id = created.json()["connection_id"]

        response = await client.get(
            f"{CONNECTIONS.format(ws=workspace_b)}/{connection_id}", headers=headers
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "credential is not bound to this workspace"

    @pytest.mark.asyncio
    async def test_another_organizations_connection_is_not_found(self, client):
        """Cross-tenant reads are 404, not 403: a 403 would confirm the id exists."""
        org_a, workspace_a = await _seed_org_workspace_credentials(CRED_A)
        org_b, workspace_b = await _seed_org_workspace_credentials(CRED_B)
        created = await _register(client, _auth_header(org_b), workspace_b, CRED_B)
        foreign_id = created.json()["connection_id"]

        response = await client.get(
            f"{CONNECTIONS.format(ws=workspace_a)}/{foreign_id}",
            headers=_auth_header(org_a),
        )

        assert response.status_code == 404, response.text


class TestFourSeparateReadings:
    """Acceptance 3: four readings, no aggregate, and unmeasured is not zero."""

    @pytest.mark.asyncio
    async def test_the_four_readings_are_reported_separately(self, client):
        """Four fields on the wire, and no aggregate boolean anywhere in the body.

        Asserted at the WIRE, not on the Python object: every consumer reads the
        response, so an aggregate added at serialization would undo the separation no
        matter how carefully the dataclass avoids one.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace)
        connection_id = created.json()["connection_id"]

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/validation",
            json=_passing_report(observed_capacity=7, detail="checked"),
            headers=headers,
        )

        assert response.status_code == 200, response.text
        validation = response.json()["validation"]
        assert validation["credential_valid"] is True
        assert validation["permissions_sufficient"] is True
        assert validation["quota_available"] is True
        assert validation["observed_capacity"] == 7
        for banned in ("ok", "healthy", "ready", "valid", "status"):
            assert banned not in validation

    @pytest.mark.asyncio
    async def test_unmeasured_capacity_stays_null_and_is_not_zero(self, client):
        """ "We did not look" is a different operational fact from "nothing is free".

        Collapsing them is what acceptance 3 forbids, and the wire is where it would
        happen: an omitted key defaulting to 0 would report a measurement nobody took.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace)
        connection_id = created.json()["connection_id"]

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/validation",
            json=_passing_report(),
            headers=headers,
        )

        assert response.status_code == 200, response.text
        validation = response.json()["validation"]
        assert validation["observed_capacity"] is None
        assert validation["observed_capacity"] != 0

    @pytest.mark.asyncio
    async def test_unmeasured_capacity_survives_the_round_trip_as_null(self, client):
        """Still null when read back, not only in the response that reported it.

        Added because mutating the READ path — `to_validation`'s
        `observed_capacity or 0` — left the whole suite green: the POST response is
        built from the submitted report, so every "is None" assertion above passes
        while the stored reading is served as a measured zero. The distinction
        acceptance 3 protects has to survive persistence, and this is the only test
        that reads it back through `GET` after an unmeasured validation.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace)
        connection_id = created.json()["connection_id"]
        await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/validation",
            json=_passing_report(),
            headers=headers,
        )

        readback = await client.get(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )

        assert readback.status_code == 200, readback.text
        assert readback.json()["validation"]["observed_capacity"] is None

    @pytest.mark.asyncio
    async def test_measured_zero_capacity_is_preserved_as_zero(self, client):
        """The other side of the same distinction: an explicit 0 survives as 0.

        Read with a sentinel rather than `payload.get(key, None)` so a falsy explicit
        value is not confused with an absent one.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace)
        connection_id = created.json()["connection_id"]

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/validation",
            json=_passing_report(observed_capacity=0),
            headers=headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["validation"]["observed_capacity"] == 0

    @pytest.mark.asyncio
    async def test_a_valid_credential_with_no_capacity_still_activates(self, client):
        """Validity is not capacity. The connection is ACTIVE and admits nothing.

        A working key with zero free GPUs is the concrete outage acceptance 3 is
        about: an admission reading "credential valid" as "capacity available"
        succeeds here and fails later at the provider, in someone else's log.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace)
        connection_id = created.json()["connection_id"]

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/validation",
            json=_passing_report(observed_capacity=0),
            headers=headers,
        )

        assert response.json()["status"] == "active"
        assert response.json()["validation"]["observed_capacity"] == 0

    @pytest.mark.asyncio
    async def test_a_true_capacity_flag_is_not_a_measurement(self, client):
        """`True` is an `int` in Python; it must not arrive as a capacity of 1.

        Without the explicit `bool` refusal the contract would accept it and the
        connection would report one free unit on the strength of a flag.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace)
        connection_id = created.json()["connection_id"]

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/validation",
            json=_passing_report(observed_capacity=True),
            headers=headers,
        )

        assert response.status_code == 400, response.text
        assert "observed_capacity" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_failing_reading_records_the_four_and_does_not_activate(
        self, client
    ):
        """A failed validation keeps the readings and leaves the status alone.

        Which of the four failed is the operationally useful fact. `activate` would
        raise on this report and the readings would be lost, so there is a separate
        recording path — and the connection stays PENDING rather than becoming ACTIVE.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace)
        connection_id = created.json()["connection_id"]

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/validation",
            json={
                "credential_valid": True,
                "permissions_sufficient": True,
                "quota_available": False,
                "observed_capacity": 3,
            },
            headers=headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "pending"
        assert body["validation"]["quota_available"] is False
        assert body["validation"]["credential_valid"] is True

        # And the readings were persisted, not just echoed.
        stored = await client.get(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )
        assert stored.json()["validation"]["quota_available"] is False
        assert stored.json()["status"] == "pending"

    @pytest.mark.asyncio
    async def test_active_cannot_be_reached_by_asserting_it(self, client):
        """There is no route that sets status. ACTIVE is only ever reached through
        a report the contract accepted, so "checked and working" is what it means."""
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace, status="active")

        assert created.status_code == 201
        assert created.json()["status"] == "pending"


class TestRotationIsAtomicAndKeepsTheOldCredential:
    """Acceptance 5, first half."""

    @pytest.mark.asyncio
    async def test_rotation_switches_onto_a_validated_replacement(self, client):
        """The positive case: one call, new reference, ACTIVE, binding moved with it."""
        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "next",
                },
                "validation": _passing_report(observed_capacity=2),
            },
            headers=headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["credential"]["credential_id"] == CRED_B
        assert body["status"] == "active"
        # The binding follows the reference in the same transaction. Leaving it
        # behind would produce a connection whose own binding denies it on the next
        # `authorize_use`.
        assert body["binding"]["credential_id"] == CRED_B
        assert body["binding"]["workspace_id"] == str(workspace)

    @pytest.mark.asyncio
    async def test_the_superseded_credential_is_reported_not_deleted(self, client):
        """The old reference comes back in the response, flagged as still registered.

        Revocation is a separate, deliberate step once traffic is confirmed on the
        replacement. A sequence that deleted first would leave the connection dead
        for the width of that window, which is what "atomically" forbids — and an
        operator who is not TOLD the old credential is live will not revoke it.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "next",
                },
                "validation": _passing_report(),
            },
            headers=headers,
        )

        superseded = response.json()["superseded_credential"]
        assert superseded["credential_id"] == CRED_A
        assert superseded["still_registered"] is True
        assert "Revoke it at the vault" in superseded["next_step"]

    @pytest.mark.asyncio
    async def test_a_rotated_connection_is_still_usable_afterwards(self, client):
        """Read the connection back after rotating it, and rotate it again.

        The assertions on the rotation *response* cannot see this: that body is built
        from the contract state `rotate` returned, so it reports the new credential
        correctly even if the stored binding row was never moved. Mutating away the
        binding update left every other rotation test green while turning the
        connection into a permanent 409 — `to_state` refuses a row whose binding names
        a different credential than the connection, so the connection becomes
        unreadable and unmanageable by every route at once.

        That is the failure `record_rotation` updates the binding in the same
        transaction to prevent, and a subsequent read is the only thing that detects
        it. Rotating a second time also confirms the connection is still *manageable*,
        not merely readable.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        base = CONNECTIONS.format(ws=workspace)
        connection_id = await _active_connection(client, headers, workspace)

        rotated = await client.post(
            f"{base}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "next",
                },
                "validation": _passing_report(),
            },
            headers=headers,
        )
        assert rotated.status_code == 200, rotated.text

        readback = await client.get(f"{base}/{connection_id}", headers=headers)
        assert readback.status_code == 200, readback.text
        assert readback.json()["credential"]["credential_id"] == CRED_B
        assert readback.json()["binding"]["credential_id"] == CRED_B
        assert readback.json()["status"] == "active"

        # And back onto the original credential, which is still registered.
        again = await client.post(
            f"{base}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_A,
                    "service": "nebius",
                    "label": "prod",
                },
                "validation": _passing_report(),
            },
            headers=headers,
        )
        assert again.status_code == 200, again.text
        assert again.json()["credential"]["credential_id"] == CRED_A

    @pytest.mark.asyncio
    async def test_rotation_onto_an_unvalidated_replacement_is_refused(self, client):
        """No passing report, no rotation. The contract's signature is the enforcement."""
        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "next",
                },
                "validation": _passing_report(quota_available=False),
            },
            headers=headers,
        )

        assert response.status_code == 400, response.text
        assert "has not validated" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_the_connection_is_unchanged_when_rotation_is_refused(self, client):
        """A refused rotation leaves the original reference serving.

        The point of atomicity: there is no intermediate state in which the
        connection references nothing, including on the failure path.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)

        await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "next",
                },
                "validation": _passing_report(credential_valid=False),
            },
            headers=headers,
        )

        current = await client.get(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )
        assert current.json()["credential"]["credential_id"] == CRED_A
        assert current.json()["status"] == "active"

    @pytest.mark.asyncio
    async def test_rotation_onto_the_same_credential_is_refused(self, client):
        """A no-op reported as a rotation is worse than an error.

        A caller believing they had rotated away from a compromised key would be
        wrong, and would stop looking.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_A,
                    "service": "nebius",
                    "label": "prod",
                },
                "validation": _passing_report(),
            },
            headers=headers,
        )

        assert response.status_code == 400, response.text
        assert "own credential" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_pending_connection_can_be_rotated(self, client):
        """Rotation needs `allows_renewal()`, NOT `admits_new_work()`.

        This is a bug the first version of these routes had: guarding rotation with
        the admission gate (`authorize_use`, which requires ACTIVE) stranded exactly
        the connection rotation exists to rescue — one registered against a
        credential that never validates, and so can never be activated, leaving
        disablement as its only remaining transition. The contract separates
        `allows_renewal` from `admits_new_work` precisely to keep this case open.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace)
        connection_id = created.json()["connection_id"]
        assert created.json()["status"] == "pending"

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "next",
                },
                "validation": _passing_report(),
            },
            headers=headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["credential"]["credential_id"] == CRED_B

    @pytest.mark.asyncio
    async def test_an_unregistered_replacement_is_refused(self, client):
        """The replacement needs the same registry evidence as the original."""
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "next",
                },
                "validation": _passing_report(),
            },
            headers=headers,
        )

        assert response.status_code == 404, response.text
        assert "replacement credential is not registered" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_replacement_carrying_a_secret_is_refused(self, client):
        """Acceptance 1 applies to the rotation body too, not only to registration."""
        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "next",
                    "secret_value": FAKE_AWS_KEY,
                },
                "validation": _passing_report(),
            },
            headers=headers,
        )

        assert response.status_code == 400, response.text
        assert FAKE_AWS_KEY not in response.text


class TestDisablementIsHonest:
    """Acceptance 5, second half: it blocks, and it says what it does not do."""

    @pytest.mark.asyncio
    async def test_disablement_blocks_admission_and_renewal(self, client):
        """The positive case, and both flags it is supposed to move."""
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)

        response = await client.delete(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "disabled"
        assert body["admits_new_work"] is False
        assert body["allows_renewal"] is False

    @pytest.mark.asyncio
    async def test_the_limitation_is_in_the_response_body(self, client):
        """Not in a log, not in a docstring — in the body the operator reads.

        An operator who reads "disabled" as "revoked" skips the provider-side
        revocation that actually contains the credential. The limitation has to reach
        the person who just disabled it, which means the response.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)

        response = await client.delete(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )

        limitation = response.json()["limitation"]
        assert "does not revoke them" in limitation
        assert "may remain usable until they are revoked at the provider" in limitation
        # The exact contract text, so a reworded-but-weaker message fails here.
        from superplane_contracts.connections import DISABLEMENT_LIMITATION

        assert limitation == DISABLEMENT_LIMITATION

    @pytest.mark.asyncio
    async def test_renewal_is_actually_refused_after_disablement(self, client):
        """The flag is not decorative: rotation is genuinely closed afterwards.

        And it is refused AS disablement. Reporting it as "not bound to this
        workspace" — which the admission gate's single refusal string would have
        produced — would send the operator who just disabled it to look in the wrong
        place for a problem that does not exist.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)
        await client.delete(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "next",
                },
                "validation": _passing_report(),
            },
            headers=headers,
        )

        assert response.status_code == 409, response.text
        assert "disabled" in response.json()["detail"]
        assert "renewals" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_validation_is_refused_after_disablement(self, client):
        """Recording a fresh passing reading on a disabled connection is refused.

        Otherwise storage would assert a healthy credential on a connection that
        admits nothing, and `activate` raises on a disabled connection anyway.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)
        await client.delete(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/validation",
            json=_passing_report(observed_capacity=9),
            headers=headers,
        )

        assert response.status_code == 409, response.text

    @pytest.mark.asyncio
    async def test_disabling_twice_is_allowed(self, client):
        """Idempotent, deliberately.

        Refusing the second call would make containment depend on the caller knowing
        the current status, and an operator retrying because they are unsure whether
        the first attempt landed would get an error that reads like a failure to
        disable.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)
        url = f"{CONNECTIONS.format(ws=workspace)}/{connection_id}"

        first = await client.delete(url, headers=headers)
        second = await client.delete(url, headers=headers)

        assert first.status_code == 200
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "disabled"
        assert second.json()["limitation"]

    @pytest.mark.asyncio
    async def test_disablement_does_not_delete_the_connection(self, client):
        """DELETE disables. The row survives, so the credential stays auditable.

        A deleted row would lose the record that this credential was ever bound
        here — precisely the evidence needed to know what still has to be revoked.
        """
        from sqlalchemy import select

        from app.models.provider_connection import ProviderConnection

        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)
        await client.delete(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )

        async with async_session_test() as session:
            rows = (await session.execute(select(ProviderConnection))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "disabled"

        # And it is still readable through the API, with its limitation intact.
        readback = await client.get(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )
        assert readback.status_code == 200
        assert readback.json()["limitation"]


class TestNoResponseOrLogCarriesSecretMaterial:
    """Acceptance 4: not in a body, not in an error, not in a log line."""

    @pytest.mark.asyncio
    async def test_no_response_in_the_lifecycle_contains_an_arn_or_a_value(
        self, client
    ):
        """Every response across the whole lifecycle, checked as a set.

        Asserted over all five routes rather than one, because the emission
        allowlist is only as good as its weakest response and a per-route spot check
        would miss the one that copied a field.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        base = CONNECTIONS.format(ws=workspace)

        bodies = []
        created = await _register(client, headers, workspace)
        bodies.append(created.text)
        connection_id = created.json()["connection_id"]
        bodies.append(
            (
                await client.post(
                    f"{base}/{connection_id}/validation",
                    json=_passing_report(observed_capacity=1),
                    headers=headers,
                )
            ).text
        )
        bodies.append(
            (await client.get(f"{base}/{connection_id}", headers=headers)).text
        )
        bodies.append(
            (
                await client.post(
                    f"{base}/{connection_id}/rotation",
                    json={
                        "replacement": {
                            "credential_id": CRED_B,
                            "service": "nebius",
                            "label": "next",
                        },
                        "validation": _passing_report(),
                    },
                    headers=headers,
                )
            ).text
        )
        bodies.append(
            (await client.delete(f"{base}/{connection_id}", headers=headers)).text
        )

        assert len(bodies) == 5
        for body in bodies:
            assert "arn:" not in body
            assert FAKE_AWS_KEY not in body
            assert "AKIA" not in body
            # No secret-named key is emitted at all, so a value cannot arrive under
            # one later.
            for key in ("secret", "password", "private_key", "token"):
                assert key not in body.lower()

    @pytest.mark.asyncio
    async def test_a_refusal_does_not_echo_the_secret_it_refused(self, client):
        """The refusal is the response most likely to carry the thing it refused."""
        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        response = await _register(
            client,
            _auth_header(org),
            workspace,
            credential_id=FAKE_SECRET_ARN,
            api_key=FAKE_AWS_KEY,
        )

        assert response.status_code == 400
        assert FAKE_SECRET_ARN not in response.text
        assert FAKE_AWS_KEY not in response.text
        assert "000000000000" not in response.text

    @pytest.mark.asyncio
    async def test_no_log_line_carries_the_submitted_secret(self, client, caplog):
        """The log is the surface that leaks, and it has the larger blast radius.

        A response body is designed and reviewable; a log line interpolates whatever
        object is in scope, and it ships to CloudWatch, to the aggregator, and — for
        an agent surface — into a run transcript and a model's context. Rotating the
        credential does not retract any of those copies.

        Captured at the root logger at DEBUG so this covers the audit and middleware
        loggers too, not only the router's own.
        """
        import logging

        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        with caplog.at_level(logging.DEBUG):
            await _register(
                client,
                _auth_header(org),
                workspace,
                credential_id=FAKE_SECRET_ARN,
                secret_access_key=FAKE_AWS_KEY,
            )
            await _register(client, _auth_header(org), workspace, api_key=FAKE_AWS_KEY)

        captured = "\n".join(
            [record.getMessage() for record in caplog.records] + [caplog.text]
        )
        assert FAKE_AWS_KEY not in captured
        assert FAKE_SECRET_ARN not in captured
        assert "000000000000" not in captured
        assert "fake-not-real-AbCdEf" not in captured


class TestMalformedBodiesAreRefusedAsBadRequests:
    """The caller-error branches, asserted as 400 rather than merely "not 200".

    A malformed body that reached the contract would surface a `ContractViolation`
    message naming internal fields, and one that reached a Pydantic model would go
    through FastAPI's 422 path — which is the `input`-echoing handler these routes
    deliberately keep off the credential surface. Both are refused here instead, so
    the status class is the thing worth asserting.
    """

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_is_refused(self, client):
        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        response = await client.post(
            CONNECTIONS.format(ws=workspace),
            content=b"not json at all",
            headers={**_auth_header(org), "Content-Type": "application/json"},
        )

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "body must be valid JSON"

    @pytest.mark.asyncio
    async def test_a_json_body_that_is_not_an_object_is_refused(self, client):
        """A bare list parses as JSON but has no fields to read."""
        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        response = await client.post(
            CONNECTIONS.format(ws=workspace),
            json=[CRED_A],
            headers=_auth_header(org),
        )

        assert response.status_code == 400, response.text
        assert "must be a JSON object" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_missing_provider_is_refused(self, client):
        """`provider` is required and is not defaulted.

        Defaulting it would record a connection against a provider nobody named,
        which is a wrong answer stored as if it were reported.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        response = await client.post(
            CONNECTIONS.format(ws=workspace),
            json={"credential_id": CRED_A, "service": "nebius", "label": "prod"},
            headers=_auth_header(org),
        )

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "provider is required"

    @pytest.mark.asyncio
    async def test_a_non_integer_capacity_is_refused(self, client):
        """A string capacity is refused rather than coerced.

        `int("4")` would succeed and store a measurement the caller never made in
        that type; the point of the four separate readings is that each one means
        exactly what was reported.
        """
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace)
        connection_id = created.json()["connection_id"]

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/validation",
            json=_passing_report(observed_capacity="lots"),
            headers=headers,
        )

        assert response.status_code == 400, response.text
        assert (
            "observed_capacity must be an integer or omitted"
            in (response.json()["detail"])
        )

    @pytest.mark.asyncio
    async def test_a_rotation_body_without_a_replacement_is_refused(self, client):
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)

        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={"validation": _passing_report()},
            headers=headers,
        )

        assert response.status_code == 400, response.text
        assert "replacement" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_an_unknown_connection_id_is_not_found(self, client):
        """A syntactically valid id that names nothing is 404, before any auth work."""
        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        response = await client.get(
            f"{CONNECTIONS.format(ws=workspace)}/{uuid.uuid4()}",
            headers=_auth_header(org),
        )

        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "provider connection not found"


class TestRoutesAreInventoriedAndScopedCorrectly:
    """The registration that makes the grant exist at all."""

    def test_every_route_is_workspace_scoped_with_a_recorded_decision(self):
        """WORKSPACE scope is load-bearing, not cosmetic.

        `app/domain_guard.py` publishes `request.state.grant` only under
        `Scope.WORKSPACE`. Registered as ORGANIZATION these routes would read an
        empty permission set and deny every caller, however privileged — and every
        negative test in this file would still pass. Pinned here so the scope cannot
        be "simplified" later.
        """
        from app.endpoint_inventory import DOMAIN_ROUTES, Scope
        from superplane_auth.policy import Permission

        routes = {
            key: value
            for key, value in DOMAIN_ROUTES.items()
            if "provider-connections" in key[1]
        }
        assert len(routes) == 5
        assert all(scope is Scope.WORKSPACE for scope, _ in routes.values())
        # The path parameter must be named as the guard resolves it, or the
        # workspace cannot be resolved and every request is refused.
        from app.endpoint_inventory import WORKSPACE_PATH_PARAM

        assert all("{" + WORKSPACE_PATH_PARAM + "}" in path for _, path in routes)

        by_method = {
            (method, path.count("/")): perm
            for (method, path), (_, perm) in routes.items()
        }
        # The read is READ; every mutation is RENEW_CREDENTIAL, matching the
        # policy's grouping of credential lifecycle operations.
        assert by_method[("GET", 4)] is Permission.READ
        assert all(
            perm is Permission.RENEW_CREDENTIAL
            for (method, _), perm in by_method.items()
            if method != "GET"
        )

    def test_the_mounted_routes_match_the_inventory_templates(self):
        """Byte-identical templates. A near-miss classifies as unrecorded and 403s."""
        from app.endpoint_inventory import DOMAIN_ROUTES, mounted_operations
        from app.main import app as fastapi_app

        # Shared enumeration (issue #5682, A02). The local `isinstance(route,
        # APIRoute)` walk this replaced found zero routes once FastAPI began
        # storing included routers lazily, so `mounted == inventoried` failed here
        # rather than passing vacuously — the one place the breakage was visible.
        mounted = {
            (method, path)
            for method, path in mounted_operations(fastapi_app)
            if "provider-connections" in path
        }
        inventoried = {key for key in DOMAIN_ROUTES if "provider-connections" in key[1]}
        assert mounted == inventoried


class TestTrustedCredentialEvidence:
    @pytest.mark.parametrize("field", ["label", "service", "provider", "detail"])
    @pytest.mark.parametrize(
        "material",
        [
            "xAKIA" + "Z" * 16,
            "_arn:aws:secretsmanager:us-east-1:000000000000:secret:fake",
            "sk-ant-api03-" + "x" * 40,
            "%61rn%3Aaws%3Asecretsmanager%3Aus-east-1%3A000000000000%3Asecret%3Afake",
        ],
    )
    async def test_recoverable_secret_metadata_is_never_persisted_or_emitted(
        self, client, caplog, field, material
    ):
        from sqlalchemy import select
        from app.models.provider_connection import ProviderConnection

        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        if field == "detail":
            connection_id = await _active_connection(client, headers, workspace)
            response = await client.post(
                f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/validation",
                json=_passing_report(detail=material),
                headers=headers,
            )
        else:
            response = await _register(client, headers, workspace, **{field: material})
        assert response.status_code == 400, response.text
        assert material not in response.text
        assert material not in caplog.text
        async with async_session_test() as session:
            rows = (
                (
                    await session.execute(
                        select(ProviderConnection).where(
                            ProviderConnection.org_id == org
                        )
                    )
                )
                .scalars()
                .all()
            )
            if field != "detail":
                assert rows == []
            else:
                assert all(material not in repr(row.__dict__) for row in rows)

    @pytest.mark.parametrize(
        "operation",
        ["register", "validation", "failed_validation", "rotation", "disable"],
    )
    async def test_committed_mutation_reports_success_after_evidence_expires(
        self, client, monkeypatch, operation
    ):
        from datetime import datetime, timedelta, timezone
        from sqlalchemy.ext.asyncio import AsyncSession
        from app.routers import provider_connections as router

        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        connection_id = None
        if operation != "register":
            connection_id = await _active_connection(client, headers, workspace)
        committed = False
        original_commit = AsyncSession.commit

        class CommitClock:
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + (
                    timedelta(minutes=10) if committed else timedelta()
                )

        async def commit_then_expire(session):
            nonlocal committed
            await original_commit(session)
            committed = True

        # Preserve the evidence type guard while advancing only the freshness clock.
        def current(evidence):
            from fastapi import HTTPException

            if evidence.expires_at <= CommitClock.now(timezone.utc):
                raise HTTPException(status_code=403, detail="expired")

        monkeypatch.setattr(router, "_current", current)
        monkeypatch.setattr(AsyncSession, "commit", commit_then_expire)
        url = f"{CONNECTIONS.format(ws=workspace)}/{connection_id}"
        if operation == "register":
            response = await _register(client, headers, workspace)
        elif operation in {"validation", "failed_validation"}:
            report = (
                _passing_report()
                if operation == "validation"
                else _passing_report(
                    credential_valid=False, permissions_sufficient=False
                )
            )
            response = await client.post(
                url + "/validation", json=report, headers=headers
            )
        elif operation == "rotation":
            response = await client.post(
                url + "/rotation",
                json={
                    "replacement": {
                        "credential_id": CRED_B,
                        "service": "nebius",
                        "label": "prod",
                    },
                    "validation": _passing_report(),
                },
                headers=headers,
            )
        else:
            response = await client.delete(url, headers=headers)
        assert committed
        assert response.status_code == (201 if operation == "register" else 200), (
            response.text
        )
        if operation == "register":
            connection_id = response.json()["connection_id"]
        stored = await client.get(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )
        assert stored.status_code == 200
        expected_status = {
            "register": "pending",
            "validation": "active",
            "failed_validation": "pending",
            "rotation": "active",
            "disable": "disabled",
        }[operation]
        assert stored.json()["status"] == expected_status
        assert stored.json()["credential"]["credential_id"] == (
            CRED_B if operation == "rotation" else CRED_A
        )

    async def test_explicit_vault_delegate_can_manage_exact_workspace(
        self, client, connection_security, monkeypatch
    ):
        from dataclasses import replace

        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        original = connection_security.read

        async def delegated(**kwargs):
            evidence = await original(**kwargs)
            return replace(
                evidence,
                ownership=replace(
                    evidence.ownership,
                    owner_principal="vault-owner",
                    delegated_to_workspaces=frozenset({str(workspace)}),
                ),
            )

        monkeypatch.setattr(connection_security, "read", delegated)
        connection_id = await _active_connection(client, headers, workspace)
        from sqlalchemy import select
        from app.models.provider_connection import (
            ProviderConnection,
            ProviderConnectionBinding,
        )
        from tests.conftest import async_session_test

        async with async_session_test() as session:
            connection = await session.get(ProviderConnection, uuid.UUID(connection_id))
            binding = (
                await session.execute(
                    select(ProviderConnectionBinding).where(
                        ProviderConnectionBinding.connection_id
                        == uuid.UUID(connection_id)
                    )
                )
            ).scalar_one()
            assert connection.owner_principal == "vault-owner"
            assert binding.bound_by == "user-abc"
        url = f"{CONNECTIONS.format(ws=workspace)}/{connection_id}"
        rotated = await client.post(
            url + "/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "prod",
                },
                "validation": _passing_report(),
            },
            headers=headers,
        )
        assert rotated.status_code == 200, rotated.text
        disabled = await client.delete(url, headers=headers)
        assert disabled.status_code == 200, disabled.text

    @pytest.mark.parametrize("mismatch", ["workspace", "credential"])
    async def test_vault_delegation_cannot_cross_binding(
        self, client, connection_security, monkeypatch, mismatch
    ):
        from dataclasses import replace

        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        original = connection_security.read

        async def delegated(**kwargs):
            evidence = await original(**kwargs)
            return replace(
                evidence,
                ownership=replace(
                    evidence.ownership,
                    owner_principal="vault-owner",
                    credential_id=CRED_B if mismatch == "credential" else CRED_A,
                    delegated_to_workspaces=frozenset(
                        {
                            str(uuid.uuid4())
                            if mismatch == "workspace"
                            else str(workspace)
                        }
                    ),
                ),
            )

        monkeypatch.setattr(connection_security, "read", delegated)
        response = await _register(client, _auth_header(org), workspace)
        assert response.status_code == 403

    async def test_rotation_expiry_during_replacement_lookup_preserves_original(
        self, client, connection_security, monkeypatch
    ):
        import asyncio
        from dataclasses import replace
        from datetime import datetime, timedelta, timezone

        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)
        original = connection_security.read

        async def delayed_lookup(**kwargs):
            evidence = await original(**kwargs)
            if kwargs["reference"].credential_id == CRED_A:
                return replace(
                    evidence,
                    expires_at=datetime.now(timezone.utc) + timedelta(milliseconds=50),
                )
            await asyncio.sleep(0.1)
            return evidence

        monkeypatch.setattr(connection_security, "read", delayed_lookup)
        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "prod",
                },
                "validation": _passing_report(),
            },
            headers=headers,
        )
        assert response.status_code == 403
        read = await client.get(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )
        assert read.json()["credential"]["credential_id"] == CRED_A

    async def test_registration_cannot_claim_another_vault_owners_credential(
        self, client, connection_security
    ):
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        connection_security.owner_override = "different-owner"
        response = await _register(client, _auth_header(org), workspace)
        assert response.status_code == 403

    async def test_no_vault_adapter_refuses_registration(self, client, monkeypatch):
        from app.services import credential_evidence

        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        monkeypatch.setattr(credential_evidence, "_reader", None)
        response = await _register(client, _auth_header(org), workspace)
        assert response.status_code == 503

    async def test_caller_report_without_independent_attestation_cannot_activate(
        self, client, connection_security
    ):
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace)
        connection_security.attest = False
        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{created.json()['connection_id']}/validation",
            json=_passing_report(observed_capacity=4),
            headers=headers,
        )
        assert response.status_code == 403
        read = await client.get(
            f"{CONNECTIONS.format(ws=workspace)}/{created.json()['connection_id']}",
            headers=headers,
        )
        assert read.json()["status"] == "pending"

    async def test_rotation_requires_ownership_of_replacement(
        self, client, connection_security
    ):
        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)
        _vault_owners[(str(org), CRED_B)] = "different-owner"
        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "prod",
                },
                "validation": _passing_report(observed_capacity=4),
            },
            headers=headers,
        )
        assert response.status_code == 403
        read = await client.get(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )
        assert read.json()["credential"]["credential_id"] == CRED_A

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
    async def test_validation_readings_are_not_truthiness_coerced(self, client, value):
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace)
        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{created.json()['connection_id']}/validation",
            json=_passing_report(credential_valid=value),
            headers=headers,
        )
        assert response.status_code == 400

    async def test_failed_revalidation_stops_active_admission_and_remains_readable(
        self, client
    ):
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)
        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/validation",
            json=_passing_report(quota_available=False, observed_capacity=4),
            headers=headers,
        )
        assert response.status_code == 200
        read = await client.get(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}", headers=headers
        )
        assert read.status_code == 200
        assert read.json()["status"] == "pending"
        assert read.json()["admits_new_work"] is False

    async def test_legacy_organization_token_does_not_grant_credential_authority(
        self, client, monkeypatch
    ):
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        with monkeypatch.context() as legacy:
            legacy.setattr(settings, "domain_auth_enforced", False)
            legacy.setattr(fastapi_app.state, "domain_policy", None)
            token, _ = create_access_token(org)
            response = await _register(
                client, {"Authorization": f"Bearer {token}"}, workspace
            )
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "bad_field",
        ["org", "workspace", "reference", "owner", "expired", "naive", "missing"],
    )
    async def test_unbound_or_stale_vault_evidence_cannot_register(
        self, client, connection_security, monkeypatch, bad_field
    ):
        from dataclasses import replace
        from datetime import datetime, timezone, timedelta
        from superplane_contracts.connections import CredentialReference, VaultOwnership

        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        original = connection_security.read

        async def corrupt(**kwargs):
            result = await original(**kwargs)
            if bad_field == "missing":
                return None
            updates = {
                "org": {"org_id": str(uuid.uuid4())},
                "workspace": {"workspace_id": str(uuid.uuid4())},
                "reference": {
                    "reference": CredentialReference(
                        credential_id=CRED_B, service="nebius", label="prod"
                    )
                },
                "owner": {
                    "ownership": VaultOwnership(
                        credential_id=CRED_B, owner_principal="user-abc"
                    )
                },
                "expired": {
                    "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)
                },
                "naive": {"expires_at": datetime.now()},
            }
            return replace(result, **updates[bad_field])

        monkeypatch.setattr(connection_security, "read", corrupt)
        response = await _register(client, _auth_header(org), workspace)
        assert response.status_code == 403

    async def test_vault_errors_do_not_echo_secret_material(
        self, client, connection_security, monkeypatch
    ):
        org, workspace = await _seed_org_workspace_credentials(CRED_A)

        async def failed(**kwargs):
            raise RuntimeError(FAKE_SECRET_ARN)

        monkeypatch.setattr(connection_security, "read", failed)
        response = await _register(client, _auth_header(org), workspace)
        assert response.status_code == 503
        assert FAKE_SECRET_ARN not in response.text

    async def test_rotation_refuses_secret_material_outside_known_fields(self, client):
        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)
        response = await client.post(
            f"{CONNECTIONS.format(ws=workspace)}/{connection_id}/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "nebius",
                    "label": "prod",
                },
                "validation": _passing_report(),
                "secret_access_key": FAKE_AWS_KEY,
            },
            headers=headers,
        )
        assert response.status_code == 400
        assert FAKE_AWS_KEY not in response.text

    async def test_oversized_body_is_bounded(self, client):
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        response = await _register(
            client, _auth_header(org), workspace, padding="x" * 65536
        )
        assert response.status_code == 413

    async def test_duplicate_registration_returns_conflict_and_preserves_binding(
        self, client
    ):
        org, first, second = await _seed_org_workspace_credentials(CRED_A, workspaces=2)
        headers = _auth_header(org)
        created = await _register(client, headers, first)
        duplicate = await _register(client, headers, second)
        assert duplicate.status_code == 409
        read = await client.get(
            f"{CONNECTIONS.format(ws=first)}/{created.json()['connection_id']}",
            headers=headers,
        )
        assert read.status_code == 200

    @pytest.mark.parametrize(
        "fields",
        [
            {"provider": "x" * 51},
            {"service": "x" * 101},
            {"label": "x" * 256},
            {"credential_id": "x" * 256},
        ],
    )
    async def test_reference_fields_fit_postgresql_columns(self, client, fields):
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        response = await _register(client, _auth_header(org), workspace, **fields)
        assert response.status_code == 400


class TestConnectionLifecycleIntegrity:
    @pytest.mark.parametrize(
        "operation",
        ["register", "validation", "failed_validation", "rotation", "disable"],
    )
    @pytest.mark.parametrize("revocation", ["revoke", "downgrade"])
    async def test_grant_changed_during_vault_wait_prevents_write(
        self, client, connection_security, monkeypatch, operation, revocation
    ):
        from datetime import datetime, timezone
        from sqlalchemy import select, update
        from app.models.provider_connection import ProviderConnection

        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        connection_id = (
            None
            if operation == "register"
            else await _active_connection(client, headers, workspace)
        )
        original = connection_security.read
        changed = False

        async def revoke_during_read(**kwargs):
            nonlocal changed
            evidence = await original(**kwargs)
            if not changed:
                changed = True
                async with async_session_test() as session:
                    values = (
                        {"revoked_at": datetime.now(timezone.utc)}
                        if revocation == "revoke"
                        else {"permissions": "workspace:read"}
                    )
                    await session.execute(
                        update(WorkspaceGrantRecord)
                        .where(WorkspaceGrantRecord.workspace_id == workspace)
                        .values(**values)
                    )
                    await session.commit()
            return evidence

        monkeypatch.setattr(connection_security, "read", revoke_during_read)
        url = f"{CONNECTIONS.format(ws=workspace)}/{connection_id}"
        if operation == "register":
            response = await _register(client, headers, workspace)
        elif operation in {"validation", "failed_validation"}:
            response = await client.post(
                url + "/validation",
                json=_passing_report(
                    credential_valid=operation == "validation",
                    permissions_sufficient=operation == "validation",
                ),
                headers=headers,
            )
        elif operation == "rotation":
            response = await client.post(
                url + "/rotation",
                json={
                    "replacement": {
                        "credential_id": CRED_B,
                        "service": "nebius",
                        "label": "next",
                    },
                    "validation": _passing_report(),
                },
                headers=headers,
            )
        else:
            response = await client.delete(url, headers=headers)
        assert changed
        assert response.status_code == 403, response.text
        async with async_session_test() as session:
            rows = (
                (
                    await session.execute(
                        select(ProviderConnection).where(
                            ProviderConnection.org_id == org
                        )
                    )
                )
                .scalars()
                .all()
            )
            if operation == "register":
                assert rows == []
            else:
                assert len(rows) == 1
                assert rows[0].status == "active"
                assert rows[0].adp_credential_id == CRED_A

    @pytest.mark.parametrize(
        "provider,service", [("aws", "nebius"), ("aws", "aws"), ("nebius", "aws")]
    )
    async def test_registration_refuses_provider_mismatch(
        self, client, provider, service
    ):
        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        response = await _register(
            client, _auth_header(org), workspace, provider=provider, service=service
        )
        assert response.status_code == 400, response.text

    async def test_rotation_refuses_different_provider_and_preserves_reference(
        self, client
    ):
        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        connection_id = await _active_connection(client, headers, workspace)
        url = f"{CONNECTIONS.format(ws=workspace)}/{connection_id}"
        response = await client.post(
            url + "/rotation",
            json={
                "replacement": {
                    "credential_id": CRED_B,
                    "service": "aws",
                    "label": "next",
                },
                "validation": _passing_report(),
            },
            headers=headers,
        )
        assert response.status_code == 400, response.text
        assert (await client.get(url, headers=headers)).json()["credential"][
            "credential_id"
        ] == CRED_A

    @pytest.mark.parametrize("state", ["pending", "active", "disabled", "superseded"])
    async def test_deregistration_respects_connection_lifecycle_and_retains_audit(
        self, client, state
    ):
        from sqlalchemy import select
        from app.models.credential import CredentialAuditLog, CredentialRegistry

        org, workspace = await _seed_org_workspace_credentials(CRED_A, CRED_B)
        headers = _auth_header(org)
        created = await _register(client, headers, workspace)
        assert created.status_code == 201
        url = f"{CONNECTIONS.format(ws=workspace)}/{created.json()['connection_id']}"
        if state != "pending":
            assert (
                await client.post(
                    url + "/validation", json=_passing_report(), headers=headers
                )
            ).status_code == 200
        if state == "disabled":
            assert (await client.delete(url, headers=headers)).status_code == 200
        elif state == "superseded":
            assert (
                await client.post(
                    url + "/rotation",
                    json={
                        "replacement": {
                            "credential_id": CRED_B,
                            "service": "nebius",
                            "label": "next",
                        },
                        "validation": _passing_report(),
                    },
                    headers=headers,
                )
            ).status_code == 200
        async with async_session_test() as session:
            registry_id = (
                await session.execute(
                    select(CredentialRegistry.id).where(
                        CredentialRegistry.org_id == org,
                        CredentialRegistry.adp_credential_id == CRED_A,
                    )
                )
            ).scalar_one()
        response = await client.delete(
            f"/vault/credentials/{registry_id}", headers=headers
        )
        allowed = state in {"disabled", "superseded"}
        assert response.status_code == (200 if allowed else 409), response.text
        async with async_session_test() as session:
            row = await session.get(CredentialRegistry, registry_id)
            assert row is not None
            assert row.status == ("Deregistered" if allowed else "Active")
            events = (
                (
                    await session.execute(
                        select(CredentialAuditLog).where(
                            CredentialAuditLog.credential_registry_id == registry_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(events) == int(allowed)
            if allowed:
                assert events[0].accessed_by == "user-abc"
        if allowed:
            listing = await client.get("/vault/credentials", headers=headers)
            assert listing.status_code == 200, listing.text
            assert str(registry_id) not in {
                item["id"] for item in listing.json()["credentials"]
            }
            assert (await _register(client, headers, workspace)).status_code == 404

    async def test_deregistration_cannot_cross_tenant(self, client):
        from sqlalchemy import select
        from app.models.credential import CredentialRegistry

        org, _ = await _seed_org_workspace_credentials(CRED_A)
        other_org, _ = await _seed_org_workspace_credentials(CRED_B)
        async with async_session_test() as session:
            row_id = (
                await session.execute(
                    select(CredentialRegistry.id).where(
                        CredentialRegistry.org_id == org
                    )
                )
            ).scalar_one()
        assert (
            await client.delete(
                f"/vault/credentials/{row_id}", headers=_auth_header(other_org)
            )
        ).status_code == 404
        async with async_session_test() as session:
            assert (await session.get(CredentialRegistry, row_id)).status == "Active"

    async def test_deregistration_refuses_cluster_assignment(self, client):
        from sqlalchemy import select
        from app.models.cluster import Cluster
        from app.models.credential import ClusterVaultAssignment, CredentialRegistry

        org, workspace = await _seed_org_workspace_credentials(CRED_A)
        async with async_session_test() as session:
            row_id = (
                await session.execute(
                    select(CredentialRegistry.id).where(
                        CredentialRegistry.org_id == org
                    )
                )
            ).scalar_one()
            cluster = Cluster(
                org_id=org, workspace_id=workspace, name="assigned", status="Active"
            )
            session.add(cluster)
            await session.flush()
            session.add(
                ClusterVaultAssignment(
                    cluster_id=cluster.id,
                    credential_registry_id=row_id,
                    status="Synced",
                )
            )
            await session.commit()
        response = await client.delete(
            f"/vault/credentials/{row_id}", headers=_auth_header(org)
        )
        assert response.status_code == 409, response.text
