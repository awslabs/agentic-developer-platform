"""Unit tests for the Mode B script validator."""

import importlib.util
import tempfile
from pathlib import Path

import pytest

# Load validate_script from the skill directory using importlib
_SKILL_DIR = (
    Path(__file__).resolve().parents[3]
    / "cyber"
    / "agent"
    / "skills"
    / "stage-3-static"
)
_spec = importlib.util.spec_from_file_location(
    "validate_script", _SKILL_DIR / "validate_script.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
validate_script = _mod.validate_script


@pytest.fixture
def sample_manifest() -> dict:
    """A realistic worker manifest for testing."""
    return {
        "image_tag": "abc123",
        "generated_at": "2026-04-30T04:12:33Z",
        "python": {"version": "3.12.5", "interpreter": "/usr/local/bin/python3"},
        "python_packages": {
            "lief": "0.14.1",
            "pefile": "2024.8.26",
            "yara-python": "4.5.1",
            "capstone": "5.0.3",
            "oletools": "0.60.2",
            "magika": "0.6.1",
            "iocextract": "1.16.1",
            "ppdeep": "20260221",
            "pyelftools": "0.31",
            "macholib": "1.16.3",
            "signify": "0.7.1",
            "boto3": "1.35.0",
            "requests": "2.32.0",
            "pydantic": "2.9.0",
            "python-magic": "0.4.27",
        },
        "system_binaries": {
            "strings": {"path": "/usr/bin/strings", "version": "GNU binutils 2.40"},
            "yara": {"path": "/usr/bin/yara", "version": "YARA 4.3.0"},
            "binwalk": {"path": "/usr/bin/binwalk", "version": "2.3.4"},
            "file": {"path": "/usr/bin/file", "version": "file-5.44"},
            "osslsigncode": {"path": "/usr/bin/osslsigncode", "version": "2.5"},
            "upx": {"path": "/usr/bin/upx", "version": "upx-ucl 4.0.2"},
        },
        "resources": {"yara_rules_dir": "/opt/yara-rules", "yara_rule_count": 1847},
    }


def _write_script(content: str) -> str:
    """Write script content to a temp file and return the path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
    f.write(content)
    f.close()
    return f.name


class TestValidScript:
    """Tests for scripts that should pass validation."""

    def test_valid_script_with_allowed_imports(self, sample_manifest):
        script = _write_script(
            "import json, sys, hashlib\nimport pefile\nprint(json.dumps({}))\n"
        )
        violations = validate_script(script, sample_manifest)
        assert violations == []

    def test_valid_script_with_from_import(self, sample_manifest):
        script = _write_script("from pathlib import Path\nimport lief\n")
        violations = validate_script(script, sample_manifest)
        assert violations == []

    def test_valid_subprocess_call(self, sample_manifest):
        script = _write_script(
            'import subprocess\nsubprocess.run(["strings", "/tmp/sample"], capture_output=True)\n'
        )
        violations = validate_script(script, sample_manifest)
        assert violations == []

    def test_valid_yara_import(self, sample_manifest):
        """yara-python installs as 'yara' — should be allowed."""
        script = _write_script(
            "import yara\nrules = yara.compile('/opt/yara-rules/x.yar')\n"
        )
        violations = validate_script(script, sample_manifest)
        assert violations == []

    def test_valid_python_magic_import(self, sample_manifest):
        """python-magic installs as 'magic' — should be allowed."""
        script = _write_script("import magic\nm = magic.from_file('/tmp/x')\n")
        violations = validate_script(script, sample_manifest)
        assert violations == []

    def test_valid_elftools_import(self, sample_manifest):
        """pyelftools installs as 'elftools' — should be allowed."""
        script = _write_script("from elftools.elf.elffile import ELFFile\n")
        violations = validate_script(script, sample_manifest)
        assert violations == []


class TestInvalidImports:
    """Tests for scripts with disallowed imports."""

    def test_import_ssdeep_fails(self, sample_manifest):
        """ssdeep is not in the image (ppdeep is) — should fail."""
        script = _write_script(
            "import ssdeep\nhash = ssdeep.hash_from_file('/tmp/x')\n"
        )
        violations = validate_script(script, sample_manifest)
        assert len(violations) == 1
        assert "ssdeep" in violations[0]
        assert "not in worker image" in violations[0]

    def test_import_unknown_package_fails(self, sample_manifest):
        script = _write_script("import totally_unknown_package\n")
        violations = validate_script(script, sample_manifest)
        assert len(violations) == 1
        assert "totally_unknown_package" in violations[0]

    def test_from_import_unknown_fails(self, sample_manifest):
        script = _write_script("from scapy.all import IP\n")
        violations = validate_script(script, sample_manifest)
        assert len(violations) == 1
        assert "scapy" in violations[0]

    def test_multiple_violations(self, sample_manifest):
        script = _write_script("import ssdeep\nimport numpy\nimport pandas\n")
        violations = validate_script(script, sample_manifest)
        assert len(violations) == 3


class TestInvalidSubprocess:
    """Tests for scripts with disallowed subprocess calls."""

    def test_subprocess_unknown_binary_fails(self, sample_manifest):
        script = _write_script(
            'import subprocess\nsubprocess.run(["totally-fake-tool", "/tmp/x"])\n'
        )
        violations = validate_script(script, sample_manifest)
        assert len(violations) == 1
        assert "totally-fake-tool" in violations[0]
        assert "not in worker image" in violations[0]

    def test_subprocess_python3_allowed(self, sample_manifest):
        """python3 is always allowed as a subprocess target."""
        script = _write_script(
            'import subprocess\nsubprocess.run(["python3", "helper.py"])\n'
        )
        violations = validate_script(script, sample_manifest)
        assert violations == []

    def test_subprocess_with_path(self, sample_manifest):
        """Full paths — extract basename for check."""
        script = _write_script(
            'import subprocess\nsubprocess.run(["/usr/bin/strings", "/tmp/x"])\n'
        )
        violations = validate_script(script, sample_manifest)
        assert violations == []


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_syntax_error_in_script(self, sample_manifest):
        script = _write_script("def foo(\n")  # Intentional syntax error
        violations = validate_script(script, sample_manifest)
        assert len(violations) == 1
        assert "SyntaxError" in violations[0]

    def test_empty_script(self, sample_manifest):
        script = _write_script("")
        violations = validate_script(script, sample_manifest)
        assert violations == []

    def test_stdlib_only_script(self, sample_manifest):
        script = _write_script(
            "import json\nimport sys\nimport os\nimport re\nimport hashlib\n"
            "import struct\nimport base64\nimport collections\n"
        )
        violations = validate_script(script, sample_manifest)
        assert violations == []
