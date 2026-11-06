"""Synthetic native-result evidence checks; live provider claims remain unproven."""

import subprocess
from hashlib import sha256

import pytest

from l5_services.adapters.transform.provenance import compare_source_trees
from l5_services.transform_native_probe import probe_native_result


SOURCE_REVISION = "a" * 40
SCOPE = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"}
SOURCE = {"tenant_id": "tenant-a", "project_id": "project-a", "record_revision": SOURCE_REVISION,
          "record_type": "repository_commit", "record_id": "repo-a", "source_connection_id": "connection-a"}
BASE = {"src/app.py": b"old\n"}
CANDIDATE = {"src/app.py": b"new\n"}
MODES = {"src/app.py": "100644"}
PATCH = b"diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
NATIVE = {"tenant_id": "tenant-a", "project_id": "project-a", "native_id": "session-a",
          "executor_uid": "pod-a", "attempt_token": "token-a", "input_hash": "hash-a"}
PINS = {"interface": "fictional", "cli_version": "1.0", "binary_sha256": "b" * 64,
        "image_digest": "sha256:" + "c" * 64, "definition_revision": "revision-a"}
COMMAND = {"argv": ["/check"], "cwd": ".", "environment": {}, "timeout_seconds": 10,
           "minimum_test_count": 1}
POLICY = {"commands": [COMMAND], "image_digest": "sha256:" + "d" * 64,
          "bundle_sha256": "e" * 64, "lock_sha256": "f" * 64, "oracle_version": "1"}
TREES = compare_source_trees(BASE, CANDIDATE, base_modes=MODES, candidate_modes=MODES)


def receipt(stage):
    return {"stage": stage, "argv": COMMAND["argv"], "cwd": COMMAND["cwd"],
            "environment": COMMAND["environment"], "image_digest": POLICY["image_digest"],
            "source_tree": TREES["base_tree" if stage == "before" else "candidate_tree"],
            "bundle_sha256": POLICY["bundle_sha256"], "lock_sha256": POLICY["lock_sha256"],
            "oracle_version": POLICY["oracle_version"], "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:00:01Z", "exit_code": 0, "test_count": 1,
            "timed_out": False,
            "stdout": {"artifact_id": "out-" + stage, "version": "1",
                       "sha256": sha256(b"").hexdigest(), "bytes": 0},
            "stderr": {"artifact_id": "err-" + stage, "version": "1",
                       "sha256": sha256(b"").hexdigest(), "bytes": 0}}


class FakeDetachedRunner:
    def __init__(self):
        self.stages = []

    async def run_check(self, *, stage, files, source_tree, command, image_digest,
                        bundle_sha256, oracle_version, authority, file_modes):
        assert authority is self.authority
        assert files == (self.base_files if stage == "before" else self.candidate_files)
        self.stages.append(stage)
        return {**receipt(stage), "source_tree": source_tree}

    async def read_log(self, *, reference, authority):
        assert authority is self.authority
        return b""


class FakeDriver:
    def __init__(self):
        self.authority = object()
        self.detached_runner = FakeDetachedRunner()
        self.detached_runner.authority = self.authority
        self.detached_runner.base_files = BASE
        self.detached_runner.candidate_files = CANDIDATE
        self.run = {"run_id": "run-a", "scope": SCOPE, "state": "completed",
                    "provider_state": "completed", "native_job_ref": NATIVE,
                    "input": {"sources": [SOURCE], "transform_input": {
                        "goal": {"kind": "refactor", "summary": "Change app"}}},
                    "admission": {"source_snapshots": [{"snapshot_id": "snapshot-a"}],
                                  "transform_test_policy": POLICY}}
        self.result = {"run_id": "run-a", "scope": SCOPE, "source_ref": [SOURCE],
                       "verification_status": "succeeded", "native_job_ref": NATIVE,
                       "original_patch": {"sha256": sha256(PATCH).hexdigest(), "bytes": len(PATCH),
                                          "native_job_ref": NATIVE, "provider_pins": PINS},
                       "provider_attribution": PINS, "source_trees": TREES,
                       "dependency_comparison": {"status": "not_applicable"},
                       "retained_artifacts": {"original_patch": {"sha256": sha256(PATCH).hexdigest(),
                                                                 "bytes": len(PATCH)}},
                       "tests_before": [{"status": "passed", "receipt": receipt("before")}],
                       "tests_after": [{"status": "passed", "receipt": receipt("after")}]}
        self.observed_native = {**NATIVE, "state": "completed"}
        self.patch_bytes = PATCH
        self.trees = {"base_files": BASE, "candidate_files": CANDIDATE,
                      "base_modes": MODES, "candidate_modes": MODES}
        self.native_reads = 0

    async def read_run(self, run_id):
        assert run_id == "run-a"
        return self.run

    async def read_result(self, run_id):
        assert run_id == "run-a"
        return self.result

    async def read_native(self, native):
        assert native == NATIVE
        self.native_reads += 1
        return self.observed_native

    async def read_trees(self, source, native):
        assert source == SOURCE and native == NATIVE
        return self.trees

    async def read_artifact(self, reference):
        assert reference == self.result["retained_artifacts"]["original_patch"]
        return self.patch_bytes


@pytest.mark.asyncio
async def test_replays_original_patch_and_detached_checks_without_provider_resubmission():
    driver = FakeDriver()
    evidence = await probe_native_result(driver, run_id="run-a", scope=SCOPE,
                                         source_revision=SOURCE_REVISION, capability="refactor")
    assert evidence["live_qualified"] is False
    assert evidence["native_id"] == "session-a"
    assert evidence["original_patch_sha256"] == sha256(PATCH).hexdigest()
    assert evidence["candidate_tree"] == TREES["candidate_tree"]
    assert driver.native_reads == 1 and driver.detached_runner.stages == ["before", "after"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("source", {"record_revision": "b" * 40}),
    ("native", {"native_id": "foreign-session"}),
    ("patch", b"provider summary, not a patch"),
    ("candidate", {"src/app.py": b"tampered\n"}),
    ("reported", {"test_count": 0}),
    ("verification", {"verification_status": "partial"}),
])
async def test_mismatched_source_native_patch_or_detached_receipt_refuses_claim(field, value):
    driver = FakeDriver()
    if field == "source":
        driver.run["input"]["sources"] = [{**SOURCE, **value}]
    elif field == "native":
        driver.observed_native.update(value)
    elif field == "patch":
        driver.patch_bytes = value
    elif field == "candidate":
        driver.trees["candidate_files"] = value
    elif field == "reported":
        driver.result["tests_after"][0]["receipt"].update(value)
    else:
        driver.result.update(value)
    with pytest.raises(ValueError):
        await probe_native_result(driver, run_id="run-a", scope=SCOPE,
                                  source_revision=SOURCE_REVISION, capability="refactor")


@pytest.mark.asyncio
async def test_unpinned_revision_is_refused_before_any_read():
    driver = FakeDriver()
    with pytest.raises(ValueError):
        await probe_native_result(driver, run_id="run-a", scope=SCOPE,
                                  source_revision="HEAD", capability="refactor")
    assert driver.native_reads == 0 and not driver.detached_runner.stages


@pytest.mark.asyncio
async def test_dependency_upgrade_rechecks_lock_and_requested_artifact(tmp_path):
    from l5_services.adapters.transform.dependency_oracle import compare_dependency_upgrade

    target_hash = "sha256:" + "1" * 64
    previous_hash = "sha256:" + "2" * 64

    def snapshot(version, artifact_hash):
        return {
            "pyproject.toml": (f'[project]\nname = "sample"\ndependencies = ["sample-library=={version}"]\n').encode(),
            "uv.lock": (f'version = 1\n[[package]]\nname = "sample-library"\nversion = "{version}"\n'
                        f'sdist = {{hash = "{artifact_hash}"}}\n').encode(),
        }

    base = snapshot("1.0.0", previous_hash)
    candidate = snapshot("2.1.0", target_hash)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    for path, contents in base.items():
        (checkout / path).write_bytes(contents)
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "-qm", "base"], check=True)
    for path, contents in candidate.items():
        (checkout / path).write_bytes(contents)
    patch = subprocess.check_output(["git", "-C", str(checkout), "diff", "--binary"])
    driver = FakeDriver()
    modes = {path: "100644" for path in base}
    trees = compare_source_trees(base, candidate, base_modes=modes, candidate_modes=modes)
    driver.trees = {"base_files": base, "candidate_files": candidate,
                    "base_modes": modes, "candidate_modes": modes}
    driver.detached_runner.base_files = base
    driver.detached_runner.candidate_files = candidate
    driver.run["input"]["transform_input"]["goal"] = {
        "kind": "dependency_upgrade", "summary": "Upgrade library",
        "target": {"kind": "dependency", "name": "sample-library", "version": "2.1.0"}}
    driver.run["admission"]["transform_artifact_hashes"] = [target_hash]
    driver.result["source_trees"] = trees
    driver.result["original_patch"].update(sha256=sha256(patch).hexdigest(), bytes=len(patch))
    driver.result["retained_artifacts"]["original_patch"].update(
        sha256=sha256(patch).hexdigest(), bytes=len(patch))
    driver.patch_bytes = patch
    driver.result["dependency_comparison"] = {"status": "passed", **compare_dependency_upgrade(
        base, candidate, driver.run["input"]["transform_input"]["goal"]["target"], [target_hash])}
    for stage, key in (("before", "tests_before"), ("after", "tests_after")):
        driver.result[key] = [{"status": "passed", "receipt": {
            **receipt(stage), "source_tree": trees["base_tree" if stage == "before" else "candidate_tree"]}}]
    result = await probe_native_result(driver, run_id="run-a", scope=SCOPE,
                                       source_revision=SOURCE_REVISION, capability="dependency_upgrade")
    assert result["candidate_tree"] == trees["candidate_tree"]
    assert driver.detached_runner.stages == ["before", "after"]
    driver.run["admission"]["transform_artifact_hashes"] = [previous_hash]
    with pytest.raises(ValueError, match="artifact"):
        await probe_native_result(driver, run_id="run-a", scope=SCOPE,
                                  source_revision=SOURCE_REVISION, capability="dependency_upgrade")


@pytest.mark.asyncio
async def test_hash_matching_provider_prose_is_not_a_patch():
    driver = FakeDriver()
    prose = b"The provider says the application is fixed."
    driver.patch_bytes = prose
    driver.result["original_patch"].update(sha256=sha256(prose).hexdigest(), bytes=len(prose))
    driver.result["retained_artifacts"]["original_patch"].update(
        sha256=sha256(prose).hexdigest(), bytes=len(prose))
    with pytest.raises(ValueError, match="original patch"):
        await probe_native_result(driver, run_id="run-a", scope=SCOPE,
                                  source_revision=SOURCE_REVISION, capability="refactor")
