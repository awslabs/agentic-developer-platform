"""Tests for filter-sarif-ignores.py prefix matching logic."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

# Add parent directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from importlib.util import module_from_spec, spec_from_file_location

# Load the module from its file path (hyphenated filename)
_spec = spec_from_file_location(
    "filter_sarif_ignores",
    Path(__file__).resolve().parent.parent / "filter-sarif-ignores.py",
)
_mod = module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_ignore_cves = _mod.load_ignore_cves
filter_sarif = _mod.filter_sarif


def _make_sarif(rule_ids: list[str]) -> dict:
    """Build a minimal SARIF structure with the given ruleIds."""
    return {
        "runs": [
            {
                "results": [{"ruleId": rid} for rid in rule_ids],
            }
        ]
    }


def _write_sarif(tmp_path: Path, rule_ids: list[str]) -> str:
    sarif_file = tmp_path / "test.sarif"
    sarif_file.write_text(json.dumps(_make_sarif(rule_ids)))
    return str(sarif_file)


def _write_config(tmp_path: Path, cves: list[str]) -> str:
    config_file = tmp_path / ".grype.yaml"
    config = {"ignore": [{"vulnerability": cve} for cve in cves]}
    config_file.write_text(yaml.dump(config))
    return str(config_file)


class TestPrefixMatching:
    """Test that ruleId prefix matching works correctly."""

    def test_suffix_package_is_filtered(self, tmp_path: Path):
        """CVE-2025-22871-stdlib IS filtered when CVE-2025-22871 is ignored."""
        sarif_path = _write_sarif(tmp_path, ["CVE-2025-22871-stdlib"])
        ignore_cves = {"CVE-2025-22871"}

        result = filter_sarif(sarif_path, ignore_cves)

        assert result["runs"][0]["results"] == []

    def test_different_suffix_is_filtered(self, tmp_path: Path):
        """CVE-2025-22871-net/http IS filtered when CVE-2025-22871 is ignored."""
        sarif_path = _write_sarif(tmp_path, ["CVE-2025-22871-net/http"])
        ignore_cves = {"CVE-2025-22871"}

        result = filter_sarif(sarif_path, ignore_cves)

        assert result["runs"][0]["results"] == []

    def test_similar_prefix_not_filtered(self, tmp_path: Path):
        """CVE-2025-2287-something is NOT filtered when CVE-2025-22871 is ignored.

        This guards against false prefix matches where a shorter CVE number
        accidentally matches the start of a longer one.
        """
        sarif_path = _write_sarif(tmp_path, ["CVE-2025-2287-something"])
        ignore_cves = {"CVE-2025-22871"}

        result = filter_sarif(sarif_path, ignore_cves)

        assert len(result["runs"][0]["results"]) == 1
        assert result["runs"][0]["results"][0]["ruleId"] == "CVE-2025-2287-something"

    def test_exact_match_still_filtered(self, tmp_path: Path):
        """A ruleId exactly equal to CVE-2025-22871 (no suffix) IS filtered."""
        sarif_path = _write_sarif(tmp_path, ["CVE-2025-22871"])
        ignore_cves = {"CVE-2025-22871"}

        result = filter_sarif(sarif_path, ignore_cves)

        assert result["runs"][0]["results"] == []

    def test_non_ignored_cve_preserved(self, tmp_path: Path):
        """CVEs not in the ignore set are preserved in output."""
        sarif_path = _write_sarif(
            tmp_path, ["CVE-2025-22871-stdlib", "CVE-2099-99999-pkg"]
        )
        ignore_cves = {"CVE-2025-22871"}

        result = filter_sarif(sarif_path, ignore_cves)

        assert len(result["runs"][0]["results"]) == 1
        assert result["runs"][0]["results"][0]["ruleId"] == "CVE-2099-99999-pkg"

    def test_multiple_ignore_rules(self, tmp_path: Path):
        """Multiple CVEs in ignore set all get filtered by prefix."""
        sarif_path = _write_sarif(
            tmp_path,
            [
                "CVE-2023-45853-zlib1g",
                "CVE-2024-52308-gh",
                "CVE-2025-22871-stdlib",
                "CVE-2099-11111-safe",
            ],
        )
        ignore_cves = {"CVE-2023-45853", "CVE-2024-52308", "CVE-2025-22871"}

        result = filter_sarif(sarif_path, ignore_cves)

        assert len(result["runs"][0]["results"]) == 1
        assert result["runs"][0]["results"][0]["ruleId"] == "CVE-2099-11111-safe"
