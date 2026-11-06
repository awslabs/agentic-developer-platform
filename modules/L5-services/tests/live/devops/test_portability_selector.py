"""Offline SCN-04 selector integration without a live provider or account."""

import json
import time
from datetime import UTC, datetime

import pytest

from context_selector import (PORTABILITY_ACTIONS, PORTABILITY_READS, HTTPSScopedTarget,
                              authorize_channels, qualify_portability)
from test_portability import FakePlatform, FakeReader, portable
from test_selector import FakeReader as FakeReleaseReader, offline_policy, selector_case

NOW = datetime(2026, 10, 28, tzinfo=UTC)


@pytest.fixture
def portable_selection(selector_case, monkeypatch):
    manifest, path, directory, prior_reader = selector_case
    manifest["projects"] = portable.__wrapped__()["projects"]
    manifest["authorization"]["scenarios"].append("portability")
    manifest["portability"] = {"limits": {"max_requests": 60, "deadline_seconds": 120,
                                           "cleanup_grace_seconds": 30}}
    for source, names in (("action", PORTABILITY_ACTIONS), ("readback", PORTABILITY_READS)):
        manifest["portability"][source + "_channels"] = {}
        for name in names:
            env = f"DEVOPS_PORTABILITY_{source.upper()}_{name.upper()}_TOKEN"
            manifest["portability"][source + "_channels"][name] = {
                "source": name, "url": f"https://{source}-{name.replace('_', '-')}.example.org/",
                "token_env": env}
            monkeypatch.setenv(env, f"private-portability-{source}-{name}-token")
    path.write_text(json.dumps(manifest))
    return manifest, path, directory, prior_reader


class PortableAdapter:
    def __init__(self, manifest, directory, scenario, *, fault=None, **kwargs):
        self.driver = FakePlatform(manifest, fault=fault)
        self.reader = FakeReader(self.driver)
        self.closed = False

    def close(self):
        self.closed = True

    def __getattr__(self, name):
        origin = self.driver if hasattr(self.driver, name) else self.reader
        return getattr(origin, name)


def test_portability_selector_records_two_results_on_identical_artifacts(portable_selection):
    manifest, path, directory, prior_reader = portable_selection
    manifest["readback_channels"].pop("ingress")
    path.write_text(json.dumps(manifest))

    class NoHTTPIngress(FakeReleaseReader):
        def read_ingress(self, target):
            pytest.fail("portability must use the selected AWS ingress policy")

    adapters = []
    def factory(fixture, evidence_dir, scenario, **kwargs):
        adapter = PortableAdapter(fixture, evidence_dir, scenario, **kwargs)
        adapters.append(adapter)
        return adapter
    run_id = qualify_portability(manifest["release"]["source_sha"], path, directory,
                                 target_factory=factory,
                                 release_reader_factory=lambda fixture, **kwargs: NoHTTPIngress(
                                     fixture, prior_reader, **kwargs), policy_reader=offline_policy, now=NOW)
    evidence = json.loads((directory / f"devops-{run_id}-portability.json").read_text())
    assert len(evidence["projects"]) == 2
    assert evidence["requirements_revision"] == "R3-secure-native-ui-ingress"
    assert adapters[0].closed
    assert [item[0] for item in adapters[0].driver.actions].count("cleanup") == 2


@pytest.mark.parametrize("tamper", [
    lambda manifest, monkeypatch: manifest["portability"]["readback_channels"].pop("platform"),
    lambda manifest, monkeypatch: manifest["projects"][1].update(
        operation_receipt=manifest["projects"][0]["operation_receipt"]),
    lambda manifest, monkeypatch: monkeypatch.setenv("DEVOPS_PORTABILITY_ACTION_CONFIGURE_TOKEN",
                                                    "private-portability-readback-platform-token"),
    lambda manifest, monkeypatch: manifest["portability"]["limits"].update(cleanup_grace_seconds=0),
    lambda manifest, monkeypatch: manifest["projects"][1].update(workload_kind="unsupported"),
])
def test_portability_selector_denies_unsafe_configuration_before_native_calls(portable_selection, monkeypatch, tamper):
    manifest, path, directory, prior_reader = portable_selection
    tamper(manifest, monkeypatch)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        qualify_portability(manifest["release"]["source_sha"], path, directory,
                            target_factory=lambda *args, **kwargs: pytest.fail("target constructed"), now=NOW)
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("fault", ["adp_product", "wrong_account", "wrong_source", "foreign_federation",
                                    "redeploy", "delete_retained", "other_project_mutation"])
def test_portability_selector_rejects_bad_provider_and_cleanup_readbacks(portable_selection, fault):
    manifest, path, directory, prior_reader = portable_selection
    with pytest.raises(ValueError):
        qualify_portability(manifest["release"]["source_sha"], path, directory,
                            target_factory=lambda fixture, evidence_dir, scenario, **kwargs: PortableAdapter(
                                fixture, evidence_dir, scenario, fault=fault, **kwargs),
                            release_reader_factory=lambda fixture, **kwargs: FakeReleaseReader(
                                fixture, prior_reader, **kwargs), policy_reader=offline_policy, now=NOW)
    assert list(directory.iterdir()) == []


def test_http_portability_channel_binds_denial_and_durable_cleanup(portable_selection):
    import httpx
    manifest, path, directory, prior_reader = portable_selection
    target = HTTPSScopedTarget(manifest, directory, "portability", deadline=time.monotonic() + 20, max_requests=60)
    target.client.close()
    def respond(request):
        is_denial = "probe-foreign" in request.url.host
        return httpx.Response(404 if is_denial else 204, json={
            "target_ref": manifest["target_ref"], "qualification_run_id": manifest["authorization"]["run_id"],
            "observation": {"disclosed_resources": []} if is_denial else {}})
    target.client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False)
    try:
        first, second = manifest["projects"]
        assert target.probe_foreign(first["scope"], second["scope"]) == {
            "status": 404, "disclosed_resources": []}
        target.cleanup_owned(first["scope"], first["owned_assets"])
        assert target.cleanup_phase
    finally:
        target.close()
    journal = directory / f"devops-{manifest['authorization']['run_id']}-portability.jsonl"
    assert [json.loads(item)["operation"] for item in journal.read_text().splitlines()] == [
        "probe_foreign", "probe_foreign", "cleanup_owned", "cleanup_owned"]
