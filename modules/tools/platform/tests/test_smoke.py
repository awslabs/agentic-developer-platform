import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modules.tools.platform.scripts import smoke


class SmokeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.targets = self.directory / "targets.jsonl"
        self.targets.write_text("\n".join(json.dumps(target) for target in [
            {"targetId": "second", "lastSynchronizedAt": "2026-01-02T03:04:05.001Z"},
            {"targetId": "first", "lastSynchronizedAt": "2026-01-02T03:04:05+00:00"},
        ]))

    def test_target_revision_matches_runtime_datetime_serialization(self):
        expected = [["first", "2026-01-02T03:04:05+00:00"], ["second", "2026-01-02T03:04:05.001000+00:00"]]
        self.assertEqual(smoke.target_revision(self.targets), hashlib.sha256(
            json.dumps(expected, separators=(",", ":")).encode()).hexdigest())
        self.targets.write_text("\n".join(reversed(self.targets.read_text().splitlines())))
        self.assertEqual(smoke.target_revision(self.targets), hashlib.sha256(
            json.dumps(expected, separators=(",", ":")).encode()).hexdigest())

    def test_invalid_sync_timestamp_refuses_evidence(self):
        for timestamp in ("invalid", "2026-01-01T00:00:00"):
            self.targets.write_text(json.dumps({"targetId": "first", "lastSynchronizedAt": timestamp}))
            with self.assertRaises(ValueError):
                smoke.target_revision(self.targets)

    def test_redirect_cannot_forward_bearer_to_another_origin(self):
        with self.assertRaisesRegex(ValueError, "redirected"):
            smoke.NoRedirects().redirect_request(None, None, 302, "redirect", {}, "https://other.example.invalid")

    def test_legacy_login_entrypoint_is_retired_without_authentication_or_effect(self):
        with patch.dict(os.environ, {"ADP_TOOLS_SMOKE_REFRESH_TOKEN": "private-refresh-sentinel"}), \
                patch("subprocess.run") as subprocess_run:
            with self.assertRaisesRegex(ValueError, "Legacy smoke is retired") as error:
                smoke.main()
            self.assertNotIn("private-refresh-sentinel", str(error.exception))
            subprocess_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
