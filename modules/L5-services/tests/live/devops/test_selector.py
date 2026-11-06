"""No network: guarded SCN-01 selector and private evidence write checks."""

import json
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest

from selector import authorize, qualify_journeys
from test_journeys import fixture as release_fixture

NOW = datetime(2026, 10, 28, tzinfo=UTC)
RUN_ID = "devops-offline-run-example"


@pytest.fixture
def selector_case(tmp_path, monkeypatch):
    monkeypatch.setenv("L5_BROWSER_AUTOMATION", "allowed")
    manifest, prior_reader = release_fixture.__wrapped__()
    manifest["authorization"] = {"source_sha": manifest["release"]["source_sha"],
                                 "run_id": RUN_ID, "scenarios": ["journeys"],
                                 "expires_at": (NOW + timedelta(hours=2)).isoformat()}
    names = ("release", "ingress", "capabilities", "journeys")
    manifest["readback_channels"] = {name: {"source": name,
        "url": f"https://{name}.example.org/independent-readback", "token_env": f"DEVOPS_{name.upper()}_TOKEN"}
        for name in names}
    manifest["limits"] = {name: {"max_spend_usd": "0.20", "max_requests": 4, "deadline_seconds": 60}
                          for name in ("native_ui", "investigation", "release_readiness",
                                       "release_verification", "prevention", "knowledge")}
    manifest["ingress_policy"] = {
        "connection_label": "example", "account_id": "123456789012",
        "role_arn": "arn:aws:iam::123456789012:role/example-readonly",
        "distribution_id": "EXAMPLE123", "gateway_origin_id": "example-api-origin",
        "gateway_origin_domain": "api.example.org", "frontend_hostname": "gateway.example.org",
        "launch_path": "/api/v1/products/example/projects/example/services/devops/bindings/example/browser-launch",
        "functions": ["example-sso"], "allowed_invoke_statements": {"example-sso": []}}
    for name in names:
        monkeypatch.setenv(f"DEVOPS_{name.upper()}_TOKEN", f"private-{name}-token")
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    cli = tmp_path / "installed-adp"
    cli.write_text("#!/bin/sh\nexit 1\n")
    cli.chmod(0o700)
    resolver = tmp_path / "adp_deployments.py"
    resolver.write_text("# installed resolver fixture\n")
    resolver.chmod(0o600)
    home = tmp_path / "cli-home"
    home.mkdir(mode=0o700)
    state = tmp_path / "browser-state.json"
    state.write_text("{}")
    state.chmod(0o600)
    scope = {"product_id": "example", "project_id": "example", "binding_id": "example"}
    body = {"service_id": "devops", "configuration_revision": 1, "binding_id": "example",
            "capability": "investigation", "devops_input": {"kind": "investigation", "question": "example"}}
    import hashlib
    manifest["journey_driver"] = {"scope": scope, "gateway_url": "https://gateway.example.org",
        "provider_hostname": "signin.aws.amazon.com", "cli_deployment": "example", "cli_executable": str(cli),
        "cli_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(), "cli_home": str(home),
        "browser_storage_state": str(state), "api_token_env": "DEVOPS_PRODUCT_TOKEN",
        "provider_readback": {"connection_label": "example", "account_id": "123456789012",
            "role_arn": "arn:aws:iam::123456789012:role/example-readonly", "region": "us-east-1",
            "source_connection_id": "example-source"},
        "mcp_token_env": "DEVOPS_MCP_TOKEN", "inputs": {entry: body.copy()
            for entry in ("product", "cli", "mcp", "ui")}, "receipts": {
                entry: f"receipt-{entry}-example-0001" for entry in ("product", "cli", "mcp")}}
    manifest["limits"]["investigation"]["max_requests"] = 12
    monkeypatch.setenv("DEVOPS_PRODUCT_TOKEN", "private-product-token")
    monkeypatch.setenv("DEVOPS_MCP_TOKEN", "private-mcp-token")
    path = tmp_path / "owner-manifest.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o600)
    return manifest, path, directory, prior_reader


def offline_drive(manifest, directory, *, deadline):
    return {entry: {"run_id": f"run-{entry}-example", "native_id": f"native-{entry}-example"}
            for entry in ("product", "cli", "mcp", "ui")}


def offline_policy(manifest, **kwargs):
    scope = manifest["ingress_policy"]
    return {"aws_account": scope["account_id"], "function_urls": [], "public_launchers": [],
            "verified_functions": sorted(scope["allowed_invoke_statements"]),
            "distribution": {"distribution_id": scope["distribution_id"],
                             "gateway_origin_id": scope["gateway_origin_id"],
                             "frontend_hostname": scope["frontend_hostname"],
                             "launch_behavior": "/api/*", "status": "Deployed"}}


class FakeReader:
    def __init__(self, manifest, prior_reader, *, deadline, max_requests):
        self.manifest = manifest
        self.prior_reader = prior_reader
        self.deadline = deadline
        self.max_requests = max_requests
        self.called = []
        self.closed = False

    def close(self):
        self.closed = True

    def read_release(self, target):
        self.called.append("release")
        return self.prior_reader.read_release(target)

    def read_ingress(self, target):
        self.called.append("ingress")
        return self.prior_reader.read_ingress(target)

    def read_capabilities(self, target):
        self.called.append("capabilities")
        return [{**item, "qualification_run_id": RUN_ID,
                 "usage": {"spend_usd": "0.01", "requests": 2, "elapsed_seconds": 10}}
                for item in self.prior_reader.read_capabilities(target)]

    def read_journeys(self, target):
        self.called.append("journeys")
        return [{**item, "qualification_run_id": RUN_ID,
                 "run_id": f"run-{item['entry_point']}-example",
                 "native_id": f"native-{item['entry_point']}-example",
                 "scope": self.manifest["journey_driver"]["scope"],
                 "usage": {"spend_usd": "0.01", "requests": 1, "elapsed_seconds": 5}}
                for item in self.prior_reader.read_journeys(target)]


def test_bounded_selector_writes_fresh_private_evidence(selector_case):
    manifest, path, directory, prior_reader = selector_case
    active = []
    def factory(fixture, **kwargs):
        reader = FakeReader(fixture, prior_reader, **kwargs)
        active.append(reader)
        return reader
    assert qualify_journeys(manifest["release"]["source_sha"], path, directory,
                            reader_factory=factory, policy_reader=offline_policy, journey_runner=offline_drive, now=NOW) == RUN_ID
    assert active[0].called == ["release", "release", "capabilities", "journeys"]
    assert active[0].closed
    evidence_file = directory / f"devops-{RUN_ID}-journeys.json"
    assert stat.S_IMODE(evidence_file.stat().st_mode) == 0o600
    assert json.loads(evidence_file.read_text())["authorization_run_id"] == RUN_ID
    with pytest.raises(FileExistsError):
        qualify_journeys(manifest["release"]["source_sha"], path, directory,
                         reader_factory=factory, policy_reader=offline_policy, journey_runner=offline_drive, now=NOW)


def test_journey_refuses_wrong_release_before_first_party_action(selector_case):
    manifest, path, directory, prior_reader = selector_case
    observed = []

    class WrongRelease(FakeReader):
        def read_release(self, target):
            release = super().read_release(target)
            return {**release, "artifacts": {**release["artifacts"], "worker": "0" * 64}}

    def factory(fixture, **kwargs):
        reader = WrongRelease(fixture, prior_reader, **kwargs)
        observed.append(reader)
        return reader

    with pytest.raises(ValueError, match="independent deployed release"):
        qualify_journeys(manifest["release"]["source_sha"], path, directory,
                         reader_factory=factory, policy_reader=offline_policy,
                         journey_runner=lambda *args, **kwargs: pytest.fail("paid action before release readback"),
                         now=NOW)
    assert observed[0].called == ["release"]
    assert observed[0].closed
    assert list(directory.iterdir()) == []


def test_journey_refetches_release_after_action_to_reject_drift(selector_case):
    manifest, path, directory, prior_reader = selector_case
    actions = []

    class DriftingRelease(FakeReader):
        def read_release(self, target):
            release = super().read_release(target)
            if self.called.count("release") == 2:
                return {**release, "source_sha": "0" * 40}
            return release

    def drive(fixture, evidence_dir, *, deadline):
        actions.append("journeys")
        return offline_drive(fixture, evidence_dir, deadline=deadline)

    with pytest.raises(ValueError, match="independent deployed release"):
        qualify_journeys(manifest["release"]["source_sha"], path, directory,
                         reader_factory=lambda fixture, **kwargs: DriftingRelease(
                             fixture, prior_reader, **kwargs),
                         policy_reader=offline_policy, journey_runner=drive, now=NOW)
    assert actions == ["journeys"]
    assert list(directory.iterdir()) == []


def test_selected_ingress_policy_replaces_https_ingress_readback(selector_case):
    manifest, path, directory, prior_reader = selector_case
    manifest["readback_channels"].pop("ingress")
    path.write_text(json.dumps(manifest))
    active = []

    class NoHTTPIngress(FakeReader):
        def read_ingress(self, target):
            pytest.fail("independent AWS policy must replace invented HTTPS ingress")

    def factory(fixture, **kwargs):
        reader = NoHTTPIngress(fixture, prior_reader, **kwargs)
        active.append(reader)
        return reader

    assert qualify_journeys(manifest["release"]["source_sha"], path, directory,
                            reader_factory=factory, policy_reader=offline_policy,
                            journey_runner=offline_drive, now=NOW) == RUN_ID
    assert active[0].called == ["release", "release", "capabilities", "journeys"]
    assert active[0].closed


def test_unverified_selected_policy_stops_before_any_readback_or_action(selector_case):
    manifest, path, directory, _ = selector_case
    manifest["readback_channels"].pop("ingress")
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="ingress policy"):
        qualify_journeys(manifest["release"]["source_sha"], path, directory,
                         reader_factory=lambda *args, **kwargs: pytest.fail("reader called"),
                         journey_runner=lambda *args, **kwargs: pytest.fail("action called"),
                         policy_reader=lambda *args, **kwargs: {**offline_policy(manifest),
                                                                "function_urls": ["public-url"]}, now=NOW)
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("change", [
    lambda policy: policy.pop("distribution"),
    lambda policy: policy["distribution"].update(distribution_id="FOREIGN123"),
    lambda policy: policy["distribution"].update(gateway_origin_id="foreign-origin"),
    lambda policy: policy["distribution"].update(frontend_hostname="foreign.example.org"),
    lambda policy: policy["distribution"].update(launch_behavior="/foreign/*"),
    lambda policy: policy["distribution"].pop("launch_behavior"),
    lambda policy: policy.update(verified_functions=["foreign-helper"]),
])
def test_unbound_selected_policy_stops_before_action_or_reader(selector_case, change):
    manifest, path, directory, _ = selector_case
    manifest["readback_channels"].pop("ingress")
    path.write_text(json.dumps(manifest))
    policy = offline_policy(manifest)
    change(policy)
    with pytest.raises(ValueError, match="deployed ingress policy was not verified"):
        qualify_journeys(manifest["release"]["source_sha"], path, directory,
                         reader_factory=lambda *args, **kwargs: pytest.fail("reader called"),
                         journey_runner=lambda *args, **kwargs: pytest.fail("action called"),
                         policy_reader=lambda *args, **kwargs: policy, now=NOW)
    assert list(directory.iterdir()) == []


def test_selected_policy_for_another_frontend_stops_before_action_or_reader(selector_case):
    manifest, path, directory, _ = selector_case
    manifest["readback_channels"].pop("ingress")
    manifest["journey_driver"]["gateway_url"] = "https://other.example.org"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="deployed ingress policy was not verified"):
        qualify_journeys(manifest["release"]["source_sha"], path, directory,
                         reader_factory=lambda *args, **kwargs: pytest.fail("reader called"),
                         journey_runner=lambda *args, **kwargs: pytest.fail("action called"),
                         policy_reader=offline_policy, now=NOW)
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("tamper", [
    lambda manifest, directory: manifest["authorization"].update(source_sha="0" * 40),
    lambda manifest, directory: manifest["authorization"].update(expires_at=(NOW - timedelta(seconds=1)).isoformat()),
    lambda manifest, directory: manifest["authorization"].update(scenarios="journeys"),
    lambda manifest, directory: manifest["limits"].pop("prevention"),
    lambda manifest, directory: manifest["limits"]["knowledge"].update(max_spend_usd="0"),
    lambda manifest, directory: manifest["readback_channels"]["ingress"].update(url="http://ingress.example.org/"),
    lambda manifest, directory: manifest["readback_channels"]["ingress"].update(token_env="DEVOPS_RELEASE_TOKEN"),
    lambda manifest, directory: directory.chmod(0o755),
])
def test_selector_denies_stale_unbounded_public_or_shared_readback(selector_case, tamper):
    manifest, path, directory, prior_reader = selector_case
    tamper(manifest, directory)
    with pytest.raises(ValueError):
        authorize(manifest, manifest["release"]["source_sha"], directory, now=NOW)


def test_selector_refuses_results_from_another_qualification_run(selector_case):
    manifest, path, directory, prior_reader = selector_case
    class WrongRun(FakeReader):
        def read_journeys(self, target):
            return [{**item, "qualification_run_id": "older-run-example"}
                    for item in self.prior_reader.read_journeys(target)]
    with pytest.raises(ValueError, match="journeys"):
        qualify_journeys(manifest["release"]["source_sha"], path, directory,
                         reader_factory=lambda fixture, **kwargs: WrongRun(fixture, prior_reader, **kwargs),
                         policy_reader=offline_policy, journey_runner=offline_drive, now=NOW)
    assert list(directory.iterdir()) == []


def test_http_reader_bounds_response_and_never_follows_redirect(selector_case):
    import httpx
    import time
    from selector import HTTPReader

    manifest, path, directory, prior_reader = selector_case
    observed = []
    def handler(request):
        observed.append(request)
        return httpx.Response(200, json={"target_ref": "target-example", "observation": {"status": "ready"}})
    reader = HTTPReader(manifest, deadline=time.monotonic() + 10, max_requests=1)
    reader.client.close()
    reader.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        assert reader.read_release("target-example") == {"status": "ready"}
        assert observed[0].url.params["target_ref"] == "target-example"
        assert observed[0].headers["Authorization"] == "Bearer private-release-token"
        with pytest.raises(ValueError, match="bound"):
            reader.read_ingress("target-example")
    finally:
        reader.close()


@pytest.mark.parametrize("response", [
    lambda: {"target_ref": "foreign-example", "observation": {}},
    lambda: {"target_ref": "target-example", "observation": "x" * (1024 * 1024)},
])
def test_http_reader_rejects_foreign_or_oversized_readback(selector_case, response):
    import httpx
    import time
    from selector import HTTPReader

    manifest, path, directory, prior_reader = selector_case
    reader = HTTPReader(manifest, deadline=time.monotonic() + 10, max_requests=1)
    reader.client.close()
    reader.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response())), follow_redirects=False)
    try:
        with pytest.raises(ValueError):
            reader.read_release("target-example")
    finally:
        reader.close()


def test_selector_rejects_native_usage_over_budget(selector_case):
    manifest, path, directory, prior_reader = selector_case
    class OverBudget(FakeReader):
        def read_capabilities(self, target):
            results = super().read_capabilities(target)
            results[0]["usage"]["requests"] = 5
            return results
    with pytest.raises(ValueError, match="budget"):
        qualify_journeys(manifest["release"]["source_sha"], path, directory,
                         reader_factory=lambda fixture, **kwargs: OverBudget(fixture, prior_reader, **kwargs),
                         policy_reader=offline_policy, journey_runner=offline_drive, now=NOW)
    assert list(directory.iterdir()) == []


def test_http_reader_rejects_redirects(selector_case):
    import httpx
    import time
    from selector import HTTPReader

    manifest, path, directory, prior_reader = selector_case
    reader = HTTPReader(manifest, deadline=time.monotonic() + 10, max_requests=1)
    reader.client.close()
    reader.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"Location": "https://other.example.org/"})),
        follow_redirects=False)
    try:
        with pytest.raises(ValueError, match="unavailable"):
            reader.read_release("target-example")
    finally:
        reader.close()
