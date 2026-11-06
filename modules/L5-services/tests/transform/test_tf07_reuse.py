"""Synthetic consumer receipts and later-task controls; installed peers remain unverified."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from l5_services.publication.contributions import acknowledge, create_transform_publications
from l5_services.transform_reuse_probe import probe_reuse


NOW = datetime(2026, 1, 1, tzinfo=UTC)
SCOPE = {"tenant_id": "tenant-example", "product_id": "product-example",
         "project_id": "project-example", "relationship_revision": "1",
         "owner_id": "owner-example", "visibility": "only_me"}
SOURCE = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
          "source_connection_id": "repository", "record_type": "repository_commit",
          "record_id": "example/widget", "record_revision": "a" * 40}
PATCH = {"tenant_id": SCOPE["tenant_id"], "project_id": SCOPE["project_id"],
         "artifact_id": "patch-example", "version": "1", "sha256": "c" * 64,
         "expires_at": "2027-01-01T00:00:00Z"}


class FixtureDriver:
    def __init__(self):
        goal = {"kind": "refactor", "summary": "Improve structure"}
        self.run = {"run_id": "run-example", "scope": SCOPE,
                    "input": {"sources": [SOURCE], "transform_input": {"goal": goal}}}
        self.result = {"schema_version": "1.0", "service_id": "transform",
                       "run_id": "run-example", "result_id": "result-example", "revision": 1,
                       "scope": SCOPE, "source_ref": [SOURCE],
                       "retained_artifacts": {"original_patch": deepcopy(PATCH)},
                       "retention_expires_at": PATCH["expires_at"],
                       "source_trees": {"base_tree": "a" * 40, "candidate_tree": "b" * 40},
                       "verification_status": "succeeded"}
        run = SimpleNamespace(service_id="transform", tenant_id=SCOPE["tenant_id"],
                              product_id=SCOPE["product_id"], project_id=SCOPE["project_id"],
                              run_id=self.run["run_id"], input=self.run["input"])
        result = SimpleNamespace(tenant_id=SCOPE["tenant_id"], run_id=run.run_id,
                                 result_id=self.result["result_id"], version=1)
        detail = SimpleNamespace(tenant_id=SCOPE["tenant_id"], result_id=result.result_id,
                                 payload=self.result)
        publications = create_transform_publications(run, result, detail, now=NOW)
        self.rows = []
        for publication in publications:
            acknowledge(publication, {"receipt_id": f"receipt-{publication.destination}",
                                      "contribution_id": publication.publication_id,
                                      "admitted_schema": "1.0", "consumer_revision": "1",
                                      "record_refs": [publication.payload["result_ref"]]})
            publication.state = "published"
            self.rows.append({name: getattr(publication, name) for name in (
                "tenant_id", "publication_id", "result_id", "result_version", "destination",
                "schema_version", "contribution_hash", "payload", "state", "receipt",
                "invalidation_watermark", "applied_watermark")})
        reference = self.rows[0]["payload"]["result_ref"]
        citation = {"result_ref": deepcopy(reference),
                    "evidence_refs": deepcopy(self.rows[0]["payload"]["evidence_refs"])}
        self.later_task = {"task_id": "task-later", "run_id": "run-later", "scope": SCOPE,
                           "state": "succeeded", "evidence_ref": "private/later-task",
                           "input": {"citation": citation, "candidate_tree": "b" * 40},
                           "result": {"cited_result_ref": reference},
                           "entitlement": {"decision": "allowed", "scope": SCOPE,
                                           "evidence_ref": "private/current-entitlement"}}
        self.denials = {kind: {"kind": kind, "status": 404, "body": {"detail": {"code": "not_found"}},
                               "effect_count": 0, "evidence_ref": f"private/{kind}-denial",
                               "actor_id": f"actor-{kind}",
                               "actor_scope": {**SCOPE, "tenant_id": "foreign"} if kind == "foreign" else SCOPE,
                               "entitlement": {"decision": "denied", "evidence_ref": f"private/{kind}-fence",
                                               "watermark": 1, "applied_watermark": 1}}
                        for kind in ("foreign", "revoked")}
        self.calls = []

    async def read_run(self, run_id):
        self.calls.append("run")
        assert run_id == self.run["run_id"]
        return deepcopy(self.run)

    async def read_result(self, result_id):
        self.calls.append("result")
        assert result_id == self.result["result_id"]
        return deepcopy(self.result)

    async def read_publications(self, result_id):
        self.calls.append("sql-publications")
        assert result_id == self.result["result_id"]
        return deepcopy(self.rows)

    async def read_consumer_receipt(self, publication):
        self.calls.append(f"{publication['destination']}-receipt")
        return {"source": publication["destination"], "status": 200,
                "evidence_ref": f"private/{publication['destination']}-receipt",
                "data": deepcopy(publication["receipt"])}

    async def read_consumer_projection(self, publication):
        self.calls.append(f"{publication['destination']}-projection")
        return {"source": publication["destination"], "status": 200,
                "evidence_ref": f"private/{publication['destination']}-projection",
                "data": {**deepcopy(publication["payload"]),
                         "contribution_hash": publication["contribution_hash"]}}

    async def read_later_task(self, result_id):
        self.calls.append("later-task")
        return deepcopy(self.later_task)

    async def read_reuse_denial(self, result_id, kind):
        self.calls.append(f"{kind}-denial")
        return deepcopy(self.denials[kind])


async def check(driver):
    return await probe_reuse(driver, run_id="run-example", result_id="result-example",
                             scope=SCOPE, now=NOW)


@pytest.mark.asyncio
async def test_both_consumer_digests_receipts_and_later_citation_are_checked():
    driver = FixtureDriver()
    report = await check(driver)
    assert report["live_qualified"] is False
    assert len(report["consumer_refs"]) == 4
    assert report["later_task_ref"] == "private/later-task"
    assert driver.calls == ["run", "result", "sql-publications",
                            "product-context-receipt", "product-context-projection",
                            "engineering-insights-receipt", "engineering-insights-projection",
                            "later-task", "foreign-denial", "revoked-denial"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "foreign_source", "expired", "missing_consumer", "stale_publication", "digest",
    "receipt", "projection", "wrong_destination", "revoked_watermark",
    "fake_citation", "old_task", "wrong_candidate", "failed_task", "foreign_reuse",
    "revoked_reuse", "missing_peer_evidence", "reused_peer_evidence",
    "missing_entitlement", "same_scope_foreign", "unapplied_revocation",
])
async def test_missing_or_false_consumer_and_reuse_evidence_is_rejected(change):
    driver = FixtureDriver()
    if change == "foreign_source":
        driver.result["retained_artifacts"]["original_patch"]["tenant_id"] = "foreign"
    elif change == "expired":
        driver.result["retention_expires_at"] = "2025-01-01T00:00:00Z"
    elif change == "missing_consumer":
        driver.rows.pop()
    elif change == "stale_publication":
        driver.rows[0]["state"] = "pending"
    elif change == "digest":
        driver.rows[0]["contribution_hash"] = "d" * 64
    elif change == "receipt":
        driver.rows[0]["receipt"]["record_refs"] = []
    elif change == "projection":
        original = driver.read_consumer_projection

        async def changed(publication):
            observation = await original(publication)
            observation["data"]["goal"] = {"kind": "refactor", "summary": "Different"}
            return observation

        driver.read_consumer_projection = changed
    elif change == "wrong_destination":
        driver.rows[0]["destination"] = "foreign"
    elif change == "revoked_watermark":
        driver.rows[0]["invalidation_watermark"] = 1
    elif change == "fake_citation":
        driver.later_task["input"]["citation"]["result_ref"]["record_id"] = "unrelated"
    elif change == "old_task":
        driver.later_task["run_id"] = "run-example"
    elif change == "wrong_candidate":
        driver.later_task["input"]["candidate_tree"] = "d" * 40
    elif change == "failed_task":
        driver.later_task["state"] = "failed"
    elif change == "missing_entitlement":
        driver.later_task["entitlement"] = None
    elif change == "same_scope_foreign":
        driver.denials["foreign"]["actor_scope"] = SCOPE
    elif change == "unapplied_revocation":
        driver.denials["revoked"]["entitlement"]["applied_watermark"] = 0
    elif change == "foreign_reuse":
        driver.denials["foreign"]["status"] = 200
    elif change == "revoked_reuse":
        driver.denials["revoked"]["body"] = {"result_id": "result-example"}
    elif change == "missing_peer_evidence":
        original = driver.read_consumer_receipt

        async def missing(publication):
            observation = await original(publication)
            observation["evidence_ref"] = ""
            return observation

        driver.read_consumer_receipt = missing
    elif change == "reused_peer_evidence":
        original = driver.read_consumer_projection

        async def duplicate(publication):
            observation = await original(publication)
            observation["evidence_ref"] = f"private/{publication['destination']}-receipt"
            return observation

        driver.read_consumer_projection = duplicate
    with pytest.raises(ValueError, match="later task" if change == "fake_citation" else None):
        await check(driver)
