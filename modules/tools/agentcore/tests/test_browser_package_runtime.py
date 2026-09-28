"""Exercise lazy Browser entry points with only shared packages installed."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[4]


def test_shared_browser_startup_without_cyber_or_path_mutation(tmp_path):
    script = r'''
import sys
from unittest.mock import Mock, patch
before = list(sys.path)
from agentcore_tools.browser_runtime import local_browser, investigation_browser, native_browser
from agentcore_tools.browser_runtime.errors import BrowserBrokerError
assert sys.path == before
assert investigation_browser.validate_start({"url": "https://example.com"})["url"] == "https://example.com"
for url in ("https://user:pass@example.com", "https://example.com/?token=secret"):
    try:
        investigation_browser.validate_start({"url": url})
    except ValueError:
        pass
    else:
        raise AssertionError("credential URL accepted")
# Exercise the actual lazy imports and process entry point without provider work.
with patch.object(local_browser.subprocess, "Popen", return_value=Mock(poll=lambda: 1)) as launch:
    try:
        local_browser.investigation_request("start", {"url": "https://example.com"})
    except BrowserBrokerError as error:
        assert error.code == "session_ended"
    else:
        raise AssertionError("dead child was accepted")
    assert launch.call_args.args[0][1:3] == ["-m", "agentcore_tools.browser_runtime.local_browser"]
provider = Mock()
provider.start.return_value = "session"
provider.generate_ws_headers.return_value = ("wss://example.com", {})
playwright = Mock()
session = native_browser.open_native_browser("https://example.com", playwright, client_factory=lambda **kw: provider)
assert session is not None and provider.start.call_count == 1
assert not any(name in sys.modules for name in ("cyber_tools", "research_case", "browser_client", "case_capture"))
'''
    with tempfile.TemporaryDirectory(prefix="b6671-") as short_tmp:
        env = {**os.environ, "PYTHONPATH": str(ROOT / "modules/tools/agentcore"), "TMPDIR": short_tmp}
        subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, check=True, timeout=20)
