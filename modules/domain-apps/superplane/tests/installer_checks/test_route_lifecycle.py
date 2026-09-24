"""Ordered publication and actual Gateway cache checks at lifecycle boundaries."""

import copy
import importlib.util
import json
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from installation.config import MODULE, Refusal
from installation.runner import Installer
from .test_ambiguous_s3 import key_is, restart
from .test_complete_command import setup


def pending_before_put(installer, tools):
    original = tools.call

    def interrupt(args, **kwargs):
        if "put-object" in args and key_is(args, installer.route_key):
            raise KeyboardInterrupt
        return original(args, **kwargs)

    tools.call = interrupt
    return original


@pytest.mark.parametrize("action", ["compensate", "recover", "cleanup"])
@pytest.mark.parametrize("present", [False, True])
def test_uncommitted_enable_is_never_published_by_compensation(
    tmp_path, environment, release, monkeypatch, action, present
):
    installer, tools = setup(tmp_path / "initial", environment, release, monkeypatch)
    if present:
        installer.route(False)
    original = pending_before_put(installer, tools)
    with pytest.raises(KeyboardInterrupt):
        if action == "recover":
            with installer.exclusive():
                installer.route(True)
        else:
            installer.route(True)
    previous = json.loads(installer.receipt_path.read_text())
    assert previous["pending_route"]["value"]["enabled"] is True
    writes = []

    def record(args, **kwargs):
        result = original(args, **kwargs)
        if "put-object" in args and key_is(args, installer.route_key):
            writes.append(tools.route["enabled"])
        return result

    tools.call = record
    if action == "cleanup":
        Installer(environment, release, tmp_path / "cleanup", tools).cleanup(previous)
    elif action == "recover":
        resumed = restart(installer, tools)
        resumed.recover_lock(resumed.run_id)
    else:
        installer.disable_route()
    assert writes == [False]
    assert tools.route["enabled"] is False


def test_late_enable_commit_is_fenced_by_retained_compensation_identity(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    installer.route(False)
    original = pending_before_put(installer, tools)
    with pytest.raises(KeyboardInterrupt):
        installer.route(True)
    pending_enable = copy.deepcopy(installer.receipt["pending_route"]["value"])
    writes = []
    raced = False

    def late_commit(args, **kwargs):
        nonlocal raced
        if "put-object" in args and key_is(args, installer.route_key) and not raced:
            raced = True
            persisted = json.loads(installer.receipt_path.read_text())
            assert persisted["pending_route"]["superseded_enable"] == pending_enable
            tools.route = pending_enable
            tools.route_etag = '"late-enable"'
        result = original(args, **kwargs)
        if "put-object" in args and key_is(args, installer.route_key):
            writes.append(tools.route["enabled"])
        return result

    tools.call = late_commit
    installer.disable_route()
    assert raced and writes == [False]
    assert "pending_route" not in installer.receipt


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def gateway_cache(monkeypatch):
    """Load the maintained proxy and its real five-second cache, with only S3/HTTP faked."""
    # This suite runs outside Gateway's conftest, but importing its proxy initializes
    # the real authentication middleware. Supply a test-only signing key first.
    monkeypatch.setenv(
        "BG_TOKEN_SECRET_KEY", "installer-test-key-do-not-use-in-production"
    )
    source = MODULE.parents[1] / "gateway/src/domain_proxy/superplane.py"
    spec = importlib.util.spec_from_file_location("installer_gateway_proxy", source)
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    clock = Clock()
    monkeypatch.setattr(proxy, "time", clock)
    monkeypatch.setattr("installation.runner.time", clock)
    monkeypatch.setenv("BG_ENVIRONMENT", "dev")
    monkeypatch.setenv("BG_SUPERPLANE_ROUTE_BUCKET", "adp-terraform-state-879318057152")
    monkeypatch.delenv("FEATURE_SUPERPLANE_ENABLED", raising=False)

    def attach(tools):
        monkeypatch.setattr(
            proxy.boto3,
            "client",
            lambda *args, **kwargs: SimpleNamespace(
                get_object=lambda **kwargs: {
                    "Body": BytesIO(json.dumps(tools.route).encode())
                }
            ),
        )
        app = FastAPI()
        app.include_router(proxy.router)
        app.add_api_route("/health", lambda: {"status": "healthy"})
        client = TestClient(app)
        observations = []

        def get(url, headers=None, **kwargs):
            assert not headers or "Authorization" not in headers
            path = url.split("/api", 1)[1]
            response = client.get(path, headers=headers, follow_redirects=False)
            observations.append((clock.now, path, response.status_code))
            return response

        monkeypatch.setattr("installation.runner.httpx.get", get)
        return proxy, client, clock, observations

    return attach


def warm_enabled_cache(proxy, client):
    proxy._cache = (0, {})
    assert client.get("/superplane/v1/workspaces").status_code == 401
    assert proxy._cache[1]["enabled"] is True


@pytest.mark.parametrize(
    "action", ["cleanup", "recover", "rollback", "failed-verification"]
)
def test_lifecycle_observes_actual_gateway_cache_off_before_completion(
    tmp_path, environment, release, monkeypatch, gateway_cache, action
):
    installer, tools = setup(tmp_path / "initial", environment, release, monkeypatch)
    installer.preflight()
    installer.execute(installer.receipt["plan_sha256"], "verified-user")
    previous = copy.deepcopy(installer.receipt)
    proxy, client, clock, observations = gateway_cache(tools)
    warm_enabled_cache(proxy, client)
    started = clock.now
    lifecycle = Installer(environment, release, tmp_path / action, tools)
    tools.installer = lifecycle
    original = tools.call
    workload_mutations = []

    def record(args, **kwargs):
        if "delete-object" in args:
            assert clock.now >= started + 5
            assert lifecycle.receipt["route_disabled_verification"]["status"] == 404
        if args[0] == "kubectl" and any(
            v in args for v in ("delete", "apply", "create")
        ):
            workload_mutations.append(clock.now)
            assert clock.now >= started + 5
            assert lifecycle.receipt["route_disabled_verification"]["status"] == 404
        return original(args, **kwargs)

    tools.call = record
    if action == "cleanup":
        lifecycle.cleanup(previous)
        assert workload_mutations and lifecycle.receipt["status"] == "workloads-removed"
    elif action == "recover":
        lifecycle.plan()
        lifecycle.check_route_owner()
        lifecycle.receipt["public_route_enabled"] = True
        with pytest.raises(RuntimeError):
            with lifecycle.exclusive():
                raise RuntimeError("crashed before compensation")
        lifecycle = restart(lifecycle, tools)
        lifecycle.recover_lock(lifecycle.run_id)
        assert tools.lock_object is None
    elif action == "rollback":
        monkeypatch.setattr(lifecycle, "images", lambda: None)
        monkeypatch.setattr(lifecycle, "secrets", lambda: None)
        monkeypatch.setattr(
            lifecycle,
            "database",
            lambda: lifecycle.receipt.update(
                database_observations={
                    "runtime-url": {"revision": release["schema"]["observed"]["head"]}
                }
            ),
        )

        def restore():
            assert clock.now >= started + 5
            assert lifecycle.receipt["route_disabled_verification"]["status"] == 404
            raise Refusal("stop after verified pre-rollback disable")

        monkeypatch.setattr(lifecycle, "foundations", restore)
        with pytest.raises(Refusal, match="verified pre-rollback"):
            lifecycle.rollback(previous, "verified-user")
    else:
        # Run the real execute failure handler after public verification has populated cache.
        lifecycle.plan()
        monkeypatch.setattr(
            lifecycle,
            "preflight",
            lambda: lifecycle.receipt.update(plan_sha256="approved"),
        )
        for name in ("foundations", "migrate", "rollout", "private_services"):
            monkeypatch.setattr(lifecycle, name, lambda: None)
        monkeypatch.setattr(lifecycle, "bootstrap", lambda _: None)
        enabled_at = []

        def failed_verify(_):
            warm_enabled_cache(proxy, client)
            enabled_at.append(clock.now)
            raise Refusal("negative authorization check failed")

        monkeypatch.setattr(lifecycle, "verify", failed_verify)
        with pytest.raises(Refusal, match="negative authorization check failed"):
            lifecycle.execute("approved", "verified-user")
        assert clock.now >= enabled_at[0] + 5
        assert lifecycle.receipt["status"] == "recovery-required"
    assert tools.route["enabled"] is False
    assert lifecycle.receipt["route_disabled_verification"]["adp_healthy"] is True
    assert "route_disable_pending" not in lifecycle.receipt
    assert any(
        path.endswith("/workspaces") and status == 401
        for _, path, status in observations
    )
    off = [
        (at, status)
        for at, path, status in observations
        if path.endswith("/workspaces")
    ]
    assert off[-1][1] == 404 and observations[-1][1:] == ("/health", 200)


@pytest.mark.parametrize(
    "failure", ["cached-enabled", "health", "wrong-404", "upstream-404"]
)
def test_unverified_disable_retains_lock_and_never_removes_workloads(
    tmp_path, environment, release, monkeypatch, failure
):
    installer, tools = setup(tmp_path / "initial", environment, release, monkeypatch)
    installer.preflight()
    installer.execute(installer.receipt["plan_sha256"], "verified-user")
    previous, objects = copy.deepcopy(installer.receipt), copy.deepcopy(tools.objects)
    clock = Clock()
    monkeypatch.setattr("installation.runner.time", clock)
    original = tools.http

    def unconfirmed(url, **kwargs):
        if url.endswith("/v1/workspaces"):
            if failure == "cached-enabled":
                return httpx.Response(401)
            if failure == "wrong-404":
                return httpx.Response(404, json={"detail": "upstream missing"})
            if failure == "upstream-404":
                return httpx.Response(
                    404,
                    json={"detail": "Not found"},
                    headers={"X-Superplane-Release": "x"},
                )
        if (
            url.endswith("/api/health")
            and failure == "health"
            and tools.route["enabled"] is False
        ):
            return httpx.Response(503)
        return original(url, **kwargs)

    monkeypatch.setattr("installation.runner.httpx.get", unconfirmed)
    cleanup = Installer(environment, release, tmp_path / "cleanup", tools)
    with pytest.raises(Refusal):
        cleanup.cleanup(previous)
    assert tools.objects == objects
    assert cleanup.receipt["route_disable_pending"] is True
    assert tools.lock_object is not None and cleanup.receipt["remote_lock"]


def test_first_off_replica_does_not_bypass_other_replica_cache_lifetime(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(tmp_path, environment, release, monkeypatch)
    installer.route(True)
    clock = Clock()
    monkeypatch.setattr("installation.runner.time", clock)
    original = tools.http
    observations = []

    def fresh_replica(url, **kwargs):
        response = original(url, **kwargs)
        if url.endswith("/installation-support"):
            response = httpx.Response(200, json=dict(response.json(), cache_seconds=5))
        if url.endswith("/v1/workspaces"):
            observations.append((clock.now, response.status_code))
        return response

    monkeypatch.setattr("installation.runner.httpx.get", fresh_replica)
    installer.disable_route()
    assert observations[0] == (100.0, 404)
    assert observations[-1] == (105.0, 404)
    assert installer.receipt["route_disabled_verification"]["cache_seconds"] == 5


def test_disable_observation_is_bounded_by_environment_deadline(
    tmp_path, environment, release, monkeypatch
):
    installer, tools = setup(
        tmp_path, dict(environment, timeout_seconds=2), release, monkeypatch
    )
    clock = Clock()
    monkeypatch.setattr("installation.runner.time", clock)
    original = tools.http

    def longer_cache(url, **kwargs):
        response = original(url, **kwargs)
        if url.endswith("/installation-support"):
            response = httpx.Response(200, json=dict(response.json(), cache_seconds=5))
        return response

    monkeypatch.setattr("installation.runner.httpx.get", longer_cache)
    with pytest.raises(Refusal, match="not observed"):
        installer.disable_route()
    assert clock.now == 102.0
    assert installer.receipt["route_disable_pending"] is True
