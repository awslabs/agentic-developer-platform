"""Synthetic cleanup evidence controls; actual termination and SCM readback remain live."""

from datetime import UTC, datetime
from hashlib import sha256

import pytest

from l5_services.transform_cleanup_probe import probe_cleanup


NOW = datetime(2026, 1, 1, tzinfo=UTC)
SCOPE = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"}
NATIVE = {"native_id": "session-a", "executor_uid": "pod-a"}
OWNED = {"tenant_id": "tenant-a", "project_id": "project-a", "record_type": "transform_definition",
         "record_id": "definition-owned", "record_revision": "1"}
ADOPTED = {**OWNED, "record_id": "definition-adopted"}
PATCH = b"diff --git a/src/app.py b/src/app.py\n"


def artifact(name, contents):
    return {"tenant_id": "tenant-a", "project_id": "project-a", "artifact_id": name,
            "version": "1", "sha256": sha256(contents).hexdigest(), "bytes": len(contents),
            "expires_at": "2027-01-01T00:00:00Z"}


class FixtureDriver:
    def __init__(self):
        self.contents = {"original_patch": PATCH}
        for stage in ("tests_before", "tests_after"):
            for stream in ("stdout", "stderr"):
                self.contents[f"{stage}-0-{stream}"] = (stage + stream).encode()
        self.run = {"run_id": "run-a", "scope": SCOPE, "native_job_ref": NATIVE}
        self.result = {"run_id": "run-a", "scope": SCOPE,
                       "retained_artifacts": {name: artifact(name, data)
                                              for name, data in self.contents.items()},
                       "original_patch": {"sha256": sha256(PATCH).hexdigest(),
                                          "native_job_ref": NATIVE}}
        for stage in ("tests_before", "tests_after"):
            self.result[stage] = [{"status": "failed", "receipt": {
                stream: {"sha256": self.result["retained_artifacts"][f"{stage}-0-{stream}"]["sha256"],
                         "bytes": len(self.contents[f"{stage}-0-{stream}"])}
                for stream in ("stdout", "stderr")}}]
        self.termination = {"pod_uid": "pod-a", "observedTermination": True,
                            "evidence_ref": "private/termination"}
        self.identity = {"account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/example",
                         "region": "us-east-1"}
        self.creation = {"operation_id": "create-a", "intent_hash": "hash-a",
                         "native_resource_ref": OWNED, "provider_identity": self.identity}
        self.absence = {"tenant_id": "tenant-a", "project_id": "project-a",
                        "record_type": "transform_absence", "record_id": "definition-owned",
                        "record_revision": "absence-a"}
        self.cleanup = {"native_resource_ref": OWNED, "absent": True,
                        "creation_receipt": self.creation,
                        "provider_identity": self.creation["provider_identity"],
                        "absence_evidence_ref": self.absence}
        self.inventory = {"definition-owned": {"native_resource_ref": OWNED, "absent": True,
                                                "absence_evidence_ref": self.absence},
                          "definition-adopted": {"native_resource_ref": ADOPTED, "present": True,
                                                 "ownership": "customer_linked", "creation_receipt": None}}
        self.adopted_cleanup = None
        self.scm = {"before": {"revision": "commit-a", "pull_requests": 0},
                    "after": {"revision": "commit-a", "pull_requests": 0},
                    "evidence_ref": "private/scm-readback"}
        self.calls = []

    async def read_result(self, run_id):
        self.calls.append("result")
        return self.result

    async def read_run(self, run_id):
        self.calls.append("run")
        return self.run

    async def read_termination(self, native):
        assert native == NATIVE
        self.calls.append("termination")
        return self.termination

    async def read_artifact(self, reference):
        self.calls.append("artifact")
        return self.contents[reference["artifact_id"]]

    async def read_identity(self):
        self.calls.append("identity")
        return self.identity

    async def read_cleanup_receipt(self, reference):
        self.calls.append("cleanup")
        return self.cleanup if reference == OWNED else self.adopted_cleanup

    async def read_native_resource(self, reference):
        self.calls.append("inventory")
        return self.inventory[reference["record_id"]]

    async def read_scm(self, run_id):
        self.calls.append("scm")
        return self.scm


@pytest.mark.asyncio
async def test_retained_patch_and_test_logs_survive_termination_and_owned_cleanup():
    driver = FixtureDriver()
    result = await probe_cleanup(driver, run_id="run-a", scope=SCOPE,
                                 owned=[OWNED], adopted=[ADOPTED], now=NOW)
    assert result["live_qualified"] is False
    assert result["artifact_hashes"]["original_patch"] == sha256(PATCH).hexdigest()
    assert result["owned_absent"] == result["adopted_retained"] == 1
    assert driver.calls == ["result", "run", "termination"] + ["artifact"] * 5 + [
        "identity", "cleanup", "inventory", "cleanup", "inventory", "scm"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hash_mismatch", "expired", "missing_log", "no_termination", "scm_effect"])
async def test_missing_retention_termination_or_unchanged_scm_refuses_claim(failure):
    driver = FixtureDriver()
    if failure == "hash_mismatch":
        driver.contents["original_patch"] = b"replaced"
    elif failure == "expired":
        driver.result["retained_artifacts"]["original_patch"]["expires_at"] = "2025-01-01T00:00:00Z"
    elif failure == "missing_log":
        driver.result["retained_artifacts"].pop("tests_after-0-stderr")
    elif failure == "no_termination":
        driver.termination["observedTermination"] = False
    else:
        driver.scm["after"]["pull_requests"] = 1
    with pytest.raises(ValueError):
        await probe_cleanup(driver, run_id="run-a", scope=SCOPE,
                            owned=[OWNED], adopted=[ADOPTED], now=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "adopted_cleanup", "adopted_deleted", "missing_ownership", "foreign_identity", "missing_absence",
])
async def test_adopted_or_unproven_resource_cleanup_is_never_accepted(failure):
    driver = FixtureDriver()
    if failure == "adopted_cleanup":
        driver.adopted_cleanup = {"native_resource_ref": ADOPTED, "absent": True}
    elif failure == "adopted_deleted":
        driver.inventory["definition-adopted"] = {"native_resource_ref": ADOPTED, "absent": True}
    elif failure == "missing_ownership":
        driver.cleanup["creation_receipt"] = None
    elif failure == "foreign_identity":
        driver.identity = {**driver.identity, "account_id": "000000000002"}
    else:
        driver.cleanup["absence_evidence_ref"] = None
    with pytest.raises(ValueError):
        await probe_cleanup(driver, run_id="run-a", scope=SCOPE,
                            owned=[OWNED], adopted=[ADOPTED], now=NOW)


@pytest.mark.asyncio
async def test_overlapping_owned_and_adopted_selection_stops_before_readback():
    driver = FixtureDriver()
    with pytest.raises(ValueError):
        await probe_cleanup(driver, run_id="run-a", scope=SCOPE,
                            owned=[OWNED], adopted=[OWNED], now=NOW)
    assert not driver.calls
