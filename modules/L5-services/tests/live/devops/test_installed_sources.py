"""Installed document readbacks exercise real adapters with external HTTP mocked."""

import base64
import hashlib
import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import test_context_selector as context_fixtures
import test_selector as selector_fixtures

from context_selector import HTTPSScopedTarget, authorize_channels, qualify_context
from test_context_selector import FakeReleaseReader
from test_selector import offline_policy


selector_case = selector_fixtures.selector_case
live_context = context_fixtures.live_context
CONTENT = b"# Operations\nInspect the original task before retrying.\n"


@pytest.fixture
def installed_source(live_context, monkeypatch):
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", "test-secret-key-do-not-use-in-production")
    from src.products.source_routes import binding_view

    manifest, path, directory, prior, state, driver, reader = live_context
    context = manifest["context"]
    now = datetime.now(UTC)
    manifest["authorization"]["expires_at"] = (now + timedelta(hours=1)).isoformat()
    context["scope"].update({field: manifest["journey_driver"]["scope"][field] for field in ("product_id", "project_id")})
    source = context["sources"][0]
    native_digest = hashlib.sha1(b"blob " + str(len(CONTENT)).encode() + b"\0" + CONTENT).hexdigest()
    source.update(tenant_id=context["scope"]["tenant_id"], project_id=context["scope"]["project_id"],
                  source_connection_id="installation-example", record_type="document", record_id="123:docs/operations.md",
                  record_revision=native_digest, sha256=hashlib.sha256(CONTENT).hexdigest())
    context["installed_sources"] = {"product_token_env": "DEVOPS_SOURCE_PRODUCT_TOKEN",
                                    "github_token_env": "DEVOPS_SOURCE_GITHUB_TOKEN",
                                    "bindings": {source["record_id"]: "document-binding-example"}}
    context["readback_channels"].pop("source")
    monkeypatch.delenv("DEVOPS_CONTEXT_SOURCE_TOKEN")
    monkeypatch.setenv("DEVOPS_SOURCE_PRODUCT_TOKEN", "independent-source-product")
    monkeypatch.setenv("DEVOPS_SOURCE_GITHUB_TOKEN", "independent-source-github")
    binding = binding_view(SimpleNamespace(
        id="document-binding-example", project_id=source["project_id"], generation=1, revision=2, status="healthy",
        desired={"kind": "document", "provider": "github", "native_id": source["record_id"],
                 "connection_ref": {"kind": "github_installation", "id": source["source_connection_id"]}, "namespace": "456"},
        observed={"source_revision": native_digest, "observed_at": (now - timedelta(minutes=1)).isoformat(),
                  "stale_after": (now + timedelta(minutes=14)).isoformat()}), True)
    case = SimpleNamespace(manifest=manifest, path=path, directory=directory, prior=prior, state=state,
                           binding=binding, profile={**context["scope"], "revision": 1},
                           repository={"id": 123, "name": "product", "owner": {"id": 456, "login": "example"}},
                           blob={"sha": native_digest, "encoding": "base64", "size": len(CONTENT),
                                 "content": base64.b64encode(CONTENT).decode()}, calls=[], pages=None,
                           mutate_after_blob=None, status=200, raw_body=None)
    for observation in state.values():
        if "scope" in observation:
            observation["scope"] = context["scope"]
        if "source_refs" in observation:
            observation["source_refs"] = context["sources"]
    state["later"]["citations"][0]["source_refs"] = context["sources"]

    def handle(request):
        case.calls.append(request)
        if request.url.host in {"gateway.example.org", "api.github.com"}:
            assert request.method == "GET"
            native = request.url.host == "api.github.com"
            assert request.headers["Authorization"] == "Bearer independent-source-" + ("github" if native else "product")
            if case.status != 200:
                return httpx.Response(case.status, headers={"location": "https://untrusted.example.org"})
            if case.raw_body is not None:
                return httpx.Response(200, content=case.raw_body)
            if request.url.path.endswith("/bindings"):
                if case.pages is not None:
                    payload = case.pages.get(request.url.params.get("cursor"))
                else:
                    payload = {"items": [case.binding], "next_cursor": None}
            elif request.url.path.startswith("/api/v1/products/"):
                payload = case.profile
            elif request.url.path == "/repositories/123":
                payload = case.repository
            else:
                assert request.url.path == "/repos/example/product/git/blobs/" + source["record_revision"]
                payload = case.blob
                if case.mutate_after_blob is not None:
                    case.mutate_after_blob(case)
            return httpx.Response(200, json=deepcopy(payload))
        selection = json.loads(request.content) if request.method == "POST" else json.loads(request.url.params["selection"])
        channel = next((name for name, channel in {**context["readback_channels"], **context["action_channels"]}.items()
                        if str(request.url).split("?", 1)[0] == channel["url"]), None)
        assert channel is not None and channel != "source"
        observed = ({"run_id": "later-example"} if channel == "run_later" else
                    state["denials"][selection["case"]] if channel == "denial" else
                    state[{"native_job": "native"}.get(channel, channel)])
        return httpx.Response(200, json={"target_ref": manifest["target_ref"],
                                       "qualification_run_id": manifest["authorization"]["run_id"], "observation": observed})

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    path.write_text(json.dumps(manifest))
    return case


def read_selected(case, *, scope=None, source=None, deadline=None, max_requests=25):
    target = HTTPSScopedTarget(case.manifest, case.directory, "context",
                               deadline=time.monotonic() + 60 if deadline is None else deadline, max_requests=max_requests)
    try:
        return target.read_source(scope or case.manifest["context"]["scope"], source or case.manifest["context"]["sources"][0])
    finally:
        target.close()


def test_installed_source_uses_current_product_authority_and_independent_native_bytes(installed_source):
    case = installed_source
    assert authorize_channels(case.manifest, case.manifest["release"]["source_sha"], case.directory, "context") == case.directory
    observed = read_selected(case)
    assert observed["content"] == CONTENT and observed["revoked"] is False
    assert observed["record_revision"] == case.blob["sha"]
    assert observed["readback"]["binding"] == case.binding
    assert [request.url.host for request in case.calls] == ["gateway.example.org"] * 2 + ["api.github.com"] * 2 + ["gateway.example.org"] * 2
    assert "independent-source" not in json.dumps(observed["readback"])


@pytest.mark.parametrize("mutation", [
    lambda case: case.profile.update(tenant_id="foreign"),
    lambda case: case.binding.update(project_id="foreign"),
    lambda case: case.binding.update(status="restricted"),
    lambda case: case.binding.update(source_revision="0" * 40),
    lambda case: case.binding.update(stale_after=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()),
    lambda case: case.binding["selection"].update(native_id="124:docs/operations.md"),
    lambda case: case.binding["selection"]["connection_ref"].update(id="foreign"),
    lambda case: setattr(case, "pages", {None: {"items": [], "next_cursor": None}}),
    lambda case: setattr(case, "pages", {None: {"items": [case.binding, case.binding], "next_cursor": None}}),
])
def test_invalid_product_sources_never_contact_native_provider(installed_source, mutation):
    mutation(installed_source)
    with pytest.raises(ValueError):
        read_selected(installed_source)
    assert all(request.url.host != "api.github.com" for request in installed_source.calls)


@pytest.mark.parametrize("mutation", [
    lambda case: case.repository.update(id=124),
    lambda case: case.repository["owner"].update(id=457),
    lambda case: case.repository["owner"].update(login="../foreign"),
    lambda case: case.repository.update(name=".."),
    lambda case: case.blob.update(sha="0" * 40),
    lambda case: case.blob.update(encoding="raw"),
    lambda case: case.blob.update(content="not base64"),
    lambda case: case.blob.update(content=base64.b64encode(b"tampered").decode()),
    lambda case: case.blob.update(size=65537),
    lambda case: case.manifest["context"]["sources"][0].update(sha256="0" * 64),
])
def test_native_identity_and_both_content_digests_are_verified(installed_source, mutation):
    mutation(installed_source)
    with pytest.raises(ValueError, match="native"):
        read_selected(installed_source)


@pytest.mark.parametrize("mutation", [
    lambda case: case.binding.update(status="restricted"),
    lambda case: case.binding.update(revision=3),
    lambda case: case.profile.update(project_id="foreign"),
    lambda case: case.manifest["authorization"].update(expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()),
])
def test_product_authority_is_rechecked_after_native_read(installed_source, mutation):
    installed_source.mutate_after_blob = mutation
    with pytest.raises(ValueError):
        read_selected(installed_source)


def test_binding_pagination_is_bounded_and_rechecked(installed_source):
    case = installed_source
    case.pages = {None: {"items": [], "next_cursor": "cursor-next"},
                  "cursor-next": {"items": [case.binding], "next_cursor": None}}
    assert read_selected(case)["content"] == CONTENT
    assert len(case.calls) == 8


@pytest.mark.parametrize("failure", ["cursor-loop", "redirect", "unavailable", "oversize", "non-object"])
def test_bad_provider_responses_fail_closed_without_redirects(installed_source, failure):
    case = installed_source
    if failure == "cursor-loop":
        case.pages = {None: {"items": [], "next_cursor": "again"}, "again": {"items": [], "next_cursor": "again"}}
    elif failure in {"redirect", "unavailable"}:
        case.status = 302 if failure == "redirect" else 503
    else:
        case.raw_body = b"x" * (1024 * 1024 + 1) if failure == "oversize" else b"[]"
    with pytest.raises(ValueError):
        read_selected(case)
    assert all(request.url.host == "gateway.example.org" for request in case.calls)


@pytest.mark.parametrize("kwargs", [{"deadline": 0}, {"max_requests": 5}, {"scope": {"tenant_id": "foreign"}},
                                   {"source": {"record_id": "foreign"}}])
def test_invalid_scope_or_bounds_prevent_external_reads(installed_source, kwargs):
    with pytest.raises(ValueError):
        read_selected(installed_source, **kwargs)
    assert not installed_source.calls


@pytest.mark.parametrize("token", ["DEVOPS_SOURCE_PRODUCT_TOKEN", "DEVOPS_PRODUCT_TOKEN", "DEVOPS_MCP_TOKEN",
                                  "DEVOPS_CONTEXT_RUN_TOKEN", "DEVOPS_RELEASE_TOKEN"])
def test_source_credentials_cannot_alias_other_authorities(installed_source, token):
    case = installed_source
    case.manifest["context"]["installed_sources"]["github_token_env"] = token
    with pytest.raises(ValueError, match="independent"):
        authorize_channels(case.manifest, case.manifest["release"]["source_sha"], case.directory, "context")
    assert not case.calls


@pytest.mark.parametrize("record_id", ["https://foreign.example.org", "123:../private", "123:/etc/passwd", "123:docs//file"])
def test_source_selectors_cannot_supply_a_url_or_traversal(installed_source, record_id):
    case = installed_source
    case.manifest["context"]["sources"][0]["record_id"] = record_id
    case.manifest["context"]["installed_sources"]["bindings"] = {record_id: "document-binding-example"}
    with pytest.raises(ValueError):
        read_selected(case)
    assert not case.calls


def test_installed_source_cannot_fall_back_to_custom_channel(installed_source):
    case = installed_source
    case.manifest["context"]["readback_channels"]["source"] = {
        "source": "source", "url": "https://custom.example.org", "token_env": "DEVOPS_SOURCE_PRODUCT_TOKEN"}
    with pytest.raises(ValueError, match="bounded actions"):
        authorize_channels(case.manifest, case.manifest["release"]["source_sha"], case.directory, "context")


def test_context_selector_emits_installed_source_readback_without_custom_source_server(installed_source):
    case = installed_source
    run_id = qualify_context(case.manifest["release"]["source_sha"], case.path, case.directory,
                            policy_reader=offline_policy,
                            release_reader_factory=lambda manifest, **kwargs: FakeReleaseReader(manifest, case.prior, **kwargs))
    result = json.loads((case.directory / f"devops-{run_id}-context.json").read_text())
    assert result["citation"]["source_refs"] == case.manifest["context"]["sources"]
    assert len(case.calls) == 18
    assert all(request.url.host != "context-source.example.org" for request in case.calls)
