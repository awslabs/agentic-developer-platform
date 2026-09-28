"""Exercise production runner gates and immutable output handoff."""

import ast
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

INGESTION = Path(__file__).resolve().parents[2] / "images" / "ingestion"
sys.path.insert(0, str(INGESTION))
from isolated_runner import IsolatedParserRunner  # noqa: E402
from parser_capability import TestAuthorizer as SyntheticAuthorizer  # noqa: E402
from parser_manifest import InvocationBinding, LanguageResult, ParseOutputManifest  # noqa: E402
from parser_publisher import OutputPublisher, PublicationError  # noqa: E402
from source_snapshot import source_snapshot  # noqa: E402


class FakeParser:
    def __init__(self):
        self.calls = []

    def is_available(self):
        return True

    def cleanup(self):
        pass

    def run(self, manifest, source, output, **kwargs):
        self.calls.append((manifest, source, output))
        assert not Path(source, ".git").exists()
        body = b"validated SCIP fixture"
        Path(output, "python.scip").write_bytes(body)
        value = ParseOutputManifest(
            invocation_id=manifest.invocation_id,
            asset_id=manifest.asset_id,
            attempt_id=manifest.attempt_id,
            languages=[
                LanguageResult(
                    language="python",
                    success=True,
                    scip_path="python.scip",
                    digest=hashlib.sha256(body).hexdigest(),
                    output_bytes=len(body),
                )
            ],
            total_output_bytes=len(body),
            status="complete",
        )
        Path(output, "output_manifest.json").write_text(value.to_json())
        return 0


def test_default_authority_denies_before_dependency_fetch_or_parser(tmp_path, monkeypatch):
    backend = FakeParser()
    fetch = Mock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr("isolated_runner.prepare_dependencies", fetch)
    result = IsolatedParserRunner(backend=backend).run(str(tmp_path), "org/repo")
    assert result.status == "authority_unavailable"
    fetch.assert_not_called()
    assert not backend.calls


def test_valid_injected_grants_traverse_real_runner_and_validator(tmp_path):
    Path(tmp_path, "code.py").write_text("x = 1\n")
    Path(tmp_path, ".git").mkdir()
    Path(tmp_path, ".git", "config").write_text("private transport metadata")
    backend = FakeParser()
    authorizer = SyntheticAuthorizer()
    result = IsolatedParserRunner(backend=backend, authorizer=authorizer).run(
        str(tmp_path), "org/repo"
    )
    try:
        assert result.status == "complete", result.error
        assert Path(result.scip_files["python"]).read_bytes() == b"validated SCIP fixture"
        assert len(authorizer.issued_grants) == 3
        assert len({grant["invocation_id"] for grant in authorizer.issued_grants}) == 1
        assert result.publish_cap.asset_id == "org/repo"
    finally:
        result.cleanup()


def test_expired_grant_denies_before_backend(tmp_path):
    backend = FakeParser()
    result = IsolatedParserRunner(
        backend=backend, authorizer=SyntheticAuthorizer(expiry_seconds=-1)
    ).run(str(tmp_path), "org/repo")
    assert result.status == "authority_unavailable" and not backend.calls


def test_private_source_rejects_symlink_before_parser(tmp_path):
    Path(tmp_path, "bad.py").symlink_to("/etc/passwd")
    backend = FakeParser()
    result = IsolatedParserRunner(backend=backend, authorizer=SyntheticAuthorizer()).run(
        str(tmp_path), "org/repo"
    )
    assert result.status == "error" and not backend.calls


def manifest_fixture(output, *, claimed_size=None, digest=True):
    body = b"original bytes"
    Path(output, "python.scip").write_bytes(body)
    value = ParseOutputManifest(
        invocation_id="run",
        asset_id="asset",
        attempt_id="attempt",
        total_output_bytes=len(body),
        languages=[
            LanguageResult(
                language="python",
                success=True,
                scip_path="python.scip",
                output_bytes=len(body) if claimed_size is None else claimed_size,
                digest=hashlib.sha256(body).hexdigest() if digest else None,
            )
        ],
    )
    Path(output, "output_manifest.json").write_text(value.to_json())
    return OutputPublisher(
        InvocationBinding(
            invocation_id="run", asset_id="asset", attempt_id="attempt", source_digest="digest"
        )
    )


def test_zero_size_cannot_bypass_actual_byte_validation(tmp_path):
    publisher = manifest_fixture(tmp_path, claimed_size=0)
    with pytest.raises(PublicationError, match="size mismatch"):
        publisher.validate_and_collect(str(tmp_path))


def test_missing_digest_is_refused(tmp_path):
    publisher = manifest_fixture(tmp_path, digest=False)
    with pytest.raises(PublicationError, match="digest mismatch"):
        publisher.validate_and_collect(str(tmp_path))


def test_consumers_receive_exact_validated_bytes_after_path_tampering(tmp_path):
    publisher = manifest_fixture(tmp_path)
    manifest = publisher.validate_and_collect(str(tmp_path))
    Path(tmp_path, "python.scip").write_bytes(b"tampered")
    paths = publisher.collect_scip_files(str(tmp_path), manifest)
    assert Path(paths["python"]).read_bytes() == b"original bytes"


def test_manifest_symlink_is_refused(tmp_path):
    publisher = manifest_fixture(tmp_path)
    Path(tmp_path, "output_manifest.json").unlink()
    Path(tmp_path, "output_manifest.json").symlink_to("/etc/passwd")
    with pytest.raises(PublicationError):
        publisher.validate_and_collect(str(tmp_path))


def test_private_source_cleanup_is_exact_even_on_failure():
    with tempfile.TemporaryDirectory(dir="/tmp") as root:
        persistent = Path(root, "persistent")
        persistent.mkdir()
        sentinel = persistent / "keep"
        sentinel.write_text("keep")
        with pytest.raises(RuntimeError):
            with source_snapshot(root, (str(persistent),)) as source:
                owned = Path(source).parent
                Path(source).mkdir()
                raise RuntimeError("failed attempt")
        assert not owned.exists() and sentinel.read_text() == "keep"


def test_real_ingest_wrapper_uses_private_snapshot_and_cleans_all_exits():
    tree = ast.parse(Path(INGESTION, "ingest-repo.py").read_text())
    function = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ingest_repo"
    )
    with tempfile.TemporaryDirectory(dir="/tmp") as root:
        persistent = Path(root, "persistent")
        persistent.mkdir()

        def core(*args, clone_path, **kwargs):
            assert not Path(clone_path).exists()
            Path(clone_path).mkdir()
            return {"source": clone_path}

        namespace = {
            "settings": SimpleNamespace(scratch_base=root, state_dir=str(persistent)),
            "CLONE_BASE": str(persistent),
            "Any": object,
            "_ingest_repo_in_snapshot": core,
        }
        exec(
            compile(ast.Module(body=[function], type_ignores=[]), "real-ingest-wrapper", "exec"),
            namespace,
        )
        first = namespace["ingest_repo"]("org/repo")
        second = namespace["ingest_repo"]("org/repo")
        assert first["source"] != second["source"]
        assert not Path(first["source"]).exists() and not Path(second["source"]).exists()


def test_docker_output_is_bounded_and_container_removed(tmp_path, monkeypatch):
    from isolated_runner import DockerBackend
    from parser_manifest import ParseInputManifest

    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=b"0", stderr=b"")

    monkeypatch.setattr("isolated_runner.subprocess.run", execute)
    backend = DockerBackend(image="example/parser@sha256:" + "a" * 64)
    manifest = ParseInputManifest(
        invocation_id="run",
        asset_id="asset",
        attempt_id="attempt",
        source_dir="/source",
        source_digest="x",
        output_dir="/output",
        allowed_languages=["python"],
    )
    monkeypatch.setattr(backend, "_export_output", Mock())
    assert backend.run(manifest, str(tmp_path), str(tmp_path)) == 0
    creation = calls[0]
    assert "--network=none" in creation and "--pids-limit=256" in creation
    assert any(item.startswith("/output:size=") for item in creation)
    assert not any(item == str(tmp_path) + ":/output" for item in creation)
    assert calls[-1][:3] == ["docker", "rm", "--force"]
    backend._export_output.assert_called_once()


def test_docker_timeout_cleans_owned_container(tmp_path, monkeypatch):
    import subprocess
    from isolated_runner import DockerBackend
    from parser_manifest import ParseInputManifest

    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if command[1] == "exec":
            raise subprocess.TimeoutExpired(command, 10)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("isolated_runner.subprocess.run", execute)
    backend = DockerBackend(image="example/parser@sha256:" + "a" * 64)
    manifest = ParseInputManifest(
        invocation_id="run",
        asset_id="asset",
        attempt_id="attempt",
        source_dir="/source",
        source_digest="x",
        output_dir="/output",
        allowed_languages=["python"],
    )
    with pytest.raises(subprocess.TimeoutExpired):
        backend.run(manifest, str(tmp_path), str(tmp_path))
    assert calls[-1][:3] == ["docker", "rm", "--force"]


def test_dependency_environment_does_not_inherit_credentials(monkeypatch):
    from dep_preparation import _safe_dep_env

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("HOME", "/credential-home")
    env = _safe_dep_env("/source")
    assert not any(key in env for key in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "HOME"))


@pytest.mark.parametrize(
    "image, allowed",
    [
        ("sha256:" + "a" * 64, True),
        ("local/parser@sha256:" + "b" * 64, True),
        ("local/parser:latest", False),
        ("sha256:short", False),
    ],
)
def test_docker_requires_immutable_image_identity(monkeypatch, image, allowed):
    from isolated_runner import DockerBackend

    inspect = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr("isolated_runner.subprocess.run", inspect)
    assert DockerBackend(image=image).is_available() is allowed
    assert inspect.called is allowed


@pytest.mark.parametrize(
    "url",
    [
        "https://registry.npmjs.org.evil.invalid/pkg.tgz",
        "https://registry.npmjs.org@evil.invalid/pkg.tgz",
        "http://registry.npmjs.org/pkg.tgz",
        "https://registry.npmjs.org:444/pkg.tgz",
    ],
)
def test_registry_admission_rejects_origin_substitution(tmp_path, url):
    from dep_preparation import _validate_npm_lockfile, _validate_npm_package_json
    import json

    lock = tmp_path / "package-lock.json"
    lock.write_text(json.dumps({"packages": {"node_modules/pkg": {"resolved": url}}}))
    assert _validate_npm_lockfile(str(lock))
    package = tmp_path / "package.json"
    package.write_text(json.dumps({"dependencies": {"pkg": url}}))
    assert _validate_npm_package_json(str(package))


@pytest.mark.parametrize("replacement", ["../private", "/etc/private", '"../private"'])
def test_go_multiline_replace_cannot_read_host_paths(tmp_path, replacement):
    from dep_preparation import _validate_go_mod

    manifest = tmp_path / "go.mod"
    manifest.write_text(
        f"module example.test/mod\nreplace (\n example.test/dep => {replacement}\n)\n"
    )
    assert _validate_go_mod(str(manifest))


@pytest.mark.parametrize(
    "name,kind", [("../escape", "file"), ("link", "symlink"), ("nested/file", "file")]
)
def test_docker_archive_export_rejects_unsafe_members(tmp_path, monkeypatch, name, kind):
    import io
    import tarfile
    from isolated_runner import DockerBackend
    from parser_capability import CapabilityDeniedError

    def execute(command, **kwargs):
        with tarfile.open(fileobj=kwargs["stdout"], mode="w|") as archive:
            member = tarfile.TarInfo(name)
            member.size = 1
            if kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                member.size = 0
            archive.addfile(member, io.BytesIO(b"x"))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("isolated_runner.subprocess.run", execute)
    with pytest.raises(CapabilityDeniedError):
        DockerBackend()._export_output("owned-container", str(tmp_path), 1024)
    assert not list(tmp_path.iterdir())


class NarrowingAuthorizer(SyntheticAuthorizer):
    """A server grant may authorize less than the caller requested."""

    def __init__(self, **changes):
        super().__init__()
        self.changes = changes

    def issue_parse(self, *args, **kwargs):
        from dataclasses import replace

        return replace(super().issue_parse(*args, **kwargs), **self.changes)


def test_omitted_backend_never_selects_host_subprocess(tmp_path, monkeypatch):
    constructor = Mock(side_effect=AssertionError("implicit subprocess backend"))
    monkeypatch.setattr("isolated_runner.SubprocessBackend", constructor)
    authority = Mock()
    result = IsolatedParserRunner(authorizer=authority).run(str(tmp_path), "org/repo")
    assert result.status == "backend_unavailable"
    constructor.assert_not_called()
    authority.issue_fetch.assert_not_called()


def test_production_authorizer_cannot_select_host_subprocess_even_with_future_issuer(tmp_path):
    from isolated_runner import SubprocessBackend
    from parser_capability import ProductionAuthorizer

    class FutureProductionAuthorizer(ProductionAuthorizer):
        def issue_fetch(self, *args, **kwargs):
            raise AssertionError("production subprocess must be refused before issuing grants")

    result = IsolatedParserRunner(
        backend=SubprocessBackend(), authorizer=FutureProductionAuthorizer()
    ).run(str(tmp_path), "org/repo")
    assert result.status == "backend_unavailable"


@pytest.mark.parametrize("field", ["source_dir", "output_dir"])
@pytest.mark.parametrize("path", ["", "/", "/tmp", "/unrelated/source", "relative/path"])
def test_grant_path_substitution_denies_before_dependencies(tmp_path, monkeypatch, field, path):
    Path(tmp_path, "code.py").write_text("x = 1\n")
    prepare = Mock(side_effect=AssertionError("must not prepare dependencies"))
    monkeypatch.setattr("isolated_runner.prepare_dependencies", prepare)
    backend = FakeParser()
    result = IsolatedParserRunner(
        backend=backend, authorizer=NarrowingAuthorizer(**{field: path})
    ).run(str(tmp_path), "org/repo")
    assert result.status == "error" and "grant paths" in result.error
    prepare.assert_not_called()
    assert not backend.calls


@pytest.mark.parametrize("languages", [[], ["go"], [""], [None], "python", None])
def test_empty_disjoint_or_malformed_language_grant_denies_before_dependencies(
    tmp_path, monkeypatch, languages
):
    Path(tmp_path, "code.py").write_text("x = 1\n")
    prepare = Mock(side_effect=AssertionError("must not prepare dependencies"))
    monkeypatch.setattr("isolated_runner.prepare_dependencies", prepare)
    backend = FakeParser()
    result = IsolatedParserRunner(
        backend=backend, authorizer=NarrowingAuthorizer(allowed_languages=languages)
    ).run(str(tmp_path), "org/repo")
    assert result.status == "error" and "language" in result.error
    prepare.assert_not_called()
    assert not backend.calls


@pytest.mark.parametrize(
    "granted,requested", [(["python"], ["python", "go"]), (["python", "go"], ["python"])]
)
def test_language_intersection_reaches_dependency_preparation_and_backend(
    tmp_path, monkeypatch, granted, requested
):
    from dep_preparation import PreparedDeps

    Path(tmp_path, "code.py").write_text("x = 1\n")
    Path(tmp_path, "code.go").write_text("package main\n")
    prepare = Mock(return_value=PreparedDeps())
    monkeypatch.setattr("isolated_runner.prepare_dependencies", prepare)
    backend = FakeParser()
    result = IsolatedParserRunner(
        backend=backend, authorizer=NarrowingAuthorizer(allowed_languages=granted)
    ).run(str(tmp_path), "org/repo", allowed_languages=requested)
    try:
        assert result.status == "complete", result.error
        assert prepare.call_args.args[1] == ["python"]
        manifest, source, output = backend.calls[0]
        assert manifest.allowed_languages == ["python"]
        assert manifest.source_dir == source and manifest.output_dir == output
    finally:
        result.cleanup()


@pytest.mark.parametrize("field", ["output_bytes_max", "deadline_seconds"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10", None])
def test_invalid_grant_bounds_deny_before_dependencies(tmp_path, monkeypatch, field, value):
    Path(tmp_path, "code.py").write_text("x = 1\n")
    prepare = Mock(side_effect=AssertionError("must not prepare dependencies"))
    monkeypatch.setattr("isolated_runner.prepare_dependencies", prepare)
    backend = FakeParser()
    result = IsolatedParserRunner(
        backend=backend, authorizer=NarrowingAuthorizer(**{field: value})
    ).run(str(tmp_path), "org/repo")
    assert result.status == "error" and "positive integers" in result.error
    prepare.assert_not_called()
    assert not backend.calls


@pytest.mark.parametrize(
    "grant_bytes,requested_bytes,grant_seconds,requested_seconds,expected_bytes,expected_seconds",
    [(1024, 8192, 7, 60, 1024, 7), (8192, 1024, 60, 7, 1024, 7)],
)
def test_effective_limits_bound_backend(
    tmp_path,
    grant_bytes,
    requested_bytes,
    grant_seconds,
    requested_seconds,
    expected_bytes,
    expected_seconds,
):
    from parser_manifest import ResourceLimits

    Path(tmp_path, "code.py").write_text("x = 1\n")
    backend = FakeParser()
    result = IsolatedParserRunner(
        backend=backend,
        authorizer=NarrowingAuthorizer(
            output_bytes_max=grant_bytes, deadline_seconds=grant_seconds
        ),
    ).run(
        str(tmp_path),
        "org/repo",
        resource_limits=ResourceLimits(
            output_bytes_max=requested_bytes, deadline_seconds=requested_seconds
        ),
    )
    try:
        assert result.status == "complete", result.error
        limits = backend.calls[0][0].resource_limits
        assert limits.output_bytes_max == expected_bytes
        assert limits.deadline_seconds == expected_seconds
    finally:
        result.cleanup()


def test_output_exceeding_narrowed_byte_grant_is_rejected(tmp_path):
    Path(tmp_path, "code.py").write_text("x = 1\n")
    backend = FakeParser()
    result = IsolatedParserRunner(
        backend=backend, authorizer=NarrowingAuthorizer(output_bytes_max=1)
    ).run(str(tmp_path), "org/repo")
    assert len(backend.calls) == 1
    assert result.status == "error" and "exceeds limit 1" in result.error
    assert not result.scip_files


def test_success_after_granted_deadline_is_not_published(tmp_path, monkeypatch):
    Path(tmp_path, "code.py").write_text("x = 1\n")
    monkeypatch.setattr("isolated_runner.time.monotonic", Mock(side_effect=[10, 12]))
    backend = FakeParser()
    result = IsolatedParserRunner(
        backend=backend, authorizer=NarrowingAuthorizer(deadline_seconds=1)
    ).run(str(tmp_path), "org/repo")
    assert len(backend.calls) == 1
    assert result.status == "error" and "exceeded granted deadline" in result.error
    assert not result.scip_files


def test_backend_cannot_return_ungranted_language(tmp_path, monkeypatch):
    from dep_preparation import PreparedDeps

    Path(tmp_path, "code.py").write_text("x = 1\n")
    Path(tmp_path, "code.go").write_text("package main\n")
    monkeypatch.setattr("isolated_runner.prepare_dependencies", Mock(return_value=PreparedDeps()))
    backend = FakeParser()  # Always emits Python, even when only Go is granted.
    result = IsolatedParserRunner(
        backend=backend, authorizer=NarrowingAuthorizer(allowed_languages=["go"])
    ).run(str(tmp_path), "org/repo")
    assert backend.calls[0][0].allowed_languages == ["go"]
    assert result.status == "error" and "language outside its grant" in result.error
    assert not result.scip_files


def test_explicit_test_subprocess_enforces_granted_deadline(tmp_path, monkeypatch):
    from isolated_runner import SubprocessBackend

    Path(tmp_path, "code.py").write_text("x = 1\n")
    launch = Mock(return_value=SimpleNamespace(returncode=1, stderr=b"synthetic test exit"))
    monkeypatch.setattr("isolated_runner.subprocess.run", launch)
    result = IsolatedParserRunner(
        backend=SubprocessBackend(timeout=600),
        authorizer=NarrowingAuthorizer(deadline_seconds=3),
    ).run(str(tmp_path), "org/repo")
    assert result.status == "indexing_failed"
    assert launch.call_args.kwargs["timeout"] == 3


def test_docker_operations_and_export_use_remaining_granted_deadline(tmp_path, monkeypatch):
    from isolated_runner import DockerBackend
    from parser_manifest import ParseInputManifest, ResourceLimits

    calls = []

    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"0", stderr=b"")

    monkeypatch.setattr("isolated_runner.subprocess.run", execute)
    backend = DockerBackend(image="example/parser@sha256:" + "a" * 64)
    export = Mock()
    monkeypatch.setattr(backend, "_export_output", export)
    manifest = ParseInputManifest(
        invocation_id="run",
        asset_id="asset",
        attempt_id="attempt",
        source_dir="/source",
        source_digest="x",
        output_dir="/output",
        allowed_languages=["python"],
        resource_limits=ResourceLimits(deadline_seconds=1, output_bytes_max=1024),
    )
    assert backend.run(manifest, str(tmp_path), str(tmp_path)) == 0
    assert all(0 < options["timeout"] <= 1 for _, options in calls[:-1])
    assert 0 < export.call_args.kwargs["timeout"] <= 1
    assert export.call_args.args[2] == 1024
    assert calls[-1][0][:3] == ["docker", "rm", "--force"]


def test_docker_poll_timeout_removes_container_before_return(tmp_path, monkeypatch):
    import subprocess
    from isolated_runner import DockerBackend
    from parser_manifest import ParseInputManifest, ResourceLimits

    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if command[1] == "exec":
            assert 0 < kwargs["timeout"] <= 1
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("isolated_runner.subprocess.run", execute)
    backend = DockerBackend(image="example/parser@sha256:" + "a" * 64)
    manifest = ParseInputManifest(
        invocation_id="run",
        asset_id="asset",
        attempt_id="attempt",
        source_dir="/source",
        source_digest="x",
        output_dir="/output",
        allowed_languages=["python"],
        resource_limits=ResourceLimits(deadline_seconds=1),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        backend.run(manifest, str(tmp_path), str(tmp_path))
    assert calls[-1][:3] == ["docker", "rm", "--force"]
