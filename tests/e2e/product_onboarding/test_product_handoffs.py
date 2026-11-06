"""Offline cross-client checks; none of these fixtures is live provider evidence."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.e2e.cli_uplift.test_product_handoffs import FakeRequest, receipt
from tests.e2e.new_ui.test_product_handoffs import browser_admin_resume


@pytest.fixture(autouse=True)
def workspace_navigation(monkeypatch):
    monkeypatch.setattr("tests.e2e.new_ui.test_product_handoffs.open_product_section", lambda *args: None)
    monkeypatch.setattr("tests.e2e.new_ui.test_product_handoffs.product_session_status", lambda page: page.get_by_role("status"))


class Browser:
    def __init__(self, request):
        self.request = request
        self.navigated = False
        self.reloads = 0
        self.status_queries = []

    def get_by_role(self, role, name=None):
        if role == "button":
            assert name == "Start or resume onboarding"
            return SimpleNamespace(click=lambda: None)
        assert role == "status"
        return self

    def filter(self, *, has_text):
        self.status_queries.append(has_text)
        return self

    def wait_for(self, *, timeout):
        assert timeout == 30_000
        assert self.status_queries[-1] == "Session session-owned"

    def reload(self, *, wait_until):
        assert wait_until == "domcontentloaded"
        self.reloads += 1

    def locator(self, selector):
        assert selector == "body"
        return SimpleNamespace(inner_text=lambda: "Session session-owned · Revision 1")


def owned_fixture():
    return {"run_id": "run-owned", "tenant_id": "tenant-owned", "actor_id": "actor-owned",
            "product_id": "product-owned", "project_id": "project-owned",
            "owner": {"access_token": "synthetic-owner-token"},
            "observer": {"access_token": "synthetic-observer-token"}}


def run_browser(fixture, request, page, *, complete_admin=None, receipt_observer=None, resume_cli=None):
    original_get = request.get

    def observe(url, *, headers, timeout):
        if headers.get("Authorization") == "Bearer synthetic-observer-token":
            assert url.endswith("/onboarding-sessions/session-owned") and timeout == 30_000
            session = deepcopy(request.session)
            session["operation_ids"] = [] if request.finished else ["operation-owned"]
            return SimpleNamespace(status=200, json=lambda: session)
        return original_get(url, headers=headers, timeout=timeout)

    request.get = observe
    return browser_admin_resume(
        page, SimpleNamespace(request=request), "https://example.invalid", fixture,
        "session-owned", "owned-handoff-key-123", navigate=lambda: None,
        complete_admin=complete_admin or (lambda _operation: setattr(request, "finished", True)),
        receipt_observer=receipt_observer or (lambda _operation: receipt(fixture)),
        resume_cli=resume_cli or (lambda _session: deepcopy(request.session)),
    )


def test_browser_resumes_same_admin_operation_and_progress():
    fixture = owned_fixture()
    request = FakeRequest(fixture)
    page = Browser(request)
    result = run_browser(fixture, request, page)
    assert result["operation"]["id"] == "operation-owned"
    assert result["callback_receipt"]["provider_operation_id"] == "owned-membership"
    assert result["cli_session"]["steps"] == result["observer_session"]["steps"]
    assert page.reloads == 2 and len(page.status_queries) == 3


@pytest.mark.parametrize("fault", ["scope", "receipt", "progress", "secret"])
def test_browser_does_not_accept_wrong_scope_receipt_lost_progress_or_secrets(fault):
    fixture = owned_fixture()
    request = FakeRequest(fixture)
    page = Browser(request)
    observed_receipt = receipt(fixture)
    if fault == "scope":
        observed_receipt["tenant_id"] = "other-tenant"
    if fault == "receipt":
        observed_receipt["sequence"] = 0
    if fault == "progress":
        request.session["steps"]["financials"]["name"] = "saved work"
    if fault == "secret":
        page.locator = lambda _selector: SimpleNamespace(inner_text=lambda: "synthetic-owner-token")
    def admin_effect(_operation):
        request.finished = True
        if fault == "progress":
            request.session["steps"]["financials"]["name"] = "lost work"

    with pytest.raises(AssertionError, match="receipt|progress|credentials"):
        run_browser(fixture, request, page, complete_admin=admin_effect,
                    receipt_observer=lambda _operation: observed_receipt)


class RepositoryBrowser:
    def __init__(self, request, fixture, plan):
        self.request, self.fixture, self.plan = request, fixture, plan
        self.previewed = False
        self.intercepted = False
        self.route_handler = None
        self.source_seen = False
        self.name = None
        self.visibility = None
        self.status_text = ""
        self.confirm_body = None
        self.confirm_status = 202

    def get_by_role(self, role, name=None):
        if role == "status":
            return self
        assert role == "button"
        self.button_name = name
        return self

    def click(self):
        if self.button_name.startswith("Inspect "):
            assert self.button_name == "Inspect owned-source · github"
            self.source_seen = True
        elif self.button_name == "Preview creation from this template":
            assert self.source_seen and self.name == self.plan["name"]
            self.previewed = True
        else:
            assert self.button_name == "Confirm and create this repository"
            assert self.previewed and self.route_handler is not None
            self.route_handler(RepositoryRoute(self))

    def get_by_label(self, label):
        assert label in ("New repository name", "Visibility", "Repository creation preview")
        self.label = label
        return self

    def fill(self, name):
        assert self.label == "New repository name"
        self.name = name

    def select_option(self, *, value):
        assert self.label == "Visibility"
        self.visibility = value

    def get_by_text(self, text):
        assert text == "repository: owned-template"
        return self

    def locator(self, selector):
        assert selector == ".."
        return self

    def expect_response(self, predicate, *, timeout):
        assert timeout == 30_000
        response = SimpleNamespace(status=201, url="https://example.invalid/api/v1/repository-plans",
                                   request=SimpleNamespace(method="POST", post_data_json={"repository_plan": self.plan},
                                                           headers={"idempotency-key": "browser-preview-key-123"}),
                                   json=lambda: {"id": "preview-owned", "revision": 1, "repository_plan": self.plan})
        assert predicate(response)
        return PreviewContext(self, response)

    def inner_text(self):
        if self.label == "Repository creation preview":
            return "run-owned-project · private · template-owned · v1"
        return self.status_text

    def route(self, pattern, callback):
        assert pattern.endswith("/onboarding-sessions/session-owned/handoffs")
        self.route_handler = callback

    def unroute(self, pattern, callback):
        assert self.route_handler == callback
        self.route_handler = None

    def filter(self, *, has_text):
        assert has_text == "Repository outcome is uncertain"
        return self

    def wait_for(self, *, timeout):
        assert timeout == 30_000 and self.intercepted


class PreviewContext:
    def __init__(self, page, response):
        self.page, self.value = page, response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        assert self.page.previewed


class RepositoryRoute:
    def __init__(self, page):
        self.page = page
        self.request = SimpleNamespace(post_data_json=page.confirm_body or {
            "target": "repository_create", "action": "create_repository", "preview_id": "preview-owned",
            "repository_plan": {**page.plan, "confirmed": True}},
            headers={"idempotency-key": "browser-confirm-key-123"})

    def fetch(self, *, timeout):
        assert timeout == 30_000
        if self.page.confirm_status in (200, 202):
            self.page.request.create_calls += 1
            self.page.request.lost_ack = False
        return SimpleNamespace(status=self.page.confirm_status,
                               json=lambda: deepcopy(self.page.request.operation))

    def abort(self, reason):
        assert reason == "failed"
        self.page.intercepted = True

    def fulfill(self, *, response):
        assert response.status == self.page.confirm_status


def test_browser_confirmation_reconciles_the_same_owned_repository():
    from tests.e2e.cli_uplift.test_product_handoffs import (
        RepositoryRequest,
        repository_case,
    )
    from tests.e2e.new_ui.test_product_handoffs import browser_repository_recovery

    fixture = owned_fixture()
    plan = repository_case(fixture)
    fixture.update(source_label="owned-source", source_provider="github", template_name="owned-template")
    request = RepositoryRequest(fixture, plan)
    page = RepositoryBrowser(request, fixture, plan)
    posted = request.post

    def replay(url, *, data, headers, timeout):
        if url.endswith("/handoffs"):
            assert headers["Idempotency-Key"] == "browser-confirm-key-123"
            headers = {**headers, "Idempotency-Key": "owned-confirm-key-123"}
        return posted(url, data=data, headers=headers, timeout=timeout)

    request.post = replay

    def observe():
        return {"repositories": ([{"native_id": "91", "owner_native_id": "27", "name": plan["name"],
                                   "visibility": "private", "protections": fixture["expected_protections"]}]
                                 if request.create_calls else []), "observation_ref": "private:provider-observation"}

    result = browser_repository_recovery(
        page, SimpleNamespace(request=request), "https://example.invalid", fixture, "session-owned", plan,
        {"reconcile": "owned-reconcile-key-123"}, observe, navigate=lambda: None)
    assert result["confirmation_key"] == "browser-confirm-key-123"
    assert page.previewed and page.intercepted and request.create_calls == request.reconcile_calls == 1
    assert result["provider"]["repositories"][0]["protections"] == fixture["expected_protections"]


@pytest.mark.parametrize("fault", ["wrong_plan", "rejected", "duplicate_provider"])
def test_browser_confirmation_rejects_wrong_plan_denial_or_duplicate_provider(fault):
    from tests.e2e.cli_uplift.test_product_handoffs import (
        RepositoryRequest,
        repository_case,
    )
    from tests.e2e.new_ui.test_product_handoffs import browser_repository_recovery

    fixture = owned_fixture()
    plan = repository_case(fixture)
    fixture.update(source_label="owned-source", source_provider="github", template_name="owned-template")
    request = RepositoryRequest(fixture, plan)
    page = RepositoryBrowser(request, fixture, plan)
    if fault == "wrong_plan":
        page.confirm_body = {"target": "repository_create", "action": "create_repository",
                             "preview_id": "preview-owned", "repository_plan": {**plan, "name": "other", "confirmed": True}}
    if fault == "rejected":
        page.confirm_status = 403
    posted = request.post

    def replay(url, *, data, headers, timeout):
        if url.endswith("/handoffs"):
            headers = {**headers, "Idempotency-Key": "owned-confirm-key-123"}
        return posted(url, data=data, headers=headers, timeout=timeout)

    request.post = replay

    def observe():
        repository = {"native_id": "91", "owner_native_id": "27", "name": plan["name"],
                      "visibility": "private", "protections": fixture["expected_protections"]}
        return {"repositories": ([repository] * (2 if fault == "duplicate_provider" else 1)
                                 if request.create_calls else []), "observation_ref": "private:provider-observation"}

    with pytest.raises(AssertionError, match="different repository plan|403|Independent provider"):
        browser_repository_recovery(page, SimpleNamespace(request=request), "https://example.invalid", fixture,
                                    "session-owned", plan, {"reconcile": "owned-reconcile-key-123"},
                                    observe, navigate=lambda: None)
    assert request.create_calls == (1 if fault == "duplicate_provider" else 0)


class AdoptionRequest:
    def __init__(self, fixture):
        self.fixture = fixture
        self.revision = 1
        self.calls = []

    def get(self, url, *, headers, timeout):
        assert headers == {"Authorization": "Bearer synthetic-owner-token"} and timeout == 30_000
        assert url.endswith("/projects/project-owned")
        return SimpleNamespace(status=200, json=lambda: {"tenant_id": self.fixture["tenant_id"],
                                                            "revision": self.revision,
                                                            "product": {"starting_point": "running_product"}})

    def post(self, url, *, data, headers, timeout):
        assert url.endswith("/bindings") and timeout == 30_000
        assert data == self.fixture["existing_selection"]
        assert headers == {"Authorization": "Bearer synthetic-owner-token", "Idempotency-Key": "owned-adoption-key-123",
                           "If-Profile-Revision": str(self.revision)}
        self.calls.append("bind")
        self.revision += 1
        return SimpleNamespace(status=201, json=lambda: {"id": "owned-binding"})

    def delete(self, url, *, headers, timeout):
        assert url.endswith("/bindings/owned-binding") and timeout == 30_000
        assert headers == {"Authorization": "Bearer synthetic-owner-token", "Idempotency-Key": "owned-unlink-key-123",
                           "If-Profile-Revision": str(self.revision)}
        self.calls.append("unlink")
        self.revision += 1
        return SimpleNamespace(status=200, json=lambda: {"status": "unlinked"})


def adoption_case():
    fixture = owned_fixture()
    fixture.update(
        ownership_manifest={"status": "recorded", "manifest_ref": "private:owned-manifest",
                            "run_id": fixture["run_id"], "tenant_id": fixture["tenant_id"],
                            "product_id": fixture["product_id"], "project_id": fixture["project_id"],
                            "native_id": "91", "name": "run-owned-project", "creation_operation_id": "repo-operation"},
        existing_selection={"kind": "repository", "provider": "github", "namespace": "27", "native_id": "35",
                            "connection_ref": {"kind": "github_installation", "id": "existing-connection"}},
        adoption_keys={"bind": "owned-adoption-key-123", "unlink": "owned-unlink-key-123"},
    )
    request = AdoptionRequest(fixture)
    provider = {"existing": {"native_id": "35", "protected": True},
                "owned": {"native_id": "91", "name": "run-owned-project"}}
    snapshots = []
    history = [{"operation_id": "repo-operation", "native_id": "91", "result": "created"}]

    def observe(name):
        value = {"repository": deepcopy(provider[name]), "observation_ref": f"private:{name}-{len(snapshots)}"}
        snapshots.append((name, deepcopy(value)))
        return value

    def delete_owned(manifest):
        assert manifest == fixture["ownership_manifest"]
        assert request.calls == ["bind", "unlink"]
        provider["owned"] = None
        history.append({"receipt_ref": "private:owned-deletion", "native_id": "91", "result": "deleted"})
        return {"run_id": fixture["run_id"], "native_id": "91", "status": "verified",
                "receipt_ref": "private:owned-deletion"}

    return fixture, request, provider, snapshots, history, observe, delete_owned


def test_existing_adoption_unlink_and_owned_cleanup_preserve_provider_and_history():
    from tests.e2e.cli_uplift.product_handoffs import adopt_and_cleanup

    fixture, request, provider, snapshots, history, observe, delete_owned = adoption_case()
    result = adopt_and_cleanup(request, "https://example.invalid", fixture,
                               lambda: observe("existing"), lambda: observe("owned"), delete_owned,
                               lambda: deepcopy(history))
    assert request.calls == ["bind", "unlink"]
    assert provider["existing"] == {"native_id": "35", "protected": True} and provider["owned"] is None
    assert result["existing_before"]["repository"] == result["existing_after"]["repository"]
    assert result["owned_after"]["repository"] is None
    assert [item["result"] for item in result["history"]] == ["created", "deleted"]
    assert snapshots[0][0] == "existing" and snapshots[-1][0] == "existing"


@pytest.mark.parametrize("fault", ["manifest", "existing_changed", "owned_changed", "not_absent", "history_lost"])
def test_adoption_cleanup_refuses_unowned_or_unverified_effect(fault):
    from tests.e2e.cli_uplift.product_handoffs import adopt_and_cleanup

    fixture, request, provider, _snapshots, history, observe, delete_owned = adoption_case()
    if fault == "manifest":
        fixture["ownership_manifest"]["run_id"] = "other-run"
    if fault == "existing_changed":
        original = request.post

        def changed(*args, **kwargs):
            response = original(*args, **kwargs)
            provider["existing"]["protected"] = False
            return response

        request.post = changed
    if fault == "owned_changed":
        original = request.delete

        def changed(*args, **kwargs):
            response = original(*args, **kwargs)
            provider["owned"]["native_id"] = "another-repository"
            return response

        request.delete = changed
    if fault == "not_absent":
        def delete_owned(_manifest):
            return {"run_id": fixture["run_id"], "native_id": "91", "status": "verified",
                    "receipt_ref": "private:unverified-deletion"}
    if fault == "history_lost":
        history.clear()
    with pytest.raises((AssertionError, ValueError), match="manifest|Adoption changed|Unlink modified|deleted|history"):
        adopt_and_cleanup(request, "https://example.invalid", fixture, lambda: observe("existing"),
                          lambda: observe("owned"), delete_owned, lambda: deepcopy(history))
    assert provider["existing"]["native_id"] == "35"
    if fault in ("manifest", "existing_changed", "owned_changed"):
        assert history == [{"operation_id": "repo-operation", "native_id": "91", "result": "created"}]
