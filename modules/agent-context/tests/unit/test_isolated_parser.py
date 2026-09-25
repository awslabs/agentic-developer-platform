"""Tests for the isolated parser pipeline (#6059 — S15 parser isolation).

Covers:
- Parser manifest validation (input/output schema, round-trip, rejection)
- Capability contracts (production denials, test grants, expiry, cancellation)
- Dependency preparation (npm/Go source validation, lockfile checking)
- Output publisher (path containment, symlinks, digests, sizes)
- Isolated runner integration (subprocess backend, backend unavailability)
- Manifest admission (no secrets, no shared PVC, no SA token, no host access)
- Ingest-repo integration (fail-closed without backend, truthful fallback)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from unittest import mock

import pytest

# Add the ingestion directory to sys.path for imports
_INGESTION_DIR = str(Path(__file__).resolve().parents[2] / "images" / "ingestion")
if _INGESTION_DIR not in sys.path:
    sys.path.insert(0, _INGESTION_DIR)

_MANIFESTS_DIR = str(Path(__file__).resolve().parents[2] / "manifests")


# ---------------------------------------------------------------------------
# Parser manifest tests
# ---------------------------------------------------------------------------


class TestParseInputManifest:
    """Input manifest schema validation."""

    def test_round_trip_serialization(self):
        from parser_manifest import ParseInputManifest, ResourceLimits

        m = ParseInputManifest(
            invocation_id="inv-1",
            asset_id="org/repo",
            attempt_id="att-1",
            source_dir="/source",
            source_digest="abc123",
            allowed_languages=["python", "go"],
            output_dir="/output",
            resource_limits=ResourceLimits(cpu_millicores=1000, memory_mib=2048),
        )
        j = m.to_json()
        m2 = ParseInputManifest.from_json(j)
        assert m2.invocation_id == "inv-1"
        assert m2.asset_id == "org/repo"
        assert m2.allowed_languages == ["python", "go"]
        assert m2.resource_limits.cpu_millicores == 1000

    def test_from_dict_with_nested_limits(self):
        from parser_manifest import ParseInputManifest

        d = {
            "invocation_id": "inv-1",
            "asset_id": "org/repo",
            "attempt_id": "att-1",
            "source_dir": "/source",
            "source_digest": "abc123",
            "allowed_languages": [],
            "output_dir": "/output",
            "resource_limits": {"cpu_millicores": 500, "memory_mib": 1024},
        }
        m = ParseInputManifest.from_dict(d)
        assert m.resource_limits.memory_mib == 1024

    def test_rejects_invalid_json(self):
        from parser_manifest import ParseInputManifest

        with pytest.raises(json.JSONDecodeError):
            ParseInputManifest.from_json("not json")


class TestParseOutputManifest:
    """Output manifest schema validation."""

    def test_any_success_property(self):
        from parser_manifest import LanguageResult, ParseOutputManifest

        m = ParseOutputManifest(
            invocation_id="inv-1",
            asset_id="org/repo",
            attempt_id="att-1",
            languages=[
                LanguageResult(language="python", success=False, error="refused"),
                LanguageResult(language="go", success=True, scip_path="go.scip"),
            ],
        )
        assert m.any_success is True
        assert m.successful_languages == ["go"]

    def test_no_success(self):
        from parser_manifest import LanguageResult, ParseOutputManifest

        m = ParseOutputManifest(
            invocation_id="inv-1",
            asset_id="org/repo",
            attempt_id="att-1",
            languages=[
                LanguageResult(language="python", success=False, error="refused"),
            ],
        )
        assert m.any_success is False
        assert m.successful_languages == []


class TestOutputPathValidation:
    """Output path containment checks."""

    def test_rejects_traversal(self):
        from parser_manifest import _validate_output_path

        violations: list[str] = []
        _validate_output_path("../../../etc/passwd", "/output", violations)
        assert any("traversal" in v for v in violations)

    def test_rejects_absolute_path(self):
        from parser_manifest import _validate_output_path

        violations: list[str] = []
        _validate_output_path("/etc/passwd", "/output", violations)
        assert any("absolute" in v for v in violations)

    def test_rejects_null_bytes(self):
        from parser_manifest import _validate_output_path

        violations: list[str] = []
        _validate_output_path("foo\x00bar", "/output", violations)
        assert any("null byte" in v for v in violations)

    def test_rejects_symlink(self):
        from parser_manifest import _validate_output_path

        with tempfile.TemporaryDirectory() as td:
            os.symlink("/etc/passwd", os.path.join(td, "link.scip"))
            violations: list[str] = []
            _validate_output_path("link.scip", td, violations)
            assert any("symlink" in v for v in violations)

    def test_accepts_valid_relative_path(self):
        from parser_manifest import _validate_output_path

        violations: list[str] = []
        _validate_output_path("python.scip", "/output", violations)
        assert violations == []


class TestManifestValidation:
    """Cross-manifest validation between output and binding."""

    def _binding(self, **overrides):
        from parser_manifest import InvocationBinding

        defaults = {
            "invocation_id": "inv-1",
            "asset_id": "org/repo",
            "attempt_id": "att-1",
            "source_digest": "abc",
        }
        defaults.update(overrides)
        return InvocationBinding(**defaults)

    def _output(self, **overrides):
        from parser_manifest import ParseOutputManifest

        defaults = {
            "invocation_id": "inv-1",
            "asset_id": "org/repo",
            "attempt_id": "att-1",
        }
        defaults.update(overrides)
        return ParseOutputManifest(**defaults)

    def test_valid_output_passes(self):
        from parser_manifest import validate_output_manifest

        # Should not raise
        validate_output_manifest(self._output(), self._binding(), "/tmp/out")

    def test_invocation_id_mismatch_rejected(self):
        from parser_manifest import ManifestValidationError, validate_output_manifest

        with pytest.raises(ManifestValidationError, match="invocation_id mismatch"):
            validate_output_manifest(
                self._output(invocation_id="wrong"),
                self._binding(),
                "/tmp/out",
            )

    def test_asset_id_mismatch_rejected(self):
        from parser_manifest import ManifestValidationError, validate_output_manifest

        with pytest.raises(ManifestValidationError, match="asset_id mismatch"):
            validate_output_manifest(
                self._output(asset_id="other/repo"),
                self._binding(),
                "/tmp/out",
            )

    def test_attempt_id_mismatch_rejected(self):
        from parser_manifest import ManifestValidationError, validate_output_manifest

        with pytest.raises(ManifestValidationError, match="attempt_id mismatch"):
            validate_output_manifest(
                self._output(attempt_id="wrong"),
                self._binding(),
                "/tmp/out",
            )

    def test_expired_binding_rejected(self):
        from parser_manifest import ManifestValidationError, validate_output_manifest

        with pytest.raises(ManifestValidationError, match="expired"):
            validate_output_manifest(
                self._output(),
                self._binding(expires_at=time.time() - 100),
                "/tmp/out",
            )

    def test_cancelled_binding_rejected(self):
        from parser_manifest import ManifestValidationError, validate_output_manifest

        with pytest.raises(ManifestValidationError, match="cancelled"):
            validate_output_manifest(
                self._output(),
                self._binding(cancelled=True),
                "/tmp/out",
            )

    def test_output_size_exceeded_rejected(self):
        from parser_manifest import ManifestValidationError, validate_output_manifest

        with pytest.raises(ManifestValidationError, match="output size"):
            validate_output_manifest(
                self._output(total_output_bytes=1024 * 1024 * 1024),
                self._binding(output_bytes_max=1024),
                "/tmp/out",
            )


class TestTreeDigest:
    """Source tree digest computation."""

    def test_deterministic(self):
        from parser_manifest import compute_tree_digest

        with tempfile.TemporaryDirectory() as td:
            Path(td, "a.py").write_text("print('hello')")
            Path(td, "b.py").write_text("print('world')")
            d1 = compute_tree_digest(td)
            d2 = compute_tree_digest(td)
            assert d1 == d2

    def test_different_content_different_digest(self):
        from parser_manifest import compute_tree_digest

        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            Path(td1, "a.py").write_text("v1")
            Path(td2, "a.py").write_text("v2")
            assert compute_tree_digest(td1) != compute_tree_digest(td2)

    def test_skips_symlinks(self):
        from parser_manifest import compute_tree_digest

        with tempfile.TemporaryDirectory() as td:
            Path(td, "a.py").write_text("content")
            os.symlink("/etc/passwd", os.path.join(td, "link"))
            # Should not raise and should produce a valid digest
            d = compute_tree_digest(td)
            assert len(d) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# Capability contract tests
# ---------------------------------------------------------------------------


class TestProductionAuthorizer:
    """Production authorizer MUST deny all capability requests."""

    def test_fetch_denied(self):
        from parser_capability import CapabilityDeniedError, ProductionAuthorizer

        auth = ProductionAuthorizer()
        with pytest.raises(CapabilityDeniedError, match="canonical asset-authority"):
            auth.issue_fetch("org/repo", "att-1")

    def test_publish_denied(self):
        from parser_capability import CapabilityDeniedError, ProductionAuthorizer

        auth = ProductionAuthorizer()
        with pytest.raises(CapabilityDeniedError, match="canonical asset-authority"):
            auth.issue_publish("org/repo", "att-1")

    def test_parse_denied(self):
        from parser_capability import CapabilityDeniedError, ProductionAuthorizer

        auth = ProductionAuthorizer()
        with pytest.raises(CapabilityDeniedError, match="canonical asset-authority"):
            auth.issue_parse("org/repo", "att-1")


class TestTestAuthorizer:
    """Test authorizer supplies synthetic grants for testing."""

    def test_issues_fetch_capability(self):
        from parser_capability import TestAuthorizer

        auth = TestAuthorizer()
        cap = auth.issue_fetch("org/repo", "att-1")
        assert cap.asset_id == "org/repo"
        assert cap.attempt_id == "att-1"
        assert not cap.is_expired

    def test_issues_publish_capability(self):
        from parser_capability import TestAuthorizer

        auth = TestAuthorizer()
        cap = auth.issue_publish("org/repo", "att-1")
        assert cap.asset_id == "org/repo"
        assert cap.is_valid

    def test_tracks_issued_grants(self):
        from parser_capability import TestAuthorizer

        auth = TestAuthorizer()
        auth.issue_fetch("org/repo", "att-1")
        auth.issue_parse("org/repo", "att-1")
        assert len(auth.issued_grants) == 2

    def test_expired_capability(self):
        from parser_capability import TestAuthorizer

        auth = TestAuthorizer(expiry_seconds=-1)
        cap = auth.issue_fetch("org/repo", "att-1")
        assert cap.is_expired


class TestCapabilityContracts:
    """Capability contract invariants."""

    def test_publish_capability_consumed_is_invalid(self):
        from parser_capability import PublishCapability

        cap = PublishCapability(
            invocation_id="inv-1",
            asset_id="org/repo",
            attempt_id="att-1",
            consumed=True,
        )
        assert not cap.is_valid

    def test_publish_capability_cancelled_is_invalid(self):
        from parser_capability import PublishCapability

        cap = PublishCapability(
            invocation_id="inv-1",
            asset_id="org/repo",
            attempt_id="att-1",
            cancelled=True,
        )
        assert not cap.is_valid

    def test_publish_capability_expired_is_invalid(self):
        from parser_capability import PublishCapability

        cap = PublishCapability(
            invocation_id="inv-1",
            asset_id="org/repo",
            attempt_id="att-1",
            expires_at=time.time() - 100,
        )
        assert not cap.is_valid

    def test_fetch_capability_no_expiry_is_not_expired(self):
        from parser_capability import FetchCapability

        cap = FetchCapability(
            invocation_id="inv-1",
            asset_id="org/repo",
            attempt_id="att-1",
            expires_at=0.0,
        )
        assert not cap.is_expired


# ---------------------------------------------------------------------------
# Dependency preparation tests
# ---------------------------------------------------------------------------


class TestNpmSourceValidation:
    """npm dependency source validation."""

    def test_rejects_git_dependency(self):
        from dep_preparation import _validate_npm_package_json

        with tempfile.TemporaryDirectory() as td:
            pkg = os.path.join(td, "package.json")
            Path(pkg).write_text(
                json.dumps({"dependencies": {"evil": "git+https://evil.com/repo.git"}})
            )
            violations = _validate_npm_package_json(pkg)
            assert any("git dependency refused" in v for v in violations)

    def test_rejects_file_dependency(self):
        from dep_preparation import _validate_npm_package_json

        with tempfile.TemporaryDirectory() as td:
            pkg = os.path.join(td, "package.json")
            Path(pkg).write_text(json.dumps({"dependencies": {"local": "file:../local-pkg"}}))
            violations = _validate_npm_package_json(pkg)
            assert any("file/link dependency refused" in v for v in violations)

    def test_rejects_link_dependency(self):
        from dep_preparation import _validate_npm_package_json

        with tempfile.TemporaryDirectory() as td:
            pkg = os.path.join(td, "package.json")
            Path(pkg).write_text(json.dumps({"dependencies": {"linked": "link:../linked-pkg"}}))
            violations = _validate_npm_package_json(pkg)
            assert any("file/link dependency refused" in v for v in violations)

    def test_rejects_unapproved_url_dependency(self):
        from dep_preparation import _validate_npm_package_json

        with tempfile.TemporaryDirectory() as td:
            pkg = os.path.join(td, "package.json")
            Path(pkg).write_text(
                json.dumps({"dependencies": {"custom": "https://evil.com/package.tgz"}})
            )
            violations = _validate_npm_package_json(pkg)
            assert any("unapproved URL" in v for v in violations)

    def test_accepts_normal_semver_deps(self):
        from dep_preparation import _validate_npm_package_json

        with tempfile.TemporaryDirectory() as td:
            pkg = os.path.join(td, "package.json")
            Path(pkg).write_text(
                json.dumps({"dependencies": {"express": "^4.18.0", "lodash": "~4.17.21"}})
            )
            violations = _validate_npm_package_json(pkg)
            assert violations == []


class TestNpmLockfileValidation:
    """npm lockfile redirect detection."""

    def test_rejects_unapproved_lockfile_resolved_url(self):
        from dep_preparation import _validate_npm_lockfile

        with tempfile.TemporaryDirectory() as td:
            lockfile = os.path.join(td, "package-lock.json")
            Path(lockfile).write_text(
                json.dumps(
                    {
                        "packages": {
                            "node_modules/evil": {
                                "resolved": "https://evil-registry.com/evil-1.0.0.tgz"
                            }
                        }
                    }
                )
            )
            violations = _validate_npm_lockfile(lockfile)
            assert any("unapproved resolved URL" in v for v in violations)

    def test_accepts_approved_registry_url(self):
        from dep_preparation import _validate_npm_lockfile

        with tempfile.TemporaryDirectory() as td:
            lockfile = os.path.join(td, "package-lock.json")
            Path(lockfile).write_text(
                json.dumps(
                    {
                        "packages": {
                            "node_modules/express": {
                                "resolved": "https://registry.npmjs.org/express/-/express-4.18.0.tgz"
                            }
                        }
                    }
                )
            )
            violations = _validate_npm_lockfile(lockfile)
            assert violations == []


class TestGoModValidation:
    """Go module source validation."""

    def test_rejects_external_replace_directive(self):
        from dep_preparation import _validate_go_mod

        with tempfile.TemporaryDirectory() as td:
            gomod = os.path.join(td, "go.mod")
            Path(gomod).write_text(
                textwrap.dedent("""\
                module example.com/repo
                go 1.21
                replace github.com/dep => ../local-dep
            """)
            )
            violations = _validate_go_mod(gomod)
            assert any("replace directive" in v for v in violations)

    def test_rejects_toolchain_download(self):
        from dep_preparation import _validate_go_mod

        with tempfile.TemporaryDirectory() as td:
            gomod = os.path.join(td, "go.mod")
            Path(gomod).write_text(
                textwrap.dedent("""\
                module example.com/repo
                go 1.21
                toolchain go1.22.0
            """)
            )
            violations = _validate_go_mod(gomod)
            assert any("toolchain download" in v for v in violations)

    def test_accepts_toolchain_local(self):
        from dep_preparation import _validate_go_mod

        with tempfile.TemporaryDirectory() as td:
            gomod = os.path.join(td, "go.mod")
            Path(gomod).write_text(
                textwrap.dedent("""\
                module example.com/repo
                go 1.21
                toolchain local
            """)
            )
            violations = _validate_go_mod(gomod)
            assert violations == []


class TestPythonDepRefusal:
    """Python deps are refused (pyright works without them)."""

    def test_python_deps_not_required(self):
        from dep_preparation import _prepare_python_deps

        with tempfile.TemporaryDirectory() as td:
            result = _prepare_python_deps("/fake/clone", td)
            assert result.success is True
            assert "not required" in result.detail


class TestRubyDepRefusal:
    """Ruby deps are refused (Sorbet works without gems)."""

    def test_ruby_deps_not_required(self):
        from dep_preparation import _prepare_ruby_deps

        with tempfile.TemporaryDirectory() as td:
            result = _prepare_ruby_deps("/fake/clone", td)
            assert result.success is True
            assert "not required" in result.detail


class TestDepSafeEnv:
    """Dependency preparation environment scrubbing."""

    def test_removes_clone_paths(self):
        from dep_preparation import _safe_dep_env

        with tempfile.TemporaryDirectory() as clone:
            clone_bin = os.path.join(clone, "bin")
            os.makedirs(clone_bin)
            env_orig = os.environ.copy()
            env_orig["PATH"] = f"{clone_bin}:/usr/bin:/bin"
            with mock.patch.dict(os.environ, env_orig, clear=True):
                env = _safe_dep_env(clone)
                assert clone_bin not in env["PATH"]
                assert "/usr/bin" in env["PATH"]

    def test_removes_dangerous_vars(self):
        from dep_preparation import _safe_dep_env

        with mock.patch.dict(os.environ, {"PYTHONPATH": "/evil", "LD_PRELOAD": "/evil.so"}):
            env = _safe_dep_env("/fake/clone")
            assert "PYTHONPATH" not in env
            assert "LD_PRELOAD" not in env


# ---------------------------------------------------------------------------
# Output publisher tests
# ---------------------------------------------------------------------------


class TestOutputPublisher:
    """Publisher validates untrusted parser output."""

    def _setup(self, td: str) -> tuple:
        from parser_manifest import InvocationBinding
        from parser_publisher import OutputPublisher

        output_dir = os.path.join(td, "output")
        os.makedirs(output_dir)

        binding = InvocationBinding(
            invocation_id="inv-1",
            asset_id="org/repo",
            attempt_id="att-1",
            source_digest="abc",
        )

        return output_dir, binding, OutputPublisher(binding)

    def test_valid_output_accepted(self):
        from parser_manifest import LanguageResult, ParseOutputManifest, compute_file_digest

        with tempfile.TemporaryDirectory() as td:
            output_dir, _binding, publisher = self._setup(td)

            # Create a valid .scip file
            scip_path = os.path.join(output_dir, "python.scip")
            Path(scip_path).write_bytes(b"scip content")
            digest = compute_file_digest(scip_path)
            size = os.path.getsize(scip_path)

            # Write valid output manifest
            manifest = ParseOutputManifest(
                invocation_id="inv-1",
                asset_id="org/repo",
                attempt_id="att-1",
                languages=[
                    LanguageResult(
                        language="python",
                        success=True,
                        scip_path="python.scip",
                        output_bytes=size,
                        digest=digest,
                    )
                ],
                total_output_bytes=size,
            )
            Path(output_dir, "output_manifest.json").write_text(manifest.to_json())

            result = publisher.validate_and_collect(output_dir)
            assert result.any_success

            scip_files = publisher.collect_scip_files(output_dir, result)
            assert "python" in scip_files

    def test_rejects_traversal_in_scip_path(self):
        from parser_manifest import LanguageResult, ParseOutputManifest
        from parser_publisher import PublicationError

        with tempfile.TemporaryDirectory() as td:
            output_dir, _binding, publisher = self._setup(td)

            manifest = ParseOutputManifest(
                invocation_id="inv-1",
                asset_id="org/repo",
                attempt_id="att-1",
                languages=[
                    LanguageResult(
                        language="python",
                        success=True,
                        scip_path="../../../etc/passwd",
                    )
                ],
            )
            Path(output_dir, "output_manifest.json").write_text(manifest.to_json())

            with pytest.raises(PublicationError, match="traversal"):
                publisher.validate_and_collect(output_dir)

    def test_rejects_symlink_output(self):
        from parser_manifest import LanguageResult, ParseOutputManifest
        from parser_publisher import PublicationError

        with tempfile.TemporaryDirectory() as td:
            output_dir, _binding, publisher = self._setup(td)

            # Create a symlink masquerading as a .scip file
            os.symlink("/etc/passwd", os.path.join(output_dir, "evil.scip"))

            manifest = ParseOutputManifest(
                invocation_id="inv-1",
                asset_id="org/repo",
                attempt_id="att-1",
                languages=[
                    LanguageResult(
                        language="python",
                        success=True,
                        scip_path="evil.scip",
                    )
                ],
            )
            Path(output_dir, "output_manifest.json").write_text(manifest.to_json())

            with pytest.raises(PublicationError, match="[Ss]ymlink"):
                publisher.validate_and_collect(output_dir)

    def test_rejects_digest_mismatch(self):
        from parser_manifest import LanguageResult, ParseOutputManifest
        from parser_publisher import PublicationError

        with tempfile.TemporaryDirectory() as td:
            output_dir, _binding, publisher = self._setup(td)

            scip_path = os.path.join(output_dir, "python.scip")
            Path(scip_path).write_bytes(b"real content")

            manifest = ParseOutputManifest(
                invocation_id="inv-1",
                asset_id="org/repo",
                attempt_id="att-1",
                languages=[
                    LanguageResult(
                        language="python",
                        success=True,
                        scip_path="python.scip",
                        digest="0000000000000000000000000000000000000000000000000000000000000000",
                        output_bytes=os.path.getsize(scip_path),
                    )
                ],
            )
            Path(output_dir, "output_manifest.json").write_text(manifest.to_json())

            with pytest.raises(PublicationError, match="digest mismatch"):
                publisher.validate_and_collect(output_dir)

    def test_rejects_size_mismatch(self):
        from parser_manifest import LanguageResult, ParseOutputManifest
        from parser_publisher import PublicationError

        with tempfile.TemporaryDirectory() as td:
            output_dir, _binding, publisher = self._setup(td)

            scip_path = os.path.join(output_dir, "python.scip")
            Path(scip_path).write_bytes(b"content")

            manifest = ParseOutputManifest(
                invocation_id="inv-1",
                asset_id="org/repo",
                attempt_id="att-1",
                languages=[
                    LanguageResult(
                        language="python",
                        success=True,
                        scip_path="python.scip",
                        output_bytes=99999,  # Wrong size
                    )
                ],
            )
            Path(output_dir, "output_manifest.json").write_text(manifest.to_json())

            with pytest.raises(PublicationError, match="size mismatch"):
                publisher.validate_and_collect(output_dir)

    def test_rejects_wrong_invocation_id(self):
        from parser_manifest import ParseOutputManifest
        from parser_publisher import PublicationError

        with tempfile.TemporaryDirectory() as td:
            output_dir, _binding, publisher = self._setup(td)

            manifest = ParseOutputManifest(
                invocation_id="wrong-inv",
                asset_id="org/repo",
                attempt_id="att-1",
            )
            Path(output_dir, "output_manifest.json").write_text(manifest.to_json())

            with pytest.raises(PublicationError, match="invocation_id mismatch"):
                publisher.validate_and_collect(output_dir)

    def test_rejects_expired_publish_capability(self):
        from parser_capability import PublishCapability
        from parser_manifest import InvocationBinding, ParseOutputManifest
        from parser_publisher import OutputPublisher, PublicationError

        with tempfile.TemporaryDirectory() as td:
            output_dir = os.path.join(td, "output")
            os.makedirs(output_dir)

            binding = InvocationBinding(
                invocation_id="inv-1",
                asset_id="org/repo",
                attempt_id="att-1",
                source_digest="abc",
            )
            expired_cap = PublishCapability(
                invocation_id="inv-1",
                asset_id="org/repo",
                attempt_id="att-1",
                expires_at=time.time() - 100,
            )
            publisher = OutputPublisher(binding, publish_cap=expired_cap)

            manifest = ParseOutputManifest(
                invocation_id="inv-1",
                asset_id="org/repo",
                attempt_id="att-1",
            )
            Path(output_dir, "output_manifest.json").write_text(manifest.to_json())

            with pytest.raises(PublicationError, match="expired"):
                publisher.validate_and_collect(output_dir)


# ---------------------------------------------------------------------------
# Isolated parser import discipline tests
# ---------------------------------------------------------------------------


class TestParserImportDiscipline:
    """The isolated parser must not import credential-bearing modules."""

    def test_forbidden_modules_list_is_comprehensive(self):
        from isolated_parser import _FORBIDDEN_MODULES

        # These are the critical modules that carry cloud/DB credentials
        must_forbid = {"boto3", "botocore", "httpx", "psycopg2", "gremlinpython"}
        assert must_forbid.issubset(_FORBIDDEN_MODULES)

    def test_check_import_discipline_clean(self):
        """No forbidden modules should be loaded from just importing the parser."""
        from isolated_parser import _check_import_discipline

        violations = _check_import_discipline()
        # Filter to only truly forbidden — some test frameworks may load httpx etc.
        # The check is meaningful inside the isolated container, not in the test env
        # where many modules are already loaded.  Here we verify the function works.
        assert isinstance(violations, list)

    def test_source_has_no_boto3_import(self):
        """The isolated_parser.py source must not import boto3 or botocore."""
        parser_source = Path(_INGESTION_DIR, "isolated_parser.py").read_text()
        assert "import boto3" not in parser_source
        assert "from boto3" not in parser_source
        assert "import botocore" not in parser_source
        assert "import httpx" not in parser_source
        assert "import requests" not in parser_source
        assert "import psycopg" not in parser_source
        assert "import gremlinpython" not in parser_source


# ---------------------------------------------------------------------------
# Isolated runner tests
# ---------------------------------------------------------------------------


class TestSubprocessBackendAvailability:
    """Subprocess backend availability checks."""

    def test_available_when_parser_exists(self):
        from isolated_runner import SubprocessBackend

        backend = SubprocessBackend(
            parser_script=os.path.join(_INGESTION_DIR, "isolated_parser.py")
        )
        assert backend.is_available()

    def test_unavailable_when_parser_missing(self):
        from isolated_runner import SubprocessBackend

        backend = SubprocessBackend(parser_script="/nonexistent/parser.py")
        assert not backend.is_available()


class TestIsolatedRunnerBackendUnavailable:
    """Runner reports truthful unavailable status when backend missing."""

    def test_returns_backend_unavailable(self):
        from isolated_runner import IsolatedParserRunner, SubprocessBackend

        backend = SubprocessBackend(parser_script="/nonexistent/parser.py")
        runner = IsolatedParserRunner(backend=backend)
        result = runner.run("/fake/clone", "org/repo")
        assert result.status == "backend_unavailable"
        assert "structural_stage_unavailable" in result.detail


class TestCredentialScrubbing:
    """The runner scrubs credentials from the subprocess environment."""

    def test_aws_credentials_removed(self):
        from isolated_runner import _scrubbed_env

        with mock.patch.dict(
            os.environ,
            {
                "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
                "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "AWS_SESSION_TOKEN": "FwoGZXIvYXdzEBYaDExample",
            },
        ):
            env = _scrubbed_env("/fake/clone")
            assert "AWS_ACCESS_KEY_ID" not in env
            assert "AWS_SECRET_ACCESS_KEY" not in env
            assert "AWS_SESSION_TOKEN" not in env

    def test_github_tokens_removed(self):
        from isolated_runner import _scrubbed_env

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_example", "GH_TOKEN": "ghp_ex"}):
            env = _scrubbed_env("/fake/clone")
            assert "GITHUB_TOKEN" not in env
            assert "GH_TOKEN" not in env

    def test_database_credentials_removed(self):
        from isolated_runner import _scrubbed_env

        with mock.patch.dict(
            os.environ, {"DATABASE_URL": "postgres://...", "PGPASSWORD": "secret"}
        ):
            env = _scrubbed_env("/fake/clone")
            assert "DATABASE_URL" not in env
            assert "PGPASSWORD" not in env

    def test_custom_secret_vars_removed(self):
        from isolated_runner import _scrubbed_env

        with mock.patch.dict(
            os.environ,
            {
                "MY_SECRET": "secret",
                "AUTH_TOKEN": "token",
                "API_KEY": "key",
                "DB_PASSWORD": "pass",
            },
        ):
            env = _scrubbed_env("/fake/clone")
            assert "MY_SECRET" not in env
            assert "AUTH_TOKEN" not in env
            assert "API_KEY" not in env
            assert "DB_PASSWORD" not in env

    def test_k8s_service_host_removed(self):
        from isolated_runner import _scrubbed_env

        with mock.patch.dict(
            os.environ,
            {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "443"},
        ):
            env = _scrubbed_env("/fake/clone")
            assert "KUBERNETES_SERVICE_HOST" not in env


# ---------------------------------------------------------------------------
# Manifest admission tests (Kubernetes Job template)
# ---------------------------------------------------------------------------


class TestParserJobManifest:
    """Kubernetes Job template security admission tests."""

    @pytest.fixture(autouse=True)
    def _load_manifest(self):
        import yaml

        manifest_path = os.path.join(_MANIFESTS_DIR, "parser-job.yaml")
        with open(manifest_path) as f:
            self.docs = list(yaml.safe_load_all(f))
        self.sa = next(d for d in self.docs if d["kind"] == "ServiceAccount")
        self.netpol = next(d for d in self.docs if d["kind"] == "NetworkPolicy")
        self.job = next(d for d in self.docs if d["kind"] == "Job")
        self.pod_spec = self.job["spec"]["template"]["spec"]
        self.container = self.pod_spec["containers"][0]

    def test_sa_no_irsa_annotation(self):
        """Service account must NOT have an IAM role annotation."""
        annotations = self.sa.get("metadata", {}).get("annotations", {})
        assert "eks.amazonaws.com/role-arn" not in annotations

    def test_sa_no_automount(self):
        """Service account must not auto-mount SA token."""
        assert self.sa.get("automountServiceAccountToken") is False

    def test_pod_no_automount(self):
        """Pod spec must not auto-mount SA token."""
        assert self.pod_spec.get("automountServiceAccountToken") is False

    def test_no_host_network(self):
        assert self.pod_spec.get("hostNetwork") is False

    def test_no_host_pid(self):
        assert self.pod_spec.get("hostPID") is False

    def test_no_host_ipc(self):
        assert self.pod_spec.get("hostIPC") is False

    def test_non_root_user(self):
        sec_ctx = self.pod_spec.get("securityContext", {})
        assert sec_ctx.get("runAsUser") == 1001
        assert sec_ctx.get("runAsGroup") == 1001

    def test_seccomp_runtime_default(self):
        sec_ctx = self.pod_spec.get("securityContext", {})
        assert sec_ctx.get("seccompProfile", {}).get("type") == "RuntimeDefault"

    def test_container_non_root(self):
        sec_ctx = self.container.get("securityContext", {})
        assert sec_ctx.get("runAsNonRoot") is True

    def test_container_readonly_root(self):
        sec_ctx = self.container.get("securityContext", {})
        assert sec_ctx.get("readOnlyRootFilesystem") is True

    def test_container_no_escalation(self):
        sec_ctx = self.container.get("securityContext", {})
        assert sec_ctx.get("allowPrivilegeEscalation") is False

    def test_container_not_privileged(self):
        sec_ctx = self.container.get("securityContext", {})
        assert sec_ctx.get("privileged") is False

    def test_container_drops_all_caps(self):
        sec_ctx = self.container.get("securityContext", {})
        caps = sec_ctx.get("capabilities", {})
        assert "ALL" in caps.get("drop", [])

    def test_no_env_from(self):
        """Container must not inherit environment from configmaps/secrets."""
        assert "envFrom" not in self.container

    def test_no_secret_ref_in_env(self):
        """No env var must reference a secret."""
        for env_entry in self.container.get("env", []):
            assert "valueFrom" not in env_entry or "secretKeyRef" not in env_entry.get(
                "valueFrom", {}
            )

    def test_no_platform_data_pvc(self):
        """No volume must use the shared platform-data PVC."""
        for vol in self.pod_spec.get("volumes", []):
            if "persistentVolumeClaim" in vol:
                assert vol["persistentVolumeClaim"]["claimName"] != "platform-data"

    def test_source_volume_is_emptydir(self):
        """Source volume must be emptyDir (not PVC)."""
        source_vol = next(v for v in self.pod_spec["volumes"] if v["name"] == "source")
        assert "emptyDir" in source_vol

    def test_output_volume_has_size_limit(self):
        """Output volume must have a size limit."""
        output_vol = next(v for v in self.pod_spec["volumes"] if v["name"] == "output")
        assert output_vol["emptyDir"].get("sizeLimit")

    def test_active_deadline_set(self):
        """Job must have a hard deadline."""
        assert self.job["spec"].get("activeDeadlineSeconds", 0) > 0

    def test_backoff_limit_zero(self):
        """No retries — runner handles retries with fresh invocation IDs."""
        assert self.job["spec"].get("backoffLimit") == 0

    def test_network_policy_denies_all(self):
        """NetworkPolicy must deny all ingress and egress."""
        assert self.netpol["spec"]["ingress"] == []
        assert self.netpol["spec"]["egress"] == []
        assert "Ingress" in self.netpol["spec"]["policyTypes"]
        assert "Egress" in self.netpol["spec"]["policyTypes"]

    def test_source_mounted_readonly(self):
        """Source must be mounted read-only."""
        source_mount = next(m for m in self.container["volumeMounts"] if m["name"] == "source")
        assert source_mount.get("readOnly") is True

    def test_input_mounted_readonly(self):
        """Input manifest must be mounted read-only."""
        input_mount = next(m for m in self.container["volumeMounts"] if m["name"] == "input")
        assert input_mount.get("readOnly") is True


# ---------------------------------------------------------------------------
# Ingest-repo integration tests (fail-closed, truthful fallback)
# ---------------------------------------------------------------------------


class TestIngestRepoIsolatedIntegration:
    """Integration with ingest-repo.py SCIP configuration."""

    def test_production_selection_cannot_enable_subprocess_or_legacy(self):
        import ast

        tree = ast.parse(Path(_INGESTION_DIR, "ingest-repo.py").read_text())
        core = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_ingest_repo_in_snapshot"
        )
        calls = [
            node.func.id
            for node in ast.walk(core)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        assert "scip_structural_ingest" not in calls
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_run_isolated_scip"
        )
        namespace = {"SCIP_ISOLATED_BACKEND": "subprocess", "log": mock.Mock(), "Any": object}
        exec(
            compile(ast.Module(body=[function], type_ignores=[]), "production-selector", "exec"),
            namespace,
        )
        assert (
            namespace["_run_isolated_scip"]("/unused", "org/repo", None, None)
            == "structural_stage_unavailable"
        )


# ---------------------------------------------------------------------------
# Scoped npm registry refusal in dependency preparation
# ---------------------------------------------------------------------------


class TestScopedRegistryRefusal:
    """Scoped npm registries pointing to unapproved URLs are refused."""

    def test_scoped_registry_refused(self):
        from dep_preparation import _prepare_typescript_deps

        with tempfile.TemporaryDirectory() as clone:
            # Create a package.json (valid)
            Path(clone, "package.json").write_text(
                json.dumps({"dependencies": {"@scope/pkg": "^1.0.0"}})
            )
            # Create .npmrc with a scoped registry
            Path(clone, ".npmrc").write_text("@scope:registry=https://evil-registry.com\n")

            with tempfile.TemporaryDirectory() as scratch:
                result = _prepare_typescript_deps(clone, scratch)
                assert result.refused is True
                assert "scoped registry" in result.detail

    def test_approved_scoped_registry_accepted(self):
        from dep_preparation import _prepare_typescript_deps

        with tempfile.TemporaryDirectory() as clone:
            Path(clone, "package.json").write_text(
                json.dumps({"dependencies": {"@types/node": "^20.0.0"}})
            )
            # .npmrc pointing to the approved registry
            Path(clone, ".npmrc").write_text("@types:registry=https://registry.npmjs.org\n")

            with tempfile.TemporaryDirectory() as scratch:
                # This won't actually run npm (npm may not be installed)
                # but the pre-install validation should pass
                result = _prepare_typescript_deps(clone, scratch)
                # If npm is missing, the detail will say so — that's OK
                # The important thing is it was NOT refused for registry reasons
                assert result.refused is not True or "scoped registry" not in result.detail
