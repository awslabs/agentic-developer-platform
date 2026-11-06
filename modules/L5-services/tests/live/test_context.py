"""Synthetic Context checks; actual later-agent reuse requires live access."""

from copy import deepcopy

import httpx
import pytest

from clients import read_context_evidence
from evidence import verify_context_reuse


@pytest.fixture
def context_evidence():
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "visibility": "only_me"}
    source = {"tenant_id": "tenant-example", "project_id": "project-example",
              "source_connection_id": "source-example", "record_type": "document",
              "record_id": "architecture-example", "record_revision": "source-revision-example"}
    job_ref = {"record_id": "native-job-example", "record_revision": "1"}
    result_ref = {"tenant_id": "tenant-example", "project_id": "project-example",
                  "source_connection_id": "l5-security", "record_type": "security_result",
                  "record_id": "result-example", "record_revision": "1"}
    manifest_ref = {"artifact_id": "manifest-example", "version": "1", "sha256": "a" * 64}
    run = {"run_id": "security-run-example", "scope": scope, "input": {"sources": [source]},
           "admission": {"source_snapshots": [{"source_ref": source, "snapshot_id": "snapshot-example"}]}}
    result = {"result_id": "result-example", "run_id": run["run_id"], "scope": scope, "revision": 1,
              "native_job_ref": job_ref, "finding_count": 1}
    receipt = {"receipt_id": "receipt-example", "contribution_id": "contribution-example",
               "admitted_schema": "1.0", "consumer_revision": "context-revision-example",
               "record_refs": [result_ref]}
    payload = {"result_ref": result_ref, "scope": scope, "run_id": run["run_id"],
               "native_job_ref": job_ref, "source_refs": [source], "manifest_ref": manifest_ref,
               "validation": "unverified"}
    publication = {"publication_id": "contribution-example", "result_id": result["result_id"],
                   "result_version": 1, "schema_version": "1.0", "state": "published",
                   "payload": deepcopy(payload), "receipt": deepcopy(receipt)}
    visibility = {"scope": deepcopy(scope), "visibility": "only_me", "revision": "current-revision-example"}
    agent_read = {"agent_run_id": "later-agent-example", "contribution_id": publication["publication_id"],
                  "queried_product_id": scope["product_id"], "scope": deepcopy(scope),
                  "source": "context", "consumer_count": 1,
                  "observed_at": "2026-01-01T00:00:00Z", "current_visibility": "only_me",
                  "visibility_revision": "current-revision-example",
                  "citations": [{"result_ref": result_ref, "manifest_ref": manifest_ref,
                                 "source_ref": deepcopy(source), "source_revision": source["record_revision"],
                                 "validation": "unverified"}]}
    denied = [{"case": case, "status": 404, "disclosed_resources": []}
              for case in ("foreign_product", "revoked_access")]
    return run, result, publication, receipt, agent_read, visibility, denied, [deepcopy(receipt), deepcopy(receipt)], {
        "complete": True, "items": []}


def test_later_agent_cites_independently_read_result(context_evidence):
    evidence = verify_context_reuse(*context_evidence)
    assert evidence["source_revisions"] == ["source-revision-example"]
    assert evidence["agent_run_id"] != context_evidence[0]["run_id"]


@pytest.mark.parametrize("change,message", [
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     publication["payload"]["result_ref"].update(record_revision="2"), "terminal Security result"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     publication["payload"]["source_refs"][0].update(record_revision="latest"), "unpinned source"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     receipt.update(consumer_revision="other"), "consumer receipt"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     agent.update(consumer_count=2), "another Context contribution"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     agent.update(agent_run_id=run["run_id"]), "later authorized agent"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     agent["citations"][0].update(source_revision="old-revision"), "uncertainty"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     agent["citations"][0].update(validation="supported"), "uncertainty"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     agent.update(visibility_revision="stale"), "current Product visibility"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     agent["scope"].update(tenant_id="foreign-tenant"), "later authorized agent"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     agent["citations"][0]["manifest_ref"].update(version="2"), "uncertainty"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     denied[0]["disclosed_resources"].append("private-finding"), "counterexample"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     events.update(complete=False), "feedback assessment loop"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     denied[0].update(status=200), "counterexample"),
    (lambda run, result, publication, receipt, agent, visibility, denied, repeats, events:
     events["items"].append({"causal_origin": run["run_id"]}), "feedback assessment loop"),
])
def test_context_rejects_missing_or_foreign_evidence(context_evidence, change, message):
    evidence = deepcopy(context_evidence)
    change(*evidence)
    with pytest.raises(AssertionError, match=message):
        verify_context_reuse(*evidence)


def test_readback_queries_context_separately_and_then_later_agent(context_evidence):
    run, result, publication, receipt, agent_read, visibility, denied, repeats, events = context_evidence
    calls = []

    def context_transport(request):
        calls.append(("context", request.url.path))
        assert request.headers["Authorization"] == "Bearer test-only"
        return httpx.Response(200, json=receipt)

    def read_api(path):
        calls.append(("product", path))
        if path.endswith("/runs/security-run-example"):
            return run
        if path.endswith("/results/result-example"):
            return result
        return {"items": [{"finding_id": "finding-example"}]}

    def query_agent(product_id):
        calls.append(("agent", product_id))
        return agent_read

    with httpx.Client(base_url="https://context.example", transport=httpx.MockTransport(context_transport),
                      headers={"Authorization": "Bearer test-only"}) as context_client:
        observed = read_context_evidence(run["scope"], run["run_id"], result["result_id"],
                                         publication["publication_id"], read_api, context_client, query_agent)
    assert observed[0] == run and observed[1] == result and observed[3] == receipt and observed[4] == agent_read
    assert calls[-2:] == [("context", "/internal/v1/context/l5-contributions/contribution-example"),
                          ("agent", "product-example")]


def test_current_visibility_may_change_when_later_agent_is_authorized(context_evidence):
    evidence = deepcopy(context_evidence)
    visibility = evidence[5]
    agent_read = evidence[4]
    visibility["visibility"] = "product_team"
    visibility["scope"]["visibility"] = "product_team"
    agent_read["current_visibility"] = "product_team"
    assert verify_context_reuse(*evidence)["result_id"] == "result-example"
