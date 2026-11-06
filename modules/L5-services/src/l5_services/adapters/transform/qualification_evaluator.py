"""Evaluate a published Transform result against independent native readbacks."""

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote

import httpx

from l5_services.adapters.transform.baseline import pinned_test_policy
from l5_services.adapters.transform.provenance import capture_original_patch, verify_original_patch
from l5_services.adapters.transform.qualification_driver import TransformQualificationCollector
from l5_services.adapters.transform.verification import _receipt, _submission_order_valid, _timestamp


class TransformQualificationEvaluator:
    def __init__(self, collector, results_client, detached_runner, *, clock=None, on_verified=None):
        if (not isinstance(collector, TransformQualificationCollector)
                or not isinstance(results_client, httpx.AsyncClient)
                or not callable(getattr(detached_runner, "read_log", None))):
            raise PermissionError("Transform qualification readbacks unavailable")
        self.collector = collector
        self.results_client = results_client
        self.detached_runner = detached_runner
        self.clock = clock or (lambda: datetime.now(UTC))
        self.on_verified = on_verified

    async def _get(self, path):
        response = await asyncio.wait_for(
            self.results_client.get(path, follow_redirects=False), timeout=10)
        response.raise_for_status()
        return response

    async def evaluate(self, run, attempt, result_id, revision, expected_patch_ref, authority):
        if (not isinstance(result_id, str) or not result_id
                or any(character in result_id for character in "/?#")
                or type(revision) is not int or revision < 1):
            raise ValueError("qualification requires a pinned result revision")
        policy, _ = pinned_test_policy(run.admission)
        observation = await self.collector.collect(run, attempt, expected_patch_ref, authority)
        scope = observation["scope"]
        root = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                f"{quote(scope['project_id'], safe='')}/services/transform")
        authority.assert_current()
        response = await self._get(f"{root}/results/{quote(result_id, safe='')}")
        result = response.json()
        if (not isinstance(result, dict) or result.get("schema_version") != "1.0"
                or result.get("service_id") != "transform"
                or result.get("result_id") != result_id or result.get("run_id") != run.run_id
                or result.get("revision") != revision
                or response.headers.get("etag") != f'"{revision}"'
                or result.get("scope") != scope
                or result.get("source_ref") != run.manifest["sources"]
                or result.get("configuration_revision") != run.configuration_revision
                or result.get("test_policy_ref") != policy["test_policy_ref"]
                or result.get("native_job_ref") != observation["native_job_ref"]
                or result.get("provider_attribution") != observation["provider_pins"]):
            raise ValueError("published Transform result differs from independently observed work")
        patch = result.get("original_patch")
        retained = result.get("retained_artifacts")
        trees = result.get("source_trees")
        if (not isinstance(patch, dict) or not isinstance(retained, dict)
                or not isinstance(trees, dict)
                or {key: patch.get(key) for key in expected_patch_ref} != observation["original_patch"]
                or patch.get("native_job_ref") != observation["native_job_ref"]
                or patch.get("provider_pins") != observation["provider_pins"]
                or patch.get("base_manifest_sha256") != attempt.input_hash
                or trees.get("base_tree") != observation["source_tree"]
                or not isinstance(trees.get("candidate_tree"), str)
                or len(trees["candidate_tree"]) != 40
                or not isinstance(trees.get("changed_files"), list)
                or not trees["changed_files"]):
            raise ValueError("published Transform patch or source tree differs from native observation")
        retained_patch = retained.get("original_patch")
        if (not isinstance(retained_patch, dict)
                or retained_patch.get("tenant_id") != scope["tenant_id"]
                or retained_patch.get("project_id") != scope["project_id"]
                or retained_patch.get("media_type") != "text/x-diff"
                or any(retained_patch.get(key) != expected_patch_ref[key]
                       for key in ("sha256", "bytes"))
                or not isinstance(retained_patch.get("artifact_id"), str)
                or not retained_patch["artifact_id"]
                or not isinstance(retained_patch.get("version"), str)
                or not retained_patch["version"]
                or (_timestamp(retained_patch.get("expires_at")) or datetime.min.replace(tzinfo=UTC))
                <= self.clock() or retained_patch.get("revoked_at")):
            raise ValueError("original patch has no current scoped retention")
        artifact_path = f"{root}/artifacts/{quote(retained_patch['artifact_id'], safe='')}"
        authority.assert_current()
        access = (await self._get(artifact_path)).json()
        if (not isinstance(access, dict) or access.get("artifact") != retained_patch
                or access.get("download_path") != f"{artifact_path}/content"):
            raise ValueError("retained patch access differs from the published result")
        authority.assert_current()
        contents = (await self._get(f"{artifact_path}/content")).content
        if (len(contents) != retained_patch["bytes"]
                or sha256(contents).hexdigest() != retained_patch["sha256"]):
            raise ValueError("retained patch bytes differ from provider observation")

        authority.assert_current()
        base_files = await asyncio.wait_for(self.collector.source.read_sealed_source(
            manifest=run.manifest, authority=authority), timeout=self.collector.call_seconds)
        base_modes = {item["path"]: item.get("mode", "100644") for item in run.manifest["files"]}
        original = capture_original_patch(contents, observation["native_job_ref"],
                                          observation["provider_pins"], run.manifest)
        reconstructed = await asyncio.to_thread(verify_original_patch, base_files,
                                                trees["candidate_tree"], original, base_modes)
        authority.assert_current()
        if trees != reconstructed:
            raise ValueError("published source trees differ from the retained original patch")

        blockers = []
        for stage, key in (("before", "tests_before"), ("after", "tests_after")):
            entries = result.get(key)
            if not isinstance(entries, list) or len(entries) != len(policy["commands"]):
                blockers.append(f"{stage} checks missing")
                continue
            for index, (entry, command) in enumerate(zip(entries, policy["commands"])):
                if not isinstance(entry, dict) or entry.get("stage") != stage:
                    blockers.append(f"{stage} check {index} missing")
                    continue
                authority.assert_current()
                verified = await _receipt(stage, trees["base_tree" if stage == "before"
                                                        else "candidate_tree"], command, policy,
                                          entry.get("receipt"), self.detached_runner, authority)
                if not _submission_order_valid(stage, entry.get("receipt"), attempt.started_at):
                    verified["status"] = "inconclusive"
                if verified != entry or verified["status"] != "passed":
                    blockers.append(f"{stage} check {index} unverified")
                    continue
                for stream in ("stdout", "stderr"):
                    receipt_ref = verified["receipt"][stream]
                    retained_log = retained.get(f"tests_{stage}-{index}-{stream}")
                    if (not isinstance(retained_log, dict)
                            or retained_log.get("tenant_id") != scope["tenant_id"]
                            or retained_log.get("project_id") != scope["project_id"]
                            or retained_log.get("media_type") != "text/plain"
                            or not isinstance(retained_log.get("artifact_id"), str)
                            or not retained_log["artifact_id"]
                            or not isinstance(retained_log.get("version"), str)
                            or not retained_log["version"]
                            or any(retained_log.get(field) != receipt_ref[field]
                                   for field in ("sha256", "bytes"))
                            or (_timestamp(retained_log.get("expires_at"))
                                or datetime.min.replace(tzinfo=UTC)) <= self.clock()):
                        raise ValueError("detached check log lacks matching scoped retention")
                    authority.assert_current()
                    path = f"{root}/artifacts/{quote(retained_log['artifact_id'], safe='')}"
                    access = (await self._get(path)).json()
                    if access.get("artifact") != retained_log or access.get("download_path") != f"{path}/content":
                        raise ValueError("retained check log access differs from result")
                    contents = (await self._get(f"{path}/content")).content
                    if len(contents) != receipt_ref["bytes"] or sha256(contents).hexdigest() != receipt_ref["sha256"]:
                        raise ValueError("retained check log differs from independent executor log")
        if (result.get("scope_status") != "passed"
                or not isinstance(result.get("dependency_comparison"), dict)
                or result["dependency_comparison"].get("status") not in {"passed", "not_applicable"}
                or result.get("verification_status") != "succeeded"
                or result.get("limitations") != []):
            blockers.append("published result is not independently verified")
        authority.assert_current()
        report = {
            "schema": "transform-qualification/v1", "story": "9296",
            "status": "evaluated" if not blockers else "unresolved", "live_accepted": False,
            "run_sha256": sha256(run.run_id.encode()).hexdigest(),
            "result_sha256": sha256(result_id.encode()).hexdigest(),
            "result_revision": revision, "source_tree": observation["source_tree"],
            "original_patch_sha256": observation["original_patch"]["sha256"],
            "blockers": blockers,
        }
        # Code-owned collector hook receives the actual independently verified
        # result; fixtures cannot supply a callback or a purported verdict.
        if not blockers and self.on_verified is not None:
            await self.on_verified(run, attempt, result, report)
        return report

    async def emit(self, run, attempt, result_id, revision, expected_patch_ref, authority, output):
        report = await self.evaluate(run, attempt, result_id, revision, expected_patch_ref, authority)
        path = Path(output)
        path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
        return report
