"""Pinned, independent Transform provenance checks."""

from hashlib import sha256
import json
import subprocess

import pytest

from l5_services.adapters.transform.provenance import capture_original_patch, compare_source_trees, verify_original_patch
from test_tf03_attempts import work_db


PINS = {"interface": "synthetic", "cli_version": "1", "binary_sha256": "a" * 64,
        "image_digest": "sha256:" + "b" * 64, "definition_revision": "pinned"}
MANIFEST = {"source_snapshot_ids": ["sealed-source-a"], "sources": [{"record_id": "example/repo"}]}


def _git_tree(tmp_path, files, modes=None):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for path, contents in files.items():
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
        if modes is not None:
            destination.chmod(int(modes[path], 8))
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return subprocess.check_output(["git", "-C", str(tmp_path), "write-tree"]).decode().strip()


def test_original_patch_provenance(tmp_path):
    raw = b"diff --git a/src/app.py b/src/app.py\n+original bytes\r\n"
    native = {"tenant_id": "tenant-a", "project_id": "project-a", "native_id": "session-a",
              "executor_uid": "executor-a", "attempt_token": "token-a", "input_hash": "hash-a"}
    original = capture_original_patch(raw, native, PINS, MANIFEST)
    assert original.contents == raw
    assert original.sha256 == sha256(raw).hexdigest()
    assert original.bytes == len(raw)
    assert dict(original.native_job_ref)["native_id"] == "session-a"
    native["native_id"] = "changed-after-capture"
    assert dict(original.native_job_ref)["native_id"] == "session-a"
    assert dict(original.provider_pins) == PINS
    MANIFEST["sources"][0]["record_id"] = "changed-after-capture"
    assert json.loads(original.base_manifest)["sources"][0]["record_id"] == "example/repo"
    MANIFEST["sources"][0]["record_id"] = "example/repo"

    base = {"src/app.py": b"old\n", "src/unchanged.py": b"same\n"}
    candidate = {"src/app.py": b"new\n", "src/unchanged.py": b"same\n",
                 "src/new.py": b"added\n"}
    comparison = compare_source_trees(base, candidate)
    assert comparison == {"base_tree": _git_tree(tmp_path / "base", base),
                          "candidate_tree": _git_tree(tmp_path / "candidate", candidate),
                          "changed_files": ["src/app.py", "src/new.py"]}
    assert original.contents == raw


def test_original_patch_rejects_missing_attribution_and_invalid_snapshots():
    native = {"tenant_id": "tenant-a", "project_id": "project-a", "native_id": "session-a",
              "executor_uid": "executor-a", "attempt_token": "token-a", "input_hash": "hash-a"}
    with pytest.raises(ValueError, match="original byte"):
        capture_original_patch("provider success", native, PINS, MANIFEST)
    with pytest.raises(ValueError, match="native"):
        capture_original_patch(b"patch", {**native, "native_id": ""}, PINS, MANIFEST)
    with pytest.raises(ValueError, match="provider"):
        capture_original_patch(b"patch", native, {**PINS, "cli_version": ""}, MANIFEST)
    with pytest.raises(ValueError, match="source"):
        capture_original_patch(b"patch", native, PINS, {})
    for path in ("../elsewhere", "/absolute", ".git/config", "src/../../escape", "src\\file"):
        with pytest.raises(ValueError, match="invalid path"):
            compare_source_trees({}, {path: b"bad"})


@pytest.mark.parametrize("change", ["mode", "rename", "delete"])
def test_applied_patch_reports_independent_changed_paths(change):
    base = {"src/app.py": b"old\n"}
    base_modes = {"src/app.py": "100644"}
    if change == "mode":
        candidate, candidate_modes = base, {"src/app.py": "100755"}
        raw = b"diff --git a/src/app.py b/src/app.py\nold mode 100644\nnew mode 100755\n"
    elif change == "rename":
        candidate, candidate_modes = {"src/renamed.py": b"old\n"}, {"src/renamed.py": "100644"}
        raw = (b"diff --git a/src/app.py b/src/renamed.py\nsimilarity index 100%\n"
               b"rename from src/app.py\nrename to src/renamed.py\n")
    else:
        candidate, candidate_modes = {}, {}
        raw = (b"diff --git a/src/app.py b/src/app.py\ndeleted file mode 100644\n"
               b"--- a/src/app.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n")
    native = {"tenant_id": "tenant-a", "project_id": "project-a", "native_id": "session-a",
              "executor_uid": "executor-a", "attempt_token": "token-a", "input_hash": "hash-a"}
    original = capture_original_patch(raw, native, PINS, MANIFEST)
    expected = compare_source_trees(base, candidate, base_modes=base_modes,
                                    candidate_modes=candidate_modes)
    assert verify_original_patch(base, expected["candidate_tree"], original, base_modes) == expected


@pytest.mark.asyncio
async def test_original_patch_provenance_collection_readback():
    from l5_services.adapters.transform.execution import TransformAdapter
    from test_tf03_adapter import FakeExecutor, SESSION, pinned

    run, manifest, attempt = pinned()
    run.manifest = manifest
    bridge = FakeExecutor()
    adapter = TransformAdapter(bridge)
    intent = adapter._intent(run, attempt)
    bridge.receipt = {"executor_name": intent["executor_name"], "executor_uid": "executor-a",
                      "readback": {"native_id": "session-a", "attempt_token": attempt.idempotency_token,
                                   "input_hash": attempt.input_hash, "state": "completed"}}
    native = (await adapter.submit(run, manifest, attempt, SESSION)).native_job_ref
    bridge.native_ref = native
    original_bytes = b"diff --git a/src/app.py b/src/app.py\n+original\r\n"
    reference = {"tenant_id": "tenant-a", "project_id": "project-a", "artifact_id": "patch-a",
                 "version": "1", "media_type": "text/x-diff", "sha256": sha256(original_bytes).hexdigest(),
                 "bytes": len(original_bytes)}
    bridge.original_bytes = original_bytes
    bridge.patch_reference = reference
    bridge.reads_original = 0

    async def collect_provider(*, intent, authority):
        assert authority is SESSION and intent["attempt_token"] == attempt.idempotency_token
        return {"native_job_ref": native, "state": "completed", "artifacts": {"original_patch": bridge.patch_reference}}

    async def read_original_patch(*, reference, authority):
        assert authority is SESSION and reference is bridge.patch_reference
        bridge.reads_original += 1
        return bridge.original_bytes

    bridge.collect_provider = collect_provider
    bridge.read_original_patch = read_original_patch
    result = (await adapter.collect(run, native, attempt, SESSION)).result
    assert result["original_patch"]["sha256"] == sha256(original_bytes).hexdigest()
    assert result["original_patch"]["native_job_ref"]["native_id"] == "session-a"
    assert result["original_patch"]["provider_pins"]["image_digest"] == PINS["image_digest"]
    assert result["original_patch"]["base_manifest_sha256"] == sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert bridge.reads_original == 1
    assert original_bytes not in repr(result).encode()

    bridge.original_bytes = b"provider substituted different bytes"
    with pytest.raises(ValueError, match="readback"):
        await adapter.collect(run, native, attempt, SESSION)
    bridge.original_bytes = original_bytes
    bridge.patch_reference = {**reference, "tenant_id": "tenant-b"}
    with pytest.raises(ValueError, match="scoped"):
        await adapter.collect(run, native, attempt, SESSION)
    bridge.patch_reference = reference
    async def collect_with_unverified_bytes(*, intent, authority):
        return {"native_job_ref": native, "state": "completed", "artifacts": {
            "original_patch": reference, "provider_debug_bytes": b"unverified"}}

    bridge.collect_provider = collect_with_unverified_bytes
    with pytest.raises(ValueError, match="unverified artifacts"):
        await adapter.collect(run, native, attempt, SESSION)
    assert bridge.reads_original == 2


@pytest.mark.asyncio
async def test_independent_tests_before_and_after():
    from l5_services.adapters.transform.verification import verify_detached_checks

    command = {"argv": ["/oracle/bin/check", "--junit", "/reports/tests.xml"], "cwd": "src",
               "environment": {"NO_NETWORK": "1"}, "timeout_seconds": 10, "minimum_test_count": 2}
    policy = {"image_digest": "sha256:" + "b" * 64, "bundle_sha256": "c" * 64,
              "lock_sha256": "d" * 64, "oracle_version": "oracle-1", "commands": [command]}
    before = {"src/app.py": b"old"}
    after = {"src/app.py": b"new"}
    authority = object()

    class TrustedRunner:
        def __init__(self):
            self.calls = []

        async def run_check(self, *, stage, files, source_tree, command, image_digest,
                            bundle_sha256, oracle_version, authority, file_modes):
            assert authority is trusted_authority
            assert files == (before if stage == "before" else after)
            assert file_modes == {path: "100644" for path in files}
            self.calls.append(stage)
            return {"stage": stage, "argv": command["argv"], "cwd": command["cwd"],
                    "environment": command["environment"], "image_digest": image_digest,
                    "source_tree": source_tree, "bundle_sha256": bundle_sha256,
                    "lock_sha256": policy["lock_sha256"], "oracle_version": oracle_version,
                    "started_at": "2026-01-01T00:00:00Z", "ended_at": "2026-01-01T00:00:01Z",
                    "exit_code": 0, "test_count": 2, "timed_out": False,
                    "stdout": {"artifact_id": f"stdout-{stage}", "version": "1",
                               "sha256": sha256(b"").hexdigest(), "bytes": 0},
                    "stderr": {"artifact_id": f"stderr-{stage}", "version": "1",
                               "sha256": sha256(b"").hexdigest(), "bytes": 0}}

        async def read_log(self, *, reference, authority):
            assert authority is trusted_authority
            assert reference["artifact_id"].startswith(("stdout-", "stderr-"))
            return b""

    trusted_authority = authority
    runner = TrustedRunner()
    result = await verify_detached_checks(runner, policy, before, after, authority)
    assert runner.calls == ["before", "after"]
    assert [entry["status"] for entry in result["tests_before"] + result["tests_after"]] == [
        "passed", "passed"]
    assert result["tests_before"][0]["receipt"]["argv"] == command["argv"]
    assert result["tests_after"][0]["receipt"]["source_tree"] == result["source_trees"]["candidate_tree"]
    assert result["source_trees"]["base_tree"] != result["source_trees"]["candidate_tree"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("candidate_change", "expected"), [
    ({"exit_code": 1, "test_count": 2, "provider_claim": "tests passed"}, "failed"),
    ({"exit_code": 0, "test_count": 0, "provider_claim": "tests passed"}, "failed"),
    ({"exit_code": None, "test_count": 2, "provider_claim": "tests passed"}, "inconclusive"),
    ({"exit_code": 0, "test_count": None, "provider_claim": "tests passed"}, "inconclusive"),
    ({"exit_code": 0, "test_count": 2, "stdout": None}, "inconclusive"),
    ({"timed_out": True, "exit_code": None, "termination_receipt": None}, "inconclusive"),
    ({"timed_out": True, "exit_code": None, "termination_receipt": {"stopped": True}}, "failed"),
])
async def test_independent_tests_reject_tampering(candidate_change, expected):
    from l5_services.adapters.transform.verification import verify_detached_checks

    command = {"argv": ["/oracle/check", "--report", "/reports/result.xml"], "cwd": ".",
               "environment": {"NO_NETWORK": "1"}, "timeout_seconds": 10, "minimum_test_count": 2}
    policy = {"image_digest": "sha256:" + "b" * 64, "bundle_sha256": "c" * 64,
              "lock_sha256": "d" * 64, "oracle_version": "oracle-1", "commands": [command]}
    base = {"tests/test_required.py": b"test one\ntest two\n", "src/app.py": b"old"}
    candidate = {"src/app.py": b"new"}
    authority = object()

    class DetachedFixture:
        def __init__(self):
            self.calls = []
            self.sealed_bytes = b"2 tests passed"
            self.log_bytes = self.sealed_bytes

        async def run_check(self, *, stage, files, source_tree, command, image_digest,
                            bundle_sha256, oracle_version, authority, file_modes):
            assert authority is pinned_authority
            assert files == (base if stage == "before" else candidate)
            self.calls.append(stage)
            receipt = {"stage": stage, "argv": command["argv"], "cwd": command["cwd"],
                       "environment": command["environment"], "image_digest": image_digest,
                       "source_tree": source_tree, "bundle_sha256": bundle_sha256,
                       "lock_sha256": policy["lock_sha256"], "oracle_version": oracle_version,
                       "started_at": "2026-01-01T00:00:00Z", "ended_at": "2026-01-01T00:00:01Z",
                       "exit_code": 0, "test_count": 2, "timed_out": False,
                       "stdout": {"artifact_id": f"stdout-{stage}", "version": "1",
                                  "sha256": sha256(self.sealed_bytes).hexdigest(),
                                  "bytes": len(self.sealed_bytes)},
                       "stderr": {"artifact_id": f"stderr-{stage}", "version": "1",
                                  "sha256": sha256(b"").hexdigest(), "bytes": 0}}
            return {**receipt, **(candidate_change if stage == "after" else {})}

        async def read_log(self, *, reference, authority):
            assert authority is pinned_authority
            return self.log_bytes if reference["artifact_id"].startswith("stdout-") else b""

    pinned_authority = authority
    runner = DetachedFixture()
    result = await verify_detached_checks(runner, policy, base, candidate, authority)
    assert runner.calls == ["before", "after"]
    assert result["source_trees"]["changed_files"] == ["src/app.py", "tests/test_required.py"]
    assert result["tests_before"][0]["status"] == "passed"
    assert result["tests_after"][0]["status"] == expected
    if "provider_claim" in candidate_change:
        assert result["tests_after"][0]["receipt"]["provider_claim"] == "tests passed"

    runner.log_bytes = b"provider substituted log bytes"
    result = await verify_detached_checks(runner, policy, base, candidate, authority)
    assert result["tests_before"][0]["status"] == "inconclusive"
    assert result["tests_after"][0]["status"] == "not_run"
    assert runner.calls == ["before", "after", "before"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("base_mode", "candidate_mode", "mode_only"), [
    ("100644", "100644", False), ("100755", "100755", False),
    ("100644", "100755", True),
])
async def test_independent_tests_collected_without_replaying_baseline(
        tmp_path, base_mode, candidate_mode, mode_only):
    from dataclasses import replace
    from datetime import UTC, datetime

    from l5_services.adapters.transform.execution import TransformAdapter
    from l5_services.adapters.transform.provenance import compare_source_trees
    from test_tf03_adapter import FakeExecutor, SESSION, pinned

    run, manifest, attempt = pinned()
    attempt = replace(attempt, started_at=datetime(2026, 1, 1, 1, tzinfo=UTC))
    base_files = {"src/app.py": b"old\n"}
    candidate_files = {"src/app.py": b"old\n" if mode_only else b"new\n"}
    base_modes = {"src/app.py": base_mode}
    candidate_modes = {"src/app.py": candidate_mode}
    run.manifest = {**manifest, "files": [{"path": "src/app.py",
                                          "sha256": sha256(base_files["src/app.py"]).hexdigest(),
                                          "bytes": len(base_files["src/app.py"]), "mode": base_mode}]}
    command = {"argv": ["/oracle/check", "--junit", "/reports/tests.xml"], "cwd": ".",
               "environment": {"NO_NETWORK": "1"}, "timeout_seconds": 10, "minimum_test_count": 1}
    policy = {"image_digest": "sha256:" + "b" * 64, "bundle_sha256": "c" * 64,
              "lock_sha256": "d" * 64, "oracle_version": "oracle-1", "commands": [command],
              "test_policy_ref": run.admission["transform_settings"]["test_policy_ref"]}
    run.admission["transform_test_policy"] = policy
    original_bytes = (b"diff --git a/src/app.py b/src/app.py\n"
                      b"--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n")
    if mode_only:
        original_bytes = b"diff --git a/src/app.py b/src/app.py\nold mode 100644\nnew mode 100755\n"
    reference = {"tenant_id": run.tenant_id, "project_id": run.project_id,
                 "artifact_id": "original-a", "version": "1", "media_type": "text/x-diff",
                 "sha256": sha256(original_bytes).hexdigest(), "bytes": len(original_bytes)}
    tree_ids = {"base_tree": _git_tree(tmp_path / "base", base_files, base_modes),
                "candidate_tree": _git_tree(tmp_path / "candidate", candidate_files, candidate_modes),
                "changed_files": ["src/app.py"]}

    def receipt(stage, source_tree):
        return {"stage": stage, "argv": command["argv"], "cwd": command["cwd"],
                "environment": command["environment"], "image_digest": policy["image_digest"],
                "source_tree": source_tree, "bundle_sha256": policy["bundle_sha256"],
                "lock_sha256": policy["lock_sha256"], "oracle_version": policy["oracle_version"],
                "started_at": "2026-01-01T00:00:00Z", "ended_at": "2026-01-01T00:00:01Z",
                "exit_code": 0, "test_count": 1, "timed_out": False,
                "stdout": {"artifact_id": f"stdout-{stage}", "version": "1",
                           "sha256": sha256(b"").hexdigest(), "bytes": 0},
                "stderr": {"artifact_id": f"stderr-{stage}", "version": "1",
                           "sha256": sha256(b"").hexdigest(), "bytes": 0}}

    from l5_services.adapters.transform.baseline import pinned_test_policy

    _, policy_sha256 = pinned_test_policy(run.admission)
    attempt = replace(attempt, baseline_policy_sha256=policy_sha256,
                      baseline_receipts=[receipt("before", tree_ids["base_tree"])])

    class CollectedBridge(FakeExecutor):
        baseline = [receipt("before", tree_ids["base_tree"])]
        trees = {"base_files": base_files, "candidate_files": candidate_files,
                 "base_modes": base_modes, "candidate_modes": candidate_modes}

        async def collect_provider(self, *, intent, authority):
            assert authority is SESSION
            return {"state": "completed", "native_job_ref": self.native_ref,
                    "artifacts": {"original_patch": reference}}

        async def read_original_patch(self, *, reference: dict, authority):
            assert authority is SESSION
            return original_bytes

        async def read_verified_trees(self, *, manifest, native_job_ref, original_patch, authority):
            assert manifest is run.manifest and native_job_ref == self.native_ref
            assert original_patch is reference and authority is SESSION
            return self.trees

        async def read_test_baseline(self, *, manifest, authority):
            assert manifest is run.manifest and authority is SESSION
            return self.baseline

    class CandidateRunner:
        calls = []
        stale = False

        async def run_check(self, *, stage, files, source_tree, command, image_digest,
                            bundle_sha256, oracle_version, authority, file_modes=None):
            assert stage == "after" and files == candidate_files and authority is SESSION
            assert file_modes == candidate_modes
            assert source_tree == tree_ids["candidate_tree"]
            self.calls.append(stage)
            return {**receipt(stage, source_tree),
                    "started_at": "2026-01-01T00:59:58Z" if self.stale else "2026-01-01T01:00:02Z",
                    "ended_at": "2026-01-01T00:59:59Z" if self.stale else "2026-01-01T01:00:03Z"}

        async def read_log(self, *, reference, authority):
            assert authority is SESSION
            return b""

    bridge = CollectedBridge()
    runner = CandidateRunner()
    adapter = TransformAdapter(bridge, detached_runner=runner)
    intent = adapter._intent(run, attempt)
    bridge.receipt = {"executor_name": intent["executor_name"], "executor_uid": "executor-a",
                      "readback": {"native_id": "session-a", "attempt_token": attempt.idempotency_token,
                                   "input_hash": attempt.input_hash, "state": "completed"}}
    bridge.native_ref = (await adapter.submit(run, manifest, attempt, SESSION)).native_job_ref
    result = (await adapter.collect(run, bridge.native_ref, attempt, SESSION)).result
    assert [entry["status"] for entry in result["tests_before"]] == ["passed"]
    assert [entry["status"] for entry in result["tests_after"]] == ["passed"]
    assert result["checks_status"] == "passed" and result["verification_status"] == "partial"
    assert result["source_trees"] == tree_ids and runner.calls == ["after"]

    legacy_attempt = replace(attempt, baseline_receipts=None)
    bridge.baseline = None
    missing = (await adapter.collect(run, bridge.native_ref, legacy_attempt, SESSION)).result
    assert missing["limitations"] == ["pre_submit_baseline_not_run"]
    assert missing["verification_status"] == "partial" and not missing["tests_after"]
    assert runner.calls == ["after"] and len(bridge.launches) == 1
    bridge.baseline = [{**receipt("before", tree_ids["base_tree"]),
                        "ended_at": "2026-01-01T02:00:00Z"}]
    late = (await adapter.collect(run, bridge.native_ref, legacy_attempt, SESSION)).result
    assert late["tests_before"][0]["status"] == "inconclusive"
    assert late["tests_after"][0]["status"] == "not_run"
    assert runner.calls == ["after"] and len(bridge.launches) == 1
    bridge.trees = {**bridge.trees, "base_modes": {"src/app.py": "100644" if base_mode == "100755" else "100755"}}
    wrong_modes = (await adapter.collect(run, bridge.native_ref, attempt, SESSION)).result
    assert wrong_modes["scope_status"] == "failed"
    assert runner.calls == ["after"]
    bridge.trees = {**bridge.trees, "base_files": {"src/app.py": b"tampered"}}
    with pytest.raises(ValueError, match="sealed source"):
        await adapter.collect(run, bridge.native_ref, attempt, SESSION)
    bridge.trees = {**bridge.trees, "base_files": base_files, "base_modes": base_modes}
    runner.stale = True
    stale = (await adapter.collect(run, bridge.native_ref, attempt, SESSION)).result
    assert stale["tests_after"][0]["status"] == "inconclusive"
    assert stale["checks_status"] == "partial"
    assert len(bridge.launches) == 1


def test_dependency_manifest_and_lock_parser():
    from l5_services.adapters.transform.dependency_oracle import compare_dependency_upgrade

    old_hash = "sha256:" + "a" * 64
    target_hash = "sha256:" + "b" * 64
    other_hash = "sha256:" + "c" * 64
    target = {"kind": "dependency", "name": "sample-library", "version": "2.1.0"}

    def snapshot(version, digest, *, other_version="1.2.0", other_hash=other_hash):
        manifest = (f'[project]\nname = "sample"\n'
                    f'dependencies = ["sample_library=={version}", "other=={other_version}"]\n')
        lock = (f'version = 1\n[[package]]\nname = "sample-library"\n'
                f'version = "{version}"\nsource = {{registry = "https://example.invalid/simple"}}\n'
                f'sdist = {{hash = "{digest}"}}\n'
                f'[[package]]\nname = "other"\nversion = "{other_version}"\n'
                f'source = {{registry = "https://example.invalid/simple"}}\n'
                f'sdist = {{hash = "{other_hash}"}}\n')
        return {"pyproject.toml": manifest.encode(), "uv.lock": lock.encode()}

    base = snapshot("1.0.0", old_hash)
    candidate = snapshot("2.1.0", target_hash)
    result = compare_dependency_upgrade(base, candidate, target, [target_hash])
    assert result["target"] == target
    assert result["base_version"] == "1.0.0"
    assert result["candidate_version"] == "2.1.0"
    assert result["artifact_hashes"] == [target_hash]
    assert result["base_uv_lock_sha256"] == sha256(base["uv.lock"]).hexdigest()
    assert result["candidate_pyproject_toml_sha256"] == sha256(candidate["pyproject.toml"]).hexdigest()

    for modified, pins in (
        (snapshot("2.1.0", target_hash, other_version="9.9.9"), [target_hash]),
        (snapshot("2.1.0", target_hash, other_hash=old_hash), [target_hash]),
        (snapshot("2.1.0", old_hash), [target_hash]),
        (snapshot("2.0.0", target_hash), [target_hash]),
        (candidate, [old_hash]),
    ):
        with pytest.raises(ValueError, match="manifest|lock|artifact"):
            compare_dependency_upgrade(base, modified, target, pins)
    with pytest.raises(ValueError, match="artifact"):
        compare_dependency_upgrade(base, candidate, target, [])
    with pytest.raises(ValueError, match="exact pinned"):
        compare_dependency_upgrade(base, {**candidate, "pyproject.toml": b'[project]\ndependencies = ["sample-library>=2"]'},
                                   target, [target_hash])


def test_dependency_scope_oracle_accepts_only_changed_allowed_files(tmp_path):
    import os

    from l5_services.adapters.transform.scope_oracle import check_write_scope

    base = {"src/tool.py": b"print('old')\n", "src/other.py": b"same\n"}
    candidate = {"src/tool.py": b"print('new')\n", "src/other.py": b"same\n"}
    modes = {path: "100644" for path in base}
    verdict = check_write_scope(base, candidate, base_modes=modes, candidate_modes=modes,
                                write_paths=["src/tool.py"], excluded_paths=[".git"])
    assert verdict["scope_status"] == "passed"
    assert verdict["changed_files"] == ["src/tool.py"]
    assert verdict["base_tree"] == _git_tree(tmp_path / "scope-base", base)
    assert verdict["candidate_tree"] == _git_tree(tmp_path / "scope-candidate", candidate)

    executable = {"src/tool.py": "100755", "src/other.py": "100644"}
    verdict = check_write_scope(base, base, base_modes=modes, candidate_modes=executable,
                                write_paths=["src/tool.py"], excluded_paths=[])
    checkout = tmp_path / "executable"
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    for path, contents in base.items():
        destination = checkout / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
    os.chmod(checkout / "src/tool.py", 0o755)
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)
    actual = subprocess.check_output(["git", "-C", str(checkout), "write-tree"]).decode().strip()
    assert verdict["candidate_tree"] == actual
    assert verdict["changed_files"] == ["src/tool.py"]
    with pytest.raises(ValueError, match="unsupported file mode"):
        check_write_scope(base, candidate, base_modes=modes,
                          candidate_modes={**modes, "src/tool.py": "120000"},
                          write_paths=["src"], excluded_paths=[])


@pytest.mark.parametrize(("case", "candidate", "candidate_modes", "allowed", "excluded", "reason"), [
    ("unrelated-write", {"src/tool.py": b"old", "private/token.txt": b"new"},
     {"src/tool.py": "100644", "private/token.txt": "100644"}, ["src"], [], "outside"),
    ("excluded-write", {"src/tool.py": b"new"}, {"src/tool.py": "100644"},
     ["src"], ["src/tool.py"], "outside"),
    ("traversal", {"src/tool.py": b"old", "src/../private": b"new"},
     {"src/tool.py": "100644", "src/../private": "100644"}, ["src"], [], "invalid path"),
    ("absolute", {"src/tool.py": b"old", "/elsewhere": b"new"},
     {"src/tool.py": "100644", "/elsewhere": "100644"}, ["src"], [], "invalid path"),
    ("drive-prefix", {"src/tool.py": b"old", "C:private": b"new"},
     {"src/tool.py": "100644", "C:private": "100644"}, ["src"], [], "invalid path"),
    ("git-metadata", {"src/tool.py": b"old", ".git/config": b"new"},
     {"src/tool.py": "100644", ".git/config": "100644"}, ["src"], [], "invalid path"),
    ("symlink", {"src/tool.py": b"private/target"}, {"src/tool.py": "120000"},
     ["src"], [], "unsupported file mode"),
    ("submodule", {"src/tool.py": b"gitlink"}, {"src/tool.py": "160000"},
     ["src"], [], "unsupported file mode"),
    ("missing-modes", {"src/tool.py": b"new"}, {}, ["src"], [], "file modes"),
    ("case-alias", {"src/tool.py": b"old", "SRC/tool.py": b"new"},
     {"src/tool.py": "100644", "SRC/tool.py": "100644"}, ["src"], [], "case-colliding"),
    ("unicode-alias", {"src/tool.py": b"old", "src/e\u0301.py": b"new"},
     {"src/tool.py": "100644", "src/e\u0301.py": "100644"}, ["src"], [], "invalid path"),
    ("lfs-pointer", {"src/tool.py": b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64},
     {"src/tool.py": "100644"}, ["src"], [], "LFS pointer"),
    ("no-change", {"src/tool.py": b"old"}, {"src/tool.py": "100644"},
     ["src"], [], "did not change"),
])
def test_dependency_scope_rejects_escapes_and_links(case, candidate, candidate_modes,
                                                    allowed, excluded, reason):
    from l5_services.adapters.transform.scope_oracle import check_write_scope

    base = {"src/tool.py": b"old"}
    with pytest.raises(ValueError, match=reason):
        check_write_scope(base, candidate, base_modes={"src/tool.py": "100644"},
                          candidate_modes=candidate_modes, write_paths=allowed,
                          excluded_paths=excluded)


def _git_diff(tmp_path, base, candidate):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for path, contents in base.items():
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    for path in base.keys() - candidate.keys():
        (tmp_path / path).unlink()
    for path, contents in candidate.items():
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
    return subprocess.check_output(["git", "-C", str(tmp_path), "diff", "--binary"])


@pytest.mark.asyncio
async def test_dependency_scope_oracles_bind_collected_patch_to_trusted_trees(tmp_path):
    from l5_services.adapters.transform.execution import TransformAdapter
    from test_tf03_adapter import FakeExecutor, SESSION, pinned

    target = {"kind": "dependency", "name": "sample-library", "version": "2.1.0"}
    target_hash = "sha256:" + "b" * 64

    def versioned(version, digest, other="1.0.0"):
        return {"pyproject.toml": (f'[project]\nname = "example"\n'
                                    f'dependencies = ["sample-library=={version}", "other=={other}"]\n').encode(),
                "uv.lock": (f'version = 1\n[[package]]\nname = "sample-library"\n'
                            f'version = "{version}"\nsource = {{registry = "https://example.invalid/simple"}}\n'
                            f'sdist = {{hash = "{digest}"}}\n'
                            f'[[package]]\nname = "other"\nversion = "{other}"\n'
                            f'source = {{registry = "https://example.invalid/simple"}}\n'
                            f'sdist = {{hash = "sha256:' + 'c' * 64 + '"}\n').encode()}

    base = versioned("1.0.0", "sha256:" + "a" * 64)
    candidate = versioned("2.1.0", target_hash)
    run, manifest, attempt = pinned()
    run.input["transform_input"]["goal"] = {"kind": "dependency_upgrade",
                                            "summary": "Upgrade", "target": target}
    run.admission["transform_settings"]["write_paths"] = ["pyproject.toml", "uv.lock"]
    run.admission["transform_artifact_hashes"] = [target_hash]
    run.manifest = {**manifest, "files": [{"path": path, "sha256": sha256(contents).hexdigest(),
                                          "bytes": len(contents)} for path, contents in base.items()]}

    class CollectedBridge(FakeExecutor):
        def __init__(self):
            super().__init__()
            self.original_bytes = _git_diff(tmp_path / "upgrade", base, candidate)
            self.candidate = candidate
            self.candidate_modes = {path: "100644" for path in candidate}

        async def collect_provider(self, *, intent, authority):
            assert authority is SESSION
            patch = self.original_bytes
            return {"state": "completed", "native_job_ref": self.native_ref, "artifacts": {
                "original_patch": {"tenant_id": run.tenant_id, "project_id": run.project_id,
                                   "artifact_id": "original-upgrade", "version": "1",
                                   "media_type": "text/x-diff", "sha256": sha256(patch).hexdigest(),
                                   "bytes": len(patch)}}}

        async def read_original_patch(self, *, reference, authority):
            assert authority is SESSION and reference["sha256"] == sha256(self.original_bytes).hexdigest()
            return self.original_bytes

        async def read_verified_trees(self, *, manifest, native_job_ref, original_patch, authority):
            assert manifest is run.manifest and native_job_ref == self.native_ref and authority is SESSION
            return {"base_files": base, "candidate_files": self.candidate,
                    "base_modes": {path: "100644" for path in base},
                    "candidate_modes": self.candidate_modes}

    bridge = CollectedBridge()
    adapter = TransformAdapter(bridge)
    intent = adapter._intent(run, attempt)
    bridge.receipt = {"executor_name": intent["executor_name"], "executor_uid": "executor-a",
                      "readback": {"native_id": "session-a", "attempt_token": attempt.idempotency_token,
                                   "input_hash": attempt.input_hash, "state": "completed"}}
    bridge.native_ref = (await adapter.submit(run, manifest, attempt, SESSION)).native_job_ref
    result = (await adapter.collect(run, bridge.native_ref, attempt, SESSION)).result
    assert result["scope_status"] == "passed"
    assert result["dependency_comparison"]["status"] == "passed"
    assert result["source_trees"]["changed_files"] == ["pyproject.toml", "uv.lock"]
    assert result["original_patch"]["sha256"] == sha256(bridge.original_bytes).hexdigest()
    assert result["verification_status"] == "partial" and len(bridge.launches) == 1

    bridge.candidate = versioned("2.1.0", target_hash, other="9.9.9")
    bridge.original_bytes = _git_diff(tmp_path / "unrelated", base, bridge.candidate)
    rejected = (await adapter.collect(run, bridge.native_ref, attempt, SESSION)).result
    assert rejected["scope_status"] == "passed"
    assert rejected["dependency_comparison"] == {"status": "failed"}
    assert rejected["tests_after"] == []
    assert rejected["original_patch"]["sha256"] == sha256(bridge.original_bytes).hexdigest()

    bridge.candidate = {**candidate, "src/escape.py": b"private"}
    bridge.candidate_modes = {path: "100644" for path in bridge.candidate}
    bridge.original_bytes = _git_diff(tmp_path / "outside", base, bridge.candidate)
    outside = (await adapter.collect(run, bridge.native_ref, attempt, SESSION)).result
    assert outside["scope_status"] == "failed"
    assert outside["verification_status"] == "partial" and len(bridge.launches) == 1

    bridge.candidate = candidate
    bridge.candidate_modes = {path: "100644" for path in candidate}
    bridge.original_bytes = _git_diff(tmp_path / "mismatch", base,
                                      versioned("2.1.0", target_hash, other="9.9.9"))
    mismatched = (await adapter.collect(run, bridge.native_ref, attempt, SESSION)).result
    assert mismatched["scope_status"] == "failed"
    assert mismatched["limitations"] == ["candidate_scope_or_patch_rejected"]


@pytest.mark.asyncio
async def test_retained_transform_result_before_cleanup(work_db):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from l5_services.adapters.transform.retention import normalize_transform_result, retain_transform_artifacts
    from l5_services.models import Run, ResultDetail
    from sqlalchemy import select
    from l5_services.models.evidence import AssessmentResult
    from l5_services.work_store import claim_work
    from test_tf03_attempts import NOW, accepted_poll

    native, poll_id = await accepted_poll(work_db)
    claim = await claim_work(work_db, poll_id, 1, now=NOW)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.admission = {**run.admission, "transform_settings": {
            **run.admission["transform_settings"], "test_policy_ref": {"record_id": "policy-a"}}}
        await session.commit()
    patch = b"diff --git a/src/app.py b/src/app.py\n+original\r\n"
    log = b"1 test passed\n"
    original = {"artifact_id": "provider-original", "version": "1", "sha256": sha256(patch).hexdigest(),
                "bytes": len(patch), "media_type": "text/x-diff", "native_job_ref": native,
                "provider_pins": {"interface": "synthetic"}}
    log_ref = {"artifact_id": "runner-stdout", "version": "1", "sha256": sha256(log).hexdigest(),
               "bytes": len(log)}
    empty_ref = {"artifact_id": "runner-stderr", "version": "1", "sha256": sha256(b"").hexdigest(),
                 "bytes": 0}
    result = {"original_patch": original, "scope_status": "passed",
              "dependency_comparison": {"status": "not_applicable"},
              "source_trees": {"base_tree": "a" * 40, "candidate_tree": "b" * 40,
                               "changed_files": ["src/app.py"]},
              "tests_before": [{"status": "passed", "receipt": {"stdout": log_ref, "stderr": empty_ref}}],
              "tests_after": [{"status": "failed", "receipt": {"stdout": log_ref, "stderr": empty_ref}}]}

    class DurableStore:
        def __init__(self):
            self.objects = {}
            self.corrupt = False

        async def put(self, *, tenant_id, project_id, run_id, name, contents, media_type, authority):
            assert authority is claim and (tenant_id, project_id, run_id) == ("tenant-a", "project-a", "run-a")
            reference = {"tenant_id": tenant_id, "project_id": project_id,
                         "artifact_id": f"retained-{run_id}-{name}", "version": "1",
                         "sha256": sha256(contents).hexdigest(), "bytes": len(contents),
                         "media_type": media_type, "expires_at": "2027-01-01T00:00:00Z"}
            self.objects[reference["artifact_id"]] = contents
            return reference

        async def read(self, *, reference, authority):
            assert authority is claim
            contents = self.objects[reference["artifact_id"]]
            return b"corrupted" if self.corrupt else contents

    async def read_patch(*, reference, authority):
        assert reference is original and authority is claim
        return patch

    async def read_log(*, reference, authority):
        assert authority is claim
        return log if reference["artifact_id"] == "runner-stdout" else b""

    store = DurableStore()
    source = SimpleNamespace(tenant_id="tenant-a", project_id="project-a", run_id="run-a")
    retained = await retain_transform_artifacts(source, result, SimpleNamespace(read_original_patch=read_patch),
                                                SimpleNamespace(read_log=read_log), store, claim, now=NOW)
    assert set(retained) == {"original_patch", "tests_before-0-stdout", "tests_before-0-stderr",
                             "tests_after-0-stdout", "tests_after-0-stderr"}
    assert store.objects[retained["original_patch"]["artifact_id"]] == patch
    store.corrupt = True
    with pytest.raises(ValueError, match="readback"):
        await retain_transform_artifacts(source, result, SimpleNamespace(read_original_patch=read_patch),
                                         SimpleNamespace(read_log=read_log), store, claim, now=NOW)
    store.corrupt = False
    from l5_services.work_store import finish_work

    with pytest.raises(ValueError, match="native identity"):
        await finish_work(work_db, claim, state="partial", provider_state="completed", now=NOW,
                          result={**result, "original_patch": {**original, "native_job_ref": {}},
                                  "retained_artifacts": retained})
    async with work_db() as session:
        assert (await session.scalars(select(AssessmentResult))).all() == []
    assert await finish_work(work_db, claim, state="partial", provider_state="completed", now=NOW,
                             result={**result, "retained_artifacts": retained}) == "completed"
    async with work_db() as session:
        detail = (await session.scalars(select(ResultDetail))).one()
        raw = (await session.scalars(select(AssessmentResult))).one()
        from l5_services.models import Publication

        intents = (await session.scalars(select(Publication))).all()
        assert len(intents) == 2
        assert {intent.result_id for intent in intents} == {raw.result_id}
        assert {intent.destination for intent in intents} == {"product-context", "engineering-insights"}
        assert all(intent.state == "unavailable" for intent in intents)
        assert all(intent.payload["result"]["retained_artifacts"]["original_patch"] ==
                   retained["original_patch"] for intent in intents)
        assert raw.version == 1 and raw.payload["original_patch"]["sha256"] == sha256(patch).hexdigest()
        assert detail.payload["schema_version"] == "1.0"
        assert detail.payload["verification_status"] == "partial"
        assert detail.payload["retained_artifacts"]["original_patch"] == retained["original_patch"]
        assert detail.payload["cost_status"] == "unknown"
        assert normalize_transform_result(run, "another-result", 2,
                                          {**result, "retained_artifacts": retained},
                                          datetime(2026, 1, 1, tzinfo=UTC)).payload["revision"] == 2


@pytest.mark.asyncio
async def test_retained_result_readback_after_owned_cleanup(work_db):
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI, HTTPException
    from sqlalchemy import select

    from l5_services.adapters.transform.retention import retain_transform_artifacts
    from l5_services.api.results import create_results_router
    from l5_services.findings import ResultStore
    from l5_services.models import AssessmentResult, Finding, Run
    from l5_services.run_access import RunStore
    from l5_services.work_store import claim_work, finish_work
    from test_tf03_attempts import NOW, accepted_poll

    native, poll_id = await accepted_poll(work_db)
    claim = await claim_work(work_db, poll_id, 1, now=NOW)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.admission = {**run.admission, "scope": {**run.admission["scope"],
                         "owner_id": "owner-a", "relationship_revision": "1"},
                         "transform_settings": {**run.admission["transform_settings"],
                                                "test_policy_ref": {"record_id": "policy-a"}}}
        await session.commit()

    scratch = {"patch": b"diff --git a/src/app.py b/src/app.py\n+untouched\r\n",
               "log": b"failed baseline (original log)\n"}
    original = {"artifact_id": "native-patch", "version": "1", "sha256": sha256(scratch["patch"]).hexdigest(),
                "bytes": len(scratch["patch"]), "media_type": "text/x-diff", "native_job_ref": native,
                "provider_pins": {"interface": "synthetic"}}
    stdout = {"artifact_id": "native-log", "version": "1", "sha256": sha256(scratch["log"]).hexdigest(),
              "bytes": len(scratch["log"])}
    stderr = {"artifact_id": "native-empty-log", "version": "1", "sha256": sha256(b"").hexdigest(), "bytes": 0}
    result = {"original_patch": original, "scope_status": "passed",
              "dependency_comparison": {"status": "not_applicable"},
              "tests_before": [{"status": "failed", "receipt": {"stdout": stdout, "stderr": stderr}}],
              "tests_after": [{"status": "not_run", "receipt": None}],
              "limitations": ["baseline_failed"]}

    class RetainedStore:
        def __init__(self):
            self.objects = {}
            self.reads = []

        async def put(self, *, tenant_id, project_id, run_id, name, contents, media_type, authority):
            assert authority is claim
            reference = {"tenant_id": tenant_id, "project_id": project_id,
                         "artifact_id": f"retained-{run_id}-{name}", "version": "1",
                         "sha256": sha256(contents).hexdigest(), "bytes": len(contents),
                         "media_type": media_type, "expires_at": "2027-01-01T00:00:00Z"}
            self.objects[reference["artifact_id"]] = contents
            return reference

        async def read(self, *, reference, authority):
            assert authority is claim or authority == {"tenant_id": "tenant-a", "actor_id": "owner-a"}
            self.reads.append((reference["artifact_id"], authority))
            return self.objects[reference["artifact_id"]]

    async def read_patch(*, reference, authority):
        assert authority is claim and reference == original
        return scratch["patch"]

    async def read_log(*, reference, authority):
        assert authority is claim
        return scratch["log"] if reference == stdout else b""

    store = RetainedStore()
    retained = await retain_transform_artifacts(run, result,
                                                SimpleNamespace(read_original_patch=read_patch),
                                                SimpleNamespace(read_log=read_log), store, claim, now=NOW)
    assert await finish_work(work_db, claim, state="partial", provider_state="completed", now=NOW,
                             result={**result, "retained_artifacts": retained}) == "completed"
    async with work_db() as session:
        assert len((await session.scalars(select(AssessmentResult))).all()) == 1
        assert not (await session.scalars(select(Finding))).all()
    scratch.clear()
    assert retained["original_patch"]["artifact_id"] in store.objects

    class Backend:
        transform_result_reader_ready = True

        def __init__(self, session):
            self.session = session

        async def result_store(self, product_id, project_id, service_id):
            if (product_id, project_id, service_id) != ("product-a", "project-a", "transform"):
                raise HTTPException(404, detail={"code": "not_found"})
            runs = await RunStore(self.session, "tenant-a", product_id, project_id, service_id,
                                  "owner-a", "1").list()
            return ResultStore(self.session, runs, actor_id="owner-a", clock=lambda: NOW,
                               transform_artifact_store=store,
                               artifact_authority={"tenant_id": "tenant-a", "actor_id": "owner-a"})

    async def backend():
        async with work_db() as session:
            yield Backend(session)

    app = FastAPI()
    root = "/v1/products/product-a/projects/project-a/services/transform"
    app.include_router(create_results_router(backend),
                       prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        listing = await client.get(f"{root}/results")
        assert listing.status_code == 200
        detail = listing.json()["items"][0]
        assert detail["verification_status"] == "partial" and detail["schema_version"] == "1.0"
        assert detail["tests_before"][0]["status"] == "failed"
        assert detail["tests_after"][0]["status"] == "not_run"
        assert detail["retention_expires_at"] == "2027-01-01T00:00:00Z"
        assert (await client.get(f"{root}/results/{detail['result_id']}")).status_code == 200
        for name, expected in (("original_patch", store.objects[retained["original_patch"]["artifact_id"]]),
                               ("tests_before-0-stdout", b"failed baseline (original log)\n")):
            artifact_id = retained[name]["artifact_id"]
            access = await client.get(f"{root}/artifacts/{artifact_id}")
            assert access.status_code == 200
            assert access.json()["artifact"] == retained[name]
            downloaded = await client.get(access.json()["download_path"])
            assert downloaded.status_code == 200 and downloaded.content == expected
            assert downloaded.headers["x-adp-content-sha256"] == sha256(expected).hexdigest()
            assert downloaded.headers["content-type"].startswith(retained[name]["media_type"])
            assert downloaded.headers["cache-control"] == "no-store"
    assert len(store.reads) == 3 + 4


@pytest.mark.asyncio
async def test_retained_result_denies_foreign_revoked_and_corrupt_reads(work_db):
    from datetime import timedelta

    import httpx
    from fastapi import FastAPI, HTTPException

    from l5_services.api.results import create_results_router
    from l5_services.findings import ResultStore
    from l5_services.models import Run
    from l5_services.run_access import RunStore
    from l5_services.work_store import claim_work, finish_work
    from test_tf03_attempts import NOW, accepted_poll

    native, poll_id = await accepted_poll(work_db)
    claim = await claim_work(work_db, poll_id, 1, now=NOW)
    patch = b"original private patch\r\n"
    artifact_id = "retained-run-a-original_patch"
    reference = {"tenant_id": "tenant-a", "project_id": "project-a",
                 "artifact_id": artifact_id, "version": "1", "sha256": sha256(patch).hexdigest(),
                 "bytes": len(patch), "media_type": "text/x-diff",
                 "expires_at": (NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z")}
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.admission = {**run.admission, "scope": {**run.admission["scope"],
                         "owner_id": "owner-a", "relationship_revision": "1"},
                         "transform_settings": {**run.admission["transform_settings"],
                                                "test_policy_ref": {"record_id": "policy-a"}}}
        await session.commit()
    collected = {"original_patch": {"sha256": reference["sha256"], "native_job_ref": native,
                                    "provider_pins": {"interface": "synthetic"}},
                 "retained_artifacts": {"original_patch": reference}, "tests_before": [],
                 "tests_after": [], "scope_status": "inconclusive"}
    assert await finish_work(work_db, claim, state="partial", provider_state="completed", now=NOW,
                             result=collected) == "completed"

    current = {"tenant_id": "tenant-a", "actor_id": "owner-a", "now": NOW,
               "revoked": False, "sources_allowed": True, "contents": patch, "reads": 0}

    class Store:
        async def read(self, *, reference, authority):
            current["reads"] += 1
            assert authority == {"tenant_id": "tenant-a", "actor_id": current["actor_id"]}
            if current["revoked"]:
                raise PermissionError("synthetic revocation")
            return current["contents"]

    class Backend:
        transform_result_reader_ready = True

        def __init__(self, session):
            self.session = session

        async def result_store(self, product_id, project_id, service_id):
            if (product_id, project_id, service_id) != ("product-a", "project-a", "transform"):
                raise HTTPException(404, detail={"code": "not_found"})
            runs = await RunStore(self.session, current["tenant_id"], product_id, project_id,
                                  service_id, current["actor_id"], "1").list()
            if not current["sources_allowed"]:
                runs = []
            return ResultStore(self.session, runs, actor_id=current["actor_id"],
                               clock=lambda: current["now"], transform_artifact_store=Store(),
                               artifact_authority={"tenant_id": "tenant-a", "actor_id": current["actor_id"]})

    async def backend():
        async with work_db() as session:
            yield Backend(session)

    app = FastAPI()
    app.include_router(create_results_router(backend),
                       prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")
    root = "/v1/products/product-a/projects/project-a/services/transform"
    path = f"{root}/artifacts/{artifact_id}/content"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(path)).content == patch
        reads = current["reads"]
        current["actor_id"] = "foreign-actor"
        assert (await client.get(path)).status_code == 404
        assert (await client.get(f"{root}/results")).json() == {"items": []}
        assert current["reads"] == reads
        current["actor_id"] = "owner-a"
        current["tenant_id"] = "foreign-tenant"
        assert (await client.get(path)).status_code == 404
        assert current["reads"] == reads
        current["tenant_id"] = "tenant-a"
        assert (await client.get(path.replace("product-a", "foreign-product"))).status_code == 404
        assert current["reads"] == reads
        current["sources_allowed"] = False
        assert (await client.get(path)).status_code == 404
        assert (await client.get(f"{root}/artifacts/{artifact_id}")).status_code == 404
        assert current["reads"] == reads
        current["sources_allowed"] = True
        current["revoked"] = True
        assert (await client.get(path)).status_code == 404
        assert (await client.get(f"{root}/artifacts/{artifact_id}")).status_code == 404
        current["revoked"] = False
        current["contents"] = b"tampered original bytes"
        assert (await client.get(path)).status_code == 503
        current["contents"] = patch
        current["now"] = NOW + timedelta(days=2)
        reads = current["reads"]
        assert (await client.get(path)).status_code == 404
        assert current["reads"] == reads


@pytest.mark.asyncio
async def test_collection_redelivery_uses_committed_native_attempt_without_paid_resubmit(work_db):
    from datetime import timedelta

    from sqlalchemy import select

    from l5_services.adapters.transform.execution import TransformAdapter
    from l5_services.models import Run, Work
    from l5_services.models.attempt import WorkerAttempt
    from l5_services.models.evidence import ProviderObservation
    from l5_services.work_store import claim_work, finish_work
    from l5_services.workers.execution import ExecutionService
    from test_tf03_attempts import NOW, ObservationBridge, accepted_poll

    native, poll_id = await accepted_poll(work_db)
    claim = await claim_work(work_db, poll_id, 1, now=NOW)
    collect_id = await finish_work(work_db, claim, state="collecting", provider_state="completed",
                                   next_kind="collect", due_at=NOW, now=NOW)

    class Bridge(ObservationBridge):
        async def collect_provider(self, *, intent, authority):
            self.collections += 1
            assert intent["attempt_token"] == native["attempt_token"]
            if self.collections == 1:
                raise TimeoutError("lost collection acknowledgment")
            return {"native_job_ref": native, "state": "completed",
                    "artifacts": {"patch_ref": "private-original"}}

    bridge = Bridge(native)
    published = []
    operations = []

    async def authorize(run, work, operation):
        operations.append(operation)
        return object()

    async def publish(envelope, delay):
        published.append(envelope)

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge), clock=lambda: NOW)
    assert await worker.execute({"schema_version": "1.0", "work_id": collect_id, "generation": 1})
    assert len(published) == 1
    worker.clock = lambda: NOW + timedelta(seconds=60)
    assert await worker.execute(published.pop())
    async with work_db() as session:
        attempts = (await session.scalars(select(WorkerAttempt))).all()
        run = await session.get(Run, ("tenant-a", "run-a"))
        observations = (await session.scalars(select(ProviderObservation))).all()
        assert len(attempts) == 1 and attempts[0].native_job_ref == run.native_job_ref == native
        assert any(observation.payload.get("artifact_refs") == {"patch_ref": "private-original"}
                   for observation in observations)
        assert not (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).all()
    assert bridge.collections == 2 and operations == ["collect", "collect"] and not published


@pytest.mark.asyncio
@pytest.mark.parametrize(("verification_seconds", "command_count"), [(0, 1), (61, 1), (61, 2)])
async def test_recovered_collection_retains_patch_and_tests_for_isolated_tenant(
        work_db, verification_seconds, command_count):
    from datetime import timedelta

    import httpx
    from fastapi import FastAPI, HTTPException
    from sqlalchemy import select

    from l5_services.adapters.transform.execution import TransformAdapter
    from l5_services.adapters.transform.provenance import compare_source_trees
    from l5_services.api.results import create_results_router
    from l5_services.findings import ResultStore
    from l5_services.models import AssessmentResult, Run, Work
    from l5_services.models.attempt import WorkerAttempt
    from l5_services.run_access import RunStore
    from l5_services.work_store import claim_work, finish_work
    from l5_services.workers.execution import ExecutionService
    from test_tf03_attempts import NOW, ObservationBridge, accepted_poll

    base_files = {"src/app.py": b"old\n"}
    candidate_files = {"src/app.py": b"new\n"}
    source_trees = compare_source_trees(base_files, candidate_files)
    patch = (b"diff --git a/src/app.py b/src/app.py\n"
             b"--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n")
    command = {"argv": ["/oracle/check"], "cwd": ".", "environment": {"NO_NETWORK": "1"},
               "timeout_seconds": 120, "minimum_test_count": 1}
    policy = {"image_digest": "sha256:" + "b" * 64, "bundle_sha256": "c" * 64,
              "lock_sha256": "d" * 64, "oracle_version": "oracle-1", "commands": [command] * command_count}
    scratch = {"patch": patch, "stdout-before": b"before: 1 passed\n",
               "stdout-after": b"after: 1 passed\n", "stderr": b""}
    original = {"tenant_id": "tenant-a", "project_id": "project-a", "artifact_id": "native-patch",
                "version": "1", "media_type": "text/x-diff", "sha256": sha256(patch).hexdigest(),
                "bytes": len(patch)}
    current_time = NOW

    def receipt(stage):
        output = scratch[f"stdout-{stage}"]
        return {"stage": stage, "argv": command["argv"], "cwd": command["cwd"],
                "environment": command["environment"], "image_digest": policy["image_digest"],
                "source_tree": source_trees["base_tree" if stage == "before" else "candidate_tree"],
                "bundle_sha256": policy["bundle_sha256"], "lock_sha256": policy["lock_sha256"],
                "oracle_version": policy["oracle_version"],
                "started_at": "2025-12-31T23:59:57Z" if stage == "before" else "2026-01-01T00:00:01Z",
                "ended_at": "2025-12-31T23:59:58Z" if stage == "before" else "2026-01-01T00:00:02Z",
                "timed_out": False, "exit_code": 0, "test_count": 1,
                "stdout": {"artifact_id": f"stdout-{stage}", "version": "1",
                           "sha256": sha256(output).hexdigest(), "bytes": len(output)},
                "stderr": {"artifact_id": "stderr", "version": "1",
                           "sha256": sha256(b"").hexdigest(), "bytes": 0}}

    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        run.manifest = {**run.manifest, "files": [{"path": "src/app.py",
                                                   "sha256": sha256(base_files["src/app.py"]).hexdigest(),
                                                   "bytes": len(base_files["src/app.py"])}]}
        run.admission = {**run.admission, "scope": {**run.admission["scope"],
                         "owner_id": "owner-a", "relationship_revision": "1"},
                         "transform_settings": {**run.admission["transform_settings"],
                                                "test_policy_ref": {"record_id": "policy-a"}},
                         "transform_test_policy": {**policy, "test_policy_ref": {"record_id": "policy-a"}}}
        from l5_services.adapters.transform.baseline import manifest_digest, pinned_test_policy
        from l5_services.models.evidence import ProviderObservation

        _, policy_sha256 = pinned_test_policy(run.admission)
        baseline = await session.get(ProviderObservation, ("tenant-a", "baseline-fixture"))
        baseline.payload = {**baseline.payload, "manifest_sha256": manifest_digest(run.manifest),
                            "policy_sha256": policy_sha256, "source_tree": source_trees["base_tree"],
                            "checks": [{"stage": "before", "status": "passed", "receipt": receipt("before")}] * command_count}
        await session.commit()
    native, poll_id = await accepted_poll(work_db)
    claim = await claim_work(work_db, poll_id, 1, now=NOW)
    collect_id = await finish_work(work_db, claim, state="collecting", provider_state="completed",
                                   next_kind="collect", due_at=NOW, now=NOW)

    class Bridge(ObservationBridge):
        async def collect_provider(self, *, intent, authority):
            self.collections += 1
            assert intent["attempt_token"] == native["attempt_token"]
            if self.collections == 1:
                raise TimeoutError("lost collection acknowledgment")
            return {"state": "completed", "native_job_ref": native,
                    "artifacts": {"original_patch": original}}

        async def read_original_patch(self, *, reference, authority):
            assert reference["sha256"] == original["sha256"]
            assert all(native[key] == value for key, value in reference.get("native_job_ref", {}).items())
            return scratch["patch"]

        async def read_verified_trees(self, *, manifest, native_job_ref, original_patch, authority):
            assert native_job_ref == native and original_patch == original
            return {"base_files": base_files, "candidate_files": candidate_files,
                    "base_modes": {"src/app.py": "100644"},
                    "candidate_modes": {"src/app.py": "100644"}}

        async def read_test_baseline(self, *, manifest, authority):
            return [receipt("before")] * command_count

    class Runner:
        def __init__(self):
            self.calls = []

        async def run_check(self, *, stage, files, source_tree, command, image_digest,
                            bundle_sha256, oracle_version, authority, file_modes=None):
            nonlocal current_time
            assert stage == "after" and files == candidate_files
            self.calls.append(stage)
            started_at = current_time
            current_time += timedelta(seconds=verification_seconds)
            return {**receipt(stage), "started_at": started_at.isoformat(),
                    "ended_at": current_time.isoformat()}

        async def read_log(self, *, reference, authority):
            return scratch[reference["artifact_id"]]

    class Store:
        def __init__(self):
            self.objects = {}

        async def put(self, *, tenant_id, project_id, run_id, name, contents, media_type, authority):
            nonlocal current_time
            current_time += timedelta(seconds=1)
            reference = {"tenant_id": tenant_id, "project_id": project_id,
                         "artifact_id": f"retained-{run_id}-{name}", "version": "1",
                         "sha256": sha256(contents).hexdigest(), "bytes": len(contents),
                         "media_type": media_type, "expires_at": "2027-01-01T00:00:00Z"}
            self.objects[reference["artifact_id"]] = contents
            return reference

        async def read(self, *, reference, authority):
            return self.objects[reference["artifact_id"]]

    bridge = Bridge(native)
    runner = Runner()
    store = Store()
    published = []

    async def authorize(run, work, operation):
        assert operation == "collect"
        return object()

    async def publish(envelope, delay):
        published.append(envelope)

    worker = ExecutionService(work_db, None, object(), publish, authorize,
                              transform_adapter=TransformAdapter(bridge, detached_runner=runner,
                                                                 retention_store=store), clock=lambda: current_time)
    assert await worker.execute({"schema_version": "1.0", "work_id": collect_id, "generation": 1})
    async with work_db() as session:
        assert not (await session.scalars(select(AssessmentResult))).all()
    current_time = NOW + timedelta(seconds=60)
    retry_envelope = published.pop()
    assert await worker.execute(retry_envelope)
    assert not await worker.execute(retry_envelope)
    async with work_db() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        result = (await session.scalars(select(AssessmentResult))).one()
        assert result.version == 1 and result.run_id == run.run_id
        assert run.native_job_ref == native
        assert result.payload["scope_status"] == "passed"
        assert [entry["status"] for entry in result.payload["tests_before"]] == ["passed"] * command_count
        assert [entry["status"] for entry in result.payload["tests_after"]] == ["passed"] * command_count
        assert run.state == "succeeded"
        assert len((await session.scalars(select(WorkerAttempt))).all()) == 1
        assert not (await session.scalars(select(Work).where(Work.completed_at.is_(None)))).all()
    assert bridge.collections == 2 and runner.calls == ["after"] * command_count and not published
    scratch.clear()

    current = {"tenant_id": "tenant-a", "actor_id": "owner-a"}

    class Backend:
        transform_result_reader_ready = True

        def __init__(self, session):
            self.session = session

        async def result_store(self, product_id, project_id, service_id):
            if (product_id, project_id, service_id) != ("product-a", "project-a", "transform"):
                raise HTTPException(404, detail={"code": "not_found"})
            runs = await RunStore(self.session, current["tenant_id"], product_id, project_id,
                                  service_id, current["actor_id"], "1").list()
            return ResultStore(self.session, runs, actor_id=current["actor_id"], clock=lambda: NOW,
                               transform_artifact_store=store, artifact_authority=current.copy())

    async def backend():
        async with work_db() as session:
            yield Backend(session)

    app = FastAPI()
    app.include_router(create_results_router(backend),
                       prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")
    root = "/v1/products/product-a/projects/project-a/services/transform"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        visible = (await client.get(f"{root}/results/{result.result_id}")).json()
        assert visible["verification_status"] == "succeeded"
        assert visible["tests_before"][0]["status"] == "passed"
        assert visible["tests_after"][0]["status"] == "passed"
        for name, expected in (("original_patch", patch), ("tests_before-0-stdout", b"before: 1 passed\n"),
                               ("tests_after-0-stdout", b"after: 1 passed\n")):
            artifact_id = visible["retained_artifacts"][name]["artifact_id"]
            downloaded = await client.get(f"{root}/artifacts/{artifact_id}/content")
            assert downloaded.status_code == 200 and downloaded.content == expected
        current["tenant_id"] = "foreign-tenant"
        assert (await client.get(f"{root}/results/{result.result_id}")).status_code == 404
        artifact_id = visible["retained_artifacts"]["original_patch"]["artifact_id"]
        assert (await client.get(f"{root}/artifacts/{artifact_id}/content")).status_code == 404
