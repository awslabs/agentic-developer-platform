"""Production renderer contract for the SSM-backed model allowlist policy."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = ROOT / "platform" / "scripts" / "validate-model-allowlist-config.py"


class ModelAllowlistConfigRenderTests(unittest.TestCase):
    def run_validator(self, raw: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(VALIDATOR)],
            input=raw,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_actual_empty_object_is_canonical_baseline(self) -> None:
        result = self.run_validator("{}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "{}")

    def test_both_production_renderers_use_the_fail_closed_validator(self) -> None:
        validator_path = "platform/scripts/validate-model-allowlist-config.py"
        for relative_path in (
            ".github/workflows/gateway-deploy.yml",
            "platform/scripts/deploy-all.sh",
        ):
            with self.subTest(renderer=relative_path):
                source = (ROOT / relative_path).read_text()
                self.assertIn(
                    'model-allowed-models-config" "__ADP_SSM_UNAVAILABLE__"',
                    source,
                )
                self.assertIn(validator_path, source)

    def test_ssm_unavailable_fails_instead_of_becoming_baseline(self) -> None:
        for raw in ("", "None", "__ADP_SSM_UNAVAILABLE__"):
            with self.subTest(raw=raw):
                result = self.run_validator(raw)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unavailable", result.stderr)

    def test_explicit_empty_scope_survives_canonicalization(self) -> None:
        raw = json.dumps(
            {"org-b": ["global.anthropic.claude-opus-*"], "org-a:team-a": []}
        )
        result = self.run_validator(raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"org-a:team-a": [], "org-b": ["global.anthropic.claude-opus-*"]},
        )

    def test_malformed_shape_fails(self) -> None:
        for raw in ("not-json", "[]", '{"org-a":"global.anthropic.*"}'):
            with self.subTest(raw=raw):
                self.assertNotEqual(self.run_validator(raw).returncode, 0)


if __name__ == "__main__":
    unittest.main()
