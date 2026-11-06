"""Publish, apply or observe the adapter under the native release workflow.

Native workflow admission/lease checks precede this script. Publication and
runtime observations here are inputs to canonical receipts, never acceptance.
All target/configuration bindings are supplied by deployment-owned configuration.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from adp_tools.adapter_contracts import contract_metadata

import guard_release
import registry_upload
from k8s.render import render
from runtime_evidence import MAX_RESPONSE, ObservationError, observe
from tool_adapters.config import load_configuration

ROOT = Path(__file__).resolve().parents[3]
BUILDSPEC = "codebuild/bs-tools-adapters-build.yml"
SOURCE_PATHS = (
    BUILDSPEC,
    "modules/tools/pyproject.toml",
    "modules/tools/adp_tools",
    "modules/tools/adapters/Dockerfile",
    "modules/tools/adapters/Dockerfile.dockerignore",
    "modules/tools/adapters/pyproject.toml",
    "modules/tools/adapters/uv.lock",
    "modules/tools/adapters/tool_adapters",
    "modules/tools/adapters/infra/publish_image.py",
)
MARKER = "ADP_ADAPTER_PUBLICATION_V1 "
MAX_LOG_PAGES = 20
MAX_RECORDS = 50
MAX_MANIFEST = 65536
# The one stale handoff-owned container variable this release must retire before
# it replaces the adapter. Absence means legacy routing stays disabled until an
# explicit handoff re-enables it; restored legacy routes are verified elsewhere.
LEGACY_ENVIRONMENT = "ADP_TOOL_ADAPTER_LEGACY_ENABLED"
# Definite registry refusals whose typed reason is a fixed local code, never
# provider text. They are kept verbatim so a refusal stays diagnosable; any
# other definite refusal collapses to the generic sanitized code.
REGISTRY_REFUSALS = frozenset(
    {
        "immutable_image_conflict",
        "image_tag_mismatch",
        "image_config_mismatch",
        "config_digest_mismatch",
        "archive_checksum_mismatch",
        "layer_digest_mismatch",
        "repository_mismatch",
        "authority_refused",
        "publication_intent_refused",
    }
)


class DeploymentError(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise DeploymentError("adapter_" + reason)


def command(args, *, env, input=None, timeout=60):
    # Never echo provider errors, manifest bodies or bearer-bearing kubeconfigs.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.run(
            args, cwd=ROOT, env=env, input=input, text=True, stdout=stdout, stderr=stderr, timeout=timeout, check=False
        )
        require(result.returncode == 0, "command_failed")
        stdout.seek(0)
        content = stdout.read(MAX_RESPONSE + 1)
        require(len(content) <= MAX_RESPONSE, "command_response_limit")
        return content.decode()


def write_private(path, value):
    # Evidence files are new private artifacts; do not overwrite a prior result.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def read_private(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(65537)
    require(len(raw) <= 65536, "input_limit")
    value = json.loads(raw)
    require(isinstance(value, dict), "input_invalid")
    return value


class Deployment:
    def __init__(self, env):
        self.env = {**env, "AWS_PAGER": "", "AWS_MAX_ATTEMPTS": "2"}
        self.account = env.get("ADP_TOOLS_ACCOUNT_ID", "")
        self.region = env.get("ADP_TOOLS_AWS_REGION", "")
        self.environment = env.get("ADP_TOOLS_ENVIRONMENT", "")
        self.source = env.get("ADP_SOURCE_REVISION", "")
        self.role = env.get("ADP_TOOLS_DEPLOY_ROLE_ARN", "")
        self.bucket = env.get("ADP_TOOLS_STATE_BUCKET", "")
        require(
            re.fullmatch(r"[0-9]{12}", self.account)
            and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", self.region)
            and self.environment in {"dev", "prod"}
            and re.fullmatch(r"[0-9a-f]{40}", self.source),
            "inputs_invalid",
        )
        require(
            re.fullmatch(rf"arn:aws:iam::{self.account}:role/adp-[a-z0-9-]+-trusted-deployment", self.role),
            "deployment_role_invalid",
        )
        require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket), "bucket_invalid")
        self.repository = f"adp-tools-{self.environment}-adapters"
        self.project = self.repository + "-build"
        self.registry = f"{self.account}.dkr.ecr.{self.region}.amazonaws.com"

    def run(self, args, *, env=None, **kwargs):
        # A kubectl patch mutates the live contract exactly like an apply, so it
        # rechecks the native release owner under the same guard.
        if args and args[0] == "kubectl" and ("apply" in args or "patch" in args):
            self.guard()
        return command(args, env=env or self.env, **kwargs)

    def guard(self):
        try:
            guard_release.authorize(self.env)
        except guard_release.GuardRefused:
            raise DeploymentError("adapter_native_release_guard_refused") from None

    def aws(self, *args, timeout=60):
        mutation = args[:2] in {("s3api", "put-object"), ("codebuild", "start-build")}
        if mutation:
            self.guard()
        return json.loads(
            self.run(
                [
                    "aws",
                    *args,
                    "--region",
                    self.region,
                    "--output",
                    "json",
                    "--cli-connect-timeout",
                    "10",
                    "--cli-read-timeout",
                    "30",
                ],
                env={**self.env, "AWS_MAX_ATTEMPTS": "1"} if mutation else self.env,
                timeout=timeout,
            )
        )

    def ecr(self, operation, **parameters):
        require(
            operation in {
                "describe_repositories", "batch_get_image", "batch_check_layer_availability",
                "initiate_layer_upload", "upload_layer_part", "complete_layer_upload", "put_image",
            }
            and parameters.get("registryId") == self.account,
            "registry_request_invalid",
        )
        parameters = dict(parameters)
        part = parameters.pop("layerPartBlob", None)
        require(
            (operation == "upload_layer_part") == (type(part) is bytes and 0 < len(part) <= 20 * 1024**2),
            "registry_part_invalid",
        )
        with tempfile.TemporaryDirectory(prefix="adp-adapter-registry-part-") as directory:
            args = [
                "aws", "ecr", operation.replace("_", "-"), "--cli-input-json",
                json.dumps(parameters, separators=(",", ":")), "--region", self.region, "--output", "json",
                "--cli-connect-timeout", "10", "--cli-read-timeout", "30",
            ]
            if part is not None:
                path = Path(directory) / "layer-part"
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(part)
                args.extend(["--layer-part-blob", f"fileb://{path}"])
            self.guard()
            return json.loads(self.run(args, env={**self.env, "AWS_MAX_ATTEMPTS": "1"}, timeout=90))

    def preflight(self, action):
        identity = self.aws("sts", "get-caller-identity")
        require(identity.get("Account") == self.account, "account_mismatch")
        require(
            identity.get("Arn", "").startswith(
                f"arn:aws:sts::{self.account}:assumed-role/{self.role.rsplit('/', 1)[1]}/"
            ),
            "deployment_role_mismatch",
        )
        require(self.run(["git", "rev-parse", "HEAD"]).strip() == self.source, "source_mismatch")
        # The workflow additionally proves OIDC/native authority. These local
        # checks are guardrails, not a substitute for that server-side decision.
        if action != "verify":
            require(
                self.env.get("GITHUB_ACTIONS") == "true"
                and self.env.get("GITHUB_REF") == "refs/heads/main"
                and self.env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                and self.env.get("GITHUB_RUN_ATTEMPT") == "1",
                "protected_workflow_required",
            )
            self.run(["git", "merge-base", "--is-ancestor", self.source, "refs/remotes/origin/main"])
        require(
            not self.run(
                [
                    "git",
                    "status",
                    "--porcelain",
                    "--untracked-files=no",
                    "--",
                    "modules/tools/adapters",
                    "modules/tools/adp_tools",
                    "codebuild/bs-tools-adapters-build.yml",
                ]
            ).strip(),
            "source_dirty",
        )
        self.guard()

    def build(self, build_id):
        require(re.fullmatch(re.escape(self.project) + r":[0-9a-f-]{36}", build_id), "build_id_invalid")
        values = self.aws("codebuild", "batch-get-builds", "--ids", build_id).get("builds", [])
        require(
            len(values) == 1 and values[0].get("id") == build_id and values[0].get("projectName") == self.project,
            "build_identity_mismatch",
        )
        return values[0]

    def publication_records(self, group, *, stream=None, pattern='"ADP_ADAPTER_PUBLICATION_V1"'):
        """Read ADP_ADAPTER_PUBLICATION_V1 records under a bounded page/record cap.

        Returns the records and any unconsumed pagination token so each caller
        keeps its own precise refusal for an exhausted or ambiguous history.
        """
        records, token = [], None
        for _ in range(MAX_LOG_PAGES):
            args = [
                "logs",
                "filter-log-events",
                "--log-group-name",
                group,
                "--filter-pattern",
                pattern,
                "--limit",
                "100",
                "--no-paginate",
            ]
            if stream is not None:
                args += ["--log-stream-names", stream]
            if token:
                args += ["--next-token", token]
            page = self.aws(*args)
            for event in page.get("events", []):
                line = event.get("message", "").strip()
                if line.startswith(MARKER):
                    require(len(line) <= 4096, "publication_record_limit")
                    require(len(records) < MAX_RECORDS, "publication_history_limit")
                    records.append(json.loads(line[len(MARKER) :]))
            token = page.get("nextToken")
            if not token:
                break
        return records, token

    def published_image(self):
        """Read the immutable image already carried by this source tag, if any.

        The source tag is immutable, so an existing image is the only publishable
        content for this revision; it is read, never rebuilt or overwritten.
        """
        result = self.aws(
            "ecr",
            "batch-get-image",
            "--repository-name",
            self.repository,
            "--image-ids",
            f"imageTag={self.source}",
            "--accepted-media-types",
            registry_upload.MANIFEST_TYPE,
        )
        images, failures = result.get("images", []), result.get("failures", [])
        if not images:
            require(
                len(failures) == 1
                and failures[0].get("failureCode") == "ImageNotFound"
                and failures[0].get("imageId") == {"imageTag": self.source},
                "published_image_read_failed",
            )
            return None
        require(len(images) == 1 and not failures, "published_image_read_failed")
        observed = images[0]
        manifest = observed.get("imageManifest")
        image_digest = observed.get("imageId", {}).get("imageDigest")
        require(
            observed.get("registryId") == self.account
            and observed.get("repositoryName") == self.repository
            and observed.get("imageId", {}).get("imageTag") == self.source
            and isinstance(manifest, str)
            and 0 < len(manifest) <= MAX_MANIFEST
            and isinstance(image_digest, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest)
            and registry_upload.digest(manifest.encode()) == image_digest,
            "published_image_invalid",
        )
        try:
            descriptor = json.loads(manifest)
        except ValueError:
            raise DeploymentError("adapter_published_image_invalid") from None
        config = descriptor.get("config") if isinstance(descriptor, dict) else None
        require(
            isinstance(descriptor, dict)
            and descriptor.get("schemaVersion") == 2
            and descriptor.get("mediaType") == registry_upload.MANIFEST_TYPE
            and isinstance(config, dict)
            and config.get("mediaType") == registry_upload.CONFIG_TYPE
            and isinstance(config.get("digest"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", config["digest"])
            and isinstance(descriptor.get("layers"), list)
            and 1 <= len(descriptor["layers"]) <= 100,
            "published_image_invalid",
        )
        return {"image_digest": image_digest, "config_digest": config["digest"]}

    def original(self, published):
        """Select the qualified build whose artifact produced the published image.

        Several builds may have qualified this revision; the exact published
        config digest identifies the one original artifact that may be reused.
        """
        # Narrow the group-wide read to this revision so unrelated releases
        # cannot exhaust the bounded history.
        records, token = self.publication_records(
            f"/aws/codebuild/{self.project}", pattern=f'"ADP_ADAPTER_PUBLICATION_V1" "{self.source}"'
        )
        require(not token, "publication_history_limit")
        selected = []
        for record in records:
            if (
                isinstance(record, dict)
                and record.get("source_revision") == self.source
                and record.get("repository") == self.repository
                and record.get("config_digest") == published["config_digest"]
            ):
                candidate = json.dumps(record, sort_keys=True, separators=(",", ":"))
                if candidate not in selected:
                    selected.append(candidate)
        require(selected, "published_image_unqualified")
        require(len(selected) == 1, "publication_history_ambiguous")
        record = json.loads(selected[0])
        prefix = f"codebuild/artifacts/{self.project}/{self.source}-"
        artifact_key = record.get("artifact_key")
        require(
            isinstance(artifact_key, str)
            and artifact_key.startswith(prefix)
            and artifact_key.endswith(".tar")
            and re.fullmatch(r"[0-9a-f-]{36}", artifact_key[len(prefix) : -len(".tar")]),
            "publication_archive_mismatch",
        )
        build_id = f"{self.project}:{artifact_key[len(prefix) : -len('.tar')]}"
        build = self.build(build_id)
        location = build.get("source", {}).get("location", "")
        version = build.get("sourceVersion")
        require(isinstance(location, str) and location.startswith(self.bucket + "/"), "build_binding_mismatch")
        require(
            isinstance(version, str) and 0 < len(version) <= 1024 and version != "null",
            "publication_version_invalid",
        )
        head = self.aws(
            "s3api",
            "head-object",
            "--bucket",
            self.bucket,
            "--key",
            location[len(self.bucket) + 1 :],
            "--version-id",
            version,
            "--checksum-mode",
            "ENABLED",
        )
        checksum = head.get("ChecksumSHA256")
        require(isinstance(checksum, str) and len(checksum) == 44, "source_archive_mismatch")
        try:
            archive_sha256 = base64.b64decode(checksum, validate=True).hex()
        except ValueError:
            raise DeploymentError("adapter_source_archive_mismatch") from None
        require(re.fullmatch(r"[0-9a-f]{64}", archive_sha256), "source_archive_mismatch")
        reference = self.publication(
            {
                "schema_version": "1.0",
                "source_revision": self.source,
                "build_id": build_id,
                "source_key": location[len(self.bucket) + 1 :],
                "source_version": version,
                "source_archive_sha256": archive_sha256,
                "image_digest": published["image_digest"],
                "artifact_key": artifact_key,
                "artifact_version": record.get("artifact_version"),
                "artifact_sha256": record.get("artifact_sha256"),
                "config_digest": record.get("config_digest"),
            }
        )
        require(
            reference["config_digest"] == published["config_digest"]
            and reference["image_digest"] == published["image_digest"],
            "publication_digest_mismatch",
        )
        return reference

    def publication(self, reference):
        archive_fields = {"artifact_key", "artifact_version", "artifact_sha256", "config_digest"}
        base_fields = {
            "schema_version", "source_revision", "build_id", "source_key", "source_version",
            "source_archive_sha256", "image_digest",
        }
        require(
            set(reference) in (base_fields, base_fields | archive_fields)
            and (reference.get("image_digest") is None or archive_fields <= reference.keys()),
            "publication_reference_invalid",
        )
        require(
            reference["schema_version"] == "1.0" and reference["source_revision"] == self.source,
            "publication_source_mismatch",
        )
        key = reference["source_key"]
        require(
            re.fullmatch(re.escape(f"codebuild/src/{self.project}/{self.source}-") + r"[0-9a-f]{32}\.zip", key),
            "publication_key_invalid",
        )
        require(
            isinstance(reference["source_version"], str)
            and 0 < len(reference["source_version"]) <= 1024
            and reference["source_version"] != "null"
            and re.fullmatch(r"[0-9a-f]{64}", reference["source_archive_sha256"]),
            "publication_version_invalid",
        )
        build = self.build(reference["build_id"])
        source, environment = build.get("source", {}), build.get("environment", {})
        require(
            build.get("buildStatus") == "SUCCEEDED"
            and source.get("type") == "S3"
            and source.get("location") == f"{self.bucket}/{key}"
            and source.get("buildspec") == BUILDSPEC
            and build.get("sourceVersion") == reference["source_version"]
            and build.get("serviceRole") == f"arn:aws:iam::{self.account}:role/{self.project}",
            "build_binding_mismatch",
        )
        values = environment.get("environmentVariables", [])
        require(len({item.get("name") for item in values}) == len(values), "build_environment_ambiguous")
        configured = {item.get("name"): (item.get("type"), item.get("value")) for item in values}
        require(
            all(
                configured.get(name) == ("PLAINTEXT", value)
                for name, value in {
                    "ADP_SOURCE_SHA": self.source,
                    "ACCOUNT_ID": self.account,
                    "ADP_TOOLS_ENVIRONMENT": self.environment,
                    "ADP_ADAPTER_QUALIFY_ONLY": "true",
                    "ADP_ADAPTER_ARTIFACT_BUCKET": self.bucket,
                }.items()
            ),
            "build_environment_mismatch",
        )
        logs = build.get("logs", {})
        require(
            logs.get("groupName") == f"/aws/codebuild/{self.project}"
            and isinstance(logs.get("streamName"), str)
            and 0 < len(logs["streamName"]) <= 512,
            "publication_logs_invalid",
        )
        records, token = self.publication_records(logs["groupName"], stream=logs["streamName"])
        require(not token and len(records) == 1, "publication_record_missing")
        record = records[0]
        require(
            set(record)
            == {
                "schema_version", "source_revision", "repository", "image_digest", "reused", "selfcheck_passed",
                "qualified_only", "artifact_key", "artifact_version", "artifact_sha256", "config_digest",
            }
            and record.get("schema_version") == "1.0"
            and record.get("source_revision") == self.source
            and record.get("repository") == self.repository
            and record.get("selfcheck_passed") is True
            and record.get("qualified_only") is True
            and record.get("reused") is False
            and record.get("image_digest") is None,
            "publication_record_mismatch",
        )
        artifact_key = f"codebuild/artifacts/{self.project}/{self.source}-{reference['build_id'].split(':', 1)[1]}.tar"
        require(
            record.get("artifact_key") == artifact_key
            and isinstance(record.get("artifact_version"), str)
            and 0 < len(record["artifact_version"]) <= 1024
            and record["artifact_version"] != "null"
            and isinstance(record.get("artifact_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", record["artifact_sha256"])
            and isinstance(record.get("config_digest"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", record["config_digest"])
            and all(reference.get(field, record[field]) == record[field] for field in archive_fields),
            "publication_archive_mismatch",
        )
        repositories = self.aws("ecr", "describe-repositories", "--repository-names", self.repository).get(
            "repositories", []
        )
        require(
            len(repositories) == 1
            and repositories[0].get("imageTagMutability") == "IMMUTABLE"
            and not repositories[0].get("imageTagMutabilityExclusionFilters"),
            "immutable_repository_required",
        )
        source_object = self.aws(
            "s3api",
            "head-object",
            "--bucket",
            self.bucket,
            "--key",
            key,
            "--version-id",
            reference["source_version"],
            "--checksum-mode",
            "ENABLED",
        )
        checksum = base64.b64encode(bytes.fromhex(reference["source_archive_sha256"])).decode()
        require(
            source_object.get("VersionId") == reference["source_version"]
            and source_object.get("ChecksumSHA256") == checksum,
            "source_archive_mismatch",
        )
        artifact_object = self.aws(
            "s3api", "head-object", "--bucket", self.bucket, "--key", artifact_key,
            "--version-id", record["artifact_version"], "--checksum-mode", "ENABLED",
        )
        require(
            artifact_object.get("VersionId") == record["artifact_version"]
            and artifact_object.get("ChecksumSHA256")
            == base64.b64encode(bytes.fromhex(record["artifact_sha256"])).decode()
            and type(artifact_object.get("ContentLength")) is int
            and 0 < artifact_object["ContentLength"] <= 1_073_741_824,
            "publication_archive_mismatch",
        )
        if reference["image_digest"] is not None:
            require(re.fullmatch(r"sha256:[0-9a-f]{64}", reference["image_digest"]), "publication_digest_invalid")
            images = self.aws(
                "ecr", "describe-images", "--repository-name", self.repository, "--image-ids", f"imageTag={self.source}"
            ).get("imageDetails", [])
            require(
                len(images) == 1 and images[0].get("imageDigest") == reference["image_digest"],
                "publication_digest_mismatch",
            )
        return {**reference, **{field: record[field] for field in archive_fields}}

    def qualify(self, work):
        """Build and qualify a new artifact for a source tag that has no image."""
        archive = work / "source.zip"
        self.run(["git", "archive", "--format=zip", "-o", str(archive), self.source, "--", *SOURCE_PATHS])
        with archive.open("rb") as stream:
            source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        key = f"codebuild/src/{self.project}/{self.source}-{uuid.uuid4().hex}.zip"
        uploaded = self.aws(
            "s3api", "put-object", "--bucket", self.bucket, "--key", key, "--body", str(archive),
            "--if-none-match", "*", "--checksum-algorithm", "SHA256", timeout=600,
        )
        version = uploaded.get("VersionId")
        require(isinstance(version, str) and version and version != "null", "source_versioning_required")
        started = self.aws(
            "codebuild", "start-build", "--project-name", self.project,
            "--source-location-override", f"{self.bucket}/{key}", "--source-type-override", "S3",
            "--source-version", version, "--environment-variables-override",
            json.dumps([{"name": "ADP_SOURCE_SHA", "value": self.source, "type": "PLAINTEXT"}]),
        )
        build_id = started.get("build", {}).get("id", "")
        finished = False
        try:
            for _ in range(130):
                self.guard()
                status = self.build(build_id).get("buildStatus")
                if status == "SUCCEEDED":
                    finished = True
                    break
                require(status == "IN_PROGRESS", "qualification_build_failed")
                time.sleep(15)
            require(finished, "qualification_build_timeout")
        finally:
            if not finished and re.fullmatch(re.escape(self.project) + r":[0-9a-f-]{36}", build_id):
                self.aws("codebuild", "stop-build", "--id", build_id)
        reference = {
            "schema_version": "1.0", "source_revision": self.source, "build_id": build_id,
            "source_key": key, "source_version": version, "source_archive_sha256": source_hash,
            "image_digest": None,
        }
        for attempt in range(6):
            try:
                reference = self.publication(reference)
                break
            except DeploymentError as error:
                if str(error) != "adapter_publication_record_missing" or attempt == 5:
                    raise
                time.sleep(5)
        return reference

    def publish(self, work):
        """Publish the one qualified artifact that belongs to this source tag.

        An already published immutable tag is reused from its original qualified
        artifact; rebuilding it would offer a conflicting image for the same tag.
        """
        published = self.published_image()
        reference = self.qualify(work) if published is None else self.original(published)
        image_archive = work / "qualified-image.tar"
        self.guard()
        downloaded = self.aws(
            "s3api", "get-object", "--bucket", self.bucket, "--key", reference["artifact_key"],
            "--version-id", reference["artifact_version"], "--checksum-mode", "ENABLED",
            str(image_archive), timeout=600,
        )
        require(
            downloaded.get("VersionId") == reference["artifact_version"]
            and downloaded.get("ChecksumSHA256")
            == base64.b64encode(bytes.fromhex(reference["artifact_sha256"])).decode()
            and type(downloaded.get("ContentLength")) is int
            and 0 < downloaded["ContentLength"] <= 1_073_741_824
            and image_archive.stat().st_size == downloaded["ContentLength"],
            "publication_archive_mismatch",
        )
        try:
            report = registry_upload.publish_archive(
                image_archive, archive_sha256=reference["artifact_sha256"],
                config_digest=reference["config_digest"], source_revision=self.source,
                account_id=self.account, region=self.region, environment=self.environment,
                ecr=self.ecr, guard=self.guard,
                intent=lambda digest: guard_release.authorize(self.env, publication_digest=digest),
                settle=lambda digest: guard_release.authorize(self.env, publication_digest=digest, settlement=True),
            )
        except registry_upload.RegistryUploadError as error:
            # An uncertain publication is never narrowed to a definite refusal.
            if error.publication_uncertain:
                raise DeploymentError("adapter_registry_publication_uncertain") from None
            typed = str(error).removeprefix("adapter_registry_")
            raise DeploymentError(
                "adapter_registry_" + typed
                if typed in REGISTRY_REFUSALS
                else "adapter_registry_publication_refused"
            ) from None
        require(
            report.get("source_revision") == self.source
            and report.get("repository") == self.repository
            and report.get("archive_sha256") == reference["artifact_sha256"]
            and report.get("config_digest") == reference["config_digest"]
            and (
                published is None
                or (
                    report.get("reused") is True
                    and report.get("image_digest") == published["image_digest"]
                )
            ),
            "registry_publication_mismatch",
        )
        self.guard()
        return self.publication({**reference, "image_digest": report["image_digest"]})

    def runtime(self, work, *, apply):
        namespace = self.env.get("ADP_TOOLS_ADAPTER_NAMESPACE", "")
        cluster = self.env.get("ADP_TOOLS_EKS_CLUSTER_NAME", "")
        role = self.env.get("ADP_TOOLS_ADAPTER_ROLE_ARN", "")
        require(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace)
            and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,99}", cluster)
            and re.fullmatch(rf"arn:aws:iam::{self.account}:role/[A-Za-z0-9_+=,.@/-]+", role),
            "runtime_inputs_invalid",
        )
        configuration = load_configuration(self.env.get("ADP_TOOLS_ADAPTER_CONFIG_FILE"))
        require(
            configuration is not None and configuration.release.source_revision == self.source,
            "configuration_source_mismatch",
        )
        reference = self.publication(read_private(self.env["ADP_TOOLS_ADAPTER_PUBLICATION_FILE"]))
        require(reference["image_digest"] == configuration.release.image_digest, "configuration_digest_mismatch")
        expected = render(
            image=f"{self.registry}/{self.repository}@{reference['image_digest']}",
            namespace=namespace,
            role_arn=role,
            configuration=configuration,
        )
        description = self.aws("eks", "describe-cluster", "--name", cluster).get("cluster", {})
        require(
            description.get("arn") == f"arn:aws:eks:{self.region}:{self.account}:cluster/{cluster}"
            and description.get("status") == "ACTIVE",
            "cluster_mismatch",
        )
        kubeconfig = str(work / "kubeconfig")
        self.env["KUBECONFIG"] = kubeconfig
        self.run(
            ["aws", "eks", "update-kubeconfig", "--region", self.region, "--name", cluster, "--kubeconfig", kubeconfig]
        )

        def kubectl(*args, timeout=45, input=None):
            return self.run(
                ["kubectl", "--kubeconfig", kubeconfig, "--request-timeout=30s", *args], timeout=timeout, input=input
            )

        def get(path):
            return json.loads(kubectl("get", "--raw", path))

        if apply:
            existing = kubectl("get", "deployment/tool-adapters", "-n", namespace,
                               "--ignore-not-found", "-o", "json")
            if existing:
                current = json.loads(existing)
                require(isinstance(current, dict) and current.get("kind") == "Deployment",
                        "existing_deployment_invalid")
                try:
                    evidence = observe(get, expected, contract_metadata())
                except ObservationError:
                    pass
                else:
                    return {
                        **evidence,
                        "account_id": self.account,
                        "region": self.region,
                        "cluster": cluster,
                        "publication": reference,
                    }
            # The fenced handoff patches /data/config.json on the contract
            # ConfigMap, so that one field can be owned by another field
            # manager and a plain server-side apply of it would be refused with
            # a conflict. Adopt exactly that field first, under this release's
            # own manager, with a resourceVersion-tested JSON patch: never
            # --force-conflicts, and no other field, key or resource is read or
            # written. A ConfigMap (or contract field) that does not exist yet
            # has no other owner and follows the ordinary apply below.
            contract = kubectl(
                "get", "configmap/tool-adapters-contract", "-n", namespace, "--ignore-not-found", "-o", "json"
            )
            if contract:
                current = json.loads(contract)
                require(isinstance(current, dict) and current.get("kind") == "ConfigMap", "existing_contract_invalid")
                metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
                data = current.get("data") if isinstance(current.get("data"), dict) else {}
                version = metadata.get("resourceVersion")
                require(metadata.get("name") == "tool-adapters-contract", "existing_contract_invalid")
                if isinstance(data.get("config.json"), str):
                    require(isinstance(version, str) and version, "existing_contract_invalid")
                    rendered = [item for item in expected["items"] if item.get("kind") == "ConfigMap"]
                    require(len(rendered) == 1, "rendered_contract_invalid")
                    kubectl(
                        "patch",
                        "configmap/tool-adapters-contract",
                        "-n",
                        namespace,
                        "--type=json",
                        "--field-manager=adp-tool-adapters",
                        "--patch="
                        + json.dumps(
                            [
                                {"op": "test", "path": "/metadata/resourceVersion", "value": version},
                                {
                                    "op": "replace",
                                    "path": "/data/config.json",
                                    "value": rendered[0]["data"]["config.json"],
                                },
                            ]
                        ),
                    )
            # A prior fenced handoff added LEGACY_ENVIRONMENT to the adapter
            # container's env list. Server-side apply merges that list by the
            # `name` key and preserves entries owned by another field manager, so
            # the rendered release never becomes the live spec and the runtime
            # verifier correctly rejects the extra variable. Retire exactly that
            # one stale entry first, under this release's own manager, with a
            # resourceVersion-tested and name-tested JSON patch: never
            # --force-conflicts, no other container, variable, field or resource
            # read or written, and an unexpected or ambiguous structure refuses
            # instead of guessing. A release that legitimately declares the
            # variable is left untouched.
            rendered_deployments = [item for item in expected["items"] if item.get("kind") == "Deployment"]
            require(len(rendered_deployments) == 1, "rendered_deployment_invalid")
            rendered_containers = rendered_deployments[0]["spec"]["template"]["spec"]["containers"]
            require(
                all(item.get("name") != LEGACY_ENVIRONMENT for item in rendered_containers[0]["env"]),
                "rendered_deployment_invalid",
            )
            live = kubectl(
                "get", "deployment/tool-adapters", "-n", namespace, "--ignore-not-found", "-o", "json"
            )
            if live:
                state = json.loads(live)
                require(
                    isinstance(state, dict) and state.get("kind") == "Deployment", "existing_deployment_invalid"
                )
                identity = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
                version = identity.get("resourceVersion")
                require(identity.get("name") == "tool-adapters", "existing_deployment_invalid")
                template = state.get("spec", {}).get("template", {})
                containers = template.get("spec", {}).get("containers") if isinstance(template, dict) else None
                require(isinstance(containers, list) and containers, "existing_deployment_invalid")
                selected = [
                    index
                    for index, item in enumerate(containers)
                    if isinstance(item, dict) and item.get("name") == "tool-adapters"
                ]
                require(len(selected) == 1, "existing_deployment_invalid")
                container = containers[selected[0]]
                variables = container.get("env", [])
                require(isinstance(variables, list), "existing_deployment_invalid")
                stale = [
                    index
                    for index, item in enumerate(variables)
                    if isinstance(item, dict) and item.get("name") == LEGACY_ENVIRONMENT
                ]
                require(len(stale) <= 1, "stale_legacy_environment_ambiguous")
                if stale:
                    require(set(variables[stale[0]]) == {"name", "value"}, "stale_legacy_environment_unexpected")
                    require(isinstance(version, str) and version, "existing_deployment_invalid")
                    entry = f"/spec/template/spec/containers/{selected[0]}/env/{stale[0]}"
                    kubectl(
                        "patch",
                        "deployment/tool-adapters",
                        "-n",
                        namespace,
                        "--type=json",
                        "--field-manager=adp-tool-adapters",
                        "--patch="
                        + json.dumps(
                            [
                                {"op": "test", "path": "/metadata/resourceVersion", "value": version},
                                {"op": "test", "path": entry + "/name", "value": LEGACY_ENVIRONMENT},
                                {"op": "remove", "path": entry},
                            ]
                        ),
                    )
            # No namespace, IAM, Secret, pruning or recursive manifest directory.
            kubectl(
                "apply",
                "--server-side",
                "--field-manager=adp-tool-adapters",
                "-n",
                namespace,
                "-f",
                "-",
                input=json.dumps(expected),
            )
            kubectl("rollout", "status", "deployment/tool-adapters", "-n", namespace, "--timeout=300s", timeout=330)
        for attempt in range(12):
            try:
                evidence = observe(get, expected, contract_metadata())
                return {
                    **evidence,
                    "account_id": self.account,
                    "region": self.region,
                    "cluster": cluster,
                    "publication": reference,
                }
            except ObservationError:
                if attempt == 11:
                    raise
                print("Waiting for stable adapter runtime observation", flush=True)
                time.sleep(5)
        raise DeploymentError("adapter_runtime_observation_failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("publish", "apply", "verify"))
    args = parser.parse_args()
    try:
        deploy = Deployment(dict(os.environ))
        output_key = (
            "ADP_TOOLS_ADAPTER_PUBLICATION_FILE" if args.action == "publish" else "ADP_TOOLS_ADAPTER_EVIDENCE_FILE"
        )
        output = Path(deploy.env[output_key])
        require(output.parent.is_dir() and not output.exists(), "new_evidence_path_required")
        deploy.preflight(args.action)
        with tempfile.TemporaryDirectory(prefix="adp-adapter-deploy-") as directory:
            work = Path(directory)
            result = (
                deploy.publish(work) if args.action == "publish" else deploy.runtime(work, apply=args.action == "apply")
            )
            deploy.guard()
            write_private(output, result)
    except (DeploymentError, ObservationError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        print("adapter_deployment_failed", file=sys.stderr)
        raise SystemExit(1) from None
    print("Adapter stage observation saved; canonical release verification is still required")


if __name__ == "__main__":
    main()
