"""Read durable executor checkpoints without launching, reconciling or cleaning up."""

import base64
import json
from hashlib import sha256

from l5_services.adapters.transform.provenance import _file_tree
from l5_services.adapters.transform.dependency_oracle import compare_dependency_upgrade
from l5_services.adapters.transform.scope_oracle import check_write_scope
from l5_services.adapters.transform.qualification_source import TransformQualificationAuthority
from l5_services.workers.execution import AuthorityRevoked


def verify_candidate_semantics(run, result, base_files, candidate, candidate_modes):
    """Recompute original scope and dependency assertions from independent bytes."""
    settings = run.admission["transform_settings"]
    compared = check_write_scope(base_files, candidate,
        base_modes={item["path"]: item["mode"] for item in run.manifest["files"]},
        candidate_modes=candidate_modes, write_paths=settings["write_paths"],
        excluded_paths=settings["excluded_paths"])
    if (result.get("scope_status") != compared.pop("scope_status")
            or result.get("source_trees") != compared):
        raise ValueError("Retained candidate differs from independently evaluated write scope")
    goal = run.input["transform_input"]["goal"]
    expected = {"status": "not_applicable"}
    if goal["kind"] == "dependency_upgrade":
        expected = {"status": "passed", **compare_dependency_upgrade(
            base_files, candidate, goal["target"], run.admission["transform_artifact_hashes"])}
    elif goal["kind"] != "refactor":
        raise ValueError("Retained Transform goal is outside original qualification")
    if result.get("dependency_comparison") != expected:
        raise ValueError("Retained dependency upgrade differs from independent source comparison")


class RetainedTransformObserver:
    def __init__(self, artifacts):
        self.artifacts = artifacts

    async def _checkpoint(self, intent, authority):
        if (not isinstance(authority, TransformQualificationAuthority)
                or any(intent[key] != authority.scope[key] for key in ("tenant_id", "product_id", "project_id"))
                or intent["run_id"] != authority.source_request["operation_id"]):
            raise AuthorityRevoked("Retained provider observation differs from qualification scope")
        await authority.revalidate()
        checkpoint = await self.artifacts.read_checkpoint(name=intent["executor_name"], authority=authority)
        if not isinstance(checkpoint, dict):
            raise ValueError("Retained provider checkpoint unavailable")
        receipt, cleanup = checkpoint["observation"], checkpoint["cleanup"]
        owner = sha256(f"{intent['tenant_id']}:{intent['product_id']}:{intent['project_id']}:"
                       f"{intent['run_id']}:{intent['attempt_token']}".encode()).hexdigest()[:40]
        if (any(receipt.get(key) != intent[key] for key in (
                "executor_name", "tenant_id", "product_id", "project_id", "run_id", "attempt_token",
                "input_hash", "executor_binding_ref", "image_digest", "binary_sha256", "definition_revision"))
                or not receipt.get("executor_uid") or receipt["executor_uid"] != cleanup.get("uid")
                or cleanup.get("name") != intent["executor_name"]
                or cleanup.get("labels") != {"adp.dev/transform-work": owner}
                or checkpoint["termination"].get("exitCode") != 0
                or receipt["readback"].get("state") != "completed"
                or receipt["readback"].get("attempt_token") != intent["attempt_token"]
                or receipt["readback"].get("input_hash") != intent["input_hash"]):
            raise ValueError("Retained provider checkpoint is not a matching completed job")
        patch = base64.b64decode(receipt["patch"], validate=True)
        reference = checkpoint["original_patch"]
        if len(patch) != reference["bytes"] or sha256(patch).hexdigest() != reference["sha256"]:
            raise ValueError("Retained provider patch differs from checkpoint")
        return checkpoint

    async def read_provider(self, *, intent, authority):
        checkpoint = await self._checkpoint(intent, authority)
        return {key: value for key, value in checkpoint["observation"].items()
                if key not in {"patch", "candidate_files", "candidate_modes"}}

    async def collect_provider(self, *, intent, authority):
        checkpoint = await self._checkpoint(intent, authority)
        receipt = checkpoint["observation"]
        native = {key: intent[key] for key in (
            "tenant_id", "project_id", "executor_name", "attempt_token", "input_hash")}
        native.update(executor_uid=receipt["executor_uid"], native_id=receipt["readback"]["native_id"])
        return {"native_job_ref": native, "state": "completed",
                "artifacts": {"original_patch": checkpoint["original_patch"]}}

    async def read_original_patch(self, *, reference, authority):
        await authority.revalidate()
        return await self.artifacts.read(reference=reference, authority=authority)

    async def read_log(self, *, reference, authority):
        await authority.revalidate()
        scoped = {**reference, "tenant_id": authority.scope["tenant_id"], "project_id": authority.scope["project_id"]}
        return await self.artifacts.read(reference=scoped, authority=authority)

    async def verify_checks(self, *, intent, run, result, base_files, authority):
        checkpoint = await self._checkpoint(intent, authority)
        trace = checkpoint["observation"].get("capability_trace")
        if trace is not None:
            from l5_services.adapters.transform.provider_trace import verify_provider_trace
            verify_provider_trace(trace, native_id=run.native_job_ref["native_id"],
                                  agent_minutes=run.admission["limits"]["agent_minutes"])
        checkpoints = [{"kind": "provider", "checkpoint": checkpoint}]
        observed = checkpoint["observation"]
        candidate = {name: base64.b64decode(content, validate=True)
                     for name, content in observed["candidate_files"].items()}
        verify_candidate_semantics(run, result, base_files, candidate, observed["candidate_modes"])
        policy = run.admission["transform_test_policy"]
        owner = sha256(json.dumps([authority.scope, run.run_id], sort_keys=True).encode()).hexdigest()[:40]
        for stage, files, modes, tree_key in (
                ("before", base_files, {item["path"]: item["mode"] for item in run.manifest["files"]}, "base_tree"),
                ("after", candidate, observed["candidate_modes"], "candidate_tree")):
            tree = _file_tree(files, modes)
            entries = result.get("tests_" + stage)
            if tree != result["source_trees"][tree_key] or not isinstance(entries, list) or len(entries) != len(policy["commands"]):
                raise ValueError("Retained check coverage or source tree differs")
            for command, entry in zip(policy["commands"], entries):
                payload = {"stage": stage, "source_tree": tree, "command": command,
                           "file_modes": modes, "files": {name: base64.b64encode(content).decode() for name, content in files.items()},
                           **{key: policy[key] for key in ("image_digest", "bundle_sha256", "oracle_version", "lock_sha256")}}
                raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                identity = sha256((owner + raw).encode()).hexdigest()
                name = "tf-check-" + identity[:40]
                await authority.revalidate()
                check = await self.artifacts.read_checkpoint(name=name, authority=authority)
                if (not isinstance(check, dict) or check.get("input_sha256") != identity
                        or check.get("receipt") != entry.get("receipt")
                        or not check["receipt"].get("executor_uid")
                        or check["cleanup"].get("uid") != check["receipt"]["executor_uid"]
                        or check["cleanup"].get("name") != name
                        or check["cleanup"].get("labels") != {"adp.dev/transform-check": owner}):
                    raise ValueError("Independent executor check receipt differs from published result")

                checkpoints.append({"kind": "check", "stage": stage, "checkpoint": check})
        return checkpoints
