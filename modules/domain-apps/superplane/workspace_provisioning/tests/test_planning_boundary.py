import json
import os

import pytest

from workspace_provisioning.planning_boundary import (
    snapshot,
    verify_pre_init,
    archive_preparation,
)
from workspace_provisioning.terraform import (
    copy_source,
    workspace_source,
    operation_directory,
)


@pytest.fixture
def preparation(tmp_path):
    identity = ("org", "workspace", "operation")
    directory = operation_directory(tmp_path, *identity, create=True)
    copy_source(directory / "module")
    (directory / "workspace.tfvars.json").write_text("{}")
    (directory / "review").mkdir()
    (directory / ".aws/cli/cache").mkdir(parents=True)
    (directory / ".aws/cli/cache/session.db").write_bytes(b"fixture cache")
    return tmp_path, identity, directory


def test_exact_pre_init_bytes_are_preserved_under_receipt(preparation):
    root, identity, directory = preparation
    entries = snapshot(directory)
    digest = verify_pre_init(entries, workspace_source())
    receipt = {
        "reauthorization_id": "fresh-decision",
        "proof": json.dumps(
            {
                "preparation_boundary": {
                    "kind": "before-terraform-init-v1",
                    "entries": entries,
                    "sha256": digest,
                }
            }
        ),
    }
    archive_preparation(root, identity, receipt)
    (archive,) = list(directory.parent.glob(directory.name + ".recovered-*"))
    assert snapshot(archive) == entries and not directory.exists()
    assert archive.stat().st_mode & 0o777 == 0o700
    created = operation_directory(root, *identity, create=True)
    assert not list(created.iterdir())
    with pytest.raises(ValueError):
        archive_preparation(root, identity, receipt)
    assert snapshot(archive) == entries


@pytest.mark.parametrize(
    "name",
    [
        "module/.terraform/terraform.tfstate",
        "review/workspace.tfplan",
        "module/terraform.tfstate",
        "module/extra.tf",
        "unknown",
    ],
)
def test_started_or_modified_preparation_cannot_be_recovered(preparation, name):
    _, _, directory = preparation
    target = directory / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("unreviewed")
    with pytest.raises(ValueError):
        verify_pre_init(snapshot(directory), workspace_source())


def test_changed_bytes_and_symlinks_refuse_archiving(preparation):
    root, identity, directory = preparation
    entries = snapshot(directory)
    receipt = {
        "reauthorization_id": "fresh-decision",
        "proof": json.dumps(
            {
                "preparation_boundary": {
                    "kind": "before-terraform-init-v1",
                    "entries": entries,
                }
            }
        ),
    }
    (directory / "workspace.tfvars.json").write_text('{"changed":true}')
    with pytest.raises(ValueError):
        archive_preparation(root, identity, receipt)
    assert directory.exists()
    os.symlink("/tmp", directory / "escape")
    with pytest.raises(ValueError):
        snapshot(directory)


@pytest.fixture
def incomplete_init(preparation):
    from workspace_provisioning.planning_boundary import inspection_documents

    root, identity, directory = preparation
    target = {
        "account_id": "111122223333",
        "aws_region": "us-east-1",
        "environment": "dev",
        "org_id": identity[0],
        "workspace_id": identity[1],
    }
    backend = {
        "bucket": "fixture-state",
        "region": "us-east-1",
        "lock_table": "fixture-lock",
    }
    config = {
        "bucket": backend["bucket"],
        "region": backend["region"],
        "dynamodb_table": backend["lock_table"],
        "encrypt": True,
        "key": "dev/modules/superplane-workspaces/v2/org/workspace/terraform.tfstate",
        "access_key": None,
        "secret_key": None,
    }
    (directory / "workspace.tfvars.json").write_text(json.dumps(target))
    metadata = directory / "module/.terraform/terraform.tfstate"
    metadata.parent.mkdir()
    metadata.write_text(
        json.dumps(
            {
                "version": 3,
                "terraform_version": "1.9.8",
                "backend": {"type": "s3", "config": config, "hash": 123},
            }
        )
    )
    provider = (
        metadata.parent
        / "providers/registry.terraform.io/hashicorp/tls/4.4.1/linux_amd64"
    )
    provider.mkdir(parents=True)
    (provider / "terraform-provider-tls_v4.4.1_x5").write_bytes(b"retained provider")
    (provider / "LICENSE.txt").write_text("retained license")
    return root, identity, directory, target, backend, inspection_documents(directory)


def test_incomplete_init_preserves_all_original_bytes(incomplete_init):
    from workspace_provisioning.planning_boundary import verify_incomplete_init

    root, identity, directory, target, backend, documents = incomplete_init
    entries = snapshot(directory)
    digest = verify_incomplete_init(
        entries, workspace_source(), documents, target=target, backend=backend
    )
    receipt = {
        "reauthorization_id": "separate-current-decision",
        "proof": json.dumps(
            {
                "preparation_boundary": {
                    "kind": "incomplete-provider-install-v1",
                    "entries": entries,
                    "sha256": digest,
                }
            }
        ),
    }
    archive_preparation(root, identity, receipt)
    (archive,) = directory.parent.glob(directory.name + ".recovered-*")
    assert snapshot(archive) == entries
    assert not directory.exists()
    assert not list(operation_directory(root, *identity, create=True).iterdir())


@pytest.mark.parametrize(
    "name",
    [
        "module/.terraform/providers/registry.terraform.io/hashicorp/aws",
        "module/.terraform/providers/registry.terraform.io/hashicorp/tls/4.4.1/linux_arm64/binary",
        "module/.terraform/environment",
        "module/terraform.tfstate",
        "module/terraform.tfstate.backup",
        "review/workspace.tfplan",
        "review/workspace-plan.json",
        "module/.terraform/modules/modules.json",
        "module/.terraform/providers/registry.terraform.io/hashicorp/tls/4.4.1/linux_amd64/extra",
        ".terraformrc",
    ],
)
def test_complete_or_uncertain_initialization_refuses(incomplete_init, name):
    from workspace_provisioning.planning_boundary import verify_incomplete_init

    _, _, directory, target, backend, documents = incomplete_init
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("uncertain output")
    with pytest.raises(ValueError):
        verify_incomplete_init(
            snapshot(directory),
            workspace_source(),
            documents,
            target=target,
            backend=backend,
        )


@pytest.mark.parametrize(
    "change", ["target", "bucket", "hash", "source", "credentials", "state"]
)
def test_initialized_binding_refuses_drift(incomplete_init, change):
    from workspace_provisioning.planning_boundary import (
        inspection_documents,
        verify_incomplete_init,
    )

    _, _, directory, target, backend, documents = incomplete_init
    if change == "target":
        documents["target"]["workspace_id"] = "other"
    elif change == "bucket":
        backend["bucket"] = "other-state"
    elif change == "hash":
        documents["module/.terraform/terraform.tfstate"] += " "
    elif change == "source":
        (directory / "module/main.tf").write_text("changed recipe")
    else:
        path = directory / "module/.terraform/terraform.tfstate"
        metadata = json.loads(path.read_text())
        if change == "credentials":
            metadata["backend"]["config"]["secret_key"] = "must-never-log"
        else:
            metadata["resources"] = [{"type": "aws_eks_cluster"}]
        path.write_text(json.dumps(metadata))
        with pytest.raises(ValueError):
            inspection_documents(directory)
        return
    with pytest.raises(ValueError):
        verify_incomplete_init(
            snapshot(directory),
            workspace_source(),
            documents,
            target=target,
            backend=backend,
        )


@pytest.fixture
def lock_refusal(incomplete_init, tmp_path):
    import hashlib
    from pathlib import Path
    from workspace_provisioning.planning_boundary import (
        ARCHIVE_ONLY_LOCK_SHA256,
        LOCKED_PACKAGES,
    )

    root, identity, directory, target, backend, documents = incomplete_init
    original_source = tmp_path / "original-source"
    copy_source(original_source)
    recipe = (
        Path(__file__).parent / "fixtures/prepare-workspace-plan-original.py.txt"
    ).read_bytes()
    for destination in (original_source, directory / "module"):
        (destination / "scripts/prepare_workspace_plan.py").write_bytes(recipe)
    legacy = (
        Path(__file__).parent / "fixtures/provider-lock-archive-only.hcl"
    ).read_bytes()
    assert hashlib.sha256(legacy).hexdigest() == ARCHIVE_ONLY_LOCK_SHA256
    (original_source / ".terraform.lock.hcl").write_bytes(legacy)
    (directory / "module/.terraform.lock.hcl").write_bytes(legacy)
    provider = (
        directory
        / "module/.terraform/providers/registry.terraform.io/hashicorp/aws/6.65.0/linux_amd64"
    )
    provider.mkdir(parents=True)
    (provider / "LICENSE.txt").write_text("retained license")
    (provider / "terraform-provider-aws_v6.65.0_x5").write_bytes(
        b"retained AWS package"
    )
    observation = {
        "version": 1,
        "terraform_version": "1.9.8",
        "terraform_sha256": "a" * 64,
        "lock_sha256": ARCHIVE_ONLY_LOCK_SHA256,
        "rejected_packages": dict(LOCKED_PACKAGES),
        "diagnostic_sha256": "b" * 64,
    }
    return (
        root,
        identity,
        directory,
        target,
        backend,
        documents,
        original_source,
        observation,
    )


def test_exact_lock_refusal_preserves_complete_provider_cache(lock_refusal):
    from workspace_provisioning.planning_boundary import verify_provider_lock_refusal

    root, identity, directory, target, backend, documents, source, observation = (
        lock_refusal
    )
    entries = snapshot(directory)
    digest = verify_provider_lock_refusal(
        entries, source, documents, observation, target=target, backend=backend
    )
    receipt = {
        "reauthorization_id": "separate-lock-refusal-decision",
        "proof": json.dumps(
            {
                "preparation_boundary": {
                    "kind": "provider-lock-refusal-v1",
                    "entries": entries,
                    "sha256": digest,
                }
            }
        ),
    }
    archive_preparation(root, identity, receipt)
    (archive,) = directory.parent.glob(directory.name + ".recovered-*")
    assert snapshot(archive) == entries and not directory.exists()


@pytest.mark.parametrize(
    "change",
    [
        "repaired-lock",
        "changed-recipe",
        "no-observation",
        "another-refusal",
        "another-terraform",
        "missing-package",
        "extra-package-output",
        "plan",
        "backend-drift",
    ],
)
def test_lock_refusal_does_not_admit_general_initialized_or_failed_planning(
    lock_refusal, change
):
    from workspace_provisioning.planning_boundary import (
        verify_provider_lock_refusal,
        AWS_PACKAGE,
    )

    _, _, directory, target, backend, documents, source, observation = lock_refusal
    if change == "repaired-lock":
        (source / ".terraform.lock.hcl").write_bytes(
            (workspace_source() / ".terraform.lock.hcl").read_bytes()
        )
    elif change == "changed-recipe":
        (source / "scripts/prepare_workspace_plan.py").write_text("unreviewed producer")
    elif change == "no-observation":
        observation = None
    elif change == "another-refusal":
        observation["rejected_packages"].pop("registry.terraform.io/hashicorp/tls")
    elif change == "another-terraform":
        observation["terraform_version"] = "1.10.0"
    elif change == "missing-package":
        (directory / AWS_PACKAGE / "LICENSE.txt").unlink()
    elif change == "extra-package-output":
        (directory / AWS_PACKAGE / "extra").write_text("unknown")
    elif change == "plan":
        (directory / "review/workspace.tfplan").write_text("plan")
    else:
        backend["bucket"] = "another-bucket"
    with pytest.raises(ValueError):
        verify_provider_lock_refusal(
            snapshot(directory),
            source,
            documents,
            observation,
            target=target,
            backend=backend,
        )


def test_retained_aws_package_hashing_is_streamed_and_still_bounded(
    preparation, monkeypatch
):
    from pathlib import Path
    from workspace_provisioning.planning_boundary import AWS_PACKAGE

    _, _, directory = preparation
    path = directory / AWS_PACKAGE / "terraform-provider-aws_v6.65.0_x5"
    path.parent.mkdir(parents=True)
    with path.open("wb") as stream:
        stream.truncate(48 << 20)
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: pytest.fail("snapshot must stream retained bytes"),
    )
    assert snapshot(directory)[str(path.relative_to(directory))]["kind"] == "file"
    with path.open("wb") as stream:
        stream.truncate((1 << 30) + 1)
    with pytest.raises(ValueError, match="AWS package exceeds"):
        snapshot(directory)


@pytest.mark.parametrize(
    "change", [None, "valid", "other-error", "exit-code", "stderr"]
)
def test_probe_observes_exact_native_lock_refusal_without_credentials(
    lock_refusal, monkeypatch, change
):
    from types import SimpleNamespace
    import workspace_provisioning.planning_boundary as boundary

    _, _, directory, _, _, _, _, _ = lock_refusal
    seen = []
    real_hash = boundary.file_sha256
    monkeypatch.setattr(
        boundary,
        "file_sha256",
        lambda path: "a" * 64
        if str(path) == "/usr/local/bin/terraform"
        else real_hash(path),
    )

    def run(argv, **kwargs):
        seen.append(argv)
        assert kwargs["env"]["AWS_EC2_METADATA_DISABLED"] == "true"
        assert kwargs["env"]["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
        assert not any(
            k in kwargs["env"]
            for k in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]
        )
        if argv[1] == "version":
            return SimpleNamespace(stdout='{"terraform_version":"1.9.8"}')
        assert argv == ["/usr/local/bin/terraform", "validate", "-json"]
        summary = "missing or corrupted provider plugins:\n" + "\n".join(
            f"  - {name}: the cached package for {name} {version} (in .terraform/providers) does not match any of the checksums recorded in the dependency lock file"
            for name, version in boundary.LOCKED_PACKAGES.items()
        )
        payload = {
            "valid": change == "valid",
            "error_count": 1,
            "warning_count": 0,
            "diagnostics": [
                {
                    "severity": "error",
                    "summary": "other error" if change == "other-error" else summary,
                    "detail": "",
                }
            ],
        }
        return SimpleNamespace(
            returncode=2 if change == "exit-code" else 1,
            stdout=json.dumps(payload),
            stderr="unexpected" if change == "stderr" else "",
        )

    monkeypatch.setattr(boundary.subprocess, "run", run)
    if change:
        with pytest.raises(ValueError):
            boundary.provider_lock_refusal(directory)
    else:
        result = boundary.provider_lock_refusal(directory)
        assert result["rejected_packages"] == boundary.LOCKED_PACKAGES
    assert len(seen) == 2


@pytest.fixture
def socket_refusal(lock_refusal, tmp_path):
    import shutil
    from pathlib import Path
    from workspace_provisioning.terraform import workspace_source
    from workspace_provisioning.provider_socket_boundary import (
        SOCKET_LOCK_SHA256,
        SOCKET_RUNTIME_HASHES,
    )

    root, identity, directory, target, backend, documents, source, _ = lock_refusal
    for destination in (source, directory / "module"):
        shutil.copyfile(
            workspace_source() / ".terraform.lock.hcl",
            destination / ".terraform.lock.hcl",
        )
    runtime = tmp_path / "historical-runtime"
    runtime.mkdir()
    shutil.copyfile(
        Path(__file__).parent / "fixtures/process-long-scratch.py.txt",
        runtime / "process.py",
    )
    shutil.copyfile(
        Path(__file__).parent / "fixtures/terraform-original.py.txt",
        runtime / "terraform.py",
    )
    observation = {
        "version": 1,
        "terraform_version": "1.9.8",
        "terraform_sha256": "a" * 64,
        "lock_sha256": SOCKET_LOCK_SHA256,
        "runtime_sources": dict(SOCKET_RUNTIME_HASHES),
        "operation_directory_name": directory.name,
        "temporary_path_length": 122,
        "diagnostic_sha256": "b" * 64,
    }
    return (
        root,
        identity,
        directory,
        target,
        backend,
        documents,
        source,
        runtime,
        observation,
    )


def test_exact_socket_refusal_archives_every_retained_byte(socket_refusal):
    from workspace_provisioning.provider_socket_boundary import (
        verify_provider_socket_refusal,
    )

    (
        root,
        identity,
        directory,
        target,
        backend,
        documents,
        source,
        runtime,
        observation,
    ) = socket_refusal
    entries = snapshot(directory)
    digest = verify_provider_socket_refusal(
        entries,
        source,
        documents,
        observation,
        target=target,
        backend=backend,
        runtime_source=runtime,
        operation_directory_name=directory.name,
    )
    archive_preparation(
        root,
        identity,
        {
            "reauthorization_id": "socket-recovery-decision",
            "proof": json.dumps(
                {
                    "preparation_boundary": {
                        "kind": "provider-socket-refusal-v1",
                        "entries": entries,
                        "sha256": digest,
                    }
                }
            ),
        },
    )
    (archive,) = directory.parent.glob(directory.name + ".recovered-*")
    assert snapshot(archive) == entries and not directory.exists()


@pytest.mark.parametrize(
    "change",
    [
        "different-process",
        "different-layout",
        "different-lock",
        "different-recipe",
        "different-terraform",
        "short-path",
        "missing-proof",
        "extra-proof",
        "plan",
        "provider-output",
        "missing-provider",
        "backend-drift",
    ],
)
def test_socket_refusal_is_not_general_failed_plan_recovery(socket_refusal, change):
    from workspace_provisioning.provider_socket_boundary import (
        verify_provider_socket_refusal,
    )

    _, _, directory, target, backend, documents, source, runtime, observation = (
        socket_refusal
    )
    if change == "different-process":
        (runtime / "process.py").write_text("changed source")
    elif change == "different-layout":
        observation["operation_directory_name"] = "f" * 64
    elif change == "different-lock":
        (source / ".terraform.lock.hcl").write_text("changed lock")
    elif change == "different-recipe":
        (source / "scripts/prepare_workspace_plan.py").write_text("changed recipe")
    elif change == "different-terraform":
        observation["terraform_version"] = "1.10.0"
    elif change == "short-path":
        observation["temporary_path_length"] = 50
    elif change == "missing-proof":
        observation = None
    elif change == "extra-proof":
        observation["allowed"] = True
    elif change == "plan":
        (directory / "review/workspace.tfplan").write_bytes(b"retained plan")
    elif change == "provider-output":
        (directory / "module/terraform.tfstate").write_text("{}")
    elif change == "missing-provider":
        (
            directory
            / "module/.terraform/providers/registry.terraform.io/hashicorp/aws/6.65.0/linux_amd64/LICENSE.txt"
        ).unlink()
    elif change == "backend-drift":
        backend["bucket"] = "different-state"
    with pytest.raises(ValueError):
        verify_provider_socket_refusal(
            snapshot(directory),
            source,
            documents,
            observation,
            target=target,
            backend=backend,
            runtime_source=runtime,
            operation_directory_name=directory.name,
        )


def test_pre_init_failure_is_preserved_but_does_not_mask_started_work(preparation):
    root, identity, directory = preparation
    failure = directory / "command-failure-abcdefgh"
    failure.mkdir()
    (failure / "stdout").write_text("DENIED: read-only caller check timed out\n")
    (failure / "stderr").write_text("")
    entries = snapshot(directory)
    digest = verify_pre_init(entries, workspace_source())
    receipt = {
        "reauthorization_id": "preflight-timeout-decision",
        "proof": json.dumps(
            {
                "preparation_boundary": {
                    "kind": "before-terraform-init-v1",
                    "entries": entries,
                    "sha256": digest,
                }
            }
        ),
    }
    plan = directory / "review/workspace.tfplan"
    plan.write_text("started")
    with pytest.raises(ValueError):
        verify_pre_init(snapshot(directory), workspace_source())
    plan.unlink()
    archive_preparation(root, identity, receipt)
    (archive,) = directory.parent.glob(directory.name + ".recovered-*")
    assert snapshot(archive) == entries


@pytest.mark.parametrize(
    "extra",
    [
        "command-failure-abcdefgh/extra",
        "command-failure-otherone",
        "command-failure-invalid",
    ],
)
def test_pre_init_diagnostic_layout_is_closed(preparation, extra):
    _, _, directory = preparation
    failure = directory / "command-failure-abcdefgh"
    failure.mkdir()
    (failure / "stdout").write_text("timeout")
    (failure / "stderr").write_text("")
    (directory / extra).write_text("unexpected")
    with pytest.raises(ValueError):
        verify_pre_init(snapshot(directory), workspace_source())
