"""Offline tests for the run-owned API fixture producer; not live acceptance."""

from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from tests.e2e.new_ui.product_scope_cleanup import (
    confirm_scope_retirement,
    prepare_scope_retirement,
)
from tests.e2e.new_ui.product_scope_fixture import (
    load_scope_fixture,
    prepare_scope_fixture,
    verify_scope_sources,
)


class State:
    binding = {
        "run_id": "product-12-1",
        "source_revision": "a" * 40,
        "tenant_id": "example-tenant",
        "actor_id": "example-owner",
    }

    def __init__(self):
        self.document = {"binding": self.binding, "cases": {}}
        self.saved = 0
        self.fail_at = None

    def save(self, value):
        if self.saved + 1 == self.fail_at:
            raise OSError("durable state write failed")
        self.document = deepcopy(value)
        self.saved += 1


class Response:
    def __init__(self, value, status=200):
        self.value, self.status = deepcopy(value), status

    def json(self):
        return deepcopy(self.value)


class ProductAPI:
    def __init__(self):
        self.revision = 1
        self.projects = {
            "first": {
                "id": "first",
                "name": "Owned Product",
                "configuration": {},
                "progress": {"checks": ["information", "project_scope"]},
            }
        }
        self.operations = {}
        self.mutations = 0
        self.lose_at = None
        self.deny_sources = False

    def get(self, url, **kwargs):
        project_id = unquote(urlsplit(url).path.split("/projects/")[1].split("/")[0])
        if project_id not in self.projects:
            return Response({}, 404)
        project = self.projects[project_id]
        return Response(
            {
                "revision": self.revision,
                "tenant_id": "example-tenant",
                "product_id": "owned-product",
                "project_id": project_id,
                "product": {
                    "name": "Owned Product",
                    "owner_ref": {"kind": "user", "id": "example-owner"},
                },
                "project": {"id": project_id, "name": project["name"]},
                "configuration": project["configuration"],
                "progress": project["progress"],
            }
        )

    def post(self, url, headers, data, **kwargs):
        if url.endswith("/bindings") and self.deny_sources:
            return Response({}, 404)
        return self.mutate(url, headers, data, "post")

    def patch(self, url, headers, data, **kwargs):
        return self.mutate(url, headers, data, "patch")

    def mutate(self, url, headers, data, method):
        key = headers["Idempotency-Key"]
        if key in self.operations:
            return Response(self.operations[key], 200)
        assert int(headers["If-Profile-Revision"]) == self.revision
        self.revision += 1
        self.mutations += 1
        if url.endswith("/projects"):
            self.projects["second"] = {
                "id": "second",
                "name": data["name"],
                "configuration": {},
                "progress": {"checks": ["information", "project_scope"]},
            }
            result = {"project_id": "second"}
        elif method == "post":
            project_id = unquote(
                urlsplit(url).path.split("/projects/")[1].split("/")[0]
            )
            result = {
                "id": "source-" + project_id,
                "project_id": project_id,
                "selection": data,
                "status": "pending",
            }
        else:
            project_id = unquote(
                urlsplit(url).path.split("/projects/")[1].split("/")[0]
            )
            self.projects[project_id]["configuration"] = data["configuration"]
            self.projects[project_id]["progress"] = {
                "checks": ["information", "repository", project_id]
            }
            result = {"project_id": project_id, "revision": self.revision}
        self.operations[key] = deepcopy(result)
        if self.mutations == self.lose_at:
            raise ConnectionError("server committed but client lost the receipt")
        return Response(result, 201 if method == "post" else 200)


@pytest.fixture
def setup(tmp_path):
    state, api = State(), ProductAPI()
    owner = {
        "actor_id": "example-owner",
        "tenant_id": "example-tenant",
        "access_token": "example-token",
    }
    product = {
        "product_id": "owned-product",
        "project_id": "first",
        "product_name": "Owned Product",
    }
    inputs = [
        {
            "selection": {
                "kind": "repository",
                "provider": "github",
                "connection_ref": {"kind": "github_installation", "id": str(index + 1)},
                "namespace": "example-org",
                "native_id": "example-repository-" + str(index + 1),
            },
            "record_revision": "b" * 40,
            "release_target_date": f"202{index + 7}-12-01",
        }
        for index in range(2)
    ]
    return state, api, owner, product, inputs, tmp_path / "protected-scope.json"


def produce(setup):
    state, api, owner, product, inputs, path = setup
    return prepare_scope_fixture(
        state, api, "https://example.invalid", owner, product, inputs, path
    )


def test_producer_creates_two_authorized_scope_inputs_and_private_fixture(setup):
    state, api, owner, product, inputs, path = setup
    result = produce(setup)
    assert {row["project_id"] for row in result["scopes"]} == {"first", "second"}
    assert {
        row["configuration"]["basic"]["repositories"][0]["source_connection_id"]
        for row in result["scopes"]
    } == {"1", "2"}
    assert result["scopes"][0]["progress"] != result["scopes"][1]["progress"]
    assert api.mutations == 5 and len(state.document["scope"]["intents"]) == 5
    assert state.document["scope"]["cleanup_pending"] is True
    assert state.document["scope"]["case"] == "scope"
    assert load_scope_fixture(path, state.binding) == result
    assert path.stat().st_mode & 0o077 == 0
    assert produce(setup) == result and api.mutations == 5
    with pytest.raises(ValueError, match="original scope fixture"):
        prepare_scope_fixture(
            state,
            api,
            "https://example.invalid",
            owner,
            product,
            inputs,
            path,
            case="other",
        )
    with pytest.raises(ValueError, match="protected browser run"):
        load_scope_fixture(path, {**state.binding, "actor_id": "different"})


def test_two_repositories_on_one_installation_remain_isolated(setup):
    state, api, owner, product, inputs, path = setup
    inputs[1]["selection"]["connection_ref"] = deepcopy(
        inputs[0]["selection"]["connection_ref"]
    )
    result = produce(setup)
    assert len({scope["project_id"] for scope in result["scopes"]}) == 2
    assert {
        scope["configuration"]["basic"]["repositories"][0]["record_id"]
        for scope in result["scopes"]
    } == {item["selection"]["native_id"] for item in inputs}
    assert {
        scope["configuration"]["basic"]["repositories"][0]["source_connection_id"]
        for scope in result["scopes"]
    } == {"1"}
    assert produce(setup) == result and api.mutations == 5


@pytest.mark.parametrize("duplicate", ["repository", "plan"])
def test_same_installation_rejects_duplicate_scope_inputs(setup, duplicate):
    state, api, owner, product, inputs, path = setup
    inputs[1]["selection"]["connection_ref"] = deepcopy(
        inputs[0]["selection"]["connection_ref"]
    )
    if duplicate == "repository":
        inputs[1]["selection"]["native_id"] = inputs[0]["selection"]["native_id"]
    else:
        inputs[1]["release_target_date"] = inputs[0]["release_target_date"]
    with pytest.raises(ValueError, match="distinct"):
        produce(setup)
    assert api.mutations == 0 and state.document["cases"] == {}


class SourceDiscovery(ProductAPI):
    def __init__(self, inputs):
        super().__init__()
        self.inputs = inputs
        self.response_status = 200
        self.coverage = "complete"
        self.foreign_repository = False
        self.paginated = False
        self.reads = []

    def get(self, url, **kwargs):
        if not urlsplit(url).path.endswith("/sources"):
            return super().get(url, **kwargs)
        self.reads.append((url, kwargs))
        assert kwargs["headers"] == {"Authorization": "Bearer example-token"}
        if self.response_status != 200:
            return Response({}, self.response_status)
        query = parse_qs(urlsplit(url).query)
        connection = query["connection_id"][0]
        cursor = query.get("resource_cursor", [None])[0]
        if self.paginated and cursor is None:
            resources = []
            next_cursor = "2"
        else:
            resources = [
                {"kind": "repository", "namespace": item["selection"]["namespace"],
                 "native_id": item["selection"]["native_id"]}
                for item in self.inputs if item["selection"]["connection_ref"]["id"] == connection
            ]
            next_cursor = None
        if self.foreign_repository:
            resources = [{"kind": "repository", "namespace": "foreign-tenant", "native_id": "other"}]
        source = {"provider": "github", "coverage": self.coverage,
                  "connection_ref": {"kind": "github_installation", "id": connection},
                  "resources": resources}
        if next_cursor:
            source["resource_cursor"] = next_cursor
        return Response({"items": [source], "next_cursor": None})


@pytest.mark.parametrize("shared_installation", [False, True])
def test_scope_sources_require_ordinary_owned_repository_discovery(setup, shared_installation):
    state, _, owner, product, inputs, path = setup
    if shared_installation:
        inputs[1]["selection"]["connection_ref"] = deepcopy(inputs[0]["selection"]["connection_ref"])
    api = SourceDiscovery(inputs)
    api.paginated = True
    verify_scope_sources(api, "https://example.invalid", owner, product, inputs)
    assert len(api.reads) == 4 and api.mutations == 0 and state.document["cases"] == {} and not path.exists()


@pytest.mark.parametrize("failure", ["denied", "unavailable", "foreign"])
def test_scope_sources_refuse_unverified_tenant_connection(setup, failure):
    state, _, owner, product, inputs, path = setup
    api = SourceDiscovery(inputs)
    if failure == "denied":
        api.response_status = 404
    elif failure == "unavailable":
        api.coverage = "unavailable"
    else:
        api.foreign_repository = True
    with pytest.raises(ValueError, match="not authorized|did not verify|not accessible"):
        verify_scope_sources(api, "https://example.invalid", owner, product, inputs)
    assert api.mutations == 0 and state.document["cases"] == {} and not path.exists()


@pytest.mark.parametrize("lost_at", [1, 2, 3, 4, 5])
def test_lost_response_replays_only_original_intents(setup, lost_at):
    state, api, owner, product, inputs, path = setup
    api.lose_at = lost_at
    with pytest.raises(ConnectionError, match="lost the receipt"):
        produce(setup)
    assert state.document["scope"]["cleanup_pending"] is True
    original = deepcopy(state.document["scope"]["intents"])
    api.lose_at = None
    assert produce(setup)["owned_fixture"] is True
    assert {label: intent["key"] for label, intent in original.items()} == {
        label: state.document["scope"]["intents"][label]["key"] for label in original
    }
    assert api.mutations == 5


def test_durable_intent_fails_before_first_mutation(setup):
    state, api, owner, product, inputs, path = setup
    state.fail_at = 1
    with pytest.raises(OSError, match="durable state"):
        produce(setup)
    assert api.mutations == 0 and not path.exists()


def test_unavailable_source_keeps_original_intents_without_packet(setup):
    state, api, owner, product, inputs, path = setup
    api.deny_sources = True
    with pytest.raises(RuntimeError, match="HTTP 404"):
        produce(setup)
    assert state.document["scope"]["cleanup_pending"] is True
    assert "source_0" in state.document["scope"]["intents"] and not path.exists()


def test_rejects_changed_source_and_wrong_owner_before_mutation(setup):
    state, api, owner, product, inputs, path = setup
    with pytest.raises(ValueError, match="distinct"):
        produce((state, api, owner, product, [inputs[0], inputs[0]], path))
    with pytest.raises(ValueError, match="authenticated fixture owner"):
        produce((state, api, {**owner, "actor_id": "outsider"}, product, inputs, path))
    assert api.mutations == 0
    produce(setup)
    altered = deepcopy(inputs)
    altered[1]["record_revision"] = "c" * 40
    with pytest.raises(ValueError, match="original scope fixture"):
        produce((state, api, owner, product, altered, path))


def test_duplicate_operation_must_keep_the_original_receipt(setup):
    state, api, owner, product, inputs, path = setup
    api.lose_at = 1
    with pytest.raises(ConnectionError):
        produce(setup)
    operation_key = state.document["scope"]["intents"]["second_project"]["key"]
    api.operations[operation_key] = {"project_id": "unowned-project"}
    api.lose_at = None
    with pytest.raises(ValueError, match="Second scope binding"):
        produce(setup)
    assert state.document["scope"]["cleanup_pending"] is True
    assert not path.exists()


@pytest.fixture
def pending_retirement(tmp_path):
    state = State()
    api = ProductAPI()
    owner = {
        "actor_id": state.binding["actor_id"],
        "tenant_id": state.binding["tenant_id"],
        "access_token": "owner",
    }
    product = {
        "product_id": "owned-product",
        "project_id": "first",
        "product_name": "Owned Product",
    }
    inputs = [
        {
            "selection": {
                "kind": "repository",
                "provider": "github",
                "connection_ref": {"kind": "github_installation", "id": str(index + 1)},
                "namespace": "example-org",
                "native_id": "repository-" + str(index),
            },
            "record_revision": "b" * 40,
            "release_target_date": f"202{index + 7}-12-01",
        }
        for index in range(2)
    ]
    prepare_scope_fixture(
        state,
        api,
        "https://example.invalid",
        owner,
        product,
        inputs,
        tmp_path / "scope.json",
    )
    document = {"request": {"name": "Owned Product"}}
    manifest = {
        "product_id": "owned-product",
        "project_id": "first",
        "idempotency_key": "created-product-key",
    }
    state.save(
        {
            **state.document,
            "scope": {**state.document["scope"], "case": "create"},
            "cases": {"create": {"document": document, "manifest": manifest}},
        }
    )
    restored = []

    def save(run_id, value, new_manifest):
        assert run_id == state.binding["run_id"]
        updated = deepcopy(state.document)
        updated["cases"]["create"].update(document=value, manifest=new_manifest)
        state.save(updated)

    def run(case, value):
        assert case == "create" and value == document
        return SimpleNamespace(
            _read=lambda: (
                deepcopy(state.document["cases"]["create"]["document"]),
                deepcopy(state.document["cases"]["create"]["manifest"]),
            ),
            store=SimpleNamespace(save=save),
        )

    provider = SimpleNamespace(
        state=state,
        request=api,
        owner=owner,
        editor_membership=None,
        membership=SimpleNamespace(restore=lambda state: restored.append("owner")),
        base_url="https://example.invalid",
        headers=lambda identity: {"Authorization": "Bearer owner"},
        run=run,
        observer=owner,
    )
    return provider, restored


def test_scope_compensation_persists_original_intent_before_retirement(
    pending_retirement,
):
    provider, restored = pending_retirement
    prepare_scope_retirement(provider, "create")
    scope = provider.state.document["scope"]
    manifest = provider.state.document["cases"]["create"]["manifest"]
    assert manifest["scope"]["retire_key"] == scope["retire_key"]
    assert restored == ["owner"]
    assert scope["cleanup_pending"] is True
    prepare_scope_retirement(provider, "create")
    assert provider.state.document["cases"]["create"]["manifest"] == manifest
    assert restored == ["owner", "owner"]


def test_interrupted_compensation_replays_original_intent(pending_retirement):
    provider, restored = pending_retirement
    provider.state.fail_at = provider.state.saved + 2
    with pytest.raises(OSError, match="durable state write failed"):
        prepare_scope_retirement(provider, "create")
    scope = provider.state.document["scope"]
    assert scope["cleanup_pending"] is True and scope["retire_key"]
    assert provider.state.document["cases"]["create"]["manifest"].get("scope") is None
    provider.state.fail_at = None
    prepare_scope_retirement(provider, "create")
    assert (
        provider.state.document["cases"]["create"]["manifest"]["scope"]["retire_key"]
        == scope["retire_key"]
    )
    assert restored == ["owner", "owner"]


def test_scope_cleanup_failure_retains_original_claim_and_history(pending_retirement):
    provider, _ = pending_retirement
    prepare_scope_retirement(provider, "create")
    original = deepcopy(provider.state.document["scope"])
    with pytest.raises(RuntimeError, match="did not confirm"):
        confirm_scope_retirement(provider, {"fixture_removed": False})
    assert provider.state.document["scope"] == original
    with pytest.raises(RuntimeError, match="remains readable"):
        confirm_scope_retirement(
            provider,
            {
                "fixture_removed": True,
                "retire_key": original["retire_key"],
                "scope_project_id": "second",
            },
        )
    assert provider.state.document["scope"] == original
    manifest = deepcopy(provider.state.document["cases"]["create"]["manifest"])
    prepare_scope_retirement(provider, "create")
    assert provider.state.document["cases"]["create"]["manifest"] == manifest
    provider.request.projects.pop("second")
    provider.state.fail_at = provider.state.saved + 1
    with pytest.raises(OSError, match="durable state write failed"):
        confirm_scope_retirement(
            provider,
            {
                "fixture_removed": True,
                "retire_key": original["retire_key"],
                "scope_project_id": "second",
            },
        )
    assert provider.state.document["scope"] == original
    provider.state.fail_at = None
    prepare_scope_retirement(provider, "create")
    confirm_scope_retirement(
        provider,
        {
            "fixture_removed": True,
            "retire_key": original["retire_key"],
            "scope_project_id": "second",
        },
    )
    assert provider.state.document["scope"]["cleanup_pending"] is False
    assert provider.state.document["scope"]["intents"] == original["intents"]


def test_scope_retirement_refuses_external_project_update(pending_retirement):
    provider, _ = pending_retirement
    provider.request.projects["second"]["configuration"]["financials"][
        "release_target_date"
    ] = "2035-01-01"
    with pytest.raises(ValueError, match="differs from its original ownership"):
        prepare_scope_retirement(provider, "create")
    assert provider.state.document["scope"]["cleanup_pending"] is True
    assert provider.state.document["scope"].get("retire_key") is None


def test_scope_retirement_restores_editor_before_owner_or_refuses_cleanup(
    pending_retirement,
):
    provider, restored = pending_retirement

    def unavailable(state):
        restored.append("editor")
        raise RuntimeError("membership restoration failed")

    provider.editor_membership = SimpleNamespace(restore=unavailable)
    with pytest.raises(RuntimeError, match="membership restoration failed"):
        prepare_scope_retirement(provider, "create")
    scope = provider.state.document["scope"]
    assert restored == ["editor"] and scope["cleanup_pending"] is True
    assert scope["retire_key"]
    assert provider.state.document["cases"]["create"]["manifest"].get("scope") is None
    provider.editor_membership = SimpleNamespace(
        restore=lambda state: restored.append("editor")
    )
    prepare_scope_retirement(provider, "create")
    assert restored == ["editor", "editor", "owner"]
    assert (
        provider.state.document["cases"]["create"]["manifest"]["scope"]["retire_key"]
        == scope["retire_key"]
    )


def test_connected_scope_compensation_requires_pinned_fixture_mode(pending_retirement):
    provider, restored = pending_retirement
    provider.state.binding = {**provider.state.binding, "scope_fixture_mode": "connected_scope"}
    provider.state.save({**provider.state.document, "binding": provider.state.binding})
    prepare_scope_retirement(provider, "create")
    manifest = provider.state.document["cases"]["create"]["manifest"]
    assert manifest["scope_fixture_mode"] == "connected_scope"
    assert restored == ["owner"]
    prepare_scope_retirement(provider, "create")
    assert provider.state.document["cases"]["create"]["manifest"] == manifest
    tampered = deepcopy(provider.state.document)
    tampered["cases"]["create"]["manifest"].pop("scope_fixture_mode")
    provider.state.save(tampered)
    with pytest.raises(ValueError, match="protected fixture mode"):
        prepare_scope_retirement(provider, "create")
    assert provider.state.document["scope"]["cleanup_pending"] is True
