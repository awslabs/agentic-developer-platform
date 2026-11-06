"""Offline adversarial Context and 63-pair acceptance tests, never live proof."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from acceptance import PLAN, verify_acceptance
from context import verify_context
from qualification import MATRIX

NOW = datetime(2026, 10, 28, tzinfo=UTC)
SCOPE = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
SOURCE_CONTENT = b"offline fixture document version two"
SOURCE_SHA = hashlib.sha256(SOURCE_CONTENT).hexdigest()
SOURCE = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"], "record_id": "readme-example",
          "record_revision": "v2", "sha256": SOURCE_SHA}
RELEASE = {"source_sha": "a" * 40, "deployment_id": "deployment-example",
           "deployed_at": "2026-10-25T00:00:00Z",
           "artifacts": {name: "b" * 64 for name in ("api", "ui", "cli", "mcp", "worker")}}


def manifest():
    return {"authorized": True, "fixture_owner": "owner-example", "target_ref": "target-example",
            "requirements_revision": "R3-secure-native-ui-ingress", "release": deepcopy(RELEASE)}


@pytest.fixture
def context_fixture():
    selected = manifest()
    selected["context"] = {"scope": deepcopy(SCOPE), "sources": [deepcopy(SOURCE)], "result_ref": "result-example",
                           "publication_ref": "contribution-example", "asset_ref": "asset-example",
                           "intent": "readiness-example",
                           "limits": {"max_spend_usd": "0.10", "max_requests": 4,
                                      "deadline_seconds": 120}}
    citation = {"contribution_ref": "contribution-example", "result_ref": "result-example",
                "finding_id": "finding-example", "source_refs": [SOURCE],
                "asset_ref": "asset-example", "asset_version": 3}
    state = {"result": {"scope": SCOPE, "result_ref": "result-example", "status": "completed",
                        "source_refs": [SOURCE], "native_job_id": "job-example", "finding_id": "finding-example"},
             "source": {"scope": SCOPE, "record_revision": "v2", "content": SOURCE_CONTENT, "revoked": False},
             "native": {"scope": SCOPE, "job_id": "job-example", "status": "completed",
                        "finding_id": "finding-example", "source_refs": [SOURCE]},
             "receipt": {"contribution_ref": "contribution-example", "result_ref": "result-example",
                         "consumer_revision": "consumer-v3"},
             "publication": {"scope": SCOPE, "state": "published", "result_ref": "result-example",
                             "source_refs": [SOURCE], "asset_ref": "asset-example",
                             "published_at": "2026-10-27T00:00:00Z", "consumer_revision": "consumer-v3"},
             "asset": {"scope": SCOPE, "ref": "asset-example", "status": "active", "version": 3,
                       "source_refs": [SOURCE]},
             "visibility": {"scope": SCOPE, "status": "active", "revision": "visibility-v3"},
             "baseline": {"scope": SCOPE, "intent": "readiness-example", "source_refs": [SOURCE],
                          "citations": [], "decision": "hold", "run_id": "baseline-example",
                          "observed_at": "2026-10-26T00:00:00Z"},
             "later": {"scope": SCOPE, "run_id": "later-example", "intent": "readiness-example",
                       "status": "completed", "source_refs": [SOURCE], "decision": "proceed",
                       "citations": [citation], "used_contributions": ["contribution-example"],
                       "visibility_revision": "visibility-v3", "observed_at": "2026-10-28T00:00:00Z",
                        "native_outcome": "provider-completed-example"},
             "usage": {"spend_usd": "0.02", "requests": 2, "elapsed_seconds": 15},
             "denials": {name: {"status": 403, "disclosed_resources": []}
                         for name in ("revoked", "irrelevant")}}
    calls = []
    def run_later(scope, intent, publication_ref, limits):
        calls.append((scope, intent, publication_ref, limits))
        return {"run_id": "later-example"}
    driver = SimpleNamespace(run_later=run_later)
    reader = SimpleNamespace(read_native_job=lambda scope, ref: state["native"],
                             read_receipt=lambda scope, ref: state["receipt"],
                             read_result=lambda scope, ref: state["result"],
                             read_source=lambda scope, ref: state["source"],
                             read_publication=lambda scope, ref: state["publication"],
                             read_asset=lambda scope, ref: state["asset"],
                             read_visibility=lambda scope, ref: state["visibility"],
                             read_baseline=lambda scope, intent: state["baseline"],
                             read_later=lambda scope, run_id: state["later"],
                             read_denial=lambda scope, run_id, name: state["denials"][name],
                             read_usage=lambda scope, run_id: state["usage"])
    return selected, state, driver, reader, calls


def test_later_operation_uses_exact_source_and_changes_the_decision(context_fixture):
    selected, state, driver, reader, calls = context_fixture
    result = verify_context(selected, driver, reader, now=NOW)
    assert result["later_run_id"] == "later-example"
    assert result["citation"] == state["later"]["citations"][0]
    assert calls == [(SCOPE, "readiness-example", "contribution-example",
                      selected["context"]["limits"])]


@pytest.mark.parametrize("corrupt", [
    lambda selected, state: state["source"].update(content=b"tampered"),
    lambda selected, state: state["native"].update(status="unknown"),
    lambda selected, state: state["usage"].update(spend_usd="1.00"),
    lambda selected, state: state["receipt"].update(consumer_revision="other"),
    lambda selected, state: state["source"].update(revoked=True),
    lambda selected, state: state["asset"].update(version=0),
    lambda selected, state: state["visibility"].update(status="revoked"),
    lambda selected, state: state["later"].update(decision="hold"),
    lambda selected, state: state["later"].update(citations=[]),
    lambda selected, state: state["later"].update(used_contributions=[]),
    lambda selected, state: state["later"].update(scope={**SCOPE, "project_id": "foreign"}),
    lambda selected, state: state["denials"]["irrelevant"].update(disclosed_resources=["foreign-result"]),
    lambda selected, state: selected["context"]["sources"][0].update(record_revision="latest"),
    lambda selected, state: selected["context"]["limits"].update(max_spend_usd="0"),
])
def test_context_rejects_stale_recalled_unrelated_or_uncited_knowledge(context_fixture, corrupt):
    selected, state, driver, reader, calls = context_fixture
    corrupt(selected, state)
    with pytest.raises(ValueError):
        verify_context(selected, driver, reader, now=NOW)


@pytest.fixture
def acceptance_fixture():
    selected = manifest()
    plan = json.loads(PLAN.read_text())
    observations = {}
    cases = []
    for story in plan["stories"]:
        for acceptance, scenario in zip(story["acceptance_ids"], story["scenario_ids"], strict=True):
            ref = f"{story['alias']}-{acceptance}-{scenario}"
            observed = {"story": story["alias"], "acceptance": acceptance, "scenario": scenario,
                        "source_sha": RELEASE["source_sha"], "deployment_id": RELEASE["deployment_id"],
                        "native_id": "native-" + ref, "scope": SCOPE, "status": "passed",
                        "observed_at": "2026-10-28T00:00:00Z",
                        "native_outcome": "provider-completed-example"}
            observations[ref] = observed
            cases.append({key: observed[key] for key in (
                "story", "acceptance", "scenario", "source_sha", "deployment_id", "native_id", "scope", "status")}
                | {"readback_ref": ref,
                   "readback_sha256": hashlib.sha256(json.dumps(observed, sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest()})
    ingress = {"browser_launch_route": "gateway", "function_urls": [], "public_launchers": [],
               "source_sha": RELEASE["source_sha"], "deployment_id": RELEASE["deployment_id"]}
    checks = {}
    for name in ("sso-gateway-only-ingress", "sso-denial-before-federation"):
        observations[name] = {"name": name, "status": "passed", "source_sha": RELEASE["source_sha"],
                              "deployment_id": RELEASE["deployment_id"],
                              "observed_at": "2026-10-28T00:00:00Z", "provider_session_calls": 0, **ingress}
        checks[name] = {"status": "passed", "readback_ref": name}
    release = {**RELEASE, "observed_at": "2026-10-28T00:00:00Z",
                        "native_outcome": "provider-completed-example"}
    capabilities = [{"capability": item["id"], "deployment_id": RELEASE["deployment_id"],
                     "status": "completed", "native_id": "native-" + item["id"],
                     "readback_ref": "ref-" + item["id"]}
                    for item in json.loads(MATRIX.read_text())["capabilities"]]
    journeys = [{"entry_point": name, "deployment_id": RELEASE["deployment_id"],
                 "status": "completed", "readback_ref": "ref-" + name}
                for name in ("product", "ui", "cli", "mcp")]
    evidence = {"requirements_revision": selected["requirements_revision"], "release": selected["release"],
                "epic_criteria": sorted({criterion for story in plan["stories"]
                                         for criterion in story["epic_criteria"]}),
                "cleanup": {"status": "confirmed", "owned_absent": True, "retained_unchanged": True},
                "attempts": [{"status": "reconciled", "receipt": "receipt-example"}],
                "live_checks": checks, "cases": cases}
    epic_readback = {criterion: {"status": "passed", "source_sha": RELEASE["source_sha"],
                                 "deployment_id": RELEASE["deployment_id"],
                                 "observed_at": "2026-10-28T00:00:00Z"}
                     for criterion in evidence["epic_criteria"]}
    reader = SimpleNamespace(read_epic_criteria=lambda target: epic_readback,
                             read_release=lambda target: release, read_ingress=lambda target: ingress,
                             read_capabilities=lambda target: capabilities, read_journeys=lambda target: journeys,
                             read_check=lambda target, ref: observations[ref],
                             read_case=lambda target, ref: observations[ref],
                             read_cleanup=lambda target: evidence["cleanup"])
    return selected, evidence, observations, reader


def test_acceptance_requires_all_63_pairs_and_ten_epic_criteria(acceptance_fixture):
    selected, evidence, observations, reader = acceptance_fixture
    assert verify_acceptance(selected, evidence, reader, now=NOW) == {
        "requirements_revision": "R3-secure-native-ui-ingress", "verified_pairs": 63, "verified_epic": 10}


@pytest.mark.parametrize("corrupt", [
    lambda selected, evidence, observations: evidence["cases"].pop(),
    lambda selected, evidence, observations: evidence["cases"][0].update(source_sha="0" * 40),
    lambda selected, evidence, observations: observations[evidence["cases"][0]["readback_ref"]].update(native_id="fake"),
    lambda selected, evidence, observations: evidence["cases"][0].update(status="blocked"),
    lambda selected, evidence, observations: evidence["cleanup"].update(owned_absent=False),
    lambda selected, evidence, observations: observations["sso-denial-before-federation"].update(provider_session_calls=1),
    lambda selected, evidence, observations: evidence["live_checks"].pop("sso-gateway-only-ingress"),
    lambda selected, evidence, observations: observations[evidence["cases"][0]["readback_ref"]].update(native_outcome=""),
])
def test_acceptance_rejects_missing_wrong_source_fabricated_or_cleanup_failed(acceptance_fixture, corrupt):
    selected, evidence, observations, reader = acceptance_fixture
    corrupt(selected, evidence, observations)
    with pytest.raises(ValueError):
        verify_acceptance(selected, evidence, reader, now=NOW)
