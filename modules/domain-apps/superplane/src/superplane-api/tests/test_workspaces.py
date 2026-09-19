"""Tests for workspace CRUD endpoints.

Also covers issue #5058 (U17b): workspace provisioning and teardown go through the
authorized-operation facade, and the GitHub Actions ``workflow_dispatch`` call and
its foreign-repository personal access token are gone from the runtime path. The
absence is asserted statically over the shipped source, because a behavioral test
only covers the paths it exercises.
"""

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


class TestCreateWorkspace:
    """Test POST /workspaces."""

    @pytest.mark.asyncio
    async def test_create_requires_auth(self, client):
        """Creating a workspace without auth returns 401/403."""
        response = await client.post("/workspaces", json={"name": "test-ws"})
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_create_validates_name(self, client):
        """Creating a workspace with empty name returns 422."""
        headers = _auth_header()
        response = await client.post("/workspaces", json={"name": ""}, headers=headers)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_validates_isolation_mode(self, client):
        """Invalid isolation_mode returns 422."""
        headers = _auth_header()
        response = await client.post(
            "/workspaces",
            json={"name": "test", "isolation_mode": "invalid"},
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

        req = CreateWorkspaceRequest(name="my-workspace", isolation_mode="dedicated")
        assert req.name == "my-workspace"
        assert req.isolation_mode == "dedicated"

    def test_create_workspace_request_default_isolation(self):
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(name="my-workspace")
        assert req.isolation_mode == "dedicated"

    def test_create_workspace_request_namespace_mode(self):
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(name="shared-ws", isolation_mode="namespace")
        assert req.isolation_mode == "namespace"

    def test_create_workspace_request_invalid_mode(self):
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError):
            CreateWorkspaceRequest(name="test", isolation_mode="invalid")

    # --- Research isolation mode tests ---

    def test_create_workspace_research_mode_valid(self):
        """Research mode with account is valid."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(
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
            CreateWorkspaceRequest(name="ml-research", isolation_mode="research")

    def test_create_workspace_research_mode_empty_account(self):
        """Research mode with empty account raises validation error."""
        from pydantic import ValidationError

        from app.schemas.workspace import CreateWorkspaceRequest

        with pytest.raises(ValidationError):
            CreateWorkspaceRequest(
                name="ml-research",
                isolation_mode="research",
                account="",
            )

    def test_create_workspace_research_with_budget(self):
        """Research mode with budget guardrails is valid."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(
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
                name="test",
                isolation_mode="dedicated",
                budget_max_gpus=-1,
            )

    def test_dedicated_mode_account_optional(self):
        """Dedicated mode does not require an account."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(name="my-ws", isolation_mode="dedicated")
        assert req.account is None

    def test_namespace_mode_account_optional(self):
        """Namespace mode does not require an account."""
        from app.schemas.workspace import CreateWorkspaceRequest

        req = CreateWorkspaceRequest(name="my-ws", isolation_mode="namespace")
        assert req.account is None


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

    Named and flagged explicitly (``is_mock``) for the reason U17a's contract gives:
    B's facade does not exist in ADP (there is no ``modules/harness/jobs/``), so a
    green run here closes no live criterion — it establishes that this API refuses
    to provision without an authorized operation and that no dispatch or PAT
    remains, which is exactly what this story's acceptance is.
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
            operation_id=f"op-{workspace_id}", state=self._state, detail=self._detail
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
    async def test_create_opens_a_provision_operation(self, client, facade, org_id):
        """Creating a workspace opens a provision operation with the right permission."""
        response = await client.post(
            "/workspaces", json={"name": "ws-a"}, headers=_auth_header(org_id)
        )

        assert response.status_code == 201
        assert len(facade.open_calls) == 1
        call = facade.open_calls[0]
        assert call["action"] == prov.PROVISION
        assert call["permission"] == prov.REQUIRED_PERMISSION
        # The org is the one from the verified JWT, not anything in the body.
        assert call["org_id"] == str(org_id)

    @pytest.mark.asyncio
    async def test_create_passes_shape_not_identity(self, client, facade, org_id):
        """Provisioning parameters carry shape only — no identity keys."""
        response = await client.post(
            "/workspaces",
            json={"name": "ws-shape", "isolation_mode": "dedicated"},
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
            "/workspaces", json={"name": "ws-b"}, headers=headers
        )
        workspace_id = created.json()["id"]
        facade.open_calls.clear()

        response = await client.delete(f"/workspaces/{workspace_id}", headers=headers)

        assert response.status_code == 200
        assert response.json()["status"] == "Teardown"
        assert len(facade.open_calls) == 1
        assert facade.open_calls[0]["action"] == prov.TEARDOWN
        assert facade.open_calls[0]["permission"] == prov.REQUIRED_PERMISSION


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
            "/workspaces", json={"name": "ws-fail"}, headers=_auth_header(org_id)
        )

        assert response.status_code == 201
        assert response.json()["status"] == "Failed"

    @pytest.mark.asyncio
    async def test_unknown_is_not_treated_as_failure(self, client, facade, org_id):
        """`unknown` is not a failure — collapsing the two is the bug.

        Reading UNKNOWN as failure either leaks infrastructure the platform
        believes was never created, or retries a provision that actually
        succeeded.
        """
        facade._state = prov.STATE_UNKNOWN

        response = await client.post(
            "/workspaces", json={"name": "ws-unknown"}, headers=_auth_header(org_id)
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
                workspace_id="ws-1", org_id="org-1", workspace_name="ws-1"
            )

    @pytest.mark.asyncio
    async def test_create_returns_503_not_a_fake_provisioning(self, client, org_id):
        """The regression this story fixes: no 201 for work that never started."""
        prov.set_operation_facade(None)

        response = await client.post(
            "/workspaces", json={"name": "ws-none"}, headers=_auth_header(org_id)
        )

        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_delete_restores_status_when_unavailable(
        self, client, facade, org_id
    ):
        """A refused teardown does not leave the workspace looking mid-teardown."""
        headers = _auth_header(org_id)
        created = await client.post(
            "/workspaces", json={"name": "ws-keep"}, headers=headers
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
    async def test_refusal_surfaces_as_400(self, client, facade, org_id):
        """A caller-caused refusal is a 400, and the row is not left provisioning."""
        facade._raises = prov.ProvisioningRefused("identity asserted")

        response = await client.post(
            "/workspaces", json={"name": "ws-refused"}, headers=_auth_header(org_id)
        )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_refused_teardown_restores_status(self, client, facade, org_id):
        """A refused teardown is a 400 and leaves the prior status intact."""
        headers = _auth_header(org_id)
        created = await client.post(
            "/workspaces", json={"name": "ws-refuse-teardown"}, headers=headers
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
                "/workspaces", json={"name": "ws-bad"}, headers=_auth_header(org_id)
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

    def test_no_real_operation_facade_exists_to_integrate_against(self):
        """Fails when B's facade lands — the trigger to revisit these mocks."""
        repo_root = Path(__file__).resolve().parents[6]
        assert not (repo_root / "modules" / "harness" / "jobs").exists(), (
            "modules/harness/jobs/ now exists: B's operation facade may be real. "
            "Revisit the mocked facade in this file and the live criterion."
        )
