"""Submission-loss, scope and restart controls for the installed Product journey."""

import json
from copy import deepcopy

import httpx
import pytest

from l5_services.adapters.transform.qualification_bootstrap import BootstrapIncomplete, prepare, validate_bootstrap
from l5_services.adapters.transform.qualification_release import load_fixture


@pytest.fixture
def fixture():
    return {"source_sha": "a" * 40, "bootstrap": [{
        "scope": {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"},
        "idempotency_key": "transform-fixture-refactor-01", "max_wait_seconds": 60,
        "input": {"service_id": "transform", "configuration_revision": 2, "binding_id": "binding-a",
            "capability": "refactor", "sources": [{"tenant_id": "tenant-a", "project_id": "project-a",
                "record_type": "repository_commit", "record_id": "repo-a", "record_revision": "b" * 40,
                "source_connection_id": "connection-a"}],
            "transform_input": {"goal": {"kind": "refactor", "summary": "Extract the duplicated parser"}}},
    }]}


class Product:
    def __init__(self, fixture):
        self.selection = fixture["bootstrap"][0]
        self.scope = self.selection["scope"]
        self.run = {"run_id": "run-a", "scope": self.scope, "input": self.selection["input"], "state": "succeeded"}
        self.result = {"run_id": "run-a", "result_id": "result-a", "revision": 1, "scope": self.scope}
        self.posts = []
        self.requests = []
        self.lose_ack = False
        self.configuration_state = "ready"
        self.actor_id = "fixture-user-a"

    def respond(self, request):
        self.requests.append(request)
        if request.url.path == "/auth/me":
            return httpx.Response(200, json={"user_id": self.actor_id, "org_id": self.scope["tenant_id"],
                                            "account_type": "human"})
        if request.method == "POST":
            self.posts.append((request.headers["Idempotency-Key"], json.loads(request.content)))
            assert request.url.path.endswith("/runs")
            if self.lose_ack:
                self.lose_ack = False
                raise httpx.ReadTimeout("lost acknowledgment", request=request)
            return httpx.Response(202, json=self.run)
        if request.url.path.endswith("/configuration"):
            return httpx.Response(200, json={"configuration_id": "config-a", "revision": 2,
                "scope": self.scope, "state": self.configuration_state, "settings": {"enabled": True}})
        if request.url.path.endswith("/bindings/binding-a"):
            return httpx.Response(200, json={"binding_id": "binding-a", "configuration_id": "config-a",
                                           "scope": self.scope, "state": "ready"})
        if request.url.path.endswith("/runs/run-a"):
            return httpx.Response(200, json=self.run)
        if request.url.path.endswith("/results"):
            return httpx.Response(200, json={"items": [self.result]})
        raise AssertionError(request.url.path)

    def client(self):
        return httpx.AsyncClient(base_url="https://gateway.invalid", transport=httpx.MockTransport(self.respond))


@pytest.mark.asyncio
async def test_lost_ack_replays_exact_request_and_emitter_never_submits(fixture, tmp_path):
    product = Product(fixture)
    product.lose_ack = True
    async with product.client() as client:
        with pytest.raises(httpx.ReadTimeout):
            await prepare(fixture, client, tmp_path)
        with pytest.raises(BootstrapIncomplete, match="bootstrap_submit_outcome_unknown"):
            await prepare(fixture, client, tmp_path, allow_submit=False)
        assert len(product.posts) == 1
        selected = await prepare(fixture, client, tmp_path)
        assert product.posts == [(fixture["bootstrap"][0]["idempotency_key"], fixture["bootstrap"][0]["input"])] * 2
        assert selected["runs"] == [{**product.scope, "run_id": "run-a", "result_id": "result-a", "result_revision": 1}]
        assert await prepare(fixture, client, tmp_path, allow_submit=False) == selected
        assert len(product.posts) == 2


@pytest.mark.asyncio
async def test_deadline_survives_restart_but_completed_run_can_be_read(fixture, tmp_path):
    product = Product(fixture)
    product.run["state"] = "running"
    now = [100]

    async def advance(seconds):
        now[0] += seconds

    async with product.client() as client:
        with pytest.raises(BootstrapIncomplete, match="bootstrap_wait_exhausted"):
            await prepare(fixture, client, tmp_path, clock=lambda: now[0], sleep=advance)
        assert now[0] == 160
        with pytest.raises(BootstrapIncomplete, match="bootstrap_wait_exhausted"):
            await prepare(fixture, client, tmp_path, clock=lambda: now[0], sleep=advance)
        assert now[0] == 160 and len(product.posts) == 1
        product.run["state"] = "succeeded"
        await prepare(fixture, client, tmp_path, clock=lambda: now[0])
        assert len(product.posts) == 1


@pytest.mark.asyncio
async def test_changed_request_cannot_reuse_journal(fixture, tmp_path):
    product = Product(fixture)
    product.lose_ack = True
    async with product.client() as client:
        with pytest.raises(httpx.ReadTimeout):
            await prepare(fixture, client, tmp_path)
        fixture["bootstrap"][0]["input"]["transform_input"]["goal"]["summary"] = "Another task"
        with pytest.raises(BootstrapIncomplete, match="bootstrap_selection_changed"):
            await prepare(fixture, client, tmp_path)
    assert len(product.posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unready", "foreign_run", "foreign_result", "other_result", "failed_run"])
async def test_invalid_readbacks_never_select_or_resubmit(fixture, tmp_path, failure):
    product = Product(fixture)
    if failure == "unready":
        product.configuration_state = "blocked"
    elif failure == "foreign_run":
        product.run = {**product.run, "scope": {**product.scope, "tenant_id": "foreign"}}
    elif failure == "foreign_result":
        product.result = {**product.result, "scope": {**product.scope, "project_id": "foreign"}}
    elif failure == "other_result":
        product.result["run_id"] = "some-other-run"
    else:
        product.run["state"] = "failed"
    async with product.client() as client:
        with pytest.raises(ValueError):
            await prepare(fixture, client, tmp_path)
    assert len(product.posts) == (0 if failure == "unready" else 1)
    for path in tmp_path.glob("*.jsonl"):
        assert '"state": "selected"' not in path.read_text()


@pytest.mark.asyncio
async def test_emitter_without_journal_is_read_only(fixture, tmp_path):
    product = Product(fixture)
    async with product.client() as client:
        with pytest.raises(BootstrapIncomplete, match="bootstrap_not_run"):
            await prepare(fixture, client, tmp_path, allow_submit=False)
    assert all(request.method == "GET" for request in product.requests)


@pytest.mark.asyncio
async def test_changed_actor_cannot_resubmit_after_lost_ack(fixture, tmp_path):
    product = Product(fixture)
    product.lose_ack = True
    async with product.client() as client:
        with pytest.raises(httpx.ReadTimeout):
            await prepare(fixture, client, tmp_path)
        product.actor_id = "another-entitled-user"
        with pytest.raises(BootstrapIncomplete, match="bootstrap_selection_changed"):
            await prepare(fixture, client, tmp_path)
    assert len(product.posts) == 1


def test_fixture_schema_is_bounded_and_source_scoped(fixture, tmp_path):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture))
    path.chmod(0o600)
    assert load_fixture("a" * 40, path) == fixture
    invalid = deepcopy(fixture)
    invalid["bootstrap"][0]["input"]["sources"][0]["tenant_id"] = "foreign"
    with pytest.raises(ValueError, match="source differs"):
        validate_bootstrap(invalid["bootstrap"])
    invalid = deepcopy(fixture)
    invalid["runs"] = []
    path.write_text(json.dumps(invalid))
    with pytest.raises(ValueError):
        load_fixture("a" * 40, path)
