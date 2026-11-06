"""Offline bounded Context action and final acceptance selector fixtures."""

import base64
import hashlib
import json
import time
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from context_selector import (ACCEPTANCE_READS, CONTEXT_READS, HTTPSScopedTarget,
                              authorize_channels, qualify_acceptance, qualify_context)
from test_context import acceptance_fixture, context_fixture
from test_selector import FakeReader as FakeReleaseReader, offline_policy, selector_case

NOW = datetime(2026, 10, 28, tzinfo=UTC)


def channels(manifest, scenario, names, monkeypatch):
    manifest["authorization"]["scenarios"].append(scenario)
    selected = manifest[scenario]
    selected["readback_channels"] = {}
    for name in names:
        key = f"DEVOPS_{scenario.upper()}_{name.upper()}_TOKEN"
        selected["readback_channels"][name] = {
            "source": name, "url": f"https://{scenario}-{name.replace('_', '-')}.example.org/",
            "token_env": key}
        monkeypatch.setenv(key, f"private-{scenario}-{name}-token")
    selected["action_channels"] = {}
    if scenario == "context":
        key = "DEVOPS_CONTEXT_RUN_TOKEN"
        selected["action_channels"]["run_later"] = {
            "source": "run_later", "url": "https://context-action.example.org/", "token_env": key}
        monkeypatch.setenv(key, "private-context-action-token")
        selected["receipt"] = "context-receipt-example"
        selected["limits"]["max_requests"] = 25
    else:
        selected["limits"] = {"max_requests": 80, "deadline_seconds": 120}


@pytest.fixture
def live_context(selector_case, monkeypatch):
    manifest, path, directory, prior_reader = selector_case
    other, state, driver, reader, calls = context_fixture.__wrapped__()
    manifest["context"] = deepcopy(other["context"])
    channels(manifest, "context", CONTEXT_READS, monkeypatch)
    path.write_text(json.dumps(manifest))
    return manifest, path, directory, prior_reader, state, driver, reader


class ContextAdapter:
    def __init__(self, manifest, directory, scenario, driver, reader, **kwargs):
        self.driver, self.reader = driver, reader
        self.closed = False

    def close(self):
        self.closed = True

    def __getattr__(self, name):
        origin = self.driver if name == "run_later" else self.reader
        return getattr(origin, name)


def test_context_selector_submits_later_operation_and_writes_only_private_proof(live_context):
    manifest, path, directory, prior_reader, state, driver, readback = live_context
    manifest["readback_channels"].pop("ingress")
    path.write_text(json.dumps(manifest))

    class NoHTTPIngress(FakeReleaseReader):
        def read_ingress(self, target):
            pytest.fail("Context must use the selected AWS ingress policy")

    adapters = []
    def factory(fixture, directory, scenario, **kwargs):
        instance = ContextAdapter(fixture, directory, scenario, driver, readback, **kwargs)
        adapters.append(instance)
        return instance
    assert qualify_context(manifest["release"]["source_sha"], path, directory,
                           target_factory=factory,
                           release_reader_factory=lambda fixture, **kwargs: NoHTTPIngress(
                               fixture, prior_reader, **kwargs), policy_reader=offline_policy,
                           now=NOW) == manifest["authorization"]["run_id"]
    result = json.loads((directory / f"devops-{manifest['authorization']['run_id']}-context.json").read_text())
    assert result["later_run_id"] == "later-example"
    assert adapters[0].closed


@pytest.mark.parametrize("mutate", [
    lambda manifest, monkeypatch: manifest["context"]["readback_channels"].pop("native_job"),
    lambda manifest, monkeypatch: monkeypatch.setenv("DEVOPS_CONTEXT_SOURCE_TOKEN", "private-context-action-token"),
    lambda manifest, monkeypatch: manifest["context"].update(receipt=""),
    lambda manifest, monkeypatch: manifest["context"]["limits"].update(max_requests=2),
])
def test_context_selector_denies_missing_independence_or_bounds_before_effect(live_context, monkeypatch, mutate):
    manifest, path, directory, prior_reader, state, driver, readback = live_context
    mutate(manifest, monkeypatch)
    if manifest["context"].get("receipt") == "":
        path.write_text(json.dumps(manifest))
        with pytest.raises(ValueError):
            qualify_context(manifest["release"]["source_sha"], path, directory,
                            target_factory=lambda *args, **kwargs: pytest.fail("action constructed"), now=NOW)
    else:
        with pytest.raises(ValueError):
            authorize_channels(manifest, manifest["release"]["source_sha"], directory,
                               "context", now=NOW)
    assert list(directory.iterdir()) == []


@pytest.fixture
def live_acceptance(selector_case, monkeypatch):
    manifest, path, directory, prior_reader = selector_case
    other, evidence, observations, reader = acceptance_fixture.__wrapped__()
    manifest["acceptance"] = {}
    channels(manifest, "acceptance", ACCEPTANCE_READS, monkeypatch)
    run_id = manifest["authorization"]["run_id"]
    evidence["authorization_run_id"] = run_id
    evidence["release"] = manifest["release"]
    for case in evidence["cases"]:
        case["qualification_run_id"] = run_id
        observed = observations[case["readback_ref"]]
        observed["qualification_run_id"] = run_id
        case["readback_sha256"] = hashlib.sha256(json.dumps(observed, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
    for check in observations.values():
        check["qualification_run_id"] = run_id
    epic = reader.read_epic_criteria(manifest["target_ref"])
    for criterion in epic.values():
        criterion["qualification_run_id"] = run_id
    path.write_text(json.dumps(manifest))
    return manifest, path, directory, prior_reader, evidence, reader


class AcceptanceAdapter:
    def __init__(self, manifest, directory, scenario, evidence, reader, release_reader=None, **kwargs):
        self.evidence, self.reader, self.release_reader = evidence, reader, release_reader
        self.closed = False

    def close(self):
        self.closed = True
        self.release_reader.close()

    def read_report(self):
        return self.evidence

    def __getattr__(self, name):
        origin = self.release_reader if name in {
            "read_release", "read_ingress", "read_capabilities", "read_journeys"} else self.reader
        return getattr(origin, name)


def test_acceptance_selector_requires_all_current_run_pairs(live_acceptance):
    manifest, path, directory, prior_reader, evidence, reader = live_acceptance
    manifest["readback_channels"].pop("ingress")
    path.write_text(json.dumps(manifest))

    class NoHTTPIngress(FakeReleaseReader):
        def read_ingress(self, target):
            pytest.fail("acceptance must use the selected AWS ingress policy")

    adapters = []
    def factory(fixture, directory, scenario, **kwargs):
        adapter = AcceptanceAdapter(fixture, directory, scenario, evidence, reader, **kwargs)
        adapters.append(adapter)
        return adapter
    run_id = qualify_acceptance(manifest["release"]["source_sha"], path, directory,
                                target_factory=factory,
                                release_reader_factory=lambda fixture, **kwargs: NoHTTPIngress(
                                    fixture, prior_reader, **kwargs), policy_reader=offline_policy, now=NOW)
    assert adapters[0].closed
    assert json.loads((directory / f"devops-{run_id}-acceptance.json").read_text())["verified_pairs"] == 63


def test_acceptance_selector_rejects_tampered_report(live_acceptance):
    manifest, path, directory, prior_reader, evidence, reader = live_acceptance
    evidence["cases"][0]["status"] = "fabricated"
    with pytest.raises(ValueError):
        qualify_acceptance(manifest["release"]["source_sha"], path, directory,
                           target_factory=lambda fixture, directory, scenario, **kwargs: AcceptanceAdapter(
                               fixture, directory, scenario, evidence, reader, **kwargs),
                           release_reader_factory=lambda fixture, **kwargs: FakeReleaseReader(
                               fixture, prior_reader, **kwargs), policy_reader=offline_policy, now=NOW)
    assert list(directory.iterdir()) == []


def test_http_context_target_decodes_bounded_source_content(live_context):
    import httpx
    manifest, path, directory, prior_reader, state, driver, reader = live_context
    target = HTTPSScopedTarget(manifest, directory, "context", deadline=time.monotonic() + 20, max_requests=20)
    target.client.close()
    target.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        "target_ref": manifest["target_ref"], "qualification_run_id": manifest["authorization"]["run_id"],
        "observation": {"scope": manifest["context"]["scope"], "record_revision": "v2",
                        "content_b64": base64.b64encode(b"source bytes").decode()}})), follow_redirects=False)
    try:
        assert target.read_source(manifest["context"]["scope"], manifest["context"]["sources"][0])["content"] == b"source bytes"
    finally:
        target.close()


def test_acceptance_selector_rejects_other_qualification_run(live_acceptance):
    manifest, path, directory, prior_reader, evidence, reader = live_acceptance
    evidence["cases"][0]["qualification_run_id"] = "previous-run-example"
    with pytest.raises(ValueError, match="wrong-source"):
        qualify_acceptance(manifest["release"]["source_sha"], path, directory,
                           target_factory=lambda fixture, directory, scenario, **kwargs: AcceptanceAdapter(
                               fixture, directory, scenario, evidence, reader, **kwargs),
                           release_reader_factory=lambda fixture, **kwargs: FakeReleaseReader(
                               fixture, prior_reader, **kwargs), policy_reader=offline_policy, now=NOW)
    assert list(directory.iterdir()) == []


def test_http_context_action_uncertainty_is_durable_and_not_replayed(live_context):
    import httpx
    manifest, path, directory, prior_reader, state, driver, reader = live_context
    target = HTTPSScopedTarget(manifest, directory, "context", deadline=time.monotonic() + 20, max_requests=25)
    target.client.close()
    target.client = httpx.Client(transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("ack uncertain"))), follow_redirects=False)
    try:
        with pytest.raises(TimeoutError):
            target.run_later(manifest["context"]["scope"], manifest["context"]["intent"],
                             manifest["context"]["publication_ref"], manifest["context"]["limits"])
    finally:
        target.close()
    journal = directory / f"devops-{manifest['authorization']['run_id']}-context.jsonl"
    assert [json.loads(entry)["phase"] for entry in journal.read_text().splitlines()] == ["prepared", "uncertain"]
    with pytest.raises(FileExistsError):
        HTTPSScopedTarget(manifest, directory, "context", deadline=time.monotonic() + 20, max_requests=25)
