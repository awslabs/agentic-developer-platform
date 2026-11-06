"""Bind isolated image qualification to the held native release and live readback."""

import base64
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from xml.etree import ElementTree

from deploy import Deployment, require, write_private
from foundation_contract import summary
from foundation_runtime import ADAPTER_FILES, GATEWAY_FILES, MARKER

BUILDSPEC = "codebuild/bs-tools-adapters-foundation-check.yml"


class FoundationCheck(Deployment):
    def __init__(self, env):
        super().__init__(env)
        self.adapter = Deployment(env)
        self.project = self.repository + "-foundation-check"
        self.lease_checks = 0

    def guard(self):
        super().guard()
        self.lease_checks += 1

    def source_hash(self, revision, prefix, files):
        hashes = {
            name: hashlib.sha256(self.run(["git", "show", f"{revision}:{prefix}/{name}"]).encode()).hexdigest()
            for name in files
        }
        return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()

    def readback(self, work, gateway_source):
        self.guard()
        adapter = self.adapter.runtime(work, apply=False)
        adapter.pop("observed_at", None)
        self.env["KUBECONFIG"] = self.adapter.env["KUBECONFIG"]

        def resource(path):
            return json.loads(
                self.run(
                    ["kubectl", "--kubeconfig", self.env["KUBECONFIG"], "--request-timeout=30s", "get", "--raw", path]
                )
            )

        deployment = resource("/apis/apps/v1/namespaces/adp-gateway/deployments/bedrockgateway")
        metadata, status = deployment["metadata"], deployment["status"]
        require(
            not metadata.get("deletionTimestamp") and status.get("observedGeneration") == metadata["generation"],
            "gateway_not_observed",
        )
        replicas = deployment["spec"].get("replicas", 1)
        require(
            replicas > 0 and status.get("updatedReplicas") == status.get("availableReplicas") == replicas,
            "gateway_not_ready",
        )
        containers = deployment["spec"]["template"]["spec"]["containers"]
        require(len(containers) == 1 and containers[0]["name"] == "bedrockgateway", "gateway_container_mismatch")
        image = containers[0]["image"]
        require(
            re.fullmatch(re.escape(self.registry + "/adp-gateway@sha256:") + r"[0-9a-f]{64}", image),
            "gateway_image_unpinned",
        )
        images = self.aws(
            "ecr", "describe-images", "--repository-name", "adp-gateway", "--image-ids", "imageTag=" + gateway_source
        )["imageDetails"]
        require(len(images) == 1 and images[0]["imageDigest"] == image.split("@", 1)[1], "gateway_source_mismatch")
        replicasets, pods = {}, []
        for pod in resource("/api/v1/namespaces/adp-gateway/pods")["items"]:
            owners = [owner for owner in pod["metadata"].get("ownerReferences", []) if owner.get("controller") is True]
            if pod["metadata"].get("deletionTimestamp") or len(owners) != 1 or owners[0].get("kind") != "ReplicaSet":
                continue
            owner = owners[0]
            name = owner.get("name", "")
            require(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", name), "gateway_replicaset_name_invalid")
            if name not in replicasets:
                replicasets[name] = resource("/apis/apps/v1/namespaces/adp-gateway/replicasets/" + name)["metadata"]
            replicaset = replicasets[name]
            require(
                replicaset.get("name") == name and replicaset.get("uid") == owner.get("uid"),
                "gateway_replicaset_changed",
            )
            if not replicaset.get("deletionTimestamp") and any(
                parent.get("controller") is True
                and parent.get("kind") == "Deployment"
                and parent.get("uid") == metadata["uid"]
                for parent in replicaset.get("ownerReferences", [])
            ):
                pods.append(pod)
        require(len(pods) == replicas, "gateway_pods_incomplete")
        pod_records = []
        for pod in pods:
            running = pod.get("status", {}).get("containerStatuses", [])
            require(
                len(running) == 1
                and running[0].get("ready") is True
                and bool(running[0].get("state", {}).get("running")),
                "gateway_pod_not_ready",
            )
            require(
                pod["spec"]["containers"][0]["image"] == image
                and running[0].get("imageID", "").endswith("@" + image.split("@", 1)[1]),
                "gateway_pod_image_mismatch",
            )
            pod_records.append({"uid": pod["metadata"]["uid"], "image_id": running[0]["imageID"]})
        return {
            "gateway_image": image,
            "gateway_uid": metadata["uid"],
            "gateway_generation": metadata["generation"],
            "gateway_pods": sorted(pod_records, key=lambda item: item["uid"]),
            "adapter": adapter,
        }

    def retained(self, build, work):
        """Read the bounded result record a terminal build retains for repair.

        The isolated run uploads its measurements and prints its marker even
        when qualification fails, so a failed build is diagnosable without any
        new permission. Returns None whenever that record is absent or unbound.
        """
        logs = build.get("logs", {})
        group, stream = logs.get("groupName", ""), logs.get("streamName", "")
        if group != "/aws/codebuild/" + self.project or not isinstance(stream, str) or not stream:
            return None
        page = self.aws(
            "logs",
            "filter-log-events",
            "--log-group-name",
            group,
            "--log-stream-names",
            stream,
            "--filter-pattern",
            '"ADP_FOUNDATION_RESULT_V1"',
            "--no-paginate",
        )
        records = []
        for event in page.get("events", []):
            line = event.get("message", "").strip()
            if line.startswith(MARKER) and len(line) < 4096:
                records.append(json.loads(line[len(MARKER) :]))
        if len(records) != 1:
            return None
        record = records[0]
        if not (
            isinstance(record.get("key"), str)
            and isinstance(record.get("version"), str)
            and record["version"] != "null"
            and isinstance(record.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
        ):
            return None
        path = work / "retained-result.json"
        self.aws(
            "s3api",
            "get-object",
            "--bucket",
            self.bucket,
            "--key",
            record["key"],
            "--version-id",
            record["version"],
            str(path),
        )
        if path.stat().st_size > 4 * 1024 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            return None
        report = json.loads(path.read_text())
        if not isinstance(report, dict):
            return None
        measured = {
            name: report[name]
            for name in (
                "stage_id",
                "build_id",
                "test_exit",
                "adapter_test_exit",
                "remaining_containers",
                "remaining_networks",
                "forbidden_imports",
                "adapter_statuses",
                "gateway_import",
                "adapter_import",
                "gateway_source_sha256",
                "expected_gateway_source_sha256",
                "adapter_source_sha256",
                "expected_adapter_source_sha256",
                "database_names_before",
                "database_names_after",
            )
            if name in report
        }
        measured["unmet_cases"] = self.unmet(report)
        return measured

    @staticmethod
    def unmet(report):
        """Summarize the failing generated cases without emitting their bodies."""
        unmet = []
        for field in ("junit", "adapter_junit"):
            document = report.get(field)
            if not isinstance(document, str) or not document:
                unmet.append({"document": field, "outcome": "absent"})
                continue
            if "<!DOCTYPE" in document or "<!ENTITY" in document:
                unmet.append({"document": field, "outcome": "rejected"})
                continue
            try:
                root = ElementTree.fromstring(document)
            except ElementTree.ParseError:
                unmet.append({"document": field, "outcome": "unparsable"})
                continue
            for case in root.iter("testcase"):
                for node in case:
                    if node.tag in {"failure", "error", "skipped"} and len(unmet) < 16:
                        unmet.append(
                            {
                                "document": field,
                                "case": (case.get("name") or "")[:120],
                                "outcome": node.tag,
                                "type": (node.get("type") or "")[:120],
                                "message": (node.get("message") or "")[:200],
                            }
                        )
        return unmet

    def diagnose(self, build, work):
        """Print the typed reason a terminal build refused to qualify.

        A bare refusal code hands a recoverable defect to a human; the phase
        codes and retained measurements keep the repair inside this workflow.
        """
        diagnosis = {
            "build_status": build.get("buildStatus"),
            "current_phase": build.get("currentPhase"),
            "failed_phases": [
                {
                    "phase": phase.get("phaseType"),
                    "status": phase.get("phaseStatus"),
                    "codes": sorted(
                        {
                            context.get("statusCode")
                            for context in phase.get("contexts", [])
                            if isinstance(context.get("statusCode"), str)
                        }
                    ),
                }
                for phase in build.get("phases", [])
                if isinstance(phase, dict) and phase.get("phaseStatus") not in (None, "SUCCEEDED")
            ][:10],
        }
        try:
            diagnosis["retained"] = self.retained(build, work)
        except Exception:
            # Diagnosis never widens or replaces the refusal it explains.
            diagnosis["retained"] = "unavailable"
        print("ADP_FOUNDATION_DIAGNOSIS_V1 " + json.dumps(diagnosis, sort_keys=True)[:8192], flush=True)

    def result(self, build, reference, work, expected_environment):
        require(
            build.get("buildStatus") == "SUCCEEDED"
            and build.get("serviceRole") == f"arn:aws:iam::{self.account}:role/{self.project}",
            "foundation_build_unverified",
        )
        require(
            build.get("sourceVersion") == reference["version"]
            and build.get("source", {}).get("type") == "S3"
            and build["source"].get("location") == f"{self.bucket}/{reference['key']}"
            and build["source"].get("buildspec") == BUILDSPEC,
            "foundation_source_mismatch",
        )
        variables = build.get("environment", {}).get("environmentVariables", [])
        require(len({item["name"] for item in variables}) == len(variables), "foundation_environment_duplicate")
        values = {item["name"]: item.get("value") for item in variables if item.get("type") == "PLAINTEXT"}
        require(
            all(values.get(name) == value for name, value in expected_environment.items()),
            "foundation_environment_mismatch",
        )
        source = self.aws(
            "s3api",
            "head-object",
            "--bucket",
            self.bucket,
            "--key",
            reference["key"],
            "--version-id",
            reference["version"],
            "--checksum-mode",
            "ENABLED",
        )
        require(
            source.get("VersionId") == reference["version"] and source.get("ChecksumSHA256") == reference["checksum"],
            "foundation_source_checksum",
        )
        logs = build["logs"]
        require(
            logs.get("groupName") == "/aws/codebuild/" + self.project and bool(logs.get("streamName")),
            "foundation_log_mismatch",
        )
        for attempt in range(12):
            self.guard()
            records, token = [], None
            for _ in range(20):
                args = [
                    "logs",
                    "filter-log-events",
                    "--log-group-name",
                    logs["groupName"],
                    "--log-stream-names",
                    logs["streamName"],
                    "--filter-pattern",
                    '"ADP_FOUNDATION_RESULT_V1"',
                    "--no-paginate",
                ]
                if token:
                    args += ["--next-token", token]
                page = self.aws(*args)
                for event in page.get("events", []):
                    line = event.get("message", "").strip()
                    if line.startswith(MARKER):
                        require(len(line) < 4096, "foundation_record_size")
                        records.append(json.loads(line[len(MARKER) :]))
                token = page.get("nextToken")
                if not token:
                    break
            require(not token and len(records) <= 1, "foundation_record_ambiguous")
            if records:
                break
            if attempt < 11:
                time.sleep(5)
        require(len(records) == 1, "foundation_record_missing")
        record = records[0]
        require(
            set(record) == {"key", "version", "sha256"}
            and record["key"] == f"codebuild/artifacts/{self.project}/{build['id'].split(':')[1]}/result.json"
            and isinstance(record["version"], str)
            and 0 < len(record["version"]) <= 1024
            and record["version"] != "null"
            and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]),
            "foundation_result_binding",
        )
        path = work / "qualified-result.json"
        retrieved = self.aws(
            "s3api",
            "get-object",
            "--bucket",
            self.bucket,
            "--key",
            record["key"],
            "--version-id",
            record["version"],
            "--checksum-mode",
            "ENABLED",
            str(path),
        )
        require(
            retrieved.get("VersionId") == record["version"]
            and 0 < path.stat().st_size <= 4 * 1024 * 1024
            and retrieved.get("ChecksumSHA256") == base64.b64encode(bytes.fromhex(record["sha256"])).decode()
            and hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"],
            "foundation_result_checksum",
        )
        return json.loads(path.read_text())

    def qualify(self, work):
        stage = self.env.get("ADP_QUALIFICATION_STAGE", "foundation")
        require(stage in {"foundation", "identity"}, "qualification_stage_invalid")
        self.preflight("foundation")
        gateway_source = self.env.get("ADP_TOOLS_FOUNDATION_GATEWAY_REVISION", "")
        require(
            self.environment == "dev" and re.fullmatch(r"[0-9a-f]{40}", gateway_source),
            "foundation_gateway_revision_required",
        )
        if stage == "identity":
            require(gateway_source == self.source, "identity_gateway_source_mismatch")
        self.run(["git", "merge-base", "--is-ancestor", gateway_source, "refs/remotes/origin/main"])
        before = self.readback(work, gateway_source)
        adapter_image = self.registry + "/" + self.repository + "@" + before["adapter"]["publication"]["image_digest"]
        if stage == "identity":
            from identity_contract import ADAPTER_FILES as adapter_files
            from identity_contract import GATEWAY_FILES as gateway_files
        else:
            adapter_files, gateway_files = ADAPTER_FILES, GATEWAY_FILES
        expected = {
            "ADP_SOURCE_SHA": self.source,
            "ADP_QUALIFICATION_STAGE": stage,
            "ADP_FOUNDATION_GATEWAY_REVISION": gateway_source,
            "ADP_FOUNDATION_GATEWAY_IMAGE": before["gateway_image"],
            "ADP_FOUNDATION_ADAPTER_IMAGE": adapter_image,
            "ADP_FOUNDATION_GATEWAY_HASH": self.source_hash(gateway_source, "modules/gateway", gateway_files),
            "ADP_FOUNDATION_ADAPTER_HASH": self.source_hash(self.source, "modules/tools/adapters", adapter_files),
            "ADP_FOUNDATION_ARTIFACT_BUCKET": self.bucket,
        }
        archive = work / "foundation-source.zip"
        self.run(["git", "archive", "--format=zip", "-o", str(archive), self.source])
        checksum = base64.b64encode(hashlib.sha256(archive.read_bytes()).digest()).decode()
        if stage == "identity":
            run_id = self.env["GITHUB_RUN_ID"]
            require(re.fullmatch(r"[1-9][0-9]{0,19}", run_id), "identity_run_invalid")
            key = f"codebuild/src/{self.project}/identity-{run_id}-{self.source}.zip"
        else:
            key = f"codebuild/src/{self.project}/{self.source}-{uuid.uuid4().hex}.zip"
        try:
            uploaded = self.aws(
                "s3api",
                "put-object",
                "--bucket",
                self.bucket,
                "--key",
                key,
                "--body",
                str(archive),
                "--if-none-match",
                "*",
                "--checksum-algorithm",
                "SHA256",
                "--checksum-sha256",
                checksum,
                timeout=300,
            )
        except Exception:
            if stage != "identity":
                raise
            uploaded = self.aws(
                "s3api", "head-object", "--bucket", self.bucket, "--key", key, "--checksum-mode", "ENABLED"
            )
        require(
            uploaded.get("VersionId") not in (None, "", "null") and uploaded.get("ChecksumSHA256") == checksum,
            "foundation_source_unversioned",
        )
        started = self.aws(
            "codebuild",
            "start-build",
            "--project-name",
            self.project,
            "--source-type-override",
            "S3",
            "--source-location-override",
            f"{self.bucket}/{key}",
            "--source-version",
            uploaded["VersionId"],
            "--environment-variables-override",
            json.dumps([{"name": name, "value": value, "type": "PLAINTEXT"} for name, value in expected.items()]),
            *(
                [
                    "--idempotency-token",
                    hashlib.sha256(f"identity:{run_id}:{self.source}:{uploaded['VersionId']}".encode()).hexdigest(),
                ]
                if stage == "identity"
                else []
            ),
        )
        build_id = started["build"]["id"]
        finished = False
        try:
            for _ in range(120):
                self.guard()
                build = self.build(build_id)
                if build.get("buildStatus") == "SUCCEEDED":
                    finished = True
                    break
                if build.get("buildStatus") != "IN_PROGRESS":
                    self.diagnose(build, work)
                    require(False, "foundation_build_failed")
                time.sleep(10)
            if not finished:
                # An exhausted wait is equally recoverable only when it is explained.
                self.diagnose(build, work)
            require(finished, "foundation_build_timeout")
        finally:
            if not finished and re.fullmatch(re.escape(self.project) + r":[0-9a-f-]{36}", build_id):
                self.aws("codebuild", "stop-build", "--id", build_id)
        report = self.result(
            build, {"key": key, "version": uploaded["VersionId"], "checksum": checksum}, work, expected
        )
        require(
            report.get("expected_gateway_source_sha256") == expected["ADP_FOUNDATION_GATEWAY_HASH"]
            and report.get("expected_adapter_source_sha256") == expected["ADP_FOUNDATION_ADAPTER_HASH"],
            "foundation_source_hash_mismatch",
        )
        after = self.readback(work, gateway_source)
        self.guard()
        if stage == "identity":
            from identity_contract import summary as stage_summary
        else:
            stage_summary = summary
        return stage_summary(
            report,
            source=self.source,
            gateway_source=gateway_source,
            gateway_image=before["gateway_image"],
            adapter_image=adapter_image,
            build_id=build_id,
            native={
                "before": before,
                "after": after,
                "lease_checks": self.lease_checks,
                "workflow_revision": self.env["ADP_DEFINITION_REVISION"],
                "run_id": int(self.env["GITHUB_RUN_ID"]),
                "run_attempt": int(self.env["GITHUB_RUN_ATTEMPT"]),
                "correlation": self.env.get("ADP_CORRELATION", ""),
            },
        )


def main():
    with tempfile.TemporaryDirectory(prefix="adp-foundation-release-") as name:
        report = FoundationCheck(os.environ).qualify(Path(name))
    destination = Path(os.environ["ADP_TOOLS_ADAPTER_RELEASE_DIR"])
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(destination / "atlassian-stage-summary.json", report)


if __name__ == "__main__":
    main()
