"""Mixed smoke suites must release one Playwright runtime before starting another."""

import os
from pathlib import Path
import subprocess
import sys


def test_browser_runtime_is_released_between_smoke_packages(tmp_path):
    root = Path(__file__).resolve().parents[2]
    # Substitute only the browser transport. Exercise the real fixture scopes
    # under pytest, including both suite orderings, without AWS or Chromium.
    (tmp_path / "conftest.py").write_text("""
import sys
from types import ModuleType

active = False

class Runtime:
    def start(self):
        global active
        assert not active, "another package still owns the Playwright runtime"
        active = True
        return self
    def stop(self):
        global active
        active = False
    def __enter__(self):
        return self.start()
    def __exit__(self, *args):
        self.stop()
    @property
    def chromium(self):
        return self
    def launch(self, **kwargs):
        return self
    def close(self):
        pass

playwright = ModuleType("playwright")
sync_api = ModuleType("playwright.sync_api")
sync_api.sync_playwright = Runtime
sys.modules["playwright"] = playwright
sys.modules["playwright.sync_api"] = sync_api
""")
    for suite in ("chat", "new_ui"):
        package = tmp_path / suite
        package.mkdir()
        (package / "__init__.py").touch()
        (package / "conftest.py").write_text(
            f"from tests.e2e.{suite}.conftest import browser_instance\n"
        )
        (package / "test_runtime.py").write_text(
            "def test_browser(browser_instance):\n    assert browser_instance is not None\n"
        )
    for order in (("chat", "new_ui"), ("new_ui", "chat")):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *order],
            cwd=tmp_path,
            env={
                **os.environ,
                "PYTHONPATH": str(root),
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
