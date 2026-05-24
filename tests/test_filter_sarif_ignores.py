"""Unit tests for codebuild/filter-sarif-ignores.py."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml

# Import the module under test
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "filter_sarif_ignores",
    str(Path(__file__).resolve().parent.parent / "codebuild" / "filter-sarif-ignores.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_ignore_cves = _mod.load_ignore_cves
filter_sarif = _mod.filter_sarif
main = _mod.main


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_SARIF = {
    "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "grype", "version": "0.80.2"}},
            "results": [
                {"ruleId": "CVE-2025-22871", "message": {"text": "stdlib vuln"}},
                {"ruleId": "CVE-2025-22873", "message": {"text": "stdlib vuln"}},
                {"ruleId": "CVE-2024-12345", "message": {"text": "real finding"}},
                {"ruleId": "CVE-2024-52308", "message": {"text": "gh false positive"}},
            ],
        }
    ],
}

SAMPLE_GRYPE_CONFIG = {
    "ignore": [
        {"vulnerability": "CVE-2025-22871", "package": {"type": "go-module", "name": "stdlib"}},
        {"vulnerability": "CVE-2025-22873", "package": {"type": "go-module", "name": "stdlib"}},
        {"vulnerability": "CVE-2024-52308", "package": {"name": "gh"}},
    ]
}


@pytest.fixture
def sarif_file(tmp_path: Path) -> Path:
    p = tmp_path / "scan.sarif"
    p.write_text(json.dumps(SAMPLE_SARIF))
    return p


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / ".grype.yaml"
    p.write_text(yaml.dump(SAMPLE_GRYPE_CONFIG))
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLoadIgnoreCves:
    def test_extracts_cve_ids(self, config_file: Path) -> None:
        cves = load_ignore_cves(str(config_file))
        assert cves == {"CVE-2025-22871", "CVE-2025-22873", "CVE-2024-52308"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        cves = load_ignore_cves(str(tmp_path / "nonexistent.yaml"))
        assert cves == set()

    def test_empty_ignore_list(self, tmp_path: Path) -> None:
        p = tmp_path / ".grype.yaml"
        p.write_text(yaml.dump({"ignore": []}))
        cves = load_ignore_cves(str(p))
        assert cves == set()

    def test_no_ignore_key(self, tmp_path: Path) -> None:
        p = tmp_path / ".grype.yaml"
        p.write_text(yaml.dump({"db": {"auto-update": True}}))
        cves = load_ignore_cves(str(p))
        assert cves == set()


class TestFilterSarif:
    def test_removes_matching_cves(self, sarif_file: Path) -> None:
        ignore = {"CVE-2025-22871", "CVE-2025-22873", "CVE-2024-52308"}
        result = filter_sarif(str(sarif_file), ignore)

        results = result["runs"][0]["results"]
        assert len(results) == 1
        assert results[0]["ruleId"] == "CVE-2024-12345"

    def test_empty_ignore_set_preserves_all(self, sarif_file: Path) -> None:
        result = filter_sarif(str(sarif_file), set())
        results = result["runs"][0]["results"]
        assert len(results) == 4

    def test_no_matching_cves_preserves_all(self, sarif_file: Path) -> None:
        result = filter_sarif(str(sarif_file), {"CVE-9999-99999"})
        results = result["runs"][0]["results"]
        assert len(results) == 4

    def test_all_results_removed(self, sarif_file: Path) -> None:
        all_cves = {r["ruleId"] for r in SAMPLE_SARIF["runs"][0]["results"]}
        result = filter_sarif(str(sarif_file), all_cves)
        results = result["runs"][0]["results"]
        assert len(results) == 0


class TestMainCli:
    def test_end_to_end(self, sarif_file: Path, config_file: Path, tmp_path: Path) -> None:
        output = tmp_path / "filtered.sarif"
        import sys

        sys.argv = [
            "filter-sarif-ignores.py",
            "--sarif", str(sarif_file),
            "--config", str(config_file),
            "--output", str(output),
        ]
        rc = main()
        assert rc == 0
        assert output.exists()

        result = json.loads(output.read_text())
        results = result["runs"][0]["results"]
        assert len(results) == 1
        assert results[0]["ruleId"] == "CVE-2024-12345"

    def test_missing_sarif_returns_error(self, config_file: Path, tmp_path: Path) -> None:
        import sys

        sys.argv = [
            "filter-sarif-ignores.py",
            "--sarif", str(tmp_path / "missing.sarif"),
            "--config", str(config_file),
            "--output", str(tmp_path / "out.sarif"),
        ]
        rc = main()
        assert rc == 1

    def test_in_place_overwrite(self, sarif_file: Path, config_file: Path) -> None:
        """Test that --output can be the same file as --sarif (in-place)."""
        import sys

        sys.argv = [
            "filter-sarif-ignores.py",
            "--sarif", str(sarif_file),
            "--config", str(config_file),
            "--output", str(sarif_file),
        ]
        rc = main()
        assert rc == 0

        result = json.loads(sarif_file.read_text())
        results = result["runs"][0]["results"]
        assert len(results) == 1
