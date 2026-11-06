"""Offline V01-compatible state and protected cleanup contract checks."""

import copy
from types import SimpleNamespace

import pytest

from tests.e2e.new_ui.product_recovery import ProductRun, dispatch_owned_product

BASE = "https://example.invalid"
RUN_ID = "product-42-1"
SOURCE = "a" * 40
KEY = "product-create-" + "b" * 64
BODY = {
    "name": "eval-product-product-42-1", "purpose": "Verify saved Product workspace information",
    "intended_users": ["Maintainers"], "starting_point": "idea", "project": {"mode": "create"},
}


class Store:
    def __init__(self):
        self.snapshot = (None, None)
        self.fail_save = False
        self.partial_save = False

    def load(self, run_id):
        assert run_id == RUN_ID
        return copy.deepcopy(self.snapshot)

    def save(self, run_id, document, manifest=None):
        assert run_id == RUN_ID
        if self.fail_save:
            raise OSError("unavailable")
        self.snapshot = copy.deepcopy((document, None if self.partial_save else manifest))


class Response:
    def __init__(self, status, body=None):
        self.status = status
        self.payload = body

    def json(self):
        return self.payload


class Request:
    def __init__(self):
        self.created = {}
        self.mutations = 0
        self.retired = False
        self.pages = None
        self.status_after_retire = 404
        self.list_status = 200

    def post(self, url, **kwargs):
        assert url == BASE + "/api/v1/products"
        key = kwargs["headers"]["Idempotency-Key"]
        assert kwargs["data"] == BODY
        if key not in self.created:
            self.created[key] = {
                "tenant_id": "tenant", "product_id": "owned", "project_id": "project",
                "product": {"name": BODY["name"], "purpose": BODY["purpose"],
                            "intended_users": BODY["intended_users"], "owner_ref": {"kind": "user", "id": "actor"}},
                "project": {"id": "project", "tenant_id": "tenant", "owner_user_id": "actor", "name": BODY["name"]},
            }
            self.mutations += 1
        return Response(201, self.created[key])

    def get(self, url, **kwargs):
        if url.startswith(BASE + "/api/v1/products/owned/projects/project"):
            return Response(self.status_after_retire if self.retired else 200)
        assert url.startswith(BASE + "/api/v1/products")
        assert kwargs["headers"]["Authorization"] == "Bearer observer-token"
        if self.pages is None:
            return Response(self.list_status, {"items": [], "next_cursor": None})
        return Response(self.list_status, self.pages.pop(0))


class RouteRequest:
    method = "POST"
    post_data_json = BODY
    headers = {"idempotency-key": KEY}


class Route:
    request = RouteRequest()

    def __init__(self, store):
        self.store = store
        self.forwarded = False
        self.aborted = False

    def continue_(self):
        assert self.store.load(RUN_ID)[1]["idempotency_key"] == KEY
        self.forwarded = True

    def abort(self, **kwargs):
        assert kwargs == {"error_code": "blockedbyclient"}
        self.aborted = True


def make_run(store, retire):
    return ProductRun(
        store, retire, run_id=RUN_ID, tenant_id="tenant", actor_id="actor",
        source_revision=SOURCE, owner_token="owner-token", request_body=BODY,
    )


def observer():
    return {"tenant_id": "tenant", "access_token": "observer-token"}


def protected_retire(request):
    def retire(manifest):
        request.retired = True
        return {"run_id": manifest["run_id"], "product_id": manifest["product_id"],
                "project_id": manifest["project_id"], "cleanup_revision": 2, "fixture_removed": True}
    return retire


def test_intent_is_durable_before_forward_and_recovers_lost_response():
    store, request = Store(), Request()
    run = make_run(store, protected_retire(request))
    route = Route(store)
    assert dispatch_owned_product(route, run) is True and route.forwarded and not route.aborted
    original = request.post(BASE + "/api/v1/products", headers={"Idempotency-Key": KEY}, data=BODY)
    assert original.status == 201
    resumed = make_run(store, protected_retire(request))
    assert resumed.reconcile(request, BASE) == {"product_id": "owned", "project_id": "project"}
    assert request.mutations == 1
    result = resumed.retire(request, BASE, observer())
    assert result["fixture_removed"] is True and result["cleanup_revision"] == 2
    assert result["owned_absent"] is True
    assert result["shared_unchanged"] is False
    assert request.mutations == 1
    assert store.load(RUN_ID)[1]["product_id"] == "owned"


def test_failed_or_partial_persistence_never_forwards_browser_write():
    for failure in ("fail_save", "partial_save"):
        store = Store()
        setattr(store, failure, True)
        route = Route(store)
        assert dispatch_owned_product(route, make_run(store, lambda _: None)) is False
        assert route.aborted and not route.forwarded


def test_resume_rejects_wrong_actor_intent_and_second_operation_key():
    store = Store()
    run = make_run(store, lambda _: None)
    run.record_intent(BODY, KEY)
    with pytest.raises(ValueError, match="new operation key"):
        run.record_intent(BODY, "c" * 32)
    with pytest.raises(ValueError, match="original owned mutation"):
        run.record_intent({**BODY, "name": "foreign"}, KEY)
    with pytest.raises(ValueError, match="owned run"):
        ProductRun(store, lambda _: None, run_id=RUN_ID, tenant_id="other", actor_id="actor",
                   source_revision=SOURCE, owner_token="owner-token", request_body=BODY).reconcile(Request(), BASE)


def test_protected_cleanup_and_independent_absence_are_required():
    store, request = Store(), Request()
    run = make_run(store, lambda _: {"fixture_removed": False})
    run.record_intent(BODY, KEY)
    with pytest.raises(RuntimeError, match="retirement"):
        run.retire(request, BASE, observer())
    request.retired = False
    run.protected_retire = protected_retire(request)
    request.status_after_retire = 200
    with pytest.raises(RuntimeError, match="still readable"):
        run.retire(request, BASE, observer())
    with pytest.raises(ValueError, match="observer"):
        run.retire(request, BASE, {"tenant_id": "foreign", "access_token": "other"})


def test_cleanup_discovery_is_bounded_and_refuses_duplicate_ids():
    store, request = Store(), Request()
    run = make_run(store, protected_retire(request))
    run.record_intent(BODY, KEY)
    request.pages = [{"items": [], "next_cursor": f"next-{index}"} for index in range(5)]
    with pytest.raises(RuntimeError, match="five pages"):
        run.retire(request, BASE, observer())
    request.pages = [{"items": [{"id": "owned"}], "next_cursor": None}]
    with pytest.raises(RuntimeError, match="remains visible"):
        run.retire(request, BASE, observer())
    request.pages = [{"items": [], "next_cursor": "repeat"}, {"items": [], "next_cursor": "repeat"}]
    with pytest.raises(RuntimeError, match="repeated cursor"):
        run.retire(request, BASE, observer())


def test_lost_retirement_response_can_be_verified_after_restart():
    store, request = Store(), Request()
    make_run(store, lambda _: None).record_intent(BODY, KEY)
    calls = []

    def interrupted(manifest):
        calls.append(manifest["run_id"])
        request.retired = True
        if len(calls) == 1:
            raise OSError("lost protected response")
        return protected_retire(request)(manifest)

    with pytest.raises(OSError):
        make_run(store, interrupted).retire(request, BASE, observer())
    result = make_run(store, interrupted).retire(request, BASE, observer())
    assert result["fixture_removed"] is True
    assert calls == [RUN_ID, RUN_ID] and request.mutations == 1


@pytest.mark.parametrize("invalid", [None, "scope_project_id", "retire_key", "missing_scope_project_id", "missing_retire_key"])
def test_scope_compensation_receipt_survives_browser_adapter(invalid):
    from tests.e2e.new_ui.product_scope_cleanup import confirm_scope_retirement

    class ScopeRequest(Request):
        def get(self, url, **kwargs):
            if url == BASE + "/api/v1/products/owned/projects/second":
                assert kwargs["headers"] == {"Authorization": "Bearer observer-token"}
                return Response(404 if self.retired else 200)
            return super().get(url, **kwargs)

    store, request = Store(), ScopeRequest()
    retire_key = "d" * 32

    def retire(manifest):
        result = {**protected_retire(request)(manifest), "scope_project_id": "second", "retire_key": retire_key}
        if invalid and invalid.startswith("missing_"):
            del result[invalid.removeprefix("missing_")]
        elif invalid:
            result[invalid] = "foreign"
        return result

    run = make_run(store, retire)
    run.record_intent(BODY, KEY)
    cleanup = run.retire(request, BASE, observer())
    state = SimpleNamespace(document={"scope": {
        "cleanup_pending": True, "product": {"product_id": "owned"},
        "receipts": {"second_project": {"project_id": "second"}}, "retire_key": retire_key,
    }})
    state.save = lambda document: setattr(state, "document", copy.deepcopy(document))
    provider = SimpleNamespace(state=state, request=request, base_url=BASE, observer=observer(),
                               headers=lambda actor: {"Authorization": "Bearer " + actor["access_token"]})
    if invalid:
        with pytest.raises(RuntimeError, match="owned scope compensation"):
            confirm_scope_retirement(provider, cleanup)
        assert state.document["scope"]["cleanup_pending"] is True
        assert "cleanup_receipt" not in state.document["scope"]
    else:
        confirm_scope_retirement(provider, cleanup)
        assert state.document["scope"]["cleanup_pending"] is False
        saved = state.document["scope"]["cleanup_receipt"]
        assert saved["scope_project_id"] == "second" and saved["retire_key"] == retire_key
        assert saved["fixture_removed"] is True and saved["owned_absent"] is True


@pytest.mark.parametrize("corruption", ["wrong_owner", "wrong_project", "wrong_tenant"])
def test_replayed_receipt_must_bind_exact_actor_tenant_and_project(corruption):
    store, request = Store(), Request()
    run = make_run(store, lambda _: None)
    run.record_intent(BODY, KEY)
    response = request.post(BASE + "/api/v1/products", headers={"Idempotency-Key": KEY}, data=BODY)
    payload = response.json()
    if corruption == "wrong_owner":
        payload["product"]["owner_ref"]["id"] = "other"
    elif corruption == "wrong_project":
        payload["project"]["id"] = "other"
    else:
        payload["tenant_id"] = "other"
    with pytest.raises(ValueError, match="original owned intent"):
        run.reconcile(request, BASE)
    assert store.load(RUN_ID)[1].get("product_id") is None


def test_repeated_cursor_and_time_budget_leave_cleanup_red(monkeypatch):
    from tests.e2e.new_ui import product_recovery

    store, request = Store(), Request()
    run = make_run(store, protected_retire(request))
    run.record_intent(BODY, KEY)
    clock = iter((0, 31))
    monkeypatch.setattr(product_recovery.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="time budget"):
        run.retire(request, BASE, observer())
    assert request.retired


def test_denied_observer_and_oversized_page_cannot_certify_absence():
    store, request = Store(), Request()
    run = make_run(store, protected_retire(request))
    run.record_intent(BODY, KEY)
    request.list_status = 403
    with pytest.raises(RuntimeError, match="observer could not list"):
        run.retire(request, BASE, observer())
    request.list_status = 200
    request.pages = [{"items": [{"id": str(index)} for index in range(101)], "next_cursor": None}]
    with pytest.raises(RuntimeError, match="page bound"):
        run.retire(request, BASE, observer())
