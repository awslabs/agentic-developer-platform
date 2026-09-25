"""Actual Docker validation, opt-in with an already available immutable image.

No registry pulls, model calls, host config mounts or live gateway calls occur.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "modules/agent-factory/agent-worker-image"))
from lib.codex_validation import DockerValidationExecutor, ValidationCheck  # noqa: E402

IMAGE = os.environ.get("ADP_CODEX_VALIDATION_IMAGE")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="requires explicitly provisioned immutable Docker image"
)


def source(tmp_path):
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as stream:
        content = b"supplied source\n"
        entry = tarfile.TarInfo("source.txt")
        entry.size = len(content)
        entry.mode = 0o644
        stream.addfile(entry, io.BytesIO(content))
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def containers():
    return subprocess.check_output(
        ["/usr/bin/docker", "ps", "-aq", "--filter=label=adp.codex-validation=true"], text=True
    ).split()


def test_actual_validation_reads_source_without_host_credentials_network_or_capabilities(
    tmp_path, monkeypatch
):
    archive, digest = source(tmp_path)
    monkeypatch.setenv("AWS_SESSION_TOKEN", "inherited-poison-token")
    monkeypatch.setenv("BG_CONFIG_DIR", str(tmp_path / "poison-bg"))
    before = containers()
    script = """set -eu
[ "$(id -u)" = 65534 ]
[ -z "${AWS_SESSION_TOKEN:-}" ]
[ "$HOME" = /tmp ]
[ "$BG_CONFIG_DIR" = /tmp/bg ]
[ ! -e /var/run/docker.sock ]
[ ! -e /home/ubuntu/.bedrock-gateway/tokens.json ]
grep -q 'CapEff:.*0000000000000000' /proc/self/status
! grep -q '00000000.*0003' /proc/net/route
! touch /root/host-write 2>/dev/null
grep -q 'supplied source' source.txt
printf 'validated'
"""
    result = DockerValidationExecutor().run(
        check=ValidationCheck("isolation", IMAGE, ("/bin/sh", "-c", script)),
        archive=archive,
        archive_sha256=digest,
        commit="a" * 40,
    )
    assert result["status"] == "passed", result
    assert result["output"] == "validated"
    assert result["archiveSha256"] == digest
    assert containers() == before
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(
    "argv,timeout,maximum,reason",
    [
        (("/bin/sh", "-c", "exit 7"), 30, 32768, "process_failed"),
        (("/bin/sh", "-c", "sleep 30"), 1, 32768, "timeout"),
        (("/bin/sh", "-c", "yes output"), 30, 128, "output_limit"),
        (("/bin/sh", "-c", r"printf '\377\377\377'"), 30, 4, "output_limit"),
    ],
)
def test_actual_failed_timeout_and_excess_output_never_pass_and_remove_container(
    tmp_path, argv, timeout, maximum, reason
):
    archive, digest = source(tmp_path)
    before = containers()
    result = DockerValidationExecutor().run(
        check=ValidationCheck(
            "negative", IMAGE, argv, timeout_seconds=timeout, max_output_bytes=maximum
        ),
        archive=archive,
        archive_sha256=digest,
        commit="a" * 40,
    )
    assert result["status"] == "failed" and result["reason"] == reason
    assert len(result["output"].encode()) <= maximum
    assert containers() == before


def test_changed_source_and_mutable_image_refused_before_container_creation(tmp_path):
    archive, digest = source(tmp_path)
    before = containers()
    for image, expected in [("busybox:latest", digest), (IMAGE, "0" * 64)]:
        with pytest.raises(ValueError):
            DockerValidationExecutor().run(
                check=ValidationCheck("invalid", image, ("true",)),
                archive=archive,
                archive_sha256=expected,
                commit="a" * 40,
            )
    assert containers() == before


def test_real_git_commit_is_validated_and_dirty_checkout_is_refused(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args):
        return subprocess.check_output(
            [
                "/usr/bin/git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@localhost",
                *args,
            ],
            cwd=repository,
            text=True,
        ).strip()

    git("init", "-q")
    (repository / "test.sh").write_text('test "$(cat value.txt)" = expected\n')
    (repository / "value.txt").write_text("expected\n")
    git("add", "test.sh", "value.txt")
    git("commit", "-qm", "validation fixture")
    head = git("rev-parse", "HEAD")
    check = ValidationCheck("real_commit", IMAGE, ("/bin/sh", "test.sh"))
    executor = DockerValidationExecutor()
    result = executor.run_repository(check=check, repository=repository, expected_head=head)
    assert result["status"] == "passed" and result["commit"] == head
    (repository / "value.txt").write_text("changed\n")
    from lib.codex_validation import ValidationUnavailable

    with pytest.raises(ValidationUnavailable, match="clean commit"):
        executor.run_repository(check=check, repository=repository, expected_head=head)


def test_materialized_workspace_edit_and_commit_pass_actual_container_check(tmp_path):
    from lib.codex_workspace import CodexWorkspace

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as stream:
        for name, content in {
            "source/value.txt": b"incorrect\n",
            "source/test.sh": b'test "$(cat value.txt)" = expected\n',
        }.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            stream.addfile(entry, io.BytesIO(content))
    archive = data.getvalue()
    workspace = CodexWorkspace(
        tmp_path / "workspace",
        provider="github",
        repository="fixture/repository",
        source_revision="b" * 40,
    )
    initial = workspace.materialize(archive, archive_sha256=hashlib.sha256(archive).hexdigest())
    check = ValidationCheck("acceptance", IMAGE, ("/bin/sh", "test.sh"))
    executor = DockerValidationExecutor()
    failed = executor.run_repository(
        repository=workspace.root, expected_head=initial["localHead"], check=check
    )
    assert failed["status"] == "failed"
    old = workspace.read_file("value.txt")
    workspace.write_file("value.txt", "expected\n", expected_sha256=old["sha256"])
    repaired = workspace.commit("Repair acceptance fixture")
    passed = executor.run_repository(
        repository=workspace.root, expected_head=repaired["localHead"], check=check
    )
    assert passed["status"] == "passed"
    assert passed["commit"] == repaired["localHead"] != initial["localHead"]
    assert repaired["sourceRevision"] == "b" * 40
    assert passed["specificationDigest"] == failed["specificationDigest"]
    assert passed["archiveSha256"] != failed["archiveSha256"]
