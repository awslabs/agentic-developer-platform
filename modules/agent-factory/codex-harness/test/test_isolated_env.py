"""Real child-process regression for inherited BG_CONFIG_DIR/token contamination."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class IsolationTest(unittest.TestCase):
    def test_child_cannot_inherit_login_stores_or_credentials(self):
        runner = Path(__file__).with_name("run-isolated.py")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = root / "live"
            live.mkdir()
            token = live / "tokens.json"
            token.write_text('"live-token-sentinel"')
            environment = {
                "PATH": os.environ["PATH"],
                "HOME": str(live),
                "BG_CONFIG_DIR": str(live),
                "ADP_HOME": str(live),
                "ADP_LEGACY_CONFIG_DIR": str(live),
                "AWS_SHARED_CREDENTIALS_FILE": str(token),
                "AWS_ACCESS_KEY_ID": "must-not-inherit",
                "GH_TOKEN": "must-not-inherit",
                "ADP_DEPLOYMENT_URL": "https://live.invalid",
                "ADP_TOKEN_FILE": str(token),
            }
            runtime_lib = Path(sys.base_prefix) / "lib"
            if (runtime_lib / f"libpython{sys.version_info.major}.{sys.version_info.minor}.so.1.0").is_file():
                environment["LD_LIBRARY_PATH"] = str(runtime_lib)
            code = """
import asyncio, json, os
assert asyncio.run(asyncio.sleep(0, result="ok")) == "ok"
from pathlib import Path
assert not any(k in os.environ for k in ('GH_TOKEN', 'AWS_ACCESS_KEY_ID', 'ADP_DEPLOYMENT_URL'))
paths = [os.environ[k] for k in ('HOME','BG_CONFIG_DIR','ADP_HOME','ADP_LEGACY_CONFIG_DIR',
    'ADP_TOKEN_FILE','AWS_CONFIG_FILE','AWS_SHARED_CREDENTIALS_FILE','GH_CONFIG_DIR','CODEX_HOME')]
assert all('adp-isolated-tests-' in p for p in paths)
Path(os.environ['BG_CONFIG_DIR'], 'tokens.json').write_text('test-login')
Path(os.environ['ADP_TOKEN_FILE']).write_text('test-run-token')
print(json.dumps(paths))
"""
            result = subprocess.run(
                [sys.executable, str(runner), "--", sys.executable, "-c", code],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(token.read_text(), '"live-token-sentinel"')
            self.assertTrue(all(not Path(p).exists() for p in json.loads(result.stdout)))

    def test_exit_status_propagates(self):
        runner = Path(__file__).with_name("run-isolated.py")
        result = subprocess.run(
            [sys.executable, str(runner), "--", sys.executable, "-c", "raise SystemExit(7)"],
            check=False,
        )
        self.assertEqual(result.returncode, 7)


if __name__ == "__main__":
    unittest.main()
