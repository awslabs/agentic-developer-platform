"""Deployment boundaries: no successful apply can stand in for live observation."""

import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]


def published_image(deploy_module, deployment, config_digest):
    """An immutable tag already carrying the manifest of a qualified artifact."""
    registry = deploy_module.registry_upload
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": registry.MANIFEST_TYPE,
            "config": {"mediaType": registry.CONFIG_TYPE, "size": 13, "digest": config_digest},
            "layers": [{"mediaType": registry.LAYER_TYPE, "size": 21, "digest": "sha256:" + "e" * 64}],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "registryId": deployment.account,
        "repositoryName": deployment.repository,
        "imageId": {"imageTag": deployment.source, "imageDigest": registry.digest(manifest.encode())},
        "imageManifest": manifest,
    }


def reuse(backend, deploy_module, config_digest=None):
    """Point the fixture at an already published tag and return its digest."""
    image = published_image(deploy_module, backend.deployment, config_digest or backend.reference["config_digest"])
    backend.published = image
    backend.image_digest = image["imageId"]["imageDigest"]
    return backend.image_digest


@pytest.fixture
def deploy_module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location("adapter_runtime_deployment", ROOT / "deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def environment(tmp_path, configuration):
    config = tmp_path / "config.json"
    config.write_text(configuration.model_dump_json())
    return {
        "ADP_TOOLS_ACCOUNT_ID": "123456789012",
        "ADP_TOOLS_AWS_REGION": "us-east-1",
        "ADP_TOOLS_ENVIRONMENT": "dev",
        "ADP_SOURCE_REVISION": "a" * 40,
        "ADP_TOOLS_DEPLOY_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment",
        "ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN": "arn:aws:iam::123456789012:role/ADP-Agent-dev-platform",
        "ADP_TOOLS_STATE_BUCKET": "example-state-bucket",
        "ADP_TOOLS_ADAPTER_NAMESPACE": "adp-tools",
        "ADP_TOOLS_EKS_CLUSTER_NAME": "example-cluster",
        "ADP_TOOLS_ADAPTER_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-tool-adapters",
        "ADP_TOOLS_ADAPTER_CONFIG_FILE": str(config),
        "ADP_TOOLS_ADAPTER_PUBLICATION_FILE": str(tmp_path / "publication.json"),
        "ADP_TOOLS_ADAPTER_EVIDENCE_FILE": str(tmp_path / "observation.json"),
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ATTEMPT": "1",
        "KUBECONFIG": "/do-not-use/ambient-kubeconfig",
    }


@pytest.fixture
def backend(deploy_module, environment, monkeypatch, tmp_path):
    deployment = deploy_module.Deployment(environment)
    reference = {
        "schema_version": "1.0",
        "source_revision": "a" * 40,
        "build_id": deployment.project + ":00000000-0000-0000-0000-000000000001",
        "source_key": f"codebuild/src/{deployment.project}/{'a' * 40}-{'0' * 32}.zip",
        "source_version": "version-1",
        "source_archive_sha256": hashlib.sha256(b"archive").hexdigest(),
        "image_digest": "sha256:" + "b" * 64,
        "artifact_key": f"codebuild/artifacts/{deployment.project}/{'a' * 40}-00000000-0000-0000-0000-000000000001.tar",
        "artifact_version": "image-version-1",
        "artifact_sha256": hashlib.sha256(b"image-archive").hexdigest(),
        "config_digest": "sha256:" + "c" * 64,
    }
    record = {
        "schema_version": "1.0",
        "source_revision": reference["source_revision"],
        "repository": deployment.repository,
        "image_digest": reference["image_digest"],
        "reused": False,
        "selfcheck_passed": True,
        "qualified_only": True,
        "artifact_key": reference["artifact_key"],
        "artifact_version": reference["artifact_version"],
        "artifact_sha256": reference["artifact_sha256"],
        "config_digest": reference["config_digest"],
    }
    record["image_digest"] = None
    build = {
        "id": reference["build_id"],
        "projectName": deployment.project,
        "buildStatus": "SUCCEEDED",
        "sourceVersion": "version-1",
        "serviceRole": f"arn:aws:iam::123456789012:role/{deployment.project}",
        "source": {
            "type": "S3",
            "location": deployment.bucket + "/" + reference["source_key"],
            "buildspec": deploy_module.BUILDSPEC,
        },
        "environment": {
            "environmentVariables": [
                {"name": name, "value": value, "type": "PLAINTEXT"}
                for name, value in {
                    "ADP_SOURCE_SHA": "a" * 40,
                    "ACCOUNT_ID": "123456789012",
                    "ADP_TOOLS_ENVIRONMENT": "dev",
                    "ADP_ADAPTER_QUALIFY_ONLY": "true",
                    "ADP_ADAPTER_ARTIFACT_BUCKET": deployment.bucket,
                }.items()
            ]
        },
        "logs": {"groupName": "/aws/codebuild/" + deployment.project, "streamName": "example-build-stream"},
    }
    state = SimpleNamespace(
        deployment=deployment,
        reference=reference,
        record=record,
        build=build,
        calls=[],
        account="123456789012",
        arn="arn:aws:sts::123456789012:assumed-role/adp-dev-trusted-deployment/run",
        checksum=base64.b64encode(bytes.fromhex(reference["source_archive_sha256"])).decode(),
        artifact_checksum=base64.b64encode(bytes.fromhex(reference["artifact_sha256"])).decode(),
        artifact_length=len(b"image-archive"),
        source="a" * 40,
        dirty=False,
        immutable=True,
        records=1,
        history=None,
        log_pages=1,
        published=None,
        tag_read=None,
        image_digest=reference["image_digest"],
        version="version-1",
        wrong_cluster=False,
        fail_observation=False,
        observed=False,
        failed=False,
    )
    Path(environment["ADP_TOOLS_ADAPTER_PUBLICATION_FILE"]).write_text(json.dumps(reference))

    def aws(*args, **_kwargs):
        state.calls.append(args)
        if args[:2] == ("sts", "get-caller-identity"):
            return {"Account": state.account, "Arn": state.arn}
        if args[:2] == ("codebuild", "batch-get-builds"):
            return {"builds": [copy.deepcopy(build)]}
        if args[:2] == ("logs", "filter-log-events"):
            # Only the whole-group read discovers history; a build stream read
            # still returns exactly that build's own publication record.
            grouped = "--log-stream-names" not in args
            items = state.history if grouped and state.history is not None else [record] * state.records
            page = {"events": [{"message": deploy_module.MARKER + json.dumps(item)} for item in items]}
            consumed = args.count("--next-token")
            if grouped and consumed + 1 < state.log_pages:
                page["nextToken"] = f"token-{consumed + 1}"
            return page
        if args[:2] == ("ecr", "batch-get-image"):
            if state.tag_read is not None:
                return state.tag_read
            if state.published is None:
                return {"failures": [{"failureCode": "ImageNotFound", "imageId": {"imageTag": deployment.source}}]}
            return {"images": [state.published]}
        if args[:2] == ("ecr", "describe-images"):
            return {"imageDetails": [{"imageDigest": state.image_digest}]}
        if args[:2] == ("ecr", "describe-repositories"):
            return {"repositories": [{"imageTagMutability": "IMMUTABLE" if state.immutable else "MUTABLE"}]}
        if args[:2] == ("s3api", "head-object"):
            if args[args.index("--key") + 1] == reference["artifact_key"]:
                return {
                    "VersionId": reference["artifact_version"],
                    "ChecksumSHA256": state.artifact_checksum,
                    "ContentLength": state.artifact_length,
                }
            return {"VersionId": state.version, "ChecksumSHA256": state.checksum}
        if args[:2] == ("s3api", "put-object"):
            reference["source_key"] = args[args.index("--key") + 1]
            build["source"]["location"] = deployment.bucket + "/" + reference["source_key"]
            return {"VersionId": state.version}
        if args[:2] == ("s3api", "get-object"):
            Path(args[-1]).write_bytes(b"image-archive")
            return {
                "VersionId": reference["artifact_version"],
                "ChecksumSHA256": state.artifact_checksum,
                "ContentLength": state.artifact_length,
            }
        if args[:2] == ("codebuild", "start-build"):
            return {"build": {"id": reference["build_id"]}}
        if args[:2] == ("codebuild", "stop-build"):
            return {}
        if args[:2] == ("eks", "describe-cluster"):
            name = "other-cluster" if state.wrong_cluster else "example-cluster"
            return {"cluster": {"arn": f"arn:aws:eks:us-east-1:123456789012:cluster/{name}", "status": "ACTIVE"}}
        raise AssertionError(args)

    def run(args, **kwargs):
        state.calls.append(args)
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return state.source
        if args[:2] == ["git", "status"]:
            return " M source.py" if state.dirty else ""
        if args[:2] == ["git", "archive"]:
            Path(args[args.index("-o") + 1]).write_bytes(b"archive")
        if args[0] == "kubectl":
            assert args[args.index("--kubeconfig") + 1] != environment["KUBECONFIG"]
            assert deployment.env["KUBECONFIG"] == str(tmp_path / "kubeconfig")
            if "apply" in args:
                manifest = json.loads(kwargs["input"])
                assert {item["kind"] for item in manifest["items"]} == {
                    "Deployment",
                    "ServiceAccount",
                    "ConfigMap",
                    "Service",
                    "PodDisruptionBudget",
                }
                assert len(manifest["items"]) == 5
                assert "--force-conflicts" not in args and "--prune" not in args
        return ""

    def observe(get, expected, metadata):
        state.observed = True
        assert expected["items"][1]["data"]["config.json"]
        if state.fail_observation:
            raise deploy_module.ObservationError("adapter_readiness_probe_failed")
        return {"observation_kind": "adapter_foundation", "execution_enabled": False}

    monkeypatch.setattr(deployment, "aws", aws)
    monkeypatch.setattr(deployment, "run", run)
    monkeypatch.setattr(deployment, "guard", lambda: None)
    monkeypatch.setattr(deploy_module, "observe", observe)
    monkeypatch.setattr(deploy_module.time, "sleep", lambda seconds: None)
    return state


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("account", "999999999999", "account_mismatch"),
        ("arn", "arn:aws:sts::123456789012:assumed-role/other/run", "deployment_role_mismatch"),
        ("source", "f" * 40, "source_mismatch"),
        ("dirty", True, "source_dirty"),
    ],
)
def test_preflight_fails_before_any_mutation(backend, deploy_module, field, value, reason):
    setattr(backend, field, value)
    with pytest.raises(deploy_module.DeploymentError, match=reason):
        backend.deployment.preflight("apply")
    assert not any(any(part in call for part in ("start-build", "put-object", "apply")) for call in backend.calls)


def test_preflight_refuses_native_owner_loss(backend, deploy_module, monkeypatch):
    def refused(_environment):
        raise deploy_module.guard_release.GuardRefused("native_release_guard_refused")

    monkeypatch.setattr(deploy_module.guard_release, "authorize", refused)
    monkeypatch.setattr(backend.deployment, "guard", deploy_module.Deployment.guard.__get__(backend.deployment))
    with pytest.raises(deploy_module.DeploymentError, match="native_release_guard_refused"):
        backend.deployment.preflight("apply")
    assert not any(any(part in call for part in ("start-build", "put-object", "apply")) for call in backend.calls)


def test_connection_role_must_share_selected_deployment_account(deploy_module, environment, monkeypatch):
    environment["ADP_TOOLS_ADAPTER_CONNECTION_ROLE_ARN"] = "arn:aws:iam::000000000000:role/other"
    deployment = deploy_module.Deployment(environment)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["env"]["AWS_MAX_ATTEMPTS"] == "1"
        assert kwargs["timeout"] == 90
        return "{}"

    monkeypatch.setattr(deploy_module, "command", run)
    with pytest.raises(deploy_module.DeploymentError, match="native_release_guard_refused"):
        deployment.aws("s3api", "put-object")
    assert not calls


def test_each_mutation_rechecks_native_owner_before_provider_call(deploy_module, environment, monkeypatch):
    deployment = deploy_module.Deployment(environment)
    calls = []
    authorized = []

    def guard(current):
        assert current is deployment.env
        authorized.append(True)
        if len(authorized) >= 2:
            raise deploy_module.guard_release.GuardRefused("native_release_guard_refused")

    def run(args, **_kwargs):
        calls.append(args)
        return "{}"

    monkeypatch.setattr(deploy_module.guard_release, "authorize", guard)
    monkeypatch.setattr(deploy_module, "command", run)
    assert deployment.aws("s3api", "put-object") == {}
    with pytest.raises(deploy_module.DeploymentError, match="native_release_guard_refused"):
        deployment.aws("codebuild", "start-build")
    assert len(calls) == 1
    assert calls[0][:3] == ["aws", "s3api", "put-object"]
    with pytest.raises(deploy_module.DeploymentError, match="native_release_guard_refused"):
        deployment.run(["kubectl", "apply", "-f", "-"])
    assert len(calls) == 1
    assert deployment.aws("codebuild", "stop-build") == {}
    assert calls[-1][:3] == ["aws", "codebuild", "stop-build"]


def test_source_upload_and_build_start_have_no_implicit_retry(deploy_module, environment, monkeypatch):
    deployment = deploy_module.Deployment(environment)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs["env"]["AWS_MAX_ATTEMPTS"]))
        return "{}"

    monkeypatch.setattr(deployment, "run", run)
    monkeypatch.setattr(deployment, "guard", lambda: None)
    deployment.aws("s3api", "put-object")
    deployment.aws("codebuild", "start-build")
    deployment.aws("s3api", "head-object")
    assert [(args[1:3], attempts) for args, attempts in calls] == [
        (["s3api", "put-object"], "1"),
        (["codebuild", "start-build"], "1"),
        (["s3api", "head-object"], "2"),
    ]


def test_daemonless_registry_cli_bounds_binary_request_and_rechecks_owner(deploy_module, environment, monkeypatch):
    deployment = deploy_module.Deployment(environment)
    calls, paths, authorizations = [], [], []

    def run(args, **_kwargs):
        calls.append(args)
        assert args[:3] == ["aws", "ecr", "upload-layer-part"]
        payload = json.loads(args[args.index("--cli-input-json") + 1])
        assert payload == {"registryId": deployment.account, "repositoryName": deployment.repository}
        path = Path(args[args.index("--layer-part-blob") + 1].removeprefix("fileb://"))
        paths.append(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_bytes() == b"private-layer-fragment"
        assert authorizations == [True]
        return '{"lastByteReceived":21}'

    monkeypatch.setattr(deployment, "run", run)
    monkeypatch.setattr(deployment, "guard", lambda: authorizations.append(True))
    assert deployment.ecr(
        "upload_layer_part",
        registryId=deployment.account,
        repositoryName=deployment.repository,
        layerPartBlob=b"private-layer-fragment",
    ) == {"lastByteReceived": 21}
    assert len(calls) == 1 and all(not path.exists() for path in paths)


def test_daemonless_registry_cli_refuses_owner_loss_or_wrong_account(deploy_module, environment, monkeypatch):
    deployment = deploy_module.Deployment(environment)
    calls = []
    monkeypatch.setattr(deployment, "run", lambda args, **kwargs: calls.append(args))

    def deny():
        raise deploy_module.DeploymentError("adapter_native_release_guard_refused")

    monkeypatch.setattr(deployment, "guard", deny)
    with pytest.raises(deploy_module.DeploymentError, match="native_release_guard_refused"):
        deployment.ecr("put_image", registryId=deployment.account, repositoryName=deployment.repository)
    with pytest.raises(deploy_module.DeploymentError, match="registry_request_invalid"):
        deployment.ecr("put_image", registryId="000000000000", repositoryName=deployment.repository)
    with pytest.raises(deploy_module.DeploymentError, match="registry_request_invalid"):
        deployment.ecr("batch_delete_image", registryId=deployment.account, repositoryName=deployment.repository)
    assert not calls


def test_evidence_write_refuses_native_owner_loss(deploy_module, environment, monkeypatch, tmp_path):
    output = tmp_path / "evidence.json"
    environment["ADP_TOOLS_ADAPTER_EVIDENCE_FILE"] = str(output)
    deployment = deploy_module.Deployment(environment)
    monkeypatch.setattr(deploy_module, "Deployment", lambda _env: deployment)
    monkeypatch.setattr(deployment, "preflight", lambda _action: None)
    monkeypatch.setattr(deployment, "runtime", lambda _work, *, apply: {"observed": True})

    def refuse():
        raise deploy_module.DeploymentError("adapter_native_release_guard_refused")

    monkeypatch.setattr(deployment, "guard", refuse)
    monkeypatch.setattr(sys, "argv", ["deploy.py", "verify"])
    with pytest.raises(SystemExit) as error:
        deploy_module.main()
    assert error.value.code == 1
    assert not output.exists()


def test_publication_uses_versioned_qualification_then_daemonless_upload(backend, deploy_module, monkeypatch, tmp_path):
    invoked = []

    def publish(archive, **kwargs):
        invoked.append((archive, kwargs))
        assert archive.read_bytes() == b"image-archive"
        assert kwargs["archive_sha256"] == backend.reference["artifact_sha256"]
        assert kwargs["config_digest"] == backend.reference["config_digest"]
        assert kwargs["source_revision"] == backend.deployment.source
        assert kwargs["account_id"] == backend.deployment.account
        assert kwargs["ecr"] == backend.deployment.ecr
        kwargs["guard"]()
        return {
            "source_revision": backend.deployment.source,
            "repository": backend.deployment.repository,
            "archive_sha256": backend.reference["artifact_sha256"],
            "config_digest": backend.reference["config_digest"],
            "image_digest": backend.reference["image_digest"],
        }

    monkeypatch.setattr(deploy_module.registry_upload, "publish_archive", publish)
    result = backend.deployment.publish(tmp_path)
    assert len(invoked) == 1 and result["image_digest"] == backend.reference["image_digest"]
    assert result["artifact_version"] == backend.reference["artifact_version"]
    start = next(call for call in backend.calls if call[:2] == ("codebuild", "start-build"))
    assert start[start.index("--source-version") + 1] == backend.reference["source_version"]
    assert any(call[:2] == ("s3api", "get-object") for call in backend.calls)
    # An unpublished tag is only built after the tag itself is read.
    reads = [index for index, call in enumerate(backend.calls) if call[:2] == ("ecr", "batch-get-image")]
    builds = [index for index, call in enumerate(backend.calls) if call[:2] == ("codebuild", "start-build")]
    assert reads and builds and reads[0] < builds[0]


def reuse_report(backend, expected_digest, **changed):
    """A reuse report for the published tag, with any field overridden."""
    return {
        "source_revision": backend.deployment.source,
        "repository": backend.deployment.repository,
        "archive_sha256": backend.reference["artifact_sha256"],
        "config_digest": backend.reference["config_digest"],
        "image_digest": expected_digest,
        "reused": True,
        **changed,
    }


def test_published_source_tag_reuses_original_artifact_without_rebuilding(
    backend, deploy_module, monkeypatch, tmp_path
):
    image_digest = reuse(backend, deploy_module)
    invoked = []

    def publish(archive, **kwargs):
        invoked.append(kwargs)
        assert archive.read_bytes() == b"image-archive"
        assert kwargs["archive_sha256"] == backend.reference["artifact_sha256"]
        assert kwargs["config_digest"] == backend.reference["config_digest"]
        return reuse_report(backend, image_digest)

    monkeypatch.setattr(deploy_module.registry_upload, "publish_archive", publish)
    result = backend.deployment.publish(tmp_path)
    assert len(invoked) == 1
    assert result["image_digest"] == image_digest
    assert result["build_id"] == backend.reference["build_id"]
    assert result["artifact_version"] == backend.reference["artifact_version"]
    assert result["artifact_sha256"] == backend.reference["artifact_sha256"]
    # The immutable tag is reused from its original artifact: no rebuild, no new
    # source upload and no second qualification of the same revision.
    assert not any(call[0] == "git" and call[1] == "archive" for call in backend.calls)
    assert not any(call[:2] == ("codebuild", "start-build") for call in backend.calls)
    assert not any(call[:2] == ("s3api", "put-object") for call in backend.calls)
    assert any(call[:2] == ("ecr", "batch-get-image") for call in backend.calls)
    assert any(call[:2] == ("s3api", "get-object") for call in backend.calls)
    # History discovery reads the whole group, bounded to this exact revision.
    discovery = [
        call for call in backend.calls if call[:2] == ("logs", "filter-log-events") and "--log-stream-names" not in call
    ]
    assert discovery
    for call in discovery:
        pattern = call[call.index("--filter-pattern") + 1]
        assert "ADP_ADAPTER_PUBLICATION_V1" in pattern and backend.deployment.source in pattern


@pytest.mark.parametrize(
    "changed", [{"reused": False}, {"image_digest": "sha256:" + "f" * 64}, {"config_digest": "sha256:" + "d" * 64}]
)
def test_reuse_requires_the_registry_to_confirm_the_published_image(
    backend, deploy_module, monkeypatch, tmp_path, changed
):
    image_digest = reuse(backend, deploy_module)
    monkeypatch.setattr(
        deploy_module.registry_upload,
        "publish_archive",
        lambda archive, **kwargs: reuse_report(backend, image_digest, **changed),
    )
    with pytest.raises(deploy_module.DeploymentError, match="registry_publication_mismatch"):
        backend.deployment.publish(tmp_path)


def test_published_image_without_qualified_history_is_never_rebuilt(backend, deploy_module, monkeypatch, tmp_path):
    reuse(backend, deploy_module, config_digest="sha256:" + "d" * 64)
    invoked = []
    monkeypatch.setattr(deploy_module.registry_upload, "publish_archive", lambda *a, **kwargs: invoked.append(a))
    with pytest.raises(deploy_module.DeploymentError, match="published_image_unqualified"):
        backend.deployment.publish(tmp_path)
    assert not invoked
    assert not any(call[:2] == ("codebuild", "start-build") for call in backend.calls)


def test_multiple_qualified_builds_select_the_published_artifact(backend, deploy_module, monkeypatch, tmp_path):
    image_digest = reuse(backend, deploy_module)
    rebuilt = {
        **backend.record,
        "artifact_key": backend.record["artifact_key"].replace("000000000001", "000000000002"),
        "artifact_version": "image-version-2",
        "artifact_sha256": hashlib.sha256(b"rebuilt-archive").hexdigest(),
        "config_digest": "sha256:" + "d" * 64,
    }
    backend.history = [rebuilt, backend.record, rebuilt]
    monkeypatch.setattr(
        deploy_module.registry_upload,
        "publish_archive",
        lambda archive, **kwargs: reuse_report(backend, image_digest),
    )
    result = backend.deployment.publish(tmp_path)
    assert result["build_id"] == backend.reference["build_id"]
    assert result["artifact_sha256"] == backend.reference["artifact_sha256"]
    assert not any(call[:2] == ("codebuild", "start-build") for call in backend.calls)
    reads = [call for call in backend.calls if call[:2] == ("codebuild", "batch-get-builds")]
    assert reads and all(backend.reference["build_id"] in call for call in reads)


def test_ambiguous_history_for_one_published_digest_is_refused(backend, deploy_module, tmp_path):
    reuse(backend, deploy_module)
    backend.history = [backend.record, {**backend.record, "artifact_version": "image-version-2"}]
    with pytest.raises(deploy_module.DeploymentError, match="publication_history_ambiguous"):
        backend.deployment.publish(tmp_path)


@pytest.mark.parametrize("pages,records", [(25, 1), (20, 3)])
def test_unbounded_publication_history_is_refused(backend, deploy_module, tmp_path, pages, records):
    reuse(backend, deploy_module)
    backend.log_pages = pages
    backend.records = records
    with pytest.raises(deploy_module.DeploymentError, match="publication_history_limit"):
        backend.deployment.publish(tmp_path)
    assert not any(call[:2] == ("codebuild", "start-build") for call in backend.calls)


def test_lost_lease_during_reuse_refuses_download_and_publication(backend, deploy_module, monkeypatch, tmp_path):
    reuse(backend, deploy_module)
    selected = backend.deployment.original

    def discover(published):
        result = selected(published)

        def deny():
            raise deploy_module.DeploymentError("adapter_native_release_guard_refused")

        monkeypatch.setattr(backend.deployment, "guard", deny)
        return result

    monkeypatch.setattr(backend.deployment, "original", discover)
    invoked = []
    monkeypatch.setattr(deploy_module.registry_upload, "publish_archive", lambda *a, **kwargs: invoked.append(a))
    with pytest.raises(deploy_module.DeploymentError, match="native_release_guard_refused"):
        backend.deployment.publish(tmp_path)
    assert not invoked
    assert not any(call[:2] == ("s3api", "get-object") for call in backend.calls)


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("replaced_manifest_digest", "published_image_invalid"),
        ("foreign_repository", "published_image_invalid"),
        ("foreign_registry", "published_image_invalid"),
        ("other_tag", "published_image_invalid"),
        ("unparsable_manifest", "published_image_invalid"),
        ("untyped_manifest", "published_image_invalid"),
        ("ambiguous_read", "published_image_read_failed"),
        ("unknown_failure", "published_image_read_failed"),
    ],
)
def test_published_tag_read_binds_exact_manifest_and_identity(backend, deploy_module, tmp_path, fault, reason):
    reuse(backend, deploy_module)
    image, registry = backend.published, deploy_module.registry_upload
    if fault == "replaced_manifest_digest":
        image["imageId"]["imageDigest"] = "sha256:" + "9" * 64
    elif fault == "foreign_repository":
        image["repositoryName"] = "other-repository"
    elif fault == "foreign_registry":
        image["registryId"] = "999999999999"
    elif fault == "other_tag":
        image["imageId"]["imageTag"] = "f" * 40
    elif fault == "unparsable_manifest":
        image["imageManifest"] = "not-json"
        image["imageId"]["imageDigest"] = registry.digest(b"not-json")
    elif fault == "untyped_manifest":
        descriptor = json.loads(image["imageManifest"])
        descriptor.pop("mediaType")
        manifest = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        image["imageManifest"] = manifest
        image["imageId"]["imageDigest"] = registry.digest(manifest.encode())
    elif fault == "ambiguous_read":
        backend.tag_read = {"images": [image, image]}
    elif fault == "unknown_failure":
        backend.tag_read = {"failures": [{"failureCode": "InvalidImageTag", "imageId": {"imageTag": "a" * 40}}]}
    with pytest.raises(deploy_module.DeploymentError, match=reason):
        backend.deployment.publish(tmp_path)
    assert not any(call[:2] == ("codebuild", "start-build") for call in backend.calls)


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("foreign_artifact_key", "publication_archive_mismatch"),
        ("mismatched_artifact_version", "publication_archive_mismatch"),
        ("mismatched_artifact_checksum", "publication_archive_mismatch"),
        ("foreign_bucket", "build_binding_mismatch"),
        ("unversioned_source", "publication_version_invalid"),
        ("unreadable_source_checksum", "source_archive_mismatch"),
        ("replaced_tag_digest", "publication_digest_mismatch"),
        ("mutable", "immutable_repository_required"),
    ],
)
def test_reused_publication_must_bind_original_provenance(backend, deploy_module, tmp_path, fault, reason):
    reuse(backend, deploy_module)
    if fault == "foreign_artifact_key":
        backend.history = [{**backend.record, "artifact_key": "codebuild/artifacts/other/object.tar"}]
    elif fault == "mismatched_artifact_version":
        backend.history = [{**backend.record, "artifact_version": "image-version-9"}]
    elif fault == "mismatched_artifact_checksum":
        backend.artifact_checksum = "different-checksum"
    elif fault == "foreign_bucket":
        backend.build["source"]["location"] = "other-bucket/" + backend.reference["source_key"]
    elif fault == "unversioned_source":
        backend.build["sourceVersion"] = "null"
    elif fault == "unreadable_source_checksum":
        backend.checksum = "not-base64"
    elif fault == "replaced_tag_digest":
        backend.image_digest = "sha256:" + "9" * 64
    elif fault == "mutable":
        backend.immutable = False
    with pytest.raises(deploy_module.DeploymentError, match=reason):
        backend.deployment.publish(tmp_path)
    assert not any(call[:2] == ("codebuild", "start-build") for call in backend.calls)


def test_unversioned_source_never_starts_qualification(backend, deploy_module, tmp_path):
    backend.version = "null"
    with pytest.raises(deploy_module.DeploymentError, match="source_versioning_required"):
        backend.deployment.publish(tmp_path)
    assert not any(call[:2] == ("codebuild", "start-build") for call in backend.calls)


def test_failed_qualification_is_stopped_before_download(backend, deploy_module, tmp_path):
    backend.build["buildStatus"] = "FAILED"
    with pytest.raises(deploy_module.DeploymentError, match="qualification_build_failed"):
        backend.deployment.publish(tmp_path)
    assert ("codebuild", "stop-build", "--id", backend.reference["build_id"]) in backend.calls
    assert not any(call[:2] == ("s3api", "get-object") for call in backend.calls)


def test_owner_loss_after_qualification_refuses_download_and_publication(backend, deploy_module, monkeypatch, tmp_path):
    original = backend.deployment.publication

    def qualify(reference):
        result = original(reference)

        def deny():
            raise deploy_module.DeploymentError("adapter_native_release_guard_refused")

        monkeypatch.setattr(backend.deployment, "guard", deny)
        return result

    monkeypatch.setattr(backend.deployment, "publication", qualify)
    with pytest.raises(deploy_module.DeploymentError, match="native_release_guard_refused"):
        backend.deployment.publish(tmp_path)
    assert not any(call[:2] == ("s3api", "get-object") for call in backend.calls)


def test_mismatched_download_refuses_registry_before_helper(backend, deploy_module, monkeypatch, tmp_path):
    original = backend.deployment.publication

    def qualify(reference):
        result = original(reference)
        backend.artifact_checksum = "different-checksum"
        return result

    monkeypatch.setattr(backend.deployment, "publication", qualify)
    invoked = []
    monkeypatch.setattr(deploy_module.registry_upload, "publish_archive", lambda *args, **kwargs: invoked.append(args))
    with pytest.raises(deploy_module.DeploymentError, match="publication_archive_mismatch"):
        backend.deployment.publish(tmp_path)
    assert not invoked


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("immutable_image_conflict", "adapter_registry_immutable_image_conflict"),
        ("config_digest_mismatch", "adapter_registry_config_digest_mismatch"),
        ("authority_refused", "adapter_registry_authority_refused"),
        ("request_failed", "adapter_registry_publication_refused"),
        ("archive_or_response_invalid", "adapter_registry_publication_refused"),
    ],
)
def test_definite_registry_refusal_keeps_its_precise_sanitized_code(
    backend, deploy_module, monkeypatch, tmp_path, reason, expected
):
    def refuse(*args, **kwargs):
        raise deploy_module.registry_upload.RegistryUploadError(reason)

    monkeypatch.setattr(deploy_module.registry_upload, "publish_archive", refuse)
    with pytest.raises(deploy_module.DeploymentError) as error:
        backend.deployment.publish(tmp_path)
    assert str(error.value) == expected
    assert str(error.value).islower() and " " not in str(error.value)


def test_conflicting_published_tag_reports_the_immutable_conflict(backend, deploy_module, monkeypatch, tmp_path):
    """A tag already holding a different manifest must say so, read-only."""
    reuse(backend, deploy_module)

    def refuse(archive, **kwargs):
        # registry_upload compares the existing tag read-only, before PutImage.
        raise deploy_module.registry_upload.RegistryUploadError("immutable_image_conflict")

    monkeypatch.setattr(deploy_module.registry_upload, "publish_archive", refuse)
    with pytest.raises(deploy_module.DeploymentError) as error:
        backend.deployment.publish(tmp_path)
    assert str(error.value) == "adapter_registry_immutable_image_conflict"
    assert not any(call[:2] == ("codebuild", "start-build") for call in backend.calls)


def test_uncertain_publication_is_never_narrowed_to_a_definite_code(backend, deploy_module, monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise deploy_module.registry_upload.RegistryUploadError("immutable_image_conflict", publication_uncertain=True)

    monkeypatch.setattr(deploy_module.registry_upload, "publish_archive", refuse)
    with pytest.raises(deploy_module.DeploymentError) as error:
        backend.deployment.publish(tmp_path)
    assert str(error.value) == "adapter_registry_publication_uncertain"


def test_uncertain_registry_reply_never_retries_or_writes_receipt(backend, deploy_module, monkeypatch, tmp_path):
    invoked = []

    def uncertain(*args, **kwargs):
        invoked.append(args)
        raise deploy_module.registry_upload.RegistryUploadError(
            "request_failed", publication_uncertain=True, expected_image_digest=backend.reference["image_digest"]
        )

    monkeypatch.setattr(deploy_module.registry_upload, "publish_archive", uncertain)
    with pytest.raises(deploy_module.DeploymentError, match="registry_publication_uncertain"):
        backend.deployment.publish(tmp_path)
    assert len(invoked) == 1
    assert not any(call[:2] == ("ecr", "describe-images") for call in backend.calls)


def test_unpinned_qualification_reads_exact_archive_version_and_checksum(backend):
    reference = {
        key: value
        for key, value in backend.reference.items()
        if not key.startswith("artifact_") and key != "config_digest"
    }
    reference["image_digest"] = None
    observed = backend.deployment.publication(reference)
    assert observed["artifact_key"] == backend.record["artifact_key"]
    assert observed["artifact_version"] == backend.record["artifact_version"]
    assert observed["artifact_sha256"] == backend.record["artifact_sha256"]
    assert observed["config_digest"] == backend.record["config_digest"]
    assert not any(call[:2] == ("ecr", "describe-images") for call in backend.calls)
    assert any(call[:2] == ("s3api", "head-object") and observed["artifact_key"] in call for call in backend.calls)


def test_qualification_archive_contains_only_adapter_image_inputs(backend, deploy_module, tmp_path):
    paths = deploy_module.SOURCE_PATHS
    archive = subprocess.check_output(["git", "archive", "--format=zip", "HEAD", "--", *paths], cwd=ROOT.parents[2])
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        names = {name for name in source.namelist() if not name.endswith("/")}
    assert {
        "codebuild/bs-tools-adapters-build.yml",
        "modules/tools/pyproject.toml",
        "modules/tools/adp_tools/adapter_contracts.py",
        "modules/tools/adapters/Dockerfile",
        "modules/tools/adapters/Dockerfile.dockerignore",
        "modules/tools/adapters/pyproject.toml",
        "modules/tools/adapters/uv.lock",
        "modules/tools/adapters/tool_adapters/server.py",
        "modules/tools/adapters/infra/publish_image.py",
    } <= names
    assert all(
        name in paths or name.startswith(("modules/tools/adp_tools/", "modules/tools/adapters/tool_adapters/"))
        for name in names
    )
    assert not any(name.startswith(("modules/gateway/", "modules/tools/facade/")) for name in names)

    backend.deployment.qualify(tmp_path)
    archived = next(call for call in backend.calls if call[:2] == ["git", "archive"])
    assert archived[archived.index("--") + 1 :] == list(paths)


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("no_record", "publication_record_missing"),
        ("duplicate_record", "publication_record_missing"),
        ("no_selfcheck", "publication_record_mismatch"),
        ("wrong_digest", "publication_digest_mismatch"),
        ("wrong_source", "build_environment_mismatch"),
        ("build_can_publish", "build_environment_mismatch"),
        ("record_can_publish", "publication_record_mismatch"),
        ("wrong_version", "build_binding_mismatch"),
        ("wrong_buildspec", "build_binding_mismatch"),
        ("wrong_archive", "source_archive_mismatch"),
        ("wrong_artifact_key", "publication_archive_mismatch"),
        ("wrong_artifact_checksum", "publication_archive_mismatch"),
        ("wrong_artifact_version", "publication_archive_mismatch"),
        ("wrong_config_digest", "publication_archive_mismatch"),
        ("oversized_artifact", "publication_archive_mismatch"),
        ("mutable", "immutable_repository_required"),
    ],
)
def test_publication_evidence_must_bind_actual_success(backend, deploy_module, fault, reason):
    if fault == "no_record":
        backend.records = 0
    elif fault == "duplicate_record":
        backend.records = 2
    elif fault == "no_selfcheck":
        backend.record["selfcheck_passed"] = False
    elif fault == "wrong_digest":
        backend.reference["image_digest"] = "sha256:" + "c" * 64
    elif fault == "wrong_source":
        backend.build["environment"]["environmentVariables"][0]["value"] = "f" * 40
    elif fault == "build_can_publish":
        backend.build["environment"]["environmentVariables"][-1]["value"] = "false"
    elif fault == "record_can_publish":
        backend.record["qualified_only"] = False
    elif fault == "wrong_version":
        backend.build["sourceVersion"] = "different-version"
    elif fault == "wrong_buildspec":
        backend.build["source"]["buildspec"] = "foreign-buildspec"
    elif fault == "wrong_archive":
        backend.checksum = "different-checksum"
    elif fault == "wrong_artifact_key":
        backend.record["artifact_key"] = "other-object.tar"
    elif fault == "wrong_artifact_checksum":
        backend.artifact_checksum = "different-checksum"
    elif fault == "wrong_artifact_version":
        backend.record["artifact_version"] = "different-version"
    elif fault == "wrong_config_digest":
        backend.record["config_digest"] = "sha256:" + "d" * 64
    elif fault == "oversized_artifact":
        backend.artifact_length = 1_073_741_825
    elif fault == "mutable":
        backend.immutable = False
    with pytest.raises(deploy_module.DeploymentError, match=reason):
        backend.deployment.publication(backend.reference)


@pytest.mark.parametrize("apply", [False, True])
def test_runtime_applies_only_requested_resources_and_observes(backend, tmp_path, apply):
    evidence = backend.deployment.runtime(tmp_path, apply=apply)
    mutations = [call for call in backend.calls if call[0] == "kubectl" and "apply" in call]
    assert len(mutations) == int(apply)
    assert backend.observed and evidence["execution_enabled"] is False
    assert evidence["publication"]["image_digest"] == backend.reference["image_digest"]


@pytest.mark.parametrize("state", ("matching", "partial", "invalid"))
def test_restarted_adapter_apply_reconciles_existing_runtime_without_duplicate_mutation(
    backend, deploy_module, monkeypatch, tmp_path, state
):
    original_run = backend.deployment.run

    def existing_run(args, **kwargs):
        if args[0] == "kubectl" and "--ignore-not-found" in args and "deployment/tool-adapters" in args:
            if state == "invalid":
                return json.dumps({"kind": "Other"})
            # A real restarted runtime, already free of the stale handoff
            # variable, so reconciliation neither patches nor duplicates.
            live = handoff_owned_deployment()
            live["spec"]["template"]["spec"]["containers"][0]["env"].pop(1)
            return json.dumps(live)
        return original_run(args, **kwargs)

    monkeypatch.setattr(backend.deployment, "run", existing_run)
    if state == "partial":
        original_observe = deploy_module.observe
        attempts = []

        def recovering(*arguments):
            attempts.append(True)
            if len(attempts) == 1:
                raise deploy_module.ObservationError("adapter_partial_runtime")
            return original_observe(*arguments)

        monkeypatch.setattr(deploy_module, "observe", recovering)
    if state == "invalid":
        with pytest.raises(deploy_module.DeploymentError, match="existing_deployment_invalid"):
            backend.deployment.runtime(tmp_path, apply=True)
    else:
        evidence = backend.deployment.runtime(tmp_path, apply=True)
        assert evidence["publication"]["image_digest"] == backend.reference["image_digest"]
        restarted = backend.deployment.runtime(tmp_path, apply=True)
        assert restarted["publication"]["image_digest"] == backend.reference["image_digest"]
    mutations = [call for call in backend.calls if call[0] == "kubectl" and "apply" in call]
    assert len(mutations) == int(state == "partial")


def test_lost_apply_reply_restarts_from_observed_runtime_without_repeating_mutation(backend, monkeypatch, tmp_path):
    original_run = backend.deployment.run
    effects = []

    def lost_reply(args, **kwargs):
        if args[0] == "kubectl" and "--ignore-not-found" in args and "deployment/tool-adapters" in args:
            return json.dumps({"kind": "Deployment"}) if effects else ""
        if args[0] == "kubectl" and "apply" in args:
            effects.append(json.loads(kwargs["input"]))
            raise subprocess.TimeoutExpired(args, 45)
        return original_run(args, **kwargs)

    monkeypatch.setattr(backend.deployment, "run", lost_reply)
    with pytest.raises(subprocess.TimeoutExpired):
        backend.deployment.runtime(tmp_path, apply=True)

    assert len(effects) == 1
    restarted = backend.deployment.runtime(tmp_path, apply=True)
    assert restarted["publication"]["image_digest"] == backend.reference["image_digest"]
    assert len(effects) == 1


def test_successful_rollout_does_not_hide_failed_observation(backend, deploy_module, tmp_path):
    backend.fail_observation = True
    with pytest.raises(deploy_module.ObservationError, match="readiness_probe_failed"):
        backend.deployment.runtime(tmp_path, apply=True)
    assert backend.observed


def test_different_cluster_is_refused_before_kubeconfig_or_apply(backend, deploy_module, tmp_path):
    backend.wrong_cluster = True
    with pytest.raises(deploy_module.DeploymentError, match="cluster_mismatch"):
        backend.deployment.runtime(tmp_path, apply=True)
    assert not any(call[0] == "kubectl" or "update-kubeconfig" in call for call in backend.calls)


def handoff_owned_contract(resource_version="4711"):
    """The contract ConfigMap as the fenced handoff leaves it behind.

    The handoff patches only /data/config.json, so that one field can carry a
    different field manager while every other key keeps the applied owner.
    """
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "tool-adapters-contract",
            "namespace": "adp-tools",
            "resourceVersion": resource_version,
            "managedFields": [
                {"manager": "adp-tool-adapters", "operation": "Apply", "fieldsV1": {"f:data": {"f:legacy.json": {}}}},
                {"manager": "kubectl-patch", "operation": "Update", "fieldsV1": {"f:data": {"f:config.json": {}}}},
            ],
        },
        "data": {"config.json": json.dumps({"release": {"group": "tool-connectors"}}), "legacy.json": "retained"},
    }


def contract_runner(deployment, calls, *, contract, bump=False, refuse_patch=None):
    """Record the contract read, patch and apply of one runtime application."""
    original_run = deployment.run
    state = {"contract": contract}

    def run(args, **kwargs):
        if args[0] == "kubectl" and "--ignore-not-found" in args:
            if "deployment/tool-adapters" in args:
                return ""
            calls.append(("get", tuple(args)))
            current = state["contract"]
            if current and bump:
                current = copy.deepcopy(current)
                current["metadata"]["resourceVersion"] = str(int(current["metadata"]["resourceVersion"]) + 1)
                state["contract"] = current
            return json.dumps(current) if current else ""
        if args[0] == "kubectl" and "patch" in args:
            calls.append(("patch", tuple(args)))
            if refuse_patch is not None:
                raise refuse_patch
            return ""
        if args[0] == "kubectl" and "apply" in args:
            calls.append(("apply", json.loads(kwargs["input"])))
        return original_run(args, **kwargs)

    return run


def operations(patch_args):
    body = next(value for value in patch_args if value.startswith("--patch="))
    return json.loads(body.removeprefix("--patch="))


def test_handoff_owned_contract_field_is_adopted_before_replacement_apply(backend, monkeypatch, tmp_path):
    """A handoff-patched contract field must not conflict with the replacement."""
    calls = []
    monkeypatch.setattr(
        backend.deployment, "run", contract_runner(backend.deployment, calls, contract=handoff_owned_contract())
    )
    evidence = backend.deployment.runtime(tmp_path, apply=True)
    assert evidence["publication"]["image_digest"] == backend.reference["image_digest"]
    assert [kind for kind, _ in calls] == ["get", "patch", "apply"]
    patch_args, manifest = calls[1][1], calls[2][1]
    rendered = next(item for item in manifest["items"] if item["kind"] == "ConfigMap")
    assert "configmap/tool-adapters-contract" in patch_args
    assert "--type=json" in patch_args
    assert "--field-manager=adp-tool-adapters" in patch_args
    assert "--force-conflicts" not in patch_args and "--prune" not in patch_args
    assert operations(patch_args) == [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "4711"},
        {"op": "replace", "path": "/data/config.json", "value": rendered["data"]["config.json"]},
    ]


def test_contract_adoption_preserves_every_unrelated_field(backend, monkeypatch, tmp_path):
    """Only /data/config.json is rewritten; no other key is sent or forced."""
    calls = []
    contract = handoff_owned_contract()
    monkeypatch.setattr(backend.deployment, "run", contract_runner(backend.deployment, calls, contract=contract))
    backend.deployment.runtime(tmp_path, apply=True)
    patch_args = calls[1][1]
    changes = [item for item in operations(patch_args) if item["op"] != "test"]
    assert [item["path"] for item in changes] == ["/data/config.json"]
    assert changes[0]["value"] != contract["data"]["config.json"]
    assert "legacy.json" not in "".join(patch_args)
    assert "managedFields" not in "".join(patch_args)
    assert all("deployment" not in value for value in patch_args)


@pytest.mark.parametrize("present", [False, True])
def test_absent_contract_or_field_follows_ordinary_server_side_apply(backend, monkeypatch, tmp_path, present):
    contract = None
    if present:
        contract = handoff_owned_contract()
        contract["data"].pop("config.json")
    calls = []
    monkeypatch.setattr(backend.deployment, "run", contract_runner(backend.deployment, calls, contract=contract))
    backend.deployment.runtime(tmp_path, apply=True)
    assert [kind for kind, _ in calls] == ["get", "apply"]


@pytest.mark.parametrize("fault", ["foreign_kind", "foreign_name", "missing_resource_version"])
def test_unexpected_existing_contract_is_refused_before_any_mutation(
    backend, deploy_module, monkeypatch, tmp_path, fault
):
    contract = handoff_owned_contract()
    if fault == "foreign_kind":
        contract["kind"] = "Secret"
    elif fault == "foreign_name":
        contract["metadata"]["name"] = "other-contract"
    else:
        contract["metadata"].pop("resourceVersion")
    calls = []
    monkeypatch.setattr(backend.deployment, "run", contract_runner(backend.deployment, calls, contract=contract))
    with pytest.raises(deploy_module.DeploymentError, match="existing_contract_invalid"):
        backend.deployment.runtime(tmp_path, apply=True)
    assert [kind for kind, _ in calls] == ["get"]


def test_concurrent_contract_change_is_tested_against_the_read_version(backend, monkeypatch, tmp_path):
    """Each attempt tests the resourceVersion of its own immediately prior read."""
    calls = []
    monkeypatch.setattr(
        backend.deployment,
        "run",
        contract_runner(backend.deployment, calls, contract=handoff_owned_contract(), bump=True),
    )
    backend.deployment.runtime(tmp_path, apply=True)
    backend.deployment.runtime(tmp_path, apply=True)
    patches = [args for kind, args in calls if kind == "patch"]
    assert [operations(args)[0] for args in patches] == [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "4712"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "4713"},
    ]


def test_refused_contract_patch_never_forces_or_applies(backend, deploy_module, monkeypatch, tmp_path):
    """A lost concurrency test fails the stage; it is never forced or ignored."""
    calls = []
    monkeypatch.setattr(
        backend.deployment,
        "run",
        contract_runner(
            backend.deployment,
            calls,
            contract=handoff_owned_contract(),
            refuse_patch=deploy_module.DeploymentError("adapter_command_failed"),
        ),
    )
    with pytest.raises(deploy_module.DeploymentError, match="command_failed"):
        backend.deployment.runtime(tmp_path, apply=True)
    assert [kind for kind, _ in calls] == ["get", "patch"]
    assert "--force-conflicts" not in calls[1][1]


def test_contract_patch_rechecks_native_release_owner_before_mutation(deploy_module, environment, monkeypatch):
    deployment = deploy_module.Deployment(environment)
    calls = []

    def refuse(_environment, **_kwargs):
        raise deploy_module.guard_release.GuardRefused("native_release_guard_refused")

    monkeypatch.setattr(deploy_module.guard_release, "authorize", refuse)
    monkeypatch.setattr(deploy_module, "command", lambda args, **kwargs: calls.append(args) or "")
    with pytest.raises(deploy_module.DeploymentError, match="native_release_guard_refused"):
        deployment.run(["kubectl", "patch", "configmap/tool-adapters-contract", "--type=json", "--patch=[]"])
    assert not calls
    assert deployment.run(["kubectl", "get", "configmap/tool-adapters-contract", "-o", "json"]) == ""
    assert len(calls) == 1


def handoff_owned_deployment():
    """A live Deployment still carrying the handoff-owned legacy variable.

    Server-side apply merges container env by `name` and keeps entries owned by
    another field manager, so this leftover survives the replacement apply and
    the runtime verifier rejects the live spec as adapter_deployment_mismatch.
    """
    return {
        "kind": "Deployment",
        "metadata": {
            "name": "tool-adapters",
            "namespace": "adp-tools",
            "resourceVersion": "8150",
            "managedFields": [
                {"manager": "adp-tool-adapters", "operation": "Apply"},
                {"manager": "kubectl-patch", "operation": "Update"},
            ],
        },
        "spec": {
            "replicas": 2,
            "template": {
                "spec": {
                    "serviceAccountName": "tool-adapters",
                    "containers": [
                        {
                            "name": "tool-adapters",
                            "env": [
                                {"name": "ADP_TOOL_ADAPTER_CONFIG", "value": "/etc/adp-adapters/config.json"},
                                {"name": "ADP_TOOL_ADAPTER_LEGACY_ENABLED", "value": "true"},
                            ],
                        }
                    ],
                }
            },
        },
    }


def retire(state, changes):
    """Enforce the tested JSON patch against the fixture's live Deployment."""
    live = copy.deepcopy(state["live"])
    variables = live["spec"]["template"]["spec"]["containers"][0]["env"]
    for change in changes:
        if change["op"] == "test" and change["path"] == "/metadata/resourceVersion":
            assert change["value"] == live["metadata"]["resourceVersion"]
        elif change["op"] == "test":
            assert variables[int(change["path"].split("/")[-2])]["name"] == change["value"]
        else:
            assert change["op"] == "remove"
            variables.pop(int(change["path"].split("/")[-1]))
    live["metadata"]["resourceVersion"] = str(int(live["metadata"]["resourceVersion"]) + 1)
    state["live"] = live


def runtime_runner(deploy_module, deployment, state, calls, monkeypatch):
    """Serve the live Deployment, enforce its patch and observe only the result."""
    original_run = deployment.run

    def variables():
        if not state["live"]:
            return set()
        container = state["live"]["spec"]["template"]["spec"]["containers"][0]
        return {item.get("name") for item in container.get("env", [])}

    def observe(_get, expected, _metadata):
        assert expected["items"][1]["data"]["config.json"]
        if deploy_module.LEGACY_ENVIRONMENT in variables():
            raise deploy_module.ObservationError("adapter_deployment_mismatch")
        return {"observation_kind": "adapter_foundation", "execution_enabled": False}

    def run(args, **kwargs):
        if args[0] == "kubectl" and "--ignore-not-found" in args:
            if "deployment/tool-adapters" in args:
                calls.append(("get", tuple(args)))
                return json.dumps(state["live"]) if state["live"] else ""
            return ""
        if args[0] == "kubectl" and "patch" in args:
            calls.append(("patch", tuple(args)))
            retire(state, operations(args))
            return ""
        if args[0] == "kubectl" and "apply" in args:
            calls.append(("apply", json.loads(kwargs["input"])))
        return original_run(args, **kwargs)

    monkeypatch.setattr(deploy_module, "observe", observe)
    monkeypatch.setattr(deployment, "run", run)
    return state


def test_stale_handoff_environment_is_retired_before_replacement_apply(backend, deploy_module, monkeypatch, tmp_path):
    """The leftover handoff variable is removed, so the rendered spec can hold."""
    calls = []
    state = runtime_runner(deploy_module, backend.deployment, {"live": handoff_owned_deployment()}, calls, monkeypatch)
    evidence = backend.deployment.runtime(tmp_path, apply=True)
    assert evidence["observation_kind"] == "adapter_foundation"
    assert [kind for kind, _ in calls] == ["get", "get", "patch", "apply"]
    patch_args = calls[2][1]
    assert "deployment/tool-adapters" in patch_args
    assert "--type=json" in patch_args
    assert "--field-manager=adp-tool-adapters" in patch_args
    assert "--force-conflicts" not in patch_args and "--prune" not in patch_args
    assert operations(patch_args) == [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "8150"},
        {
            "op": "test",
            "path": "/spec/template/spec/containers/0/env/1/name",
            "value": "ADP_TOOL_ADAPTER_LEGACY_ENABLED",
        },
        {"op": "remove", "path": "/spec/template/spec/containers/0/env/1"},
    ]
    container = state["live"]["spec"]["template"]["spec"]["containers"][0]
    assert container["env"] == [{"name": "ADP_TOOL_ADAPTER_CONFIG", "value": "/etc/adp-adapters/config.json"}]
    assert container["name"] == "tool-adapters"
    assert state["live"]["spec"]["replicas"] == 2
    assert state["live"]["spec"]["template"]["spec"]["serviceAccountName"] == "tool-adapters"


def test_stale_environment_retirement_touches_no_other_field(backend, deploy_module, monkeypatch, tmp_path):
    """Exactly one env entry is removed; nothing else is read, sent or forced."""
    calls = []
    runtime_runner(deploy_module, backend.deployment, {"live": handoff_owned_deployment()}, calls, monkeypatch)
    backend.deployment.runtime(tmp_path, apply=True)
    patch_args = calls[2][1]
    body = "".join(patch_args)
    changes = [item for item in operations(patch_args) if item["op"] != "test"]
    assert [item["op"] for item in changes] == ["remove"]
    assert "managedFields" not in body
    assert "configmap" not in body
    assert "ADP_TOOL_ADAPTER_CONFIG" not in body
    assert "replicas" not in body


def test_live_deployment_without_the_stale_variable_is_never_patched(backend, deploy_module, monkeypatch, tmp_path):
    """Absence is the settled state: no patch, and legacy stays disabled."""
    live = handoff_owned_deployment()
    live["spec"]["template"]["spec"]["containers"][0]["env"].pop(1)
    calls = []
    runtime_runner(deploy_module, backend.deployment, {"live": live}, calls, monkeypatch)
    backend.deployment.runtime(tmp_path, apply=True)
    assert "patch" not in [kind for kind, _ in calls]


@pytest.mark.parametrize("fault", ["duplicate", "value_from", "foreign_container", "missing_resource_version"])
def test_unexpected_stale_environment_is_refused_before_any_mutation(
    backend, deploy_module, monkeypatch, tmp_path, fault
):
    """An ambiguous or unexpected live structure refuses instead of guessing."""
    live = handoff_owned_deployment()
    container = live["spec"]["template"]["spec"]["containers"][0]
    reason = "existing_deployment_invalid"
    if fault == "duplicate":
        container["env"].append({"name": "ADP_TOOL_ADAPTER_LEGACY_ENABLED", "value": "false"})
        reason = "stale_legacy_environment_ambiguous"
    elif fault == "value_from":
        container["env"][1] = {
            "name": "ADP_TOOL_ADAPTER_LEGACY_ENABLED",
            "valueFrom": {"configMapKeyRef": {"name": "other", "key": "legacy"}},
        }
        reason = "stale_legacy_environment_unexpected"
    elif fault == "foreign_container":
        container["name"] = "other-adapter"
    else:
        live["metadata"].pop("resourceVersion")
    calls = []
    runtime_runner(deploy_module, backend.deployment, {"live": live}, calls, monkeypatch)
    with pytest.raises(deploy_module.DeploymentError, match=reason):
        backend.deployment.runtime(tmp_path, apply=True)
    assert [kind for kind, _ in calls] == ["get", "get"]


def test_stale_environment_patch_rechecks_native_release_owner(deploy_module, environment, monkeypatch):
    """The Deployment patch is a live mutation, so it reproves native ownership."""
    deployment = deploy_module.Deployment(environment)
    calls = []

    def refuse(_environment, **_kwargs):
        raise deploy_module.guard_release.GuardRefused("native_release_guard_refused")

    monkeypatch.setattr(deploy_module.guard_release, "authorize", refuse)
    monkeypatch.setattr(deploy_module, "command", lambda args, **kwargs: calls.append(args) or "")
    with pytest.raises(deploy_module.DeploymentError, match="native_release_guard_refused"):
        deployment.run(["kubectl", "patch", "deployment/tool-adapters", "--type=json", "--patch=[]"])
    assert not calls


def test_release_declaring_the_variable_is_never_retired(backend, deploy_module, monkeypatch, tmp_path):
    """Only a stale entry is retired; a release-owned one refuses the stage."""
    calls = []
    runtime_runner(deploy_module, backend.deployment, {"live": handoff_owned_deployment()}, calls, monkeypatch)
    original = deploy_module.render

    def render(**kwargs):
        value = original(**kwargs)
        deployment = next(item for item in value["items"] if item["kind"] == "Deployment")
        deployment["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": deploy_module.LEGACY_ENVIRONMENT, "value": "true"}
        )
        return value

    monkeypatch.setattr(deploy_module, "render", render)
    with pytest.raises(deploy_module.DeploymentError, match="rendered_deployment_invalid"):
        backend.deployment.runtime(tmp_path, apply=True)
    assert "patch" not in [kind for kind, _ in calls]


def test_private_artifacts_do_not_overwrite_existing_evidence(deploy_module, tmp_path):
    path = tmp_path / "evidence.json"
    deploy_module.write_private(path, {"source": "first"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        deploy_module.write_private(path, {"source": "replacement"})
    assert json.loads(path.read_text()) == {"source": "first"}


def test_provider_failure_never_echoes_private_output(deploy_module, capsys):
    with pytest.raises(deploy_module.DeploymentError, match="command_failed"):
        deploy_module.command(
            [sys.executable, "-c", "import sys; print('private-provider-value'); sys.exit(1)"], env=os.environ
        )
    assert "private-provider-value" not in repr(capsys.readouterr())
